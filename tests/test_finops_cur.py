"""The CUR collector — the free replacement for ce:GetCostAndUsage.

The fake S3 is a dict of key -> bytes that counts reads, because two of the
properties that matter here are about *not* fetching: the manifest is derived
rather than listed, and parsed totals are cached across cycles.
"""

from __future__ import annotations

import gzip
import io
import json
from datetime import date
from decimal import Decimal

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from clont.core.config import CURConfig
from clont.core.models import Period
from clont.finops.aws import cur
from clont.finops.base import FinOpsTuning
from clont.providers.aws import organizations

BUCKET = "billing-bucket"
JAN = "20240101-20240201"
FEB = "20240201-20240301"


def _gz(rows: list[dict]) -> bytes:
    header = [
        "lineItem/UsageStartDate",
        "lineItem/UnblendedCost",
        "lineItem/CurrencyCode",
        "lineItem/LineItemType",
        "lineItem/UsageAccountId",
        "product/ProductName",
    ]
    out = io.StringIO()
    out.write(",".join(header) + "\n")
    for row in rows:
        out.write(",".join(str(row.get(c, "")) for c in header) + "\n")
    return gzip.compress(out.getvalue().encode())


def _row(day: str, cost: str, service: str, *, account: str = "111", kind: str = "Usage") -> dict:
    return {
        "lineItem/UsageStartDate": f"{day}T00:00:00Z",
        "lineItem/UnblendedCost": cost,
        "lineItem/CurrencyCode": "USD",
        "lineItem/LineItemType": kind,
        "lineItem/UsageAccountId": account,
        "product/ProductName": service,
    }


def _manifest(*keys: str) -> bytes:
    return json.dumps(
        {"compression": "GZIP", "contentType": "text/csv", "reportKeys": list(keys)}
    ).encode()


class _FakeS3:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self._objects = objects
        self.reads: list[str] = []

    def get_object(self, Bucket: str, Key: str):  # noqa: N803 - boto3 spelling
        assert Bucket == BUCKET
        self.reads.append(Key)
        try:
            body = self._objects[Key]
        except KeyError:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "not found"}}, "GetObject"
            ) from None
        return {"Body": io.BytesIO(body)}


class _FakePaginator:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def paginate(self, **kw):
        return iter(self._pages)


class _FakeOrganizations:
    """Just enough of the organizations client for the ListAccounts paginator."""

    def __init__(self, names: dict[str, str]) -> None:
        self._names = names
        self.calls = 0

    def get_paginator(self, name: str):
        assert name == "list_accounts"
        self.calls += 1
        accounts = [{"Id": i, "Name": n} for i, n in self._names.items()]
        return _FakePaginator([{"Accounts": accounts}])


