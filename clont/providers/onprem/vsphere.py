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
role. The one exception is opt-in: `browse()` starts `SearchDatastoreSubFolders_Task`,
a read that needs `Datastore.Browse` on top, and `browse_grants()` asks
`HasPrivilegeOnEntities` whether that grant is there before the first one. On a vcenter
`clone_events()` also reads the event log through a history collector
(`CreateCollectorForEvents` / `RewindCollector` / `ReadNextEvents` / `DestroyCollector`),
a read under Read-Only too. And `cns()` asks vcenter's cns for its container volumes
(`CnsQueryVolume` on the `/vsanHealth` endpoint, same session cookie), a read that needs
`Cns.Searchable`. That claim is a test (`tests/test_vsphere_vcsim.py`, against vcsim),
not a promise in a readme.

An unset property is **absent** from the result rather than `None`, so the transform in
`inventory.py` reads every path with `.get()` — see its docstring for what it does with a
value that never came back.
"""

from __future__ import annotations

import ssl
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from clont.core.errors import ConfigError
from clont.core.logging import get_logger
from clont.providers.onprem.inventory import (
    CLONE_EVENTS,
    CloneHistory,
    CnsVolumes,
    DatastoreFile,
    Props,
    SiteInventory,
    build_site,
    clone_history,
    cns_volumes,
    datastore_files,
)
from clont.providers.onprem.metrics import COUNTERS, Samples, usage_rows

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
DATASTORE_PATHS = (
    "name",
    "summary.capacity",
    "summary.freeSpace",
    "summary.uncommitted",
    "summary.type",
    # `ds:///vmfs/volumes/<uuid>/` — what a vsphere csi pv names its datastore by
    "summary.url",
    # every host that mounts it, seen or not — a mount we can't see makes it shared
    "host",
    # when free space was last refreshed, a running vm's layout is judged against it
    "info.timestamp",
)
VM_PATHS = (
    "name",
    "runtime.host",
    "runtime.powerState",
    "config.template",
    "config.hardware.numCPU",
    "config.hardware.memoryMB",
    "summary.storage.committed",
    "summary.storage.uncommitted",
    "summary.storage.timestamp",
    # a kubernetes node's providerID is `vsphere://` one of these two, depending on which
    # cloud provider wrote it, so both are read and either may match a node
    "config.instanceUuid",
    "config.uuid",
    # the snapshot tree, and which files each snapshot and each disk chain owns
    "snapshot",
    "layoutEx.file",
    "layoutEx.snapshot",
    "layoutEx.disk",
    "layoutEx.timestamp",
    "runtime.consolidationNeeded",
    "config.files.vmPathName",
    # the datastores a vm uses, so a vm with no layout blinds only those
    "datastore",
)
FOLDER_PATHS = ("name", "parent")
DATACENTER_PATHS = ("name",)

DEFAULT_PORT = 443

# what gc lists. descriptor, flat/delta/sesparse extents and the vmx all match these two
BROWSE_PATTERNS = ("*.vmdk", "*.vmx")
# per pass, all datastores searched at once. a timed-out one is left out, i.e. all-live
BROWSE_TIMEOUT_SECONDS = 300
BROWSE_POLL_SECONDS = 2
# the one privilege the browse needs beyond Read-Only
BROWSE_GRANT = "Datastore.Browse"

# 2-hour rollups, because vcenter keeps those 30 days by default and the 5-minute series
# only an hour — a 14-day lookback has nothing to read in the fine one
PERF_INTERVAL_SECONDS = 7200
# entities per QueryPerf. one call for 500 vms times two counters is a reply vcenter
# builds in memory before it sends it, and the usual advice is batches of tens
PERF_BATCH = 50

# `about.apiType` of a vcenter. a host agent says "HostAgent" and keeps no event history
# worth judging a template by
VCENTER = "VirtualCenter"
EVENT_PAGE = 1000
# clone events read before giving up. a cut-off read could miss the one clone that
# matters, so past this there is no verdict at all
EVENT_LIMIT = 50_000

# vcenter's cns lives on the vsan health service, a soap endpoint of its own that takes
# the vim session cookie. 7.0u2 is the oldest vsan api that sends `containerClusterArray`
# and `backingDiskPath`, an older vcenter faults and gets no verdict
CNS_PATH = "/vsanHealth"
CNS_VERSION = "vsan.version.version16"
CNS_MANAGER = "cns-volume-manager"
CNS_GRANT = "Cns.Searchable"
CNS_PAGE = 500
# a cut-off list only loses findings, never adds one, so past this it stops and says so
CNS_LIMIT = 100_000


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
        # datastore names the last browse was refused on
        self.browse_denied: list[str] = []
        self.api_type: str | None = None
        # the last clone read failed, already warned about
        self.clones_failed = False
        # same for the cns read
        self.cns_failed = False
        self._ssl: ssl.SSLContext | None = None

    def __enter__(self) -> VsphereInventory:
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def connect(self) -> None:
        connect, self._vim = _import_pyvmomi()
        # kept, the cns stub is a second connection to the same server
        self._ssl = self._ssl_context()
        self._si = connect.SmartConnect(
            host=self.endpoint,
            port=self.port,
            user=self._user,
            pwd=self._password,
            sslContext=self._ssl,
        )
        # fetched once and kept: `si.content` is a *managed property*, so every access is
        # another round trip (one `Fetch` per property call, for a value that never changes)
        self._content = self._si.RetrieveContent()
        about = self._content.about
        self.api_type = getattr(about, "apiType", None) or None
        self.instance_uuid = getattr(about, "instanceUuid", None) or None
        if self.instance_uuid is None:
            # `instanceUuid` is the vcenter's, and a host agent has no vcenter: on a
            # standalone esxi it comes back unset. the host's own hardware uuid is the
            # stable id there — the endpoint cannot stand in, it may be a tunnel port
            self.instance_uuid = self._host_uuid()
        log.debug("connected to %s (%s %s)", self.endpoint, about.apiType, about.apiVersion)

    def _host_uuid(self) -> str | None:
        vim = self._require_vim()
        try:
            found = self.properties(vim.HostSystem, ("hardware.systemInfo.uuid",))
        except Exception:  # noqa: BLE001 - an id is not worth failing a login over
            log.warning("could not read a host uuid from %s", self.endpoint)
            return None
        uuids = sorted(str(p["hardware.systemInfo.uuid"]) for p in found.values() if p)
        return uuids[0] if uuids else None

    def close(self) -> None:
        if self._si is None:
            return
        connect, _ = _import_pyvmomi()
        try:
            connect.Disconnect(self._si)
        finally:
            self._si = None
            self._content = None

    def site(
        self,
        *,
        usage_window_days: int = 0,
        browse: bool = False,
        browse_skip: frozenset[str] = frozenset(),
        clones: bool = False,
        cns: bool = False,
    ) -> SiteInventory:
        """One full pass: clusters, hosts, datastores, vms, and the datacenter they sit in.

        `usage_window_days` above zero adds the measured half — a `QueryPerf` over the
        trailing window per vm. Zero skips it, and then every vm is charged what it
        reserved; a failed perf read does the same rather than failing the pass, because
        a site with no counter history still has a bill to report.

        `clones` reads vcenter's clone events for the templates, see `clone_events()`,
        and `cns` its container volumes, see `cns()`.
        """
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
        if usage_window_days > 0:
            site.usage.update(usage_rows(site.vms(), self.samples(usage_window_days)))
        if browse:
            try:
                site = replace(site, files=self.browse(skip=browse_skip))
            except Exception as exc:  # noqa: BLE001 - gc is extra, the pass is not
                log.warning("%s: datastore browse failed, gc skipped: %s", self.endpoint, exc)
        if clones:
            templates = {vm.uid: vm.name for vm in site.vms() if vm.template}
            if templates:
                site = replace(site, clones=self.clone_events(templates))
        if cns:
            site = replace(site, cns=self.cns())
        log.info(
            "%s: %d pools, %d hosts, %d vms, %d datastores, %d measured",
            self.endpoint,
            len(site.pools),
            len(hosts),
            len(vms),
            len(datastores),
            len(site.usage),
        )
        return site

    def samples(
        self, window_days: int, *, interval_seconds: int = PERF_INTERVAL_SECONDS
    ) -> Samples:
        """Every vm's cpu and ram series over the trailing window, keyed by moref.

        `QueryPerf` is a read method and stays inside the Read-Only role, same as the
        property calls. It is also the one call here that can fail on a healthy vcenter —
        statistics level 1 collects these two counters, but an operator may have turned
        the historical intervals off — so a fault comes back as an empty dict and a
        warning, never as a failed pass.
        """
        vim = self._require_vim()
        perf = self._content.perfManager
        counters = {
            f"{c.groupInfo.key}.{c.nameInfo.key}.{c.rollupType}": c.key for c in perf.perfCounter
        }
        by_id = {counters[name]: name for name in COUNTERS if name in counters}
        if len(by_id) != len(COUNTERS):
            log.warning("%s: vcenter does not publish %s", self.endpoint, ", ".join(COUNTERS))
            return {}

        metrics = [vim.PerformanceManager.MetricId(counterId=cid, instance="") for cid in by_id]
        end = datetime.now(UTC)
        start = end - timedelta(days=window_days)
        entities = self._entities(vim.VirtualMachine)
        out: Samples = {}
        for batch in (
            entities[index : index + PERF_BATCH] for index in range(0, len(entities), PERF_BATCH)
        ):
            specs = [
                vim.PerformanceManager.QuerySpec(
                    entity=entity,
                    metricId=metrics,
                    intervalId=interval_seconds,
                    startTime=start,
                    endTime=end,
                )
                for entity in batch
            ]
            try:
                results = perf.QueryPerf(querySpec=specs) or []
            except Exception as exc:  # noqa: BLE001 - pyvmomi faults need the extra to name
                log.warning("%s: perf query failed, usage unmeasured: %s", self.endpoint, exc)
                return {}
            for result in results:
                out[_moref(result.entity)] = {
                    by_id[series.id.counterId]: [float(value) for value in (series.value or [])]
                    for series in (result.value or [])
                    if series.id.counterId in by_id
                }
        return out

    def browse_grants(self) -> dict[str, bool] | None:
        """Datastore name -> whether this login holds `Datastore.Browse` on it.

        One `HasPrivilegeOnEntities` call, a read under Read-Only. None when the host
        can't say (older api, a fault), and then the browse finds out per datastore.
        """
        vim = self._require_vim()
        content = self._content
        try:
            datastores = self._entities(vim.Datastore)
            if not datastores:
                return {}
            names = {_moref(ds): ds.name for ds in datastores}
            found = content.authorizationManager.HasPrivilegeOnEntities(
                entity=vim.ManagedEntity.Array(datastores),
                sessionId=content.sessionManager.currentSession.key,
                privId=[BROWSE_GRANT],
            )
        except Exception as exc:  # noqa: BLE001 - a check is not worth failing a pass over
            log.debug("%s: %s check unavailable: %s", self.endpoint, BROWSE_GRANT, exc)
            return None
        out: dict[str, bool] = {}
        for row in found or []:
            name = names.get(_moref(row.entity), _moref(row.entity))
            out[name] = any(
                p.privId == BROWSE_GRANT and p.isGranted for p in (row.privAvailability or [])
            )
        return out

    def browse(
        self,
        *,
        timeout_seconds: float = BROWSE_TIMEOUT_SECONDS,
        poll_seconds: float = BROWSE_POLL_SECONDS,
        skip: frozenset[str] = frozenset(),
    ) -> dict[str, tuple[DatastoreFile, ...]]:
        """Every vmdk and vmx on every datastore, keyed by datastore moref.

        `SearchDatastoreSubFolders_Task` is a task but only reads, and needs
        `Datastore.Browse` on top of Read-Only. A datastore that faults, times out or
        isn't accessible is left out of the result, which gc reads as "all live". A
        missing grant is one warning naming it, never a failed pass. `skip` names
        datastores already known to be denied, so a run warns about them once.
        """
        vim = self._require_vim()
        self.browse_denied = []
        spec = vim.host.DatastoreBrowser.SearchSpec(
            matchPattern=list(BROWSE_PATTERNS),
            details=vim.host.DatastoreBrowser.FileInfo.Details(
                fileType=True, fileSize=True, modification=True, fileOwner=False
            ),
        )
        tasks: dict[str, tuple[str, Any]] = {}
        denied: list[str] = []
        for ds in self._entities(vim.Datastore):
            ds_id, name = _moref(ds), ds.name
            if name in skip:
                continue
            try:
                if not ds.summary.accessible:
                    log.warning("%s: datastore %s not accessible, not browsed", self.endpoint, name)
                    continue
                task = ds.browser.SearchDatastoreSubFolders_Task(
                    datastorePath=f"[{name}]", searchSpec=spec
                )
            except vim.fault.NoPermission:
                denied.append(name)
                continue
            except Exception as exc:  # noqa: BLE001 - one datastore is not the pass
                log.warning("%s: datastore %s not browsed: %s", self.endpoint, name, exc)
                continue
            tasks[ds_id] = (name, task)

        out: dict[str, tuple[DatastoreFile, ...]] = {}
        deadline = time.monotonic() + timeout_seconds
        pending = dict(tasks)
        while pending:
            for ds_id, (name, task) in list(pending.items()):
                info = task.info
                if info.state == vim.TaskInfo.State.success:
                    out[ds_id] = datastore_files(ds_id, _plain(info.result))
                    del pending[ds_id]
                elif info.state == vim.TaskInfo.State.error:
                    # the privilege check may happen inside the task, not at the call
                    if isinstance(info.error, vim.fault.NoPermission):
                        denied.append(name)
                    else:
                        log.warning(
                            "%s: datastore %s not browsed: %s", self.endpoint, name, info.error
                        )
                    del pending[ds_id]
            if not pending:
                break
            if time.monotonic() >= deadline:
                log.warning(
                    "%s: browse timed out on %s, every file there counts as live",
                    self.endpoint,
                    ", ".join(sorted(name for name, _ in pending.values())),
                )
                break
            time.sleep(poll_seconds)
        self.browse_denied = sorted(denied)
        if denied:
            log.warning(
                "%s: no %s on %s, file list skipped there — grant it to the clont role",
                self.endpoint,
                BROWSE_GRANT,
                ", ".join(sorted(denied)),
            )
        return out

    def clone_events(self, templates: dict[str, str]) -> CloneHistory | None:
        """When each template (moref -> name) was last cloned, over vcenter's event history.

        Only a vcenter answers: a host agent keeps no such history, so it gets None and no
        warning, the template finding stays as it is. A fault, a missing privilege or more
        events than `EVENT_LIMIT` is None plus one warning, never a failed pass.
        """
        self.clones_failed = False
        if self.api_type != VCENTER or not templates:
            return None
        vim = self._require_vim()
        manager = self._content.eventManager
        try:
            oldest = self._read_events(manager, vim.event.EventFilterSpec(), most=1)
            found = self._read_events(
                manager, vim.event.EventFilterSpec(eventTypeId=list(CLONE_EVENTS))
            )
        except Exception as exc:  # noqa: BLE001 - a verdict is not worth failing a pass over
            self.clones_failed = True
            log.warning(
                "%s: event history unreadable, templates get no clone verdict: %s",
                self.endpoint,
                exc,
            )
            return None
        if found is None:
            self.clones_failed = True
            log.warning(
                "%s: more than %d clone events, templates get no clone verdict",
                self.endpoint,
                EVENT_LIMIT,
            )
            return None
        since = _plain(oldest[0].createdTime) if oldest else None
        if not isinstance(since, datetime):
            return None
        events = [{"type": type(e).__name__, **_plain(e)} for e in found]
        return clone_history(
            events,
            since if since.tzinfo else since.replace(tzinfo=UTC),
            templates,
            created=self._created(templates),
        )

    def cns(self) -> CnsVolumes | None:
        """Every container volume vcenter's cns knows, paged.

        Only a vcenter has cns: a host agent gets None and no warning. A fault, a missing
        `Cns.Searchable` or no cns service at all is None plus one warning, never a failed
        pass, and then no volume gets a verdict.
        """
        self.cns_failed = False
        if self.api_type != VCENTER:
            return None
        vim = self._require_vim()
        try:
            found = self._query_cns(vim)
        except Exception as exc:  # noqa: BLE001 - a verdict is not worth failing a pass over
            self.cns_failed = True
            if isinstance(exc, vim.fault.NoPermission):
                log.warning(
                    "%s: no %s, cns volumes not read — grant it to the clont role for "
                    "cns-leftover",
                    self.endpoint,
                    CNS_GRANT,
                )
            else:
                log.warning("%s: cns volumes unreadable, no verdict: %s", self.endpoint, exc)
            return None
        return CnsVolumes(volumes=cns_volumes(found), read_at=datetime.now(UTC))

    def _cns_manager(self, vim: Any) -> Any:
        """`cns-volume-manager` on the vsan health endpoint, logged in with our cookie."""
        from pyVmomi import SoapStubAdapter

        cookie = getattr(getattr(self._si, "_stub", None), "cookie", None)
        if not cookie:
            raise ConfigError("no session cookie to hand the cns endpoint")
        stub = SoapStubAdapter(
            host=self.endpoint,
            port=self.port,
            path=CNS_PATH,
            version=CNS_VERSION,
            sslContext=self._ssl,
        )
        stub.cookie = cookie
        return vim.cns.VolumeManager(CNS_MANAGER, stub)

    def _query_cns(self, vim: Any) -> list:
        """`CnsQueryVolume` page by page, as plain dicts."""
        manager = self._cns_manager(vim)
        out: list = []
        offset = 0
        while True:
            result = manager.CnsQueryVolume(
                filter=vim.cns.QueryFilter(cursor=vim.cns.Cursor(offset=offset, limit=CNS_PAGE))
            )
            page = list(getattr(result, "volumes", None) or [])
            out.extend(_plain(v) for v in page)
            cursor = getattr(result, "cursor", None)
            after = int(getattr(cursor, "offset", 0) or 0)
            total = getattr(cursor, "totalRecords", None)
            # the cursor's offset is where the next page starts. one that doesn't move
            # (vcsim answers 0) or reached the total is the last page
            if not page or after <= offset or (total is not None and after >= int(total)):
                return out
            if len(out) >= CNS_LIMIT:
                log.warning(
                    "%s: more than %d cns volumes, the rest not read", self.endpoint, CNS_LIMIT
                )
                return out
            offset = after

    def _read_events(self, manager: Any, spec: Any, *, most: int | None = None) -> list | None:
        """Oldest first, at most `most`. None when there were more and `most` > 1."""
        most = EVENT_LIMIT if most is None else most
        collector = manager.CreateCollectorForEvents(filter=spec)
        try:
            collector.RewindCollector()
            out: list = []
            while True:
                page = collector.ReadNextEvents(maxCount=min(EVENT_PAGE, most))
                if not page:
                    return out
                out.extend(page)
                if len(out) >= most:
                    return out[:most] if most == 1 else None
        finally:
            collector.DestroyCollector()

    def _created(self, templates: dict[str, str]) -> dict[str, datetime]:
        """`config.createDate` per template. its own call, a pre-6.7 vcenter faults on it."""
        vim = self._require_vim()
        try:
            found = self.properties(vim.VirtualMachine, ("config.createDate",))
        except Exception as exc:  # noqa: BLE001 - the arrival events still judge age
            log.debug("%s: no create dates: %s", self.endpoint, exc)
            return {}
        out: dict[str, datetime] = {}
        for uid, props in found.items():
            moment = props.get("config.createDate")
            if uid in templates and isinstance(moment, datetime):
                out[uid] = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
        return out

    def _entities(self, managed_type: Any) -> list[Any]:
        """The managed objects themselves — `QueryPerf` takes objects, not morefs."""
        content = self._content
        view = content.viewManager.CreateContainerView(content.rootFolder, [managed_type], True)
        try:
            return list(view.view)
        finally:
            view.Destroy()

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


_DATA_NOISE = ("dynamicType", "dynamicProperty")


def _plain(value: Any) -> Any:
    """Strip pyvmomi types down to str/int/bool/datetime, lists and dicts, so `inventory.py`
    stays sdk-free."""
    if hasattr(value, "_moId"):
        return _moref(value)
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    if isinstance(value, bool | int | float | str | datetime):
        return value
    if hasattr(value, "_GetPropertyList"):
        # a data object (snapshot tree, layoutEx) becomes a dict, unset fields left out
        out = {}
        for prop in value._GetPropertyList():
            if prop.name in _DATA_NOISE:
                continue
            item = getattr(value, prop.name, None)
            if item is not None:
                out[prop.name] = _plain(item)
        return out
    return str(value)
