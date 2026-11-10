"""Find the account's cur export by itself, and say so when agents run without one.

`bcm-data-exports` (cur 2.0) and `cur:DescribeReportDefinitions` (legacy) both
hand back bucket, prefix, name, region and format, so the `cur:` block in the
yaml becomes an override and not a step. Every call here is free and read-only.

Without a bill the agent spend split has nothing to read. Telling everybody to
set up cur is noise, so the reminder only goes out when the account visibly
runs agents: a resource carrying an agent tag, a bedrock agent or an
opensearch serverless collection.
"""

from __future__ import annotations

from botocore.exceptions import ClientError

from clont.core.config import CURConfig
from clont.core.logging import get_logger
from clont.core.models import Cloud
from clont.events.models import Event, EventSeverity

log = get_logger("clont.finops.aws.cur_setup")

# both billing apis live in us-east-1 only
_BILLING_REGION = "us-east-1"
_CUR2_TABLE = "COST_AND_USAGE_REPORT"
_SKIP = (ClientError,)

NOTICE_KIND = "cur-setup"

SETUP_STEPS = (
    "1. billing → data exports → create → standard data export (cur 2.0), "
    "tick \"include resource ids\", format csv + gzip, overwrite, daily\n"
    "2. give the clont role s3:GetObject on the export folder in that bucket\n"
    "3. and bcm-data-exports:ListExports + bcm-data-exports:GetExport, "
    "so clont finds the export by itself — no yaml change"
)


def _code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def _cur2_candidates(provider) -> list[tuple[int, CURConfig, str]]:
    client = provider.client("bcm-data-exports", _BILLING_REGION)
    out: list[tuple[int, CURConfig, str]] = []
    token = None
    while True:
        page = client.list_exports(**({"NextToken": token} if token else {}))
        for ref in page.get("Exports", []):
            export = client.get_export(ExportArn=ref["ExportArn"]).get("Export", {})
            query = export.get("DataQuery", {})
            if _CUR2_TABLE not in query.get("QueryStatement", "").upper():
                continue  # focus / carbon / cost optimization exports, not a bill
            dest = export.get("DestinationConfigurations", {}).get("S3Destination", {})
            fmt = dest.get("S3OutputConfigurations", {})
            table = query.get("TableConfigurations", {}).get(_CUR2_TABLE, {})
            csv = fmt.get("Format") == "TEXT_OR_CSV" and fmt.get("Compression") == "GZIP"
            ids = str(table.get("INCLUDE_RESOURCES", "")).upper() == "TRUE"
            name = export.get("Name") or ref.get("ExportName", "")
            config = CURConfig(
                bucket=dest["S3Bucket"],
                report_name=name,
                prefix=dest.get("S3Prefix", ""),
                region=dest.get("S3Region") or _BILLING_REGION,
            )
            out.append((_rank(csv, ids), config, _issues(name, csv, ids)))
        token = page.get("NextToken")
        if not token:
            return out


def _legacy_candidates(provider) -> list[tuple[int, CURConfig, str]]:
    client = provider.client("cur", _BILLING_REGION)
    out: list[tuple[int, CURConfig, str]] = []
    token = None
    while True:
        page = client.describe_report_definitions(**({"NextToken": token} if token else {}))
        for report in page.get("ReportDefinitions", []):
            csv = report.get("Format") == "textORcsv" and report.get("Compression") == "GZIP"
            ids = "RESOURCES" in report.get("AdditionalSchemaElements", [])
            name = report["ReportName"]
            config = CURConfig(
                bucket=report["S3Bucket"],
                report_name=name,
                prefix=report.get("S3Prefix", ""),
                region=report.get("S3Region") or _BILLING_REGION,
            )
            out.append((_rank(csv, ids), config, _issues(name, csv, ids)))
        token = page.get("NextToken")
        if not token:
            return out