class _FakeProvider:
    def __init__(
        self,
        s3: _FakeS3,
        *,
        cur_config=None,
        account_id: str | None = "111",
        org_names: dict[str, str] | None = None,
    ) -> None:
        self._s3 = s3
        self.alias = "prod"
        self.account_id = account_id
        self.cur = cur_config if cur_config is not None else _config()
        # None = the role has no organizations access, the usual member-account case
        self.organizations = _FakeOrganizations(org_names) if org_names is not None else None

    def client(self, service: str, region: str | None = None):
        if service == "organizations":
            if self.organizations is None:
                raise ClientError(
                    {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
                    "ListAccounts",
                )
            return self.organizations
        assert service == "s3"
        return self._s3


def _config(**kw) -> CURConfig:
    return CURConfig(bucket=BUCKET, report_name="clont-cur", prefix="reports", **kw)


def _objects(**kw) -> dict[str, bytes]:
    return {
        f"reports/clont-cur/{JAN}/clont-cur-Manifest.json": _manifest("reports/clont-cur/data-1.csv.gz"),
        "reports/clont-cur/data-1.csv.gz": _gz(
            [
                _row("2024-01-01", "1.50", "Amazon Elastic Compute Cloud"),
                _row("2024-01-01", "0.50", "Amazon Elastic Compute Cloud"),
                _row("2024-01-01", "2.00", "Amazon Simple Storage Service"),
                _row("2024-01-02", "3.00", "Amazon Elastic Compute Cloud"),
            ]
            + kw.get("extra", [])
        ),
    }


@pytest.fixture(autouse=True)
def _no_cache():
    cur.clear_cache()
    organizations.clear_cache()
    yield
    cur.clear_cache()
    organizations.clear_cache()


def _collect(provider, period: Period | None = None, tuning=None):
    period = period or Period(start=date(2024, 1, 1), end=date(2024, 1, 31))
    return cur.CURCostCollector(provider, tuning).collect(period)


def test_rows_fold_into_daily_per_service_records():
    records = _collect(_FakeProvider(_FakeS3(_objects())))

    assert [(r.period.start, r.service, r.cost.amount) for r in records] == [
        (date(2024, 1, 1), "Amazon Elastic Compute Cloud", Decimal("2.00")),
        (date(2024, 1, 1), "Amazon Simple Storage Service", Decimal("2.00")),
        (date(2024, 1, 2), "Amazon Elastic Compute Cloud", Decimal("3.00")),
    ]
    assert {r.alias for r in records} == {"prod"}
    assert {r.cost.currency for r in records} == {"USD"}


def test_records_outside_the_window_are_dropped():
    records = _collect(
        _FakeProvider(_FakeS3(_objects())),
        Period(start=date(2024, 1, 2), end=date(2024, 1, 2)),
    )

    assert [r.period.start for r in records] == [date(2024, 1, 2)]


def test_linked_account_rows_are_skipped_by_default():
    objects = _objects(extra=[_row("2024-01-01", "99.00", "Amazon RDS", account="999")])

    records = _collect(_FakeProvider(_FakeS3(objects)))

    assert "Amazon RDS" not in {r.service for r in records}


def test_include_linked_keeps_the_whole_payer_report():
    objects = _objects(extra=[_row("2024-01-01", "99.00", "Amazon RDS", account="999")])
    provider = _FakeProvider(_FakeS3(objects), cur_config=_config(include_linked=True))

    records = _collect(provider)

    assert ("Amazon RDS", Decimal("99.00")) in [(r.service, r.cost.amount) for r in records]


def test_payer_report_reports_each_linked_account_under_its_own_alias():
    objects = _objects(extra=[_row("2024-01-01", "99.00", "Amazon RDS", account="999")])
    provider = _FakeProvider(
        _FakeS3(objects),
        cur_config=_config(include_linked=True),
        org_names={"111": "payer", "999": "sandbox"},
    )

    records = _collect(provider)

    # the payer keeps its configured alias; the member gets its organizations name
    assert {(r.alias, r.service) for r in records} == {
        ("prod", "Amazon Elastic Compute Cloud"),
        ("prod", "Amazon Simple Storage Service"),
        ("sandbox", "Amazon RDS"),
    }
    assert {r.dimensions["account_id"] for r in records} == {"111", "999"}
    # and the split doesn't double-count: same total as the unsplit report
    assert sum(r.cost.amount for r in records) == Decimal("106.00")


def test_linked_account_falls_back_to_its_id_without_organizations_access():
    objects = _objects(extra=[_row("2024-01-01", "99.00", "Amazon RDS", account="999")])
    provider = _FakeProvider(_FakeS3(objects), cur_config=_config(include_linked=True))

    records = _collect(provider)

    assert {r.alias for r in records} == {"prod", "999"}


def test_a_single_account_report_never_asks_organizations():
    provider = _FakeProvider(
        _FakeS3(_objects()), cur_config=_config(include_linked=True), org_names={"111": "payer"}
    )

    records = _collect(provider)

    assert {r.alias for r in records} == {"prod"}
    assert provider.organizations.calls == 0


def test_tax_lines_fall_back_to_their_line_item_type():
    objects = _objects(extra=[_row("2024-01-01", "0.40", "", kind="Tax")])

    records = _collect(_FakeProvider(_FakeS3(objects)))

    assert (date(2024, 1, 1), "Tax", Decimal("0.40")) in [
        (r.period.start, r.service, r.cost.amount) for r in records
    ]


def test_a_window_spanning_two_months_reads_both_manifests():
    objects = _objects()
    objects[f"reports/clont-cur/{FEB}/clont-cur-Manifest.json"] = _manifest(
        "reports/clont-cur/data-feb.csv.gz"
    )
    objects["reports/clont-cur/data-feb.csv.gz"] = _gz(
        [_row("2024-02-01", "7.00", "Amazon Elastic Compute Cloud")]
    )
    s3 = _FakeS3(objects)

    records = _collect(
        _FakeProvider(s3), Period(start=date(2024, 1, 30), end=date(2024, 2, 1))
    )

    # january's rows are all before the window, february's is inside it
    assert [(r.period.start, r.cost.amount) for r in records] == [
        (date(2024, 2, 1), Decimal("7.00"))
    ]
    assert f"reports/clont-cur/{FEB}/clont-cur-Manifest.json" in s3.reads


def test_missing_manifest_is_not_an_error():
    assert _collect(_FakeProvider(_FakeS3({}))) == []


def test_denied_manifest_read_surfaces():
    class _Denied(_FakeS3):
        def get_object(self, Bucket: str, Key: str):  # noqa: N803
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")

    with pytest.raises(ClientError):
        _collect(_FakeProvider(_Denied({})))


def test_non_csv_report_fails_loudly():
    objects = _objects()
    objects[f"reports/clont-cur/{JAN}/clont-cur-Manifest.json"] = json.dumps(
        {"compression": "Parquet", "contentType": "application/parquet", "reportKeys": []}
    ).encode()

    with pytest.raises(RuntimeError, match="gzip csv only"):
        _collect(_FakeProvider(_FakeS3(objects)))


def test_second_cycle_serves_the_cache():
    s3 = _FakeS3(_objects())
    provider = _FakeProvider(s3)

    first = _collect(provider)
    reads = len(s3.reads)
    second = _collect(provider)

    assert first == second
    assert len(s3.reads) == reads  # no second trip to S3


def test_cache_expires_after_refresh_minutes(monkeypatch):
    s3 = _FakeS3(_objects())
    provider = _FakeProvider(s3, cur_config=_config(refresh_minutes=1))
    clock = [1000.0]
    monkeypatch.setattr(cur.time, "monotonic", lambda: clock[0])

    _collect(provider)
    reads = len(s3.reads)
    clock[0] += 61
    _collect(provider)

    assert len(s3.reads) > reads


def test_no_cur_configured_means_no_records():
    provider = _FakeProvider(_FakeS3({}))
    provider.cur = None

    assert _collect(provider) == []


def test_only_the_manifest_and_its_data_files_are_fetched():
    # keys are derived from the billing period, so the role needs no ListBucket
    s3 = _FakeS3(_objects())

    _collect(_FakeProvider(s3))

    assert s3.reads == [
        f"reports/clont-cur/{JAN}/clont-cur-Manifest.json",
        "reports/clont-cur/data-1.csv.gz",
    ]


# --- moto: the real botocore streaming body, not our BytesIO ---------------


@pytest.fixture
def aws_creds(monkeypatch):
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@mock_aws
def test_reads_a_real_gzipped_object(aws_creds):
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=BUCKET)
    manifest_key = f"reports/clont-cur/{JAN}/clont-cur-Manifest.json"
    data_key = f"reports/clont-cur/{JAN}/assembly/clont-cur-1.csv.gz"
    s3.put_object(Bucket=BUCKET, Key=manifest_key, Body=_manifest(data_key))
    s3.put_object(
        Bucket=BUCKET,
        Key=data_key,
        # two hourly rows of the same day fold into one record
        Body=_gz(
            [
                _row("2024-01-05", "1.25", "Amazon Elastic Compute Cloud"),
                _row("2024-01-05", "0.75", "Amazon Elastic Compute Cloud"),
            ]
        ),
    )

    class _Provider(_FakeProvider):
        def client(self, service: str, region: str | None = None):
            return s3

    records = _collect(_Provider(None))

    assert [(r.period.start, r.service, r.cost.amount) for r in records] == [
        (date(2024, 1, 5), "Amazon Elastic Compute Cloud", Decimal("2.00")),
    ]


