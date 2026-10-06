"""What the hypervisor was asked for, turned into pools, capacity and provisioned usage.

This module holds no vsphere types at all: the wire (`vsphere.py`) does one
`RetrieveProperties` per object type and hands the raw `{moref: {leaf path: value}}`
dicts over, this turns them into the two numbers the rate card needs — a pool's
**capacity** and each vm's **provisioned** consumption. Keeping it pure is what makes the
arithmetic testable without a vcenter, and what lets proxmox/libvirt reuse it later by
filling the same dicts.

The decisions here are the arguable half of the on-prem story, so they are all in one
place:

* **a pool is a cluster**, and a host outside every cluster is its own pool — same rule
  as `finops/onprem/rates.py`, because that is what the operator prices
* **capacity is installed iron, not powered-on iron.** a host that is switched off still
  sits in the capex and still carries its licenses, so it counts; it comes back in
  `hosts_powered_off` instead, which is a finding ("zombie host"), not a discount
* **cores, never threads.** `numCpuThreads` is read only to report the ratio
* **provisioned means powered-on**, so a powered-off vm or a template is charged for its
  disk alone. its ram is not reserved — another vm is using it
* **a vm is charged for the disk it occupies** (`committed_gib`), never for what it was
  promised. `disk_gib` carries `committed + uncommitted` — what it may grow into — and
  that number is a *risk* (`thin-overcommit`), not spend: a 2x thin cluster would
  otherwise bill ~20% over the card the operator actually pays
* **a datastore mounted by two clusters counts fully in both.** either cluster can fill
  it, and the alternative is inventing a split. it only ever *lowers* the storage rate
  (the pool cost is the operator's per-cluster figure either way), so the error leans to
  under-stating, and `shared_datastores` names them so the report can say so

Measured *usage* is not computed here, only carried: `metrics.py` turns perf counters
into a row per vm and this module merges it into the allocator's payload. vcenter's
`quickStats` would have been the cheap way and are the wrong one — a one-second snapshot
in MHz, needing a host's hz-per-core, dressed up as a p95. A vm with no row keeps no
`used` key at all, and `allocate()` then charges it what it reserved: an absent
measurement must never read as zero usage.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from clont.core.errors import ConfigError

if TYPE_CHECKING:  # metrics.py reads Vm, so the import only goes one way at runtime
    from clont.providers.onprem.metrics import Usage

BYTES_PER_GIB = Decimal(1024**3)
MIB_PER_GIB = Decimal(1024)

# {moref id -> {leaf property path -> value}}, exactly what the property collector gives
Props = dict[str, dict[str, object]]

POWERED_ON = "poweredOn"


@dataclass(frozen=True, slots=True)
class Host:
    """One esxi host. `cores` is the divisor, everything else is for the report."""

    name: str
    cores: int
    threads: int
    ram_gib: Decimal
    powered_on: bool
    version: str | None = None


@dataclass(frozen=True, slots=True)
class Datastore:
    # moref, same reason as a vm's: two datacenters each have a "LocalDS_0"
    uid: str
    name: str
    capacity_gib: Decimal
    free_gib: Decimal
    # capacity - free + uncommitted: what every thin disk on it could grow to
    provisioned_gib: Decimal
    kind: str | None = None
    # `ds:///vmfs/volumes/<uuid>/` — the id a vsphere csi volume names its datastore by;
    # the shown name is not one, two datacenters each have a "LocalDS_0"
    url: str | None = None

    @property
    def overcommit(self) -> Decimal:
        return self.provisioned_gib / self.capacity_gib


@dataclass(frozen=True, slots=True)
class Vm:
    # moref: the only id vcenter promises is unique, and it survives a rename
    uid: str
    name: str
    host: str | None
    powered_on: bool
    template: bool
    vcpu: int
    ram_gib: Decimal
    disk_gib: Decimal       # committed + uncommitted
    committed_gib: Decimal  # what it actually occupies today
    # true when vcenter answered without the config of the vm, e.g. its host is
    # disconnected. charged on what did come back, and counted in the report
    incomplete: bool = False
    # the two ids a kubernetes node can be matched on. instance uuid is vcenter's own and
    # cannot collide; bios uuid is smbios, so a clone or a restore from backup can carry a
    # duplicate — both are kept because `vsphere://` in a providerID means either one
    instance_uuid: str | None = None
    bios_uuid: str | None = None

    @property
    def running(self) -> bool:
        return self.powered_on and not self.template

    def provisioned(self) -> dict[str, Decimal]:
        """This vm's row for `rates.allocate()` — cpu and ram only while it runs.

        storage is what the vm *occupies*, not what it promised. a thin disk eats blocks
        as it grows, so charging `committed + uncommitted` bills a 2x thin-provisioned
        cluster ~20% over the operator's own card — money that is nowhere on the invoice.
        the promise is a risk, and `thin-overcommit` is where it is reported.
        """
        return {
            "vcpu": Decimal(self.vcpu) if self.running else Decimal(0),
            "ram_gib": self.ram_gib if self.running else Decimal(0),
            "disk_gib": self.committed_gib,
        }


@dataclass(frozen=True, slots=True)
class Pool:
    """A cluster (or a standalone host) with the iron and the vms that belong to it."""

    name: str
    kind: str                      # "cluster" or "host"
    datacenter: str | None
    hosts: tuple[Host, ...]
    datastores: tuple[Datastore, ...]
    vms: tuple[Vm, ...]
    shared_datastores: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        """Stable id for the result dict — two datacenters may both hold a "prod"."""
        return f"{self.datacenter}/{self.name}" if self.datacenter else self.name

    def capacity(self) -> dict[str, Decimal]:
        """The three divisors, in `allocate()`'s vocabulary."""
        return {
            "vcpu": Decimal(sum(host.cores for host in self.hosts)),
            "ram_gib": sum((host.ram_gib for host in self.hosts), Decimal(0)),
            "storage_gib": sum((ds.capacity_gib for ds in self.datastores), Decimal(0)),
        }

    def labels(self) -> dict[str, str]:
        """moref -> the id a report shows it under.

        vcenter happily holds two vms called `web-01` — different folders, different
        datacenters — so a colliding name carries its moref. without it one vm's line
        would overwrite the other's and the cluster would silently lose a vm's spend.
        """
        seen = Counter(vm.name for vm in self.vms)
        return {
            vm.uid: vm.name if seen[vm.name] == 1 else f"{vm.name} ({vm.uid})" for vm in self.vms
        }

    def allocation_payload(self, pool_card: dict, usage: dict[str, Usage] | None = None) -> dict:
        """Capacity + vms merged into the operator's card, ready for `allocate()`.

        Keyed by moref, not name: `allocate()` rejects a duplicate key rather than eat a
        vm, and a pair of same-named vms would make the whole pool unpriceable.

        A vm with no measured row simply carries no `used`, and `allocate()` charges it
        what it reserved — an absent measurement must never read as zero usage.
        """
        measured = usage or {}
        return {
            **pool_card,
            "capacity": {key: str(value) for key, value in self.capacity().items()},
            "vms": [self._vm_row(vm, measured.get(vm.uid)) for vm in self.vms],
        }

    @staticmethod
    def _vm_row(vm: Vm, row: Usage | None) -> dict:
        out = {"name": vm.uid, "provisioned": {k: str(v) for k, v in vm.provisioned().items()}}
        if row is not None:
            out["used"] = {k: str(v) for k, v in row.used(vm.committed_gib).items()}
        return out

    @property
    def hosts_powered_off(self) -> tuple[str, ...]:
        return tuple(host.name for host in self.hosts if not host.powered_on)

    @property
    def incomplete_vms(self) -> tuple[str, ...]:
        return tuple(vm.name for vm in self.vms if vm.incomplete)


