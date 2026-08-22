"""Idle load balancer detection: ALB/NLB with no registered targets."""

from __future__ import annotations

from decimal import Decimal

from clont.finops.aws import pricing
from clont.finops.aws.idle_elb import IdleLoadBalancerCollector


class _Paginator:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def paginate(self, **kw):
        yield from self._pages


class _FakeELB:
    def __init__(self, lbs: list[dict], tgs_by_lb: dict, health_by_tg: dict) -> None:
        self._lbs = lbs
        self._tgs_by_lb = tgs_by_lb        # lb_arn -> [target group dicts]
        self._health_by_tg = health_by_tg  # tg_arn -> [target health descriptions]

    def get_paginator(self, name: str) -> _Paginator:
        assert name == "describe_load_balancers"
        return _Paginator([{"LoadBalancers": self._lbs}])

    def describe_target_groups(self, LoadBalancerArn: str) -> dict:
        return {"TargetGroups": self._tgs_by_lb.get(LoadBalancerArn, [])}

    def describe_target_health(self, TargetGroupArn: str) -> dict:
        return {"TargetHealthDescriptions": self._health_by_tg.get(TargetGroupArn, [])}


class _FakeProvider:
    def __init__(self, elb: _FakeELB, alias: str = "prod") -> None:
        self._elb = elb
        self.alias = alias

    def regions(self) -> list[str]:
        return ["us-east-1"]

    def client(self, service: str, region: str | None = None):
        assert service == "elbv2"
        return self._elb


def _lb(name: str, arn: str, lb_type: str = "application", state: str = "active") -> dict:
    return {
        "LoadBalancerArn": arn,
        "LoadBalancerName": name,
        "Type": lb_type,
        "State": {"Code": state},
    }


def test_load_balancer_with_no_targets_is_flagged():
    lbs = [
        _lb("empty-lb", "arn:lb/empty"),
        _lb("used-lb", "arn:lb/used"),
        _lb("no-tg-lb", "arn:lb/notg"),
    ]
    tgs = {
        "arn:lb/empty": [{"TargetGroupArn": "arn:tg/empty"}],
        "arn:lb/used": [{"TargetGroupArn": "arn:tg/used"}],
        "arn:lb/notg": [],  # no target groups at all -> idle
    }
    health = {
        "arn:tg/empty": [],                       # registered: none -> idle
        "arn:tg/used": [{"Target": {"Id": "i-1"}}],  # has a target -> not idle
    }
    recs = IdleLoadBalancerCollector(
        _FakeProvider(_FakeELB(lbs, tgs, health))
    ).recommendations(None)

    ids = sorted(r.resource.resource_id for r in recs)
    assert ids == ["empty-lb", "no-tg-lb"]
    by_id = {r.resource.resource_id: r for r in recs}
    assert by_id["empty-lb"].kind == "idle-elb"
    assert by_id["empty-lb"].estimated_savings.amount == pricing.LOAD_BALANCER_MONTH
    # the rate table moves when it's regenerated; the ballpark shouldn't
    assert Decimal("10") < pricing.LOAD_BALANCER_MONTH < Decimal("30")
    assert "APPLICATION" in by_id["empty-lb"].summary


def test_provisioning_load_balancer_is_skipped():
    lbs = [_lb("new-lb", "arn:lb/new", state="provisioning")]
    recs = IdleLoadBalancerCollector(
        _FakeProvider(_FakeELB(lbs, {}, {}))
    ).recommendations(None)
    assert recs == []


def test_no_load_balancers_no_recs():
    recs = IdleLoadBalancerCollector(
        _FakeProvider(_FakeELB([], {}, {}))
    ).recommendations(None)
    assert recs == []