def _rank(csv: bool, ids: bool) -> int:
    # an unreadable format is useless, missing ids only blurs the agent split
    return (2 if csv else 0) + (1 if ids else 0)


def _issues(name: str, csv: bool, ids: bool) -> str:
    if not csv:
        return f"export {name} is parquet or uncompressed; clont reads csv + gzip only"
    if not ids:
        return (
            f"export {name} has no resource ids; spend can't be tied to a collection "
            "or function — tick \"include resource ids\""
        )
    return ""


def discover(provider) -> CURConfig | None:
    """The best readable cur export on this account, or None.

    cur 2.0 first, legacy cur after; a denied api is the same as no export,
    it just goes in the log so the operator knows which grant to add.
    """
    found: list[tuple[int, CURConfig, str]] = []
    denied: list[str] = []
    for action, source in (
        ("bcm-data-exports:ListExports", _cur2_candidates),
        ("cur:DescribeReportDefinitions", _legacy_candidates),
    ):
        try:
            found.extend(source(provider))
        except _SKIP as exc:
            code = _code(exc)
            if "Denied" in code or code == "UnauthorizedOperation":
                denied.append(action)
                continue
            log.warning("%s: cur lookup via %s failed: %s", provider.alias, action, exc)
    if not found:
        if denied:
            log.info("%s: can't look for a cur export, role lacks %s", provider.alias, ", ".join(denied))
        return None
    found.sort(key=lambda item: item[0], reverse=True)
    rank, config, issue = found[0]
    if issue:
        log.warning("%s: %s", provider.alias, issue)
    if rank < 2:
        return None  # nothing clont can parse; the warning above says why
    log.info(
        "%s: found cur export %s at s3://%s/%s",
        provider.alias,
        config.report_name,
        config.bucket,
        config.prefix,
    )
    return config


def _tagged(provider, region: str, keys: tuple[str, ...]) -> bool:
    client = provider.client("resourcegroupstaggingapi", region)
    # tag filters are and-ed, so each key is asked on its own
    for key in keys:
        page = client.get_resources(TagFilters=[{"Key": key}], ResourcesPerPage=1)
        if page.get("ResourceTagMappingList"):
            return True
    return False


def agent_signs(provider, tag_keys: tuple[str, ...] = ()) -> list[str]:
    """What on this account looks like an agent, as short labels; empty when nothing does."""
    probes = (
        ("agent tag", lambda r: bool(tag_keys) and _tagged(provider, r, tag_keys)),
        (
            "bedrock agent",
            lambda r: bool(provider.client("bedrock-agent", r).list_agents(maxResults=1).get("agentSummaries")),
        ),
        (
            "opensearch serverless collection",
            lambda r: bool(provider.client("opensearchserverless", r).list_collections(maxResults=1).get("collectionSummaries")),
        ),
    )
    try:
        regions = provider.regions()
    except _SKIP as exc:
        log.info("%s: no region list for the agent check: %s", provider.alias, exc)
        return []
    signs: list[str] = []
    for label, probe in probes:
        for region in regions:
            try:
                hit = probe(region)
            except _SKIP:
                continue  # denied or the service isn't in this region
            except Exception:  # noqa: BLE001 - an unknown endpoint must not stop startup
                continue
            if hit:
                signs.append(f"{label} in {region}")
                break
    return signs


def notice(alias: str, signs: list[str]) -> Event:
    """The one message an account with agents and no bill gets."""
    return Event(
        key=f"{NOTICE_KIND}:{alias}",
        severity=EventSeverity.WARN,
        domain="finops",
        cloud=Cloud.AWS,
        title=f"{alias}: agents found, but no cur 2.0 to price them",
        message=(
            f"found {', '.join(signs)}. without a cur 2.0 export clont can't split agent "
            f"spend into tokens and floor. three steps:\n{SETUP_STEPS}"
        ),
        payload={"kind": NOTICE_KIND, "signs": signs},
    )
