"""Whose iron a node runs on — the match keys, and what happens when none of them answers.

The valuable assertions here are the misses: an unmapped node has to come back *with a
reason*, because the alternative is a namespace priced at zero and a report that adds to
less than the invoice.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from clont.core.errors import ConfigError
from clont.core.models import Cloud
from clont.finops.k8s.mapping import (
    MATCH_NAME,
    MATCH_PROVIDER_ID,
    MATCH_SYSTEM_UUID,
    MATCH_SYSTEM_UUID_SWAPPED,
    Priced,
    match,
    targets_from_instances,
    targets_from_site,
)
from clont.finops.k8s.source import KubernetesSource
from clont.providers.k8s.config import KubernetesCluster
from clont.providers.k8s.nodes import Node
from clont.providers.onprem.inventory import Pool, SiteInventory, Vm

BIOS = "4213f8a1-2b3c-4d5e-8f90-0a1b2c3d4e5f"
INSTANCE_UUID = "5001a2b3-c4d5-4e6f-9081-726354453627"
SWAPPED_BIOS = "a1f81342-3c2b-5e4d-8f90-0a1b2c3d4e5f"


def vm(name: str, uid: str, *, bios: str | None = BIOS, instance: str | None = INSTANCE_UUID) -> Vm:
    return Vm(
        uid=uid,
        name=name,
        host="esx-1",
        powered_on=True,
        template=False,
        vcpu=4,
        ram_gib=Decimal(16),
        disk_gib=Decimal(100),
        committed_gib=Decimal(60),
        instance_uuid=instance,
        bios_uuid=bios,
    )


def site(*vms: Vm, orphans: tuple[Vm, ...] = ()) -> SiteInventory:
    pool = Pool(
        name="prod-gen11",
        kind="cluster",
        datacenter="DC0",
        hosts=(),
        datastores=(),
        vms=vms,
    )
    return SiteInventory(pools=(pool,), orphan_vms=orphans)


def node(name: str, **over) -> Node:
    return Node(name=name, uid=f"uid-{name}", **over)


def test_provider_id_wins_and_either_vsphere_uuid_answers():
    targets = targets_from_site("dc1", site(vm("web-01", "vim.VirtualMachine:vm-1")))
    # the out-of-tree cpi writes the instance uuid...
    result = match("lab", [node("kube-1", provider_id=f"vsphere://{INSTANCE_UUID}")], targets)
    assert result.matched[0].matched_by == MATCH_PROVIDER_ID
    assert result.matched[0].target.ref == "dc1/DC0/prod-gen11/web-01"
    # ...the in-tree one wrote the bios uuid, and clont cannot know which runs
    result = match("lab", [node("kube-1", provider_id=f"vsphere://{BIOS}")], targets)
    assert result.matched[0].matched_by == MATCH_PROVIDER_ID


def test_smbios_matches_with_no_cloud_provider_at_all():
    targets = targets_from_site("dc1", site(vm("web-01", "vim.VirtualMachine:vm-1")))
    result = match("lab", [node("kube-1", system_uuid=BIOS.upper())], targets)
    assert result.matched[0].matched_by == MATCH_SYSTEM_UUID
    assert result.mapped_pct == Decimal("100.0")


def test_a_byte_swapped_product_uuid_is_the_same_vm():
    # older dmidecode prints the first three fields as stored, so the guest's product_uuid
    # is the mirror of the uuid vcenter holds
    targets = targets_from_site("dc1", site(vm("web-01", "vim.VirtualMachine:vm-1")))
    result = match("lab", [node("kube-1", system_uuid=SWAPPED_BIOS)], targets)
    assert result.matched[0].matched_by == MATCH_SYSTEM_UUID_SWAPPED
    assert result.unmapped == ()


def test_name_is_the_last_resort_and_can_be_switched_off():
    targets = targets_from_site("dc1", site(vm("kube-1", "vim.VirtualMachine:vm-1", bios=None, instance=None)))
    nodes = [node("kube-1.dc1.local")]
    assert match("lab", nodes, targets).matched[0].matched_by == MATCH_NAME
    # off means a missing row instead of an attribution nobody can defend
    strict = match("lab", nodes, targets, by_name=False)
    assert strict.matched == ()
    assert "name matching is off" in strict.unmapped[0].reason


def test_a_duplicated_bios_uuid_is_never_a_coin_flip():
    # config.uuid is smbios: a clone or a restore from backup can carry a duplicate, and
    # attributing a namespace to one of two clusters at random is worse than a missing row
    clone = vm("web-01-restored", "vim.VirtualMachine:vm-2", instance="6002b3c4-d5e6-4f70-9182-837465564738")
    targets = targets_from_site("dc1", site(vm("web-01", "vim.VirtualMachine:vm-1"), clone))
    result = match("lab", [node("kube-1", system_uuid=BIOS)], targets)
    assert result.matched == ()
    assert "is on 2 priced resources" in result.unmapped[0].reason
    # the vcenter-unique id still resolves that same pair
    hit = match("lab", [node("kube-1", provider_id=f"vsphere://{INSTANCE_UUID}")], targets)
    assert hit.matched[0].target.name == "web-01"


def test_two_vms_of_one_name_block_the_name_match():
    targets = targets_from_site(
        "dc1",
        site(
            vm("kube-1", "vim.VirtualMachine:vm-1", bios=None, instance=None),
            vm("kube-1", "vim.VirtualMachine:vm-2", bios=None, instance=None),
        ),
    )
    result = match("lab", [node("kube-1")], targets)
    assert "name 'kube-1' is on 2 priced resources" in result.unmapped[0].reason


def test_an_unmappable_node_says_why_and_is_not_priced():
    targets = targets_from_site("dc1", site(vm("web-01", "vim.VirtualMachine:vm-1")))
    nodes = [
        node("ghost"),                                                   # no ids at all
        node("other", system_uuid="7003c4d5-e6f7-4081-9293-948576675849"),  # unknown uuid
        node("cloudy", provider_id="aws:///eu-west-1a/i-0abc123"),        # not this site
    ]
    result = match("lab", nodes, targets)
    assert result.matched == ()
    assert len(result.unmapped) == 3
    reasons = {u.node.name: u.reason for u in result.unmapped}
    assert "no smbios uuid" in reasons["ghost"]
    assert "matches no vm" in reasons["other"]
    assert "providerID aws:///eu-west-1a/i-0abc123 matches nothing priced" in reasons["cloudy"]
    assert result.mapped_pct == Decimal(0)
    assert result.pools == ()


def test_an_eks_node_maps_to_its_instance():
    class Running:
        instance_id = "i-0abc123"
        region = "eu-west-1"

    targets = targets_from_instances("prod", [Running()])
    result = match("eks-prod", [node("ip-10-0-0-1.ec2.internal", provider_id="aws:///eu-west-1a/i-0abc123")], targets)
    assert result.matched[0].matched_by == MATCH_PROVIDER_ID
    assert result.pools == ("prod/eu-west-1",)


def test_a_cluster_can_span_pools_and_groups_by_the_one_that_pays():
    second = Pool(
        name="dev-gen9",
        kind="cluster",
        datacenter="DC0",
        hosts=(),
        datastores=(),
        vms=(vm("kube-2", "vim.VirtualMachine:vm-2", bios=None, instance=None),),
    )
    inventory = SiteInventory(
        pools=(
            Pool(
                name="prod-gen11",
                kind="cluster",
                datacenter="DC0",
                hosts=(),
                datastores=(),
                vms=(vm("kube-1", "vim.VirtualMachine:vm-1", bios=None, instance=None),),
            ),
            second,
        )
    )
    result = match("lab", [node("kube-1"), node("kube-2")], targets_from_site("dc1", inventory))
    assert result.pools == ("dc1/DC0/dev-gen9", "dc1/DC0/prod-gen11")
    assert set(result.by_pool()) == {"dc1/DC0/dev-gen9", "dc1/DC0/prod-gen11"}
    assert result.target_of("kube-1").pool == "DC0/prod-gen11"


def test_an_orphan_vm_is_still_a_vm_we_found():
    # it belongs to no pool, so it cannot carry a pool's rate — but "this node runs on a
    # vm with no cluster" is a better report than calling the node unmapped
    orphan = vm("kube-1", "vim.VirtualMachine:vm-9")
    result = match("lab", [node("kube-1", system_uuid=BIOS)], targets_from_site("dc1", site(orphans=(orphan,))))
    assert result.matched[0].target.pool == ""


def test_the_summary_names_the_pools_and_the_misses():
    targets = targets_from_site("dc1", site(vm("kube-1", "vim.VirtualMachine:vm-1")))
    result = match("lab", [node("kube-1", system_uuid=BIOS), node("ghost")], targets)
    line = result.summary()
    assert "1/2 node(s) mapped (50.0%)" in line
    assert "dc1/DC0/prod-gen11" in line
    assert "ghost:" in line


# --- the source: which provider is allowed to price a cluster

class _Site:
    cloud = Cloud.ONPREM
    alias = "dc1"

    def __init__(self, inventory: SiteInventory) -> None:
        self._inventory = inventory

    def inventory(self) -> SiteInventory:
        return self._inventory


def _source(provider, nodes, **over) -> KubernetesSource:
    config = KubernetesCluster(priced_by="dc1", **over)
    return KubernetesSource("lab", config, provider, reader=lambda: nodes)


def test_the_source_caches_one_read_per_cycle():
    reads = []

    def reader():
        reads.append(1)
        return [node("kube-1", system_uuid=BIOS)]

    provider = _Site(site(vm("kube-1", "vim.VirtualMachine:vm-1")))
    source = KubernetesSource("lab", KubernetesCluster(priced_by="dc1"), provider, reader=reader)
    assert source.mapping().matched
    source.mapping()
    assert len(reads) == 1
    source.mapping(refresh=True)
    assert len(reads) == 2


def test_a_cluster_priced_by_a_provider_that_cannot_price_nodes_is_an_error():
    class Azure:
        cloud = Cloud.AZURE
        alias = "dc1"

    with pytest.raises(ConfigError, match="cannot price nodes"):
        _source(Azure(), [node("kube-1")]).mapping()


def test_preflight_separates_an_empty_cluster_from_a_wrong_priced_by():
    provider = _Site(site(vm("web-01", "vim.VirtualMachine:vm-1")))
    assert "reports no nodes" in _source(provider, []).preflight()[0]
    # the cluster answers, the site is priced, and the two are about different iron
    stranger = [node("kube-1", system_uuid="7003c4d5-e6f7-4081-9293-948576675849")]
    assert "check priced_by" in _source(provider, stranger).preflight()[0]
    assert _source(provider, [node("web-01")]).preflight() == []