# --- cost allocation tags: the showback split ------------------------------


def _gz_cols(columns: list[str], rows: list[dict]) -> bytes:
    out = io.StringIO()
    out.write(",".join(columns) + "\n")
    for row in rows:
        out.write(",".join(str(row.get(c, "")) for c in columns) + "\n")
    return gzip.compress(out.getvalue().encode())


def _tagged_objects(columns: list[str], rows: list[dict]) -> dict[str, bytes]:
    return {
        f"reports/clont-cur/{JAN}/clont-cur-Manifest.json": _manifest(
            "reports/clont-cur/data-1.csv.gz"
        ),
        "reports/clont-cur/data-1.csv.gz": _gz_cols(columns, rows),
    }


_BASE_COLUMNS = [
    "lineItem/UsageStartDate",
    "lineItem/UnblendedCost",
    "lineItem/CurrencyCode",
    "lineItem/LineItemType",
    "lineItem/UsageAccountId",
    "product/ProductName",
]


def _tuning(*keys: str) -> FinOpsTuning:
    return FinOpsTuning(required_tags=keys)


def test_rows_split_by_tag_value_without_changing_the_service_total():
    rows = [
        _row("2024-01-01", "1.50", "Amazon Elastic Compute Cloud")
        | {"resourceTags/user:Owner": "team-a"},
        _row("2024-01-01", "0.50", "Amazon Elastic Compute Cloud")
        | {"resourceTags/user:Owner": "team-b"},
    ]
    objects = _tagged_objects([*_BASE_COLUMNS, "resourceTags/user:Owner"], rows)

    records = _collect(_FakeProvider(_FakeS3(objects)), tuning=_tuning("Owner"))

    assert [(r.cost.amount, r.tags) for r in records] == [
        (Decimal("1.50"), {"Owner": "team-a"}),
        (Decimal("0.50"), {"Owner": "team-b"}),
    ]
    # the day's ec2 total is the same 2.00 the untagged run reports
    assert sum(r.cost.amount for r in records) == Decimal("2.00")


