"""The vsphere wire against vcsim: our real `vsphere.py`, a real soap endpoint.

Opt-in, it starts a container:

    CLONT_VCSIM=1 .venv/bin/python -m pytest tests/test_vsphere_vcsim.py -q

Skips when docker or the image (`vmware/vcsim:latest`, `CLONT_VCSIM_IMAGE`) is missing,
it never pulls. What vcsim doesn't do is a skip with the reason, not a fake.
"""

from __future__ import annotations

import os
import shutil
import ssl
import subprocess
import time
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from clont.finops.base import FinOpsTuning
from clont.finops.onprem import gc

IMAGE = os.environ.get("CLONT_VCSIM_IMAGE", "vmware/vcsim:latest")
DS = "LocalDS_0"
SNAP_VM = "DC0_H0_VM0"
GONE_VM = "DC0_C0_RP0_VM1"  # unregistered, its folder stays
LOOSE = f"[{DS}] loose/loose.vmdk"

# everything a pass may call. the one task is the browse, a read, added on purpose
READ_ONLY = {
    "RetrieveServiceContent",
    "Login",
    "Logout",
    "CreateContainerView",
    "DestroyView",
    "RetrieveProperties",
    "RetrievePropertiesEx",
    "Fetch",  # one managed property, e.g. `task.info` while the browse polls
    "ContinueRetrievePropertiesEx",
    "QueryPerf",
    "QueryPerfCounter",
    "HasPrivilegeOnEntities",
    "SearchDatastoreSubFolders_Task",
}

pytestmark = pytest.mark.skipif(
    os.environ.get("CLONT_VCSIM") != "1", reason="opt-in, set CLONT_VCSIM=1"
)


def _docker(*args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _wait_ready(url: str, timeout: float = 60) -> None:
    ctx = ssl._create_unverified_context()  # loopback simulator
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, context=ctx, timeout=2):
                return
        except OSError:
            time.sleep(0.3)
    pytest.fail(f"vcsim not ready at {url}")


def _wait(task, timeout: float = 30) -> None:
    # pyVim's WaitForTask chokes on vcsim's VirtualDiskManager task entity, poll instead
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = task.info.state
        except TypeError:
            time.sleep(0.5)
            return
        if state == "success":
            return
        if state == "error":
            raise task.info.error
        time.sleep(0.1)
    pytest.fail("vcsim task did not finish")


@pytest.fixture(scope="module")
def endpoint():
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not available")
    if _docker("info", "--format", "{{.ServerVersion}}").returncode != 0:
        pytest.skip("docker daemon unavailable")
    if _docker("image", "inspect", IMAGE).returncode != 0:
        pytest.skip(f"image {IMAGE} not present locally")
    pytest.importorskip("pyVmomi")
    name = f"clont-vcsim-{uuid.uuid4().hex[:8]}"
    # loopback only, an unauthenticated vcenter on 0.0.0.0 is a public one
    started = _docker("run", "-d", "--name", name, "-p", "127.0.0.1::8989", IMAGE,
                      "-l", "0.0.0.0:8989")
    assert started.returncode == 0, started.stderr
    try:
        published = _docker("port", name, "8989/tcp")
        port = int(published.stdout.strip().splitlines()[0].rsplit(":", 1)[1])
        _wait_ready(f"https://127.0.0.1:{port}/about")
        _plant(port)
        yield port
    finally:
        _docker("rm", "-f", name)


def _plant(port: int) -> None:
    """snapshots, an unregistered vm and a loose disk, set up with the simulator's own writes."""
    from pyVim import connect
    from pyVmomi import vim

    si = connect.SmartConnect(
        host="127.0.0.1", port=port, user="u", pwd="p",
        sslContext=ssl._create_unverified_context(),  # loopback simulator
    )
    try:
        content = si.RetrieveContent()
        view = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True
        )
        vms = {vm.name: vm for vm in view.view}
        view.Destroy()
        snap = vms[SNAP_VM]
        _wait(snap.CreateSnapshot_Task(name="s1", description="", memory=False, quiesce=True))
        _wait(snap.CreateSnapshot_Task(name="s2", description="", memory=True, quiesce=False))
        gone = vms[GONE_VM]
        _wait(gone.PowerOffVM_Task())
        gone.UnregisterVM()
        dc = content.rootFolder.childEntity[0]
        content.fileManager.MakeDirectory(
            name=f"[{DS}] loose", datacenter=dc, createParentDirectories=True
        )
        spec = vim.VirtualDiskManager.FileBackedVirtualDiskSpec(
            diskType="thin", adapterType="lsiLogic", capacityKb=1024
        )
        _wait(content.virtualDiskManager.CreateVirtualDisk_Task(
            name=LOOSE, datacenter=dc, spec=spec
        ))
    finally:
        connect.Disconnect(si)


@pytest.fixture
def session(endpoint):
    from clont.providers.onprem.vsphere import VsphereInventory

    inv = VsphereInventory("127.0.0.1", "u", "p", port=endpoint, verify_ssl=False)
    with inv:
        yield inv


@contextmanager
def _soap_spy(inv):
    stub_type = type(inv._si._stub)
    original = stub_type.InvokeMethod
    called: list[str] = []

    def spy(stub, mo, info, *args, **kwargs):
        called.append(info.wsdlName)
        return original(stub, mo, info, *args, **kwargs)

    stub_type.InvokeMethod = spy
    try:
        yield called
    finally:
        stub_type.InvokeMethod = original


def _vm_props(session, name: str) -> dict:
    from pyVmomi import vim

    from clont.providers.onprem.vsphere import VM_PATHS

    props = session.properties(vim.VirtualMachine, VM_PATHS)
    return next(p for p in props.values() if p.get("name") == name)