@dataclass(frozen=True, slots=True)
class SiteInventory:
    """One pass over one vcenter. The leftovers are findings, not errors."""

    pools: tuple[Pool, ...]
    orphan_vms: tuple[Vm, ...] = ()                   # on no host vcenter will admit to
    unmounted_datastores: tuple[Datastore, ...] = ()  # no host mounts them, nothing can use them
    # every datastore of the site, once. a san mounted by three clusters sits in three
    # pools on purpose, so a site-wide storage total can only be summed here
    datastores: tuple[Datastore, ...] = ()
    # moref -> measured usage, for the vms with enough perf history. empty when the pass
    # read no counters at all, which is not the same as every vm sitting at zero
    usage: dict[str, Usage] = field(default_factory=dict)

    def pool(self, key: str) -> Pool | None:
        return next((pool for pool in self.pools if pool.key == key or pool.name == key), None)

    def vms(self) -> tuple[Vm, ...]:
        """Every vm, placed or not. A vm runs on one host, so pools cannot overlap."""
        return tuple(vm for pool in self.pools for vm in pool.vms) + self.orphan_vms


def build_site(
    clusters: Props,
    hosts: Props,
    datastores: Props,
    vms: Props,
    ancestry: Props | None = None,
    datacenters: Props | None = None,
) -> SiteInventory:
    """Assemble pools out of a handful of property calls. Membership comes off the hosts.

    `clusters[id]["host"]` lists a cluster's hosts, and each host lists its `vm` and
    `datastore`, so the whole membership graph arrives with the objects themselves — no
    per-object round trip, and no second traversal to maintain.

    `ancestry` is anything that carries a `parent` — folders and compute resources — and
    is used for one thing only: naming the datacenter a pool sits in.
    """
    dc_of = _datacenter_index(ancestry or {}, datacenters or {})
    host_objects = {host_id: _host(host_id, props) for host_id, props in hosts.items()}
    host_names = {host_id: host.name for host_id, host in host_objects.items()}
    ds_objects = {ds_id: _datastore(ds_id, props) for ds_id, props in datastores.items()}
    vm_objects = {vm_id: _vm(vm_id, props, host_names) for vm_id, props in vms.items()}

    # (name, kind, datacenter, host ids) — membership first, objects once it is settled,
    # because "is this datastore shared" can only be answered across all pools
    members: list[tuple[str, str, str | None, list[str]]] = []
    claimed: set[str] = set()
    for cluster_id, props in clusters.items():
        own = [host_id for host_id in _refs(props.get("host")) if host_id in host_objects]
        claimed.update(own)
        members.append(
            (
                _text(props.get("name")) or cluster_id,
                "cluster",
                dc_of.get(_text(props.get("parent"))),
                own,
            )
        )
    # a host in no cluster is its own pool: its own iron, its own price
    for host_id, host in host_objects.items():
        if host_id not in claimed:
            members.append(
                (host.name, "host", dc_of.get(_text(hosts[host_id].get("parent"))), [host_id])
            )

    grouped = [
        (name, kind, datacenter, own, _group(own, hosts, ds_objects, vm_objects))
        for name, kind, datacenter, own in members
    ]
    # counted by moref, never by name: two datacenters each have a "LocalDS_0" and they
    # are different arrays, so a name key would call every one of them shared
    mounts: dict[str, int] = {}
    for *_, (pool_ds, _) in grouped:
        for ds_id in pool_ds:
            mounts[ds_id] = mounts.get(ds_id, 0) + 1

    pools = tuple(
        Pool(
            name=name,
            kind=kind,
            datacenter=datacenter,
            hosts=tuple(host_objects[host_id] for host_id in own),
            datastores=tuple(pool_ds.values()),
            vms=tuple(pool_vms.values()),
            shared_datastores=tuple(
                sorted(ds.name for ds_id, ds in pool_ds.items() if mounts[ds_id] > 1)
            ),
        )
        for name, kind, datacenter, own, (pool_ds, pool_vms) in grouped
    )

    mounted = {ds_id for props in hosts.values() for ds_id in _refs(props.get("datastore"))}
    placed = {vm_id for props in hosts.values() for vm_id in _refs(props.get("vm"))}
    return SiteInventory(
        pools=pools,
        orphan_vms=tuple(vm for vm_id, vm in vm_objects.items() if vm_id not in placed),
        unmounted_datastores=tuple(
            sorted(
                (ds for ds_id, ds in ds_objects.items() if ds_id not in mounted),
                key=lambda ds: ds.name,
            )
        ),
        datastores=tuple(ds_objects.values()),
    )