def test_data_exports_column_spelling_matches_a_camelcase_key():
    rows = [
        _row("2024-01-01", "4.00", "Amazon Elastic Compute Cloud")
        | {"resource_tags_user_cost_center": "cc-42"},
    ]
    objects = _tagged_objects([*_BASE_COLUMNS, "resource_tags_user_cost_center"], rows)

    records = _collect(_FakeProvider(_FakeS3(objects)), tuning=_tuning("CostCenter"))

    assert [r.tags for r in records] == [{"CostCenter": "cc-42"}]


def test_a_key_with_no_column_reads_as_untagged_and_warns(caplog):
    # the tag is on the resources but was never activated in Billing, so the
    # report has no column for it — say so instead of reporting 0 spend
    objects = _tagged_objects(_BASE_COLUMNS, [_row("2024-01-01", "4.00", "Amazon RDS")])

    with caplog.at_level("WARNING"):
        records = _collect(_FakeProvider(_FakeS3(objects)), tuning=_tuning("Owner"))

    assert [r.tags for r in records] == [{"Owner": ""}]
    assert "no user tag column for Owner" in caplog.text


def test_untagged_rows_keep_a_blank_value():
    rows = [
        _row("2024-01-01", "1.00", "Amazon RDS") | {"resourceTags/user:Owner": "team-a"},
        _row("2024-01-02", "2.00", "Amazon RDS") | {"resourceTags/user:Owner": ""},
    ]
    objects = _tagged_objects([*_BASE_COLUMNS, "resourceTags/user:Owner"], rows)

    records = _collect(_FakeProvider(_FakeS3(objects)), tuning=_tuning("Owner"))

    assert [r.tags for r in records] == [{"Owner": "team-a"}, {"Owner": ""}]


def test_no_required_tags_leaves_records_tagless():
    records = _collect(_FakeProvider(_FakeS3(_objects())))

    assert {r.tags for r in records} == {None}


def test_changing_the_tag_keys_bypasses_the_cache():
    # the cached totals are keyed by tag combo, so a different key set is a
    # different aggregation and must not be served from the old one
    rows = [
        _row("2024-01-01", "1.00", "Amazon RDS")
        | {"resourceTags/user:Owner": "team-a", "resourceTags/user:Environment": "dev"},
    ]
    columns = [*_BASE_COLUMNS, "resourceTags/user:Owner", "resourceTags/user:Environment"]
    s3 = _FakeS3(_tagged_objects(columns, rows))
    provider = _FakeProvider(s3)

    first = _collect(provider, tuning=_tuning("Owner"))
    second = _collect(provider, tuning=_tuning("Environment"))

    assert [r.tags for r in first] == [{"Owner": "team-a"}]
    assert [r.tags for r in second] == [{"Environment": "dev"}]


def test_surplus_tag_values_fold_into_one_bucket(monkeypatch):
    # a high-cardinality required tag must not let the group table grow forever
    monkeypatch.setattr(cur, "_MAX_TAG_COMBOS", 2)
    rows = [
        _row("2024-01-01", "1.00", "Amazon RDS") | {"resourceTags/user:Name": f"n-{i}"}
        for i in range(5)
    ]
    objects = _tagged_objects([*_BASE_COLUMNS, "resourceTags/user:Name"], rows)

    records = _collect(_FakeProvider(_FakeS3(objects)), tuning=_tuning("Name"))

    assert len(records) == 3  # two real values plus the fold-in bucket
    assert {"Name": "(other)"} in [r.tags for r in records]
    assert sum(r.cost.amount for r in records) == Decimal("5.00")  # no dollars lost


