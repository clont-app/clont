"""Public IPv4: the running bill from ENIs + EIPs, and the actionable subset."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from clont.core.models import Period
from clont.finops.aws import pricing
from clont.finops.aws.public_ipv4 import PublicIPv4Collector


class _Paginator:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def paginate(self, **kw):
        yield from self._pages


class _FakeEC2:
    def __init__(self, enis: list[dict], addresses: list[dict]) -> None:
        self._enis = enis
        self._addresses = addresses

    def get_paginator(self, name: str) -> _Paginator:
        assert name == "describe_network_interfaces"
        return _Paginator([{"NetworkInterfaces": self._enis}])

    def describe_addresses(self, **kw) -> dict:
        return {"Addresses": self._addresses}


class _FakeProvider:
    def __init__(self, ec2: _FakeEC2, region: str = "us-east-1", alias: str = "prod") -> None:
        self._ec2 = ec2
        self._region = region
        self.alias = alias

    def regions(self) -> list[str]:
        return [self._region]

    def client(self, service: str, region: str | None = None):
        assert service == "ec2"
        return self._ec2


def _period() -> Period:
    return Period(date(2026, 1, 1), date(2026, 1, 31))


def _eni(eni_id: str, public_ip: str = "", **kw) -> dict:
    raw: dict = {"NetworkInterfaceId": eni_id, "Status": "in-use", **kw}
    if public_ip:
        raw["Association"] = {"PublicIp": public_ip}
        raw.setdefault(
            "PrivateIpAddresses",
            [{"Primary": True, "Association": {"PublicIp": public_ip}}],
        )
    return raw


def _collect(ec2: _FakeEC2, region: str = "us-east-1"):
    return PublicIPv4Collector(_FakeProvider(ec2, region)).collect(_period())


def _recommend(ec2: _FakeEC2, region: str = "us-east-1"):
    return PublicIPv4Collector(_FakeProvider(ec2, region)).recommendations(_period())


def test_an_associated_eip_is_counted_once_not_twice():
    # the same address comes back from both describes; double-counting it here
    # is what would inflate the headline figure
    ec2 = _FakeEC2(
        [_eni("eni-1", "1.2.3.4", Attachment={"Status": "attached"})],
        [{"PublicIp": "1.2.3.4", "AllocationId": "eipalloc-1", "AssociationId": "eipassoc-1"}],
    )
    (record,) = _collect(ec2)
    assert record.dimensions["addresses"] == "1"
    assert record.cost.amount == pricing.public_ipv4_daily_quote("us-east-1").amount


def test_spend_is_stamped_one_day_at_a_time():
    # the digest sums the latest day and the spike detector groups by period.end;
    # a record spanning the window would put a monthly figure in a daily total
    ec2 = _FakeEC2([_eni("eni-1", "1.2.3.4")], [])
    (record,) = _collect(ec2)
    assert record.period == Period(date(2026, 1, 31), date(2026, 1, 31))

    daily = pricing.public_ipv4_daily_quote("us-east-1").amount
    monthly = pricing.public_ipv4_quote("us-east-1").amount
    assert record.cost.amount == daily
    assert daily * pricing.HOURS_PER_MONTH == monthly * pricing.HOURS_PER_DAY


def test_every_kind_of_eni_with_an_address_counts():
    ec2 = _FakeEC2(
        [
            _eni("eni-nat", "1.1.1.1", InterfaceType="nat_gateway"),
            _eni("eni-nlb", "2.2.2.2", InterfaceType="network_load_balancer"),
            _eni("eni-ec2", "3.3.3.3", InterfaceType="interface"),
            _eni("eni-lambda", InterfaceType="lambda"),  # no public ip, not billed
            _eni("eni-vpce", InterfaceType="vpc_endpoint"),
        ],
        [],
    )
    (record,) = _collect(ec2)
    assert record.dimensions["addresses"] == "3"
    assert record.service == "public_ipv4"  # not `ec2`, or CUR spend double-counts
    assert record.dimensions["usage_type"] == "PublicIPv4:InUseAddress"


def test_secondary_addresses_on_one_eni_are_billed_each():
    ec2 = _FakeEC2(
        [
            {
                "NetworkInterfaceId": "eni-multi",
                "Attachment": {"Status": "attached"},
                "Association": {"PublicIp": "1.1.1.1"},
                "PrivateIpAddresses": [
                    {"Primary": True, "Association": {"PublicIp": "1.1.1.1"}},
                    {"Primary": False, "Association": {"PublicIp": "2.2.2.2"}},
                ],
            }
        ],
        [],
    )
    (record,) = _collect(ec2)
    assert record.dimensions["addresses"] == "2"

    recs = _recommend(ec2)
    assert [r.kind for r in recs] == ["secondary-public-ip"]
    assert "2.2.2.2" in recs[0].summary


def test_unassociated_eip_is_both_a_cost_and_a_recommendation():
    ec2 = _FakeEC2([], [{"PublicIp": "9.9.9.9", "AllocationId": "eipalloc-idle"}])
    (record,) = _collect(ec2)
    assert record.dimensions["addresses"] == "1"  # AWS bills it either way

    (rec,) = _recommend(ec2)
    assert rec.kind == "unassociated-eip"
    assert rec.resource.resource_id == "eipalloc-idle"
    assert rec.estimated_savings.amount == pricing.public_ipv4_quote("us-east-1").amount
    assert rec.approximate is False


def test_byoip_addresses_are_not_charged():
    ec2 = _FakeEC2(
        [], [{"PublicIp": "8.8.8.8", "AllocationId": "a", "PublicIpv4Pool": "ipv4pool-ec2-1"}]
    )
    assert _collect(ec2) == []
    assert _recommend(ec2) == []


def test_an_associated_byoip_address_is_not_charged_either():
    # it's on the eni too, so skipping it in the eip loop alone still billed it
    ec2 = _FakeEC2(
        [_eni("eni-byoip", "8.8.8.8", Attachment={"Status": "attached"})],
        [{
            "PublicIp": "8.8.8.8",
            "AllocationId": "a",
            "AssociationId": "eipassoc-9",
            "PublicIpv4Pool": "ipv4pool-ec2-1",
        }],
    )
    assert _collect(ec2) == []


def test_address_on_a_detached_eni_is_flagged():
    ec2 = _FakeEC2(
        [_eni("eni-detached", "4.4.4.4", Attachment={"Status": "detached"})],
        [{"PublicIp": "4.4.4.4", "AllocationId": "eipalloc-2", "AssociationId": "eipassoc-2"}],
    )
    (rec,) = _recommend(ec2)
    assert rec.kind == "unattached-eni-ip"
    assert rec.resource.resource_id == "eni-detached"


def test_address_on_an_available_eni_is_flagged():
    # a detached eni has no Attachment block at all — the common waste case, and
    # keying off attachment status alone read it as attached and said nothing
    ec2 = _FakeEC2(
        [{
            "NetworkInterfaceId": "eni-loose",
            "Status": "available",
            "Association": {"PublicIp": "5.5.5.5"},
        }],
        [],
    )
    (rec,) = _recommend(ec2)
    assert rec.kind == "unattached-eni-ip"
    assert rec.resource.resource_id == "eni-loose"


def test_each_secondary_address_gets_its_own_recommendation_id():
    # events dedupe on kind + resource_id, so a shared eni id would drop all but one
    ec2 = _FakeEC2(
        [{
            "NetworkInterfaceId": "eni-multi",
            "Attachment": {"Status": "attached"},
            "Association": {"PublicIp": "1.1.1.1"},
            "PrivateIpAddresses": [
                {"Primary": True, "Association": {"PublicIp": "1.1.1.1"}},
                {"Primary": False, "Association": {"PublicIp": "2.2.2.2"}},
                {"Primary": False, "Association": {"PublicIp": "3.3.3.3"}},
            ],
        }],
        [],
    )
    recs = _recommend(ec2)
    assert [r.kind for r in recs] == ["secondary-public-ip"] * 2
    assert len({r.resource.resource_id for r in recs}) == 2


def test_a_secondary_counts_when_the_primary_has_no_public_address():
    ec2 = _FakeEC2(
        [{
            "NetworkInterfaceId": "eni-sec-only",
            "Attachment": {"Status": "attached"},
            "PrivateIpAddresses": [
                {"Primary": True},
                {"Primary": False, "Association": {"PublicIp": "2.2.2.2"}},
            ],
        }],
        [],
    )
    (record,) = _collect(ec2)
    assert record.dimensions["addresses"] == "1"

    (rec,) = _recommend(ec2)
    assert rec.kind == "secondary-public-ip"


def test_a_region_we_cannot_price_says_so():
    ec2 = _FakeEC2([_eni("eni-1", "1.2.3.4")], [])
    (record,) = _collect(ec2, region="mars-north-1")
    assert record.cost.amount > Decimal("0")  # a miss must never cost nothing

    quote = pricing.public_ipv4_quote("mars-north-1")
    assert quote.approximate is True
    assert quote.region == pricing.BASE_REGION


def test_nothing_public_means_no_record():
    assert _collect(_FakeEC2([_eni("eni-private")], [])) == []