def test_snapshot_tree_comes_back_with_every_field_we_read(session):
    props = _vm_props(session, SNAP_VM)
    tree = props["snapshot"]
    root = tree["rootSnapshotList"][0]
    assert {"name", "createTime", "id", "quiesced", "snapshot", "childSnapshotList"} <= set(root)
    assert isinstance(root["createTime"], datetime) and root["quiesced"] is True
    child = root["childSnapshotList"][0]
    assert child["name"] == "s2" and tree["currentSnapshot"] == child["snapshot"]

    # layoutEx.snapshot is keyed by the snapshot moref, the join `_snapshots` makes
    layouts = {row["key"]: row for row in props["layoutEx.snapshot"]}
    assert set(layouts) == {root["snapshot"], child["snapshot"]}
    for row in layouts.values():
        assert {"dataKey", "memoryKey", "disk"} <= set(row)
        assert {"key", "chain"} <= set(row["disk"][0])
    files = {row["key"]: row for row in props["layoutEx.file"]}
    assert all({"key", "name", "type", "size"} <= set(row) for row in files.values())
    assert files[layouts[root["snapshot"]]["dataKey"]]["type"] == "snapshotData"
    assert props["layoutEx.disk"][0]["chain"][0]["fileKey"]
    assert props["runtime.consolidationNeeded"] is False
    assert props["config.files.vmPathName"] == f"[{DS}] {SNAP_VM}/{SNAP_VM}.vmx"


def test_the_tree_turns_into_findings_input(session):
    vm = next(vm for vm in session.site().vms() if vm.name == SNAP_VM)
    assert [s.path for s in vm.snapshots] == ["s1", "s1 / s2"]
    assert all(s.created is not None for s in vm.snapshots)
    assert [s.quiesced for s in vm.snapshots] == [True, False]
    assert vm.vmx_path and vm.files and not vm.consolidation_needed


def test_a_snapshot_starts_a_delta_disk(session):
    props = _vm_props(session, SNAP_VM)
    if all(len(disk["chain"]) == 1 for disk in props["layoutEx.disk"]):
        pytest.skip("vcsim adds no delta disk per snapshot, the chain stays one link")
    vm = next(vm for vm in session.site().vms() if vm.name == SNAP_VM)
    assert all(s.size_gib > 0 for s in vm.snapshots)


def test_a_memory_snapshot_names_its_vmem(session):
    props = _vm_props(session, SNAP_VM)
    s2 = props["snapshot"]["rootSnapshotList"][0]["childSnapshotList"][0]["snapshot"]
    layout = next(row for row in props["layoutEx.snapshot"] if row["key"] == s2)
    if layout["memoryKey"] == -1:
        pytest.skip("vcsim writes no vmem for memory=True, memoryKey stays -1")
    files = {row["key"]: row for row in props["layoutEx.file"]}
    assert files[layout["memoryKey"]]["type"] == "snapshotMemory"


def test_browse_lists_every_vm_folder_with_size_and_time(session):
    files = session.browse(timeout_seconds=30, poll_seconds=0.2)
    assert len(files) == 1
    rows = next(iter(files.values()))
    folders = {gc._folder(row.path) for row in rows}
    assert {f"[{DS}] {GONE_VM}", f"[{DS}] {SNAP_VM}", f"[{DS}] loose"} <= folders
    # every row has a time, or gc could never call it old
    assert all(row.modified is not None for row in rows)
    # vcsim answers `[ds]/folder`, esxi `[ds] folder/`. both land in the layoutEx form
    assert all(row.path.startswith(f"[{DS}] ") and "//" not in row.path for row in rows)
    descriptors = [row for row in rows if row.path.endswith(f"{SNAP_VM}/disk1.vmdk")]
    assert descriptors and descriptors[0].size_gib > 0
    assert session.browse_denied == []


def test_every_registered_vm_file_matches_its_layout(session):
    """the browse path and the layoutEx name have to meet, or every disk is garbage."""
    site = session.site(browse=True)
    owned = {gc._norm(f.path) for vm in site.vms() for f in vm.files}
    for rows in site.files.values():
        for row in rows:
            if GONE_VM in row.path or "/loose" in row.path or "] loose" in row.path:
                continue
            assert gc._norm(row.path) in owned, row.path


def test_gc_finds_exactly_what_was_planted(session):
    site = session.site(browse=True)
    tuning = FinOpsTuning(onprem_gc_min_age_days=0)
    found = gc.sweep(site, tuning, now=datetime.now(UTC))
    assert found is not None and not found.dropped and not found.reasons
    # the paths a finding prints, exactly as an operator would type them
    got = sorted((g.kind, g.path) for g in found.garbage)
    assert got == sorted([
        (gc.ORPHANED_VMDK, LOOSE),
        (gc.UNREGISTERED_VM, f"[{DS}] {GONE_VM}"),
    ])
    # the default min age keeps everything vcsim just wrote
    assert gc.sweep(site, FinOpsTuning(), now=datetime.now(UTC)).garbage == ()


def test_the_grant_check_answers(session):
    assert session.browse_grants() == {DS: True}


def test_a_missing_grant_is_seen_before_the_browse(session):
    pytest.skip("vcsim grants every privilege to any login, a denial can't be staged here")


def test_a_full_pass_with_browse_stays_read_only(session):
    with _soap_spy(session) as called:
        session.browse_grants()
        session.site(usage_window_days=1, browse=True)
    seen = set(called)
    assert seen, "the spy recorded nothing, it is not wrapping the stub any more"
    assert seen <= READ_ONLY, f"non read-only calls: {sorted(seen - READ_ONLY)}"
    assert {"SearchDatastoreSubFolders_Task", "HasPrivilegeOnEntities"} <= seen
