"""Cost Explorer collector, driven by a botocore Stubber.

moto v5 doesn't implement ce:GetCostAndUsage, so we feed canned responses with
botocore's Stubber instead. Covers pagination and the inclusive->exclusive end
date conversion.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import boto3
import pytest
from botocore.stub import Stubber

from clont.core.models import Period
from clont.finops.aws.cost_explorer import CostExplorerCollector
from clont.finops.base import FinOpsTuning

# the collector is dormant unless the operator accepts the per-request charge
BILLED = FinOpsTuning(allow_cost_explorer=True)

PERIOD = Period(start=date(2024, 1, 1), end=date(2024, 1, 1))
# Inclusive [1 Jan, 1 Jan] -> CE exclusive End is the next day.
EXPECTED_TIME_PERIOD = {"Start": "2024-01-01", "End": "2024-01-02"}

_BASE_PARAMS = {
    "TimePeriod": EXPECTED_TIME_PERIOD,
    "Granularity": "DAILY",
    "Metrics": ["UnblendedCost"],
    "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
}


def _group(service: str, amount: str) -> dict:
    return {
        "Keys": [service],
        "Metrics": {"UnblendedCost": {"Amount": amount, "Unit": "USD"}},
    }


class _FakeProvider:
    def __init__(self, client, alias: str = "prod") -> None:
        self._client = client
        self.alias = alias

    def client(self, service: str, region: str | None = None):
        assert service == "ce"
        assert region == "us-east-1"
        return self._client


@pytest.fixture
def ce_client():
    client = boto3.client(
        "ce",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    with Stubber(client) as stubber:
        yield client, stubber


def test_collect_paginates_and_maps_records(ce_client):
    client, stubber = ce_client

    page1 = {
        "ResultsByTime": [
            {
                "TimePeriod": EXPECTED_TIME_PERIOD,
                "Total": {},
                "Groups": [_group("Amazon EC2", "12.34")],
                "Estimated": False,
            }
        ],
        "NextPageToken": "page2",
    }
    page2 = {
        "ResultsByTime": [
            {
                "TimePeriod": EXPECTED_TIME_PERIOD,
                "Total": {},
                "Groups": [_group("Amazon S3", "5.00")],
                "Estimated": False,
            }
        ],
    }
    stubber.add_response("get_cost_and_usage", page1, _BASE_PARAMS)
    stubber.add_response(
        "get_cost_and_usage", page2, {**_BASE_PARAMS, "NextPageToken": "page2"}
    )

    records = CostExplorerCollector(_FakeProvider(client, alias="staging"), BILLED).collect(PERIOD)

    assert [(r.service, r.cost.amount, r.cost.currency) for r in records] == [
        ("Amazon EC2", Decimal("12.34"), "USD"),
        ("Amazon S3", Decimal("5.00"), "USD"),
    ]
    assert all(r.cloud == "aws" and r.period == PERIOD for r in records)
    assert all(r.alias == "staging" for r in records)  # alias attribution


def test_collect_stamps_each_record_with_its_own_day(ce_client):
    client, stubber = ce_client

    multi_day = Period(start=date(2024, 1, 1), end=date(2024, 1, 3))
    params = {**_BASE_PARAMS, "TimePeriod": {"Start": "2024-01-01", "End": "2024-01-04"}}
    resp = {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2024-01-01", "End": "2024-01-02"},
                "Groups": [_group("Amazon EC2", "10.00")],
            },
            {
                "TimePeriod": {"Start": "2024-01-02", "End": "2024-01-03"},
                "Groups": [_group("Amazon EC2", "11.00")],
            },
            {
                "TimePeriod": {"Start": "2024-01-03", "End": "2024-01-04"},
                "Groups": [_group("Amazon EC2", "30.00")],
            },
        ],
    }
    stubber.add_response("get_cost_and_usage", resp, params)

    records = CostExplorerCollector(_FakeProvider(client), BILLED).collect(multi_day)

    # One record per (service, day), each stamped with its own single-day period.
    assert [(r.period.start, r.period.end, r.cost.amount) for r in records] == [
        (date(2024, 1, 1), date(2024, 1, 1), Decimal("10.00")),
        (date(2024, 1, 2), date(2024, 1, 2), Decimal("11.00")),
        (date(2024, 1, 3), date(2024, 1, 3), Decimal("30.00")),
    ]


def test_collect_handles_empty_result(ce_client):
    client, stubber = ce_client
    stubber.add_response(
        "get_cost_and_usage",
        {"ResultsByTime": [{"TimePeriod": EXPECTED_TIME_PERIOD, "Groups": []}]},
        _BASE_PARAMS,
    )

    records = CostExplorerCollector(_FakeProvider(client), BILLED).collect(PERIOD)
    assert records == []


def test_collector_is_dormant_unless_allowed(ce_client):
    client, _ = ce_client  # no stubbed response: any call would raise

    # default tuning = no opt-in, so the billed call is never made
    assert CostExplorerCollector(_FakeProvider(client)).collect(PERIOD) == []
    assert CostExplorerCollector(_FakeProvider(client), FinOpsTuning()).collect(PERIOD) == []
