"""Agent spend buckets read off `lineItem/UsageType`.

The bill side of an ai agent: tokens are the smallest part, the rest is fixed
floors (an opensearch collection, an endpoint, a nat) and per-call plumbing
(lambda, step functions, logs). Classifying it here keeps cur dumb, same as
`usage_types` does for transfer: each row gets a bucket and the report groups
by that.

No prices, ever. The money is the cur row; this only says which kind of money
it is. Matching is on usage-type suffix shapes, never on model names, so a model
nobody has heard of still lands in the right bucket.

The ai services are agent spend on every row. The shared ones (lambda, step
functions, cloudwatch, vpc/nat) only count when the row carries an agent tag —
otherwise every lambda in the account would join the agent's bill.
"""

from __future__ import annotations

import re

DIMENSION = "agent"

# ocu as its own word: kendra's DocumentsScanned spells it too
_OCU = re.compile(r"ocu(?![a-z])")

TOKENS_IN = "tokens-in"
TOKENS_OUT = "tokens-out"
CACHE_READ = "cache-read"
CACHE_WRITE = "cache-write"
FLOOR = "floor"
PLUMBING = "plumbing"
INDEX = "index"


def _norm(text: str) -> str:
    # so cache-read-input-token-count and CacheReadInputTokenCount compare equal
    return "".join(c for c in text.lower() if c.isalnum())


def _hourly(u: str) -> bool:
    # bytehrs is storage, not a running thing
    return ("hour" in u or "hrs" in u) and "bytehrs" not in u


def _ai_service(s: str) -> str:
    if "bedrock" in s:  # also marketplace "claude ... (amazon bedrock edition)"
        return "bedrock"
    if "sagemaker" in s:
        return "sagemaker"
    if "opensearch" in s or s == "amazones":
        return "opensearch"
    if "kendra" in s:
        return "kendra"
    return ""


def _shared_service(s: str) -> str:
    if "lambda" in s:
        return "lambda"
    if "stepfunctions" in s or s == "amazonstates":
        return "sfn"
    if "cloudwatch" in s:
        return "cloudwatch"
    if "virtualprivatecloud" in s or s == "amazonvpc":
        return "vpc"
    return ""


def _tokens(u: str) -> str:
    if "cacheread" in u:
        return CACHE_READ
    if "cachewrite" in u:
        return CACHE_WRITE
    if "inputtoken" in u:
        return TOKENS_IN
    if "outputtoken" in u:
        return TOKENS_OUT
    return ""


def _ai_bucket(kind: str, raw: str, u: str) -> str:
    tokens = _tokens(u)
    if tokens:
        return tokens
    if "indexingocu" in u:
        return INDEX
    if _OCU.search(raw) or "provisioned" in u or "modelunit" in u:
        return FLOOR
    if kind in ("opensearch", "kendra") and (
        "storage" in u or "bytehrs" in u or "gbmo" in u
    ):
        return INDEX
    if "dataprocessing" in u or "serverless" in u or "request" in u or "query" in u:
        return PLUMBING
    # an instance shape (ml.m5.large, r6g.large.search) or a host volume is paid
    # by the hour whether anyone calls it or not
    if "ml." in raw or "instance" in u or "host" in u or _hourly(u):
        return FLOOR
    return PLUMBING


def _shared_bucket(kind: str, u: str) -> str:
    if kind == "lambda" and "provisioned" in u:
        return FLOOR
    if kind == "vpc" and _hourly(u):
        return FLOOR
    return PLUMBING


def agent_bucket(service: str, usage_type: str, tagged: bool = False) -> str:
    """Bucket for one cur row, "" when the row isn't agent spend.

    `tagged` means the row carries a value for one of the agent tag keys; only
    then do the shared services count.
    """
    raw = usage_type.strip().lower()
    if not raw:
        return ""
    u = _norm(raw)
    s = _norm(service)
    ai = _ai_service(s)
    if ai:
        return _ai_bucket(ai, raw, u)
    if not tagged:
        return ""
    # nat and endpoint hours sit under ec2 in cur, so match them on the usage type
    if "natgateway" in u or "vpcendpoint" in u:
        return FLOOR if _hourly(u) else PLUMBING
    shared = _shared_service(s)
    if shared:
        return _shared_bucket(shared, u)
    return ""
