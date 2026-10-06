"""Nodes as clont parses them: the three ids, and the capacity on the node.

Everything here is the pure transform — api json in, `Node`s out. The wire is one call
(`list_node`) and belongs in a functest against a real apiserver, not here.
"""

from __future__ import annotations

from decimal import Decimal

from clont.providers.k8s.nodes import (
    Node,
    build_nodes,
    normalize_uuid,
    parse_provider_id,
    quantity,
    swapped,
)

GIB = Decimal(1024**3)


def node_json(**over) -> dict:
    """One node the way the apiserver serializes it, camelCase and all."""
    item = {
        "metadata": {
            "name": "kube-worker-1",
            "uid": "6f1c0b4e-0d6a-4b0e-9a1a-2f0d9c8e7b11",
            "labels": {
                "node.kubernetes.io/instance-type": "m5.large",
                "topology.kubernetes.io/zone": "eu-west-1a",
                "topology.kubernetes.io/region": "eu-west-1",
            },
        },
        "spec": {"providerID": "vsphere://4213f8a1-2b3c-4d5e-8f90-0a1b2c3d4e5f"},
        "status": {
            "capacity": {"cpu": "4", "memory": "16308580Ki"},
            "allocatable": {"cpu": "3920m", "memory": "15158884Ki"},
            "conditions": [{"type": "MemoryPressure", "status": "False"},
                           {"type": "Ready", "status": "True"}],
            "nodeInfo": {
                "systemUUID": "4213F8A1-2B3C-4D5E-8F90-0A1B2C3D4E5F",
                "kubeletVersion": "v1.31.4",
            },
        },
    }
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(item.get(key), dict):
            item[key] = item[key] | value
        else:
            item[key] = value
    return item


def test_a_node_carries_its_ids_and_its_iron():
    node, = build_nodes([node_json()])
    assert node.name == "kube-worker-1"
    assert node.provider == ("vsphere", "4213f8a1-2b3c-4d5e-8f90-0a1b2c3d4e5f")
    assert node.vcpu == 4
    assert node.ram_gib.quantize(Decimal("0.01")) == Decimal("15.55")
    # the kubelet's reservation is real cost nobody can schedule into
    assert node.allocatable_vcpu == Decimal("3.92")
    assert node.allocatable_ram_gib < node.ram_gib
    assert (node.instance_type, node.zone, node.region) == ("m5.large", "eu-west-1a", "eu-west-1")
    assert node.ready is True
    assert node.unschedulable is False


def test_system_uuid_case_does_not_make_it_a_different_id():
    node, = build_nodes([node_json()])
    # kubelet reports smbios uppercase, vcenter answers lowercase, same vm
    assert node.uuids[0] == "4213f8a1-2b3c-4d5e-8f90-0a1b2c3d4e5f"
    assert len(node.uuids) == 2  # the straight reading and its byte-swapped mirror


def test_a_node_with_nothing_still_comes_back():
    # the row the mapping has to report as unmapped: dropping it would make a showback
    # table look complete while a node's cost is simply missing
    node, = build_nodes([{"metadata": {"name": "lonely"}}])
    assert node.uuids == ()
    assert node.provider == ("", "")
    assert node.vcpu == 0
    assert node.ready is None  # no Ready condition at all is not the same as not ready


def test_only_dicts_survive_the_build():
    assert build_nodes([None, "node", {"metadata": {"name": "ok"}}])[0].name == "ok"
    assert len(build_nodes([None, "node", {"metadata": {"name": "ok"}}])) == 1


def test_provider_id_takes_the_last_segment():
    # aws puts the zone in front of the instance id, and fargate adds a cluster segment
    assert parse_provider_id("aws:///eu-west-1a/i-0abc123") == ("aws", "i-0abc123")
    assert parse_provider_id("aws:///eu-west-1a/cluster/fargate-ip-10-0-0-1") == (
        "aws",
        "fargate-ip-10-0-0-1",
    )
    assert parse_provider_id("vsphere://4213f8a1-2b3c-4d5e-8f90-0a1b2c3d4e5f")[0] == "vsphere"
    assert parse_provider_id("kubevirt://ns/vmi-1") == ("kubevirt", "vmi-1")
    # a kubeadm cluster with no cloud controller manager sets none at all
    assert parse_provider_id("") == ("", "")
    assert parse_provider_id("vsphere://") == ("", "")


def test_an_instance_id_is_not_a_uuid():
    # it goes through the same lookup, so a non-uuid must never land in the uuid index
    assert normalize_uuid("i-0abc123") == ""
    assert normalize_uuid("urn:uuid:{4213F8A1-2B3C-4D5E-8F90-0A1B2C3D4E5F}") == (
        "4213f8a1-2b3c-4d5e-8f90-0a1b2c3d4e5f"
    )
    assert normalize_uuid(None) == ""


def test_the_byte_swap_is_the_first_three_fields_only():
    straight = "4213f8a1-2b3c-4d5e-8f90-0a1b2c3d4e5f"
    assert swapped(straight) == "a1f81342-3c2b-5e4d-8f90-0a1b2c3d4e5f"
    # and it is its own inverse, which is what lets one index answer both spellings
    assert swapped(swapped(straight)) == straight
    assert swapped("not-a-uuid") == ""


def test_quantities_come_back_in_cores_and_bytes():
    assert quantity("4") == 4
    assert quantity("3920m") == Decimal("3.92")
    assert quantity("16308580Ki") == Decimal(16308580) * 1024
    assert quantity("2Gi") == 2 * GIB
    # the decimal suffixes are not the binary ones, and "Mi" must not read as "M"
    assert quantity("500M") == Decimal(500) * 10**6
    assert quantity("500Mi") == Decimal(500) * 1024**2
    assert quantity("1e3") == 1000
    # a node that will not say how big it is still has to appear in the report
    assert quantity("") == 0
    assert quantity("garbage") == 0


def test_short_name_drops_the_dns_suffix():
    # kubelet registers either spelling depending on how the host resolves itself
    assert Node(name="web-01.dc1.local", uid="u").short_name == "web-01"