def test_many_days_and_services_keep_their_tags(monkeypatch):
    # the cap is on distinct tag values, not on rows: days x services alone must
    # not blank the tags of a report whose cardinality is fine
    monkeypatch.setattr(cur, "_MAX_TAG_COMBOS", 2)
    rows = [
        _row(f"2024-01-0{day}", "1.00", service)
        | {"resourceTags/user:Owner": f"team-{service.lower()}"}
        for day in range(1, 6)
        for service in ("A", "B")
    ]
    objects = _tagged_objects([*_BASE_COLUMNS, "resourceTags/user:Owner"], rows)

    records = _collect(_FakeProvider(_FakeS3(objects)), tuning=_tuning("Owner"))

    assert len(records) == 10
    assert {r.tags["Owner"] for r in records} == {"team-a", "team-b"}


# --- data transfer: the usage-type split -----------------------------------


_USAGE_COLUMNS = [*_BASE_COLUMNS, "lineItem/UsageType"]


def test_transfer_rows_carry_their_bucket_as_a_dimension():
    rows = [
        _row("2024-01-01", "3.00", "Amazon Virtual Private Cloud")
        | {"lineItem/UsageType": "USE1-NatGateway-Bytes"},
        _row("2024-01-01", "1.00", "Amazon Virtual Private Cloud")
        | {"lineItem/UsageType": "USE1-NatGateway-Hours"},
    ]
    objects = _tagged_objects(_USAGE_COLUMNS, rows)

    records = _collect(_FakeProvider(_FakeS3(objects)))

    # same day, same service: the bytes split off, the hourly charge doesn't
    assert [(r.cost.amount, r.dimensions) for r in records] == [
        (Decimal("1.00"), None),
        (Decimal("3.00"), {"transfer": "nat"}),
    ]


def test_the_service_total_is_unchanged_by_the_transfer_split():
    rows = [
        _row("2024-01-01", "2.00", "Amazon Elastic Compute Cloud")
        | {"lineItem/UsageType": "USE1-DataTransfer-Regional-Bytes"},
        _row("2024-01-01", "8.00", "Amazon Elastic Compute Cloud")
        | {"lineItem/UsageType": "BoxUsage:t3.micro"},
    ]
    objects = _tagged_objects(_USAGE_COLUMNS, rows)

    records = _collect(_FakeProvider(_FakeS3(objects)))

    assert sum(r.cost.amount for r in records) == Decimal("10.00")
    assert {(r.dimensions or {}).get("transfer") for r in records} == {None, "cross-az"}


def test_a_report_with_no_usage_type_column_has_no_transfer_dimension():
    records = _collect(_FakeProvider(_FakeS3(_objects())))

    assert {r.dimensions for r in records} == {None}


# --- amortization: commitments must not land on one day ---------------------


_RI_COLUMNS = [
    *_BASE_COLUMNS,
    "reservation/EffectiveCost",
    "reservation/UnusedAmortizedUpfrontFeeForBillingPeriod",
    "reservation/UnusedRecurringFee",
    "reservation/ReservationARN",
]
_SP_COLUMNS = [
    *_BASE_COLUMNS,
    "savingsPlan/SavingsPlanEffectiveCost",
    "savingsPlan/TotalCommitmentToDate",
    "savingsPlan/UsedCommitment",
]
_EC2 = "Amazon Elastic Compute Cloud"
_ARN = "arn:aws:ec2:us-east-1:111:reserved-instances/r-1"


def _amounts(records) -> dict[tuple[str, date], Decimal]:
    return {(r.service, r.period.start): r.cost.amount for r in records}


def test_reserved_usage_is_priced_at_its_effective_cost():
    # the hour itself is free on the unblended column; the money is in the ri
    rows = [
        _row("2024-01-01", "0", _EC2, kind="DiscountedUsage")
        | {"reservation/EffectiveCost": "2.40"},
    ]
    records = _collect(_FakeProvider(_FakeS3(_tagged_objects(_RI_COLUMNS, rows))))

    assert _amounts(records) == {(_EC2, date(2024, 1, 1)): Decimal("2.40")}


