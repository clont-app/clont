"""The vcenter wire: six property calls, read-only, nothing else.

pyvmomi is an optional dependency (`pip install clont[vsphere]`) — an aws-only install
has no business pulling a vcenter sdk, so it is imported when a session is opened and
not at module import.

Two rules this file exists to keep:

**One `RetrieveProperties` per object type, with an explicit leaf pathSet.** 500 vms are
one call, not 500; and the pathSet has to name leaves because vcenter's own simulator
cannot serialize a whole `VirtualMachine.runtime` (pyvmomi 9 dies on an empty
`faultToleranceState` enum, surfacing as a bare `AttributeError: runtime`). Asking for
`runtime.powerState` works everywhere, so it is both the portable and the cheap shape.

**Only read methods.** The whole pass is `CreateContainerView` / `RetrieveProperties` /
`DestroyView` plus the login, which is what lets clont run under the vsphere Read-Only
role. That claim is a test in `agents/cft/functests/vsphere` (test layer 1), not a
promise in a readme.

An unset property is **absent** from the result rather than `None`, so the transform in
`inventory.py` reads every path with `.get()` — see its docstring for what it does with a
value that never came back.
"""

from __future__ import annotations

import ssl
from typing import Any

from clont.core.errors import ConfigError
from clont.core.logging import get_logger
from clont.providers.onprem.inventory import Props, SiteInventory, build_site

log = get_logger("clont.providers.onprem.vsphere")

# ClusterComputeResource is a ComputeResource, so one call brings back both the clusters
# and the standalone hosts' compute resources — the latter only to name their datacenter
COMPUTE_PATHS = ("name", "parent", "host")
CLUSTER_PREFIX = "vim.ClusterComputeResource:"
HOST_PATHS = (
    "name",
    "parent",
    "vm",
    "datastore",
    "hardware.cpuInfo.numCpuCores",   # physical cores, the divisor
    "hardware.cpuInfo.numCpuThreads",  # reported only, threads are not capacity
    "hardware.memorySize",
    "runtime.powerState",
    "config.product.version",
)
DATASTORE_PATHS = ("name", "summary.capacity", "summary.freeSpace", "summary.uncommitted", "summary.type")
VM_PATHS = (
    "name",
    "runtime.host",
    "runtime.powerState",
    "config.template",
    "config.hardware.numCPU",
    "config.hardware.memoryMB",
    "summary.storage.committed",
    "summary.storage.uncommitted",
)
FOLDER_PATHS = ("name", "parent")
DATACENTER_PATHS = ("name",)

DEFAULT_PORT = 443