def _group(
    members: list[str],
    hosts: Props,
    ds_objects: dict[str, Datastore],
    vm_objects: dict[str, Vm],
) -> tuple[dict[str, Datastore], dict[str, Vm]]:
    """The datastores and vms hanging off a set of hosts, deduped.

    A shared datastore is mounted by every host that can see it, so without the dedupe a
    three-host cluster would count the same san three times.
    """
    pool_ds: dict[str, Datastore] = {}
    pool_vms: dict[str, Vm] = {}
    for host_id in members:
        props = hosts[host_id]
        for ds_id in _refs(props.get("datastore")):
            if ds_id in ds_objects:
                pool_ds[ds_id] = ds_objects[ds_id]
        for vm_id in _refs(props.get("vm")):
            if vm_id in vm_objects:
                pool_vms[vm_id] = vm_objects[vm_id]
    return pool_ds, pool_vms


def _datacenter_index(ancestry: Props, datacenters: Props) -> dict[str, str]:
    """moref of anything under a datacenter -> that datacenter's name.

    A cluster sits in the datacenter's host folder, possibly nested; a standalone host
    sits under a compute resource that sits in that same folder. Both are the same walk
    upwards. Absent ancestry just means pools come back unqualified.
    """
    names = {dc_id: _text(props.get("name")) or dc_id for dc_id, props in datacenters.items()}
    parent = {
        node_id: _text(props.get("parent")) or "" for node_id, props in ancestry.items()
    }
    index: dict[str, str] = dict(names)
    for node_id in parent:
        seen: set[str] = set()
        current = node_id
        while current and current not in seen:
            seen.add(current)
            if current in names:
                for step in seen:
                    index[step] = names[current]
                break
            current = parent.get(current, "")
    return index