def test_an_all_upfront_purchase_is_not_a_one_day_spike():
    rows = [
        _row("2024-01-01", "3.00", _EC2),
        # the lump: a year of ec2 bought on day one
        _row("2024-01-01", "8760.00", _EC2, kind="Fee") | {"reservation/ReservationARN": _ARN},
    ]
    records = _collect(_FakeProvider(_FakeS3(_tagged_objects(_RI_COLUMNS, rows))))

    assert _amounts(records) == {(_EC2, date(2024, 1, 1)): Decimal("3.00")}


def test_a_fee_with_no_reservation_is_still_spend():
    # support and other flat fees are type Fee too, and they are real money
    rows = [_row("2024-01-01", "100.00", "AWS Support (Business)", kind="Fee")]
    records = _collect(_FakeProvider(_FakeS3(_tagged_objects(_RI_COLUMNS, rows))))

    assert _amounts(records) == {("AWS Support (Business)", date(2024, 1, 1)): Decimal("100.00")}


def test_an_ri_fee_counts_only_the_part_nobody_used():
    rows = [
        _row("2024-01-01", "730.00", _EC2, kind="RIFee")
        | {
            "reservation/UnusedAmortizedUpfrontFeeForBillingPeriod": "10.00",
            "reservation/UnusedRecurringFee": "5.00",
        },
    ]
    records = _collect(_FakeProvider(_FakeS3(_tagged_objects(_RI_COLUMNS, rows))))

    # the used part is already on the DiscountedUsage lines; counting the whole
    # fee here would bill the reservation twice
    assert _amounts(records) == {(_EC2, date(2024, 1, 1)): Decimal("15.00")}


def test_savings_plan_covered_usage_replaces_its_on_demand_price():
    rows = [
        _row("2024-01-01", "10.00", _EC2, kind="SavingsPlanCoveredUsage")
        | {"savingsPlan/SavingsPlanEffectiveCost": "6.00"},
        _row("2024-01-01", "-10.00", _EC2, kind="SavingsPlanNegation"),
    ]
    records = _collect(_FakeProvider(_FakeS3(_tagged_objects(_SP_COLUMNS, rows))))

    assert _amounts(records) == {(_EC2, date(2024, 1, 1)): Decimal("6.00")}


def test_savings_plan_fees_count_only_the_unused_commitment():
    rows = [
        _row("2024-01-01", "0", _EC2, kind="SavingsPlanUpfrontFee")
        | {"savingsPlan/TotalCommitmentToDate": "100.00"},
        _row("2024-01-01", "24.00", _EC2, kind="SavingsPlanRecurringFee")
        | {"savingsPlan/TotalCommitmentToDate": "24.00", "savingsPlan/UsedCommitment": "18.00"},
    ]
    records = _collect(_FakeProvider(_FakeS3(_tagged_objects(_SP_COLUMNS, rows))))

    assert _amounts(records) == {(_EC2, date(2024, 1, 1)): Decimal("6.00")}


def test_amortize_off_keeps_the_raw_unblended_numbers():
    rows = [
        _row("2024-01-01", "8760.00", _EC2, kind="Fee") | {"reservation/ReservationARN": _ARN},
        _row("2024-01-01", "0", _EC2, kind="DiscountedUsage")
        | {"reservation/EffectiveCost": "2.40"},
    ]
    provider = _FakeProvider(
        _FakeS3(_tagged_objects(_RI_COLUMNS, rows)), cur_config=_config(amortize=False)
    )

    records = _collect(provider)

    assert _amounts(records) == {(_EC2, date(2024, 1, 1)): Decimal("8760.00")}


def test_a_report_without_the_amortization_columns_falls_back_to_unblended():
    # an old report has no effective-cost column; dropping the charge would be
    # worse than reporting it as the lump it is
    rows = [_row("2024-01-01", "730.00", _EC2, kind="RIFee")]
    records = _collect(_FakeProvider(_FakeS3(_tagged_objects(_BASE_COLUMNS, rows))))

    assert _amounts(records) == {(_EC2, date(2024, 1, 1)): Decimal("730.00")}