class VsphereInventory:
    """A read-only inventory session against one vcenter.

    Use it as a context manager so the session is logged out even when a call raises —
    vcenter keeps idle sessions around and an operator watching the session list should
    see one appear and go.
    """

    def __init__(
        self,
        endpoint: str,
        user: str,
        password: str,
        *,
        port: int = DEFAULT_PORT,
        verify_ssl: bool = True,
        ca_bundle: str | None = None,
    ) -> None:
        if not endpoint:
            raise ConfigError("vsphere endpoint is empty")
        self.endpoint = endpoint
        self.port = port
        self._user = user
        self._password = password
        self._verify_ssl = verify_ssl
        self._ca_bundle = ca_bundle
        self._si: Any = None
        self._content: Any = None
        self._vim: Any = None
        # this vcenter's own id, filled on connect — it survives a rename, the endpoint does not
        self.instance_uuid: str | None = None

    def __enter__(self) -> VsphereInventory:
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def connect(self) -> None:
        connect, self._vim = _import_pyvmomi()
        self._si = connect.SmartConnect(
            host=self.endpoint,
            port=self.port,
            user=self._user,
            pwd=self._password,
            sslContext=self._ssl_context(),
        )
        # fetched once and kept: `si.content` is a *managed property*, so every access is
        # another round trip (one `Fetch` per property call, for a value that never changes)
        self._content = self._si.RetrieveContent()
        about = self._content.about
        self.instance_uuid = getattr(about, "instanceUuid", None) or None
        log.debug("connected to %s (%s %s)", self.endpoint, about.apiType, about.apiVersion)

    def close(self) -> None:
        if self._si is None:
            return
        connect, _ = _import_pyvmomi()
        try:
            connect.Disconnect(self._si)
        finally:
            self._si = None
            self._content = None

    def site(self) -> SiteInventory:
        """One full pass: clusters, hosts, datastores, vms, and the datacenter they sit in."""
        vim = self._require_vim()
        compute = self.properties(vim.ComputeResource, COMPUTE_PATHS)
        hosts = self.properties(vim.HostSystem, HOST_PATHS)
        datastores = self.properties(vim.Datastore, DATASTORE_PATHS)
        vms = self.properties(vim.VirtualMachine, VM_PATHS)
        # only to qualify a pool name: two datacenters may each hold a "prod" cluster
        folders = self.properties(vim.Folder, FOLDER_PATHS)
        datacenters = self.properties(vim.Datacenter, DATACENTER_PATHS)

        clusters = {key: props for key, props in compute.items() if key.startswith(CLUSTER_PREFIX)}
        site = build_site(clusters, hosts, datastores, vms, folders | compute, datacenters)
        log.info(
            "%s: %d pools, %d hosts, %d vms, %d datastores",
            self.endpoint,
            len(site.pools),
            len(hosts),
            len(vms),
            len(datastores),
        )
        return site

    def properties(self, managed_type: Any, paths: tuple[str, ...]) -> Props:
        """Every object of one type, in one round trip, keyed by moref id."""
        vim = self._require_vim()
        content = self._content
        view = content.viewManager.CreateContainerView(content.rootFolder, [managed_type], True)
        try:
            spec = vim.PropertyCollector.FilterSpec(
                objectSet=[
                    vim.PropertyCollector.ObjectSpec(
                        obj=view,
                        skip=True,
                        selectSet=[
                            vim.PropertyCollector.TraversalSpec(
                                type=vim.view.ContainerView, path="view", skip=False
                            )
                        ],
                    )
                ],
                propSet=[vim.PropertyCollector.PropertySpec(type=managed_type, pathSet=list(paths))],
            )
            return {
                _moref(result.obj): {
                    prop.name: _plain(prop.val) for prop in (result.propSet or [])
                }
                for result in content.propertyCollector.RetrieveContents([spec])
            }
        finally:
            view.Destroy()

    def _require_vim(self) -> Any:
        if self._si is None:
            raise ConfigError("vsphere session is not connected")
        return self._vim

    def _ssl_context(self) -> ssl.SSLContext:
        if not self._verify_ssl:
            # self-signed vcenter certs are the norm in a lab; opting out is the
            # operator's call and it is never the default
            log.warning("vsphere tls verification is off for %s", self.endpoint)
            return ssl._create_unverified_context()  # noqa: S323
        return ssl.create_default_context(cafile=self._ca_bundle)


def _import_pyvmomi() -> tuple[Any, Any]:
    try:
        from pyVim import connect
        from pyVmomi import vim
    except ImportError as exc:  # pragma: no cover - depends on the install extra
        raise ConfigError(
            "the vsphere collector needs pyvmomi: pip install 'clont[vsphere]'"
        ) from exc
    return connect, vim


def _moref(obj: Any) -> str:
    """`vim.HostSystem:host-23` — type included, moids are only unique per type."""
    return f"{type(obj).__name__}:{obj._moId}"


def _plain(value: Any) -> Any:
    """Strip pyvmomi types down to str/int/bool, so `inventory.py` stays sdk-free."""
    if hasattr(value, "_moId"):
        return _moref(value)
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    if isinstance(value, bool | int | float | str):
        return value
    return str(value)
