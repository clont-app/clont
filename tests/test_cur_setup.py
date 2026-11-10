from botocore.exceptions import ClientError

from clont.agent.runner import Agent
from clont.finops.aws import cur_setup


def _denied(op):
    return ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, op)


def _export(name, fmt="TEXT_OR_CSV", comp="GZIP", ids="TRUE", table="COST_AND_USAGE_REPORT"):
    return {
        "Name": name,
        "DataQuery": {
            "QueryStatement": f"SELECT line_item_unblended_cost FROM {table}",
            "TableConfigurations": {table: {"INCLUDE_RESOURCES": ids}},
        },
        "DestinationConfigurations": {
            "S3Destination": {
                "S3Bucket": "bills",
                "S3Prefix": "cur",
                "S3Region": "eu-west-1",
                "S3OutputConfigurations": {"Format": fmt, "Compression": comp},
            }
        },
    }


class _Exports:
    def __init__(self, exports):
        self._exports = exports

    def list_exports(self, **_):
        return {"Exports": [{"ExportArn": name} for name in self._exports]}

    def get_export(self, ExportArn):
        return {"Export": self._exports[ExportArn]}


class _Legacy:
    def __init__(self, reports):
        self._reports = reports

    def describe_report_definitions(self, **_):
        return {"ReportDefinitions": self._reports}


class _Raises:
    def __getattr__(self, op):
        def call(**_):
            raise _denied(op)

        return call


class _Provider:
    alias = "prod"

    def __init__(self, clients, regions=("us-east-1",)):
        self._clients = clients
        self._regions = list(regions)

    def client(self, service, region=None):
        return self._clients.get(service, _Raises())

    def regions(self):
        return self._regions


def test_picks_csv_cur2_export_over_parquet_and_focus():
    provider = _Provider(
        {
            "bcm-data-exports": _Exports(
                {
                    "p": _export("pq", fmt="PARQUET", comp="PARQUET"),
                    "f": _export("focus", table="FOCUS_1_0"),
                    "c": _export("clont-cur"),
                }
            )
        }
    )
    found = cur_setup.discover(provider)
    assert (found.bucket, found.report_name, found.prefix, found.region) == (
        "bills",
        "clont-cur",
        "cur",
        "eu-west-1",
    )


def test_parquet_only_is_not_used():
    provider = _Provider(
        {"bcm-data-exports": _Exports({"p": _export("pq", fmt="PARQUET", comp="PARQUET")})}
    )
    assert cur_setup.discover(provider) is None


def test_export_without_resource_ids_still_used():
    provider = _Provider({"bcm-data-exports": _Exports({"c": _export("x", ids="FALSE")})})
    assert cur_setup.discover(provider).report_name == "x"


def test_falls_back_to_legacy_cur_when_exports_denied():
    report = {
        "ReportName": "old",
        "S3Bucket": "b",
        "S3Prefix": "p",
        "S3Region": "us-east-2",
        "Format": "textORcsv",
        "Compression": "GZIP",
        "AdditionalSchemaElements": ["RESOURCES"],
    }
    provider = _Provider({"cur": _Legacy([report])})
    found = cur_setup.discover(provider)
    assert (found.report_name, found.region) == ("old", "us-east-2")


def test_nothing_found_when_all_denied():
    assert cur_setup.discover(_Provider({})) is None


class _Aoss:
    def list_collections(self, **_):
        return {"collectionSummaries": [{"name": "kb"}]}


class _Tags:
    def __init__(self, hit_key):
        self.hit_key = hit_key

    def get_resources(self, TagFilters, **_):
        hit = TagFilters[0]["Key"] == self.hit_key
        return {"ResourceTagMappingList": [{"ResourceARN": "arn"}] if hit else []}


def test_agent_signs_from_collection_and_tag():
    provider = _Provider(
        {"opensearchserverless": _Aoss(), "resourcegroupstaggingapi": _Tags("agent")}
    )
    signs = cur_setup.agent_signs(provider, ("team", "agent"))
    assert signs == ["agent tag in us-east-1", "opensearch serverless collection in us-east-1"]


def test_no_signs_when_nothing_answers():
    assert cur_setup.agent_signs(_Provider({}), ("agent",)) == []


class _Channel:
    name = "rec"

    def __init__(self):
        self.sent = []

    def send(self, event):
        self.sent.append(event.key)
        return True


def test_notice_goes_out_on_first_cycle_only():
    channel = _Channel()
    agent = Agent([], [channel], notices=[cur_setup.notice("prod", ["bedrock agent in us-east-1"])])
    agent.run_once()
    agent.run_once()
    assert channel.sent == ["cur-setup:prod"]