def _host(host_id: str, props: dict[str, object]) -> Host:
    name = _text(props.get("name")) or host_id
    # cores and memory are the divisors under every rate, so a host that will not say
    # is a failed pass and not a free host. there are tens of hosts, not thousands
    cores = _int(props.get("hardware.cpuInfo.numCpuCores"))
    ram_bytes = _int(props.get("hardware.memorySize"))
    if cores <= 0 or ram_bytes <= 0:
        raise ConfigError(f"host {name!r} reported no cpu cores or memory, inventory incomplete")
    return Host(
        name=name,
        cores=cores,
        threads=_int(props.get("hardware.cpuInfo.numCpuThreads")) or cores,
        ram_gib=Decimal(ram_bytes) / BYTES_PER_GIB,
        powered_on=_text(props.get("runtime.powerState")) == POWERED_ON,
        version=_text(props.get("config.product.version")) or None,
    )


def _datastore(ds_id: str, props: dict[str, object]) -> Datastore:
    name = _text(props.get("name")) or ds_id
    capacity = _int(props.get("summary.capacity"))
    if capacity <= 0:
        raise ConfigError(f"datastore {name!r} reported no capacity, inventory incomplete")
    free = _int(props.get("summary.freeSpace"))
    return Datastore(
        uid=ds_id,
        name=name,
        capacity_gib=Decimal(capacity) / BYTES_PER_GIB,
        free_gib=Decimal(free) / BYTES_PER_GIB,
        provisioned_gib=Decimal(capacity - free + _int(props.get("summary.uncommitted")))
        / BYTES_PER_GIB,
        kind=_text(props.get("summary.type")) or None,
        url=_text(props.get("summary.url")) or None,
    )


def _vm(vm_id: str, props: dict[str, object], host_names: dict[str, str]) -> Vm:
    name = _text(props.get("name")) or vm_id
    host = _text(props.get("runtime.host"))
    vcpu = _int(props.get("config.hardware.numCPU"))
    ram_mib = _int(props.get("config.hardware.memoryMB"))
    committed = _int(props.get("summary.storage.committed"))
    uncommitted = _int(props.get("summary.storage.uncommitted"))
    return Vm(
        uid=vm_id,
        name=name,
        # a finding names the host an operator can log into, not its moref
        host=host_names.get(host, host) or None,
        powered_on=_text(props.get("runtime.powerState")) == POWERED_ON,
        template=bool(props.get("config.template")),
        vcpu=vcpu,
        ram_gib=Decimal(ram_mib) / MIB_PER_GIB,
        disk_gib=Decimal(committed + uncommitted) / BYTES_PER_GIB,
        committed_gib=Decimal(committed) / BYTES_PER_GIB,
        # a vm whose host is disconnected answers name and little else: charge what came
        # back, and let the report name it rather than dropping it
        incomplete=vcpu <= 0 or ram_mib <= 0,
        instance_uuid=_text(props.get("config.instanceUuid")) or None,
        bios_uuid=_text(props.get("config.uuid")) or None,
    )


def _refs(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple):
        return [str(item) for item in value]
    return [str(value)]


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _int(value: object) -> int:
    # an unset property is absent rather than None, and vcenter sends longs as str
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