def test_an_ri_purchase_is_not_zeroed_when_nothing_can_spread_it():
    # the arn says which reservation, but with no effective-cost or unused-fee
    # column there is nothing to spread onto — zeroing here would lose the $1200
    columns = [*_BASE_COLUMNS, "reservation/ReservationARN"]
    rows = [
        _row("2024-01-01", "1200.00", _EC2, kind="Fee") | {"reservation/ReservationARN": _ARN},
        _row("2024-01-02", "0", _EC2, kind="DiscountedUsage"),
    ]
    records = _collect(_FakeProvider(_FakeS3(_tagged_objects(columns, rows))))

    assert _amounts(records) == {(_EC2, date(2024, 1, 1)): Decimal("1200.00")}


def test_a_savings_plan_without_the_columns_still_nets_out():
    # unblended already cancels covered usage against its negation, so zeroing the
    # negation alone would count the plan twice
    rows = [
        _row("2024-01-01", "10.00", _EC2, kind="SavingsPlanCoveredUsage"),
        _row("2024-01-01", "-10.00", _EC2, kind="SavingsPlanNegation"),
        _row("2024-01-01", "24.00", _EC2, kind="SavingsPlanRecurringFee"),
    ]
    records = _collect(_FakeProvider(_FakeS3(_tagged_objects(_BASE_COLUMNS, rows))))

    assert _amounts(records) == {(_EC2, date(2024, 1, 1)): Decimal("24.00")}


def test_changing_amortization_bypasses_the_cache():
    rows = [
        _row("2024-01-01", "0", _EC2, kind="DiscountedUsage")
        | {"reservation/EffectiveCost": "2.40"},
        _row("2024-01-01", "8760.00", _EC2, kind="Fee") | {"reservation/ReservationARN": _ARN},
    ]
    s3 = _FakeS3(_tagged_objects(_RI_COLUMNS, rows))

    first = _collect(_FakeProvider(s3))
    second = _collect(_FakeProvider(s3, cur_config=_config(amortize=False)))

    assert sum(r.cost.amount for r in first) == Decimal("2.40")
    assert sum(r.cost.amount for r in second) == Decimal("8760.00")


# --- credits, refunds, tax: not usage ---------------------------------------


def test_a_credit_does_not_make_a_service_look_cheaper():
    objects = _objects(extra=[_row("2024-01-02", "-3.00", _EC2, kind="Credit")])

    records = _collect(_FakeProvider(_FakeS3(objects)))

    # ec2 keeps the day it actually consumed; the credit is its own line
    assert _amounts(records)[(_EC2, date(2024, 1, 2))] == Decimal("3.00")
    assert _amounts(records)[("Credit", date(2024, 1, 2))] == Decimal("-3.00")
    assert sum(r.cost.amount for r in records) == Decimal("4.00")


def test_refunds_and_tax_get_their_own_buckets_too():
    objects = _objects(
        extra=[
            _row("2024-01-02", "-1.00", _EC2, kind="Refund"),
            _row("2024-01-02", "0.40", _EC2, kind="Tax"),
        ]
    )

    records = _collect(_FakeProvider(_FakeS3(objects)))

    assert _amounts(records)[("Refund", date(2024, 1, 2))] == Decimal("-1.00")
    assert _amounts(records)[("Tax", date(2024, 1, 2))] == Decimal("0.40")


def test_credits_can_be_dropped_for_gross_spend():
    objects = _objects(
        extra=[
            _row("2024-01-02", "-3.00", _EC2, kind="Credit"),
            _row("2024-01-02", "-1.00", _EC2, kind="Refund"),
        ]
    )
    provider = _FakeProvider(_FakeS3(objects), cur_config=_config(include_credits=False))

    records = _collect(provider)

    assert {r.service for r in records} == {_EC2, "Amazon Simple Storage Service"}
    assert sum(r.cost.amount for r in records) == Decimal("7.00")


def test_tax_can_be_dropped():
    objects = _objects(extra=[_row("2024-01-02", "0.40", _EC2, kind="Tax")])
    provider = _FakeProvider(_FakeS3(objects), cur_config=_config(include_tax=False))

    records = _collect(provider)

    assert "Tax" not in {r.service for r in records}


def test_a_discount_stays_on_the_service_it_discounts():
    # unlike a credit, an edp discount tracks usage, so moving it would make the
    # service look more expensive than it is billed
    objects = _objects(extra=[_row("2024-01-02", "-0.30", _EC2, kind="EdpDiscount")])

    records = _collect(_FakeProvider(_FakeS3(objects)))

    assert _amounts(records)[(_EC2, date(2024, 1, 2))] == Decimal("2.70")
