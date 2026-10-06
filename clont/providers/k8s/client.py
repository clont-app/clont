"""The cluster wire: six list calls, and nothing else.

The `kubernetes` client is an optional dependency (`pip install clont[k8s]`) — an aws-only
install has no business carrying it — so it is imported when a session opens and not at
module import. Same shape as `vsphere.py`.

**Only `list`, only on `nodes`, `pods`, `namespaces`, `persistentvolumeclaims`,
`persistentvolumes` and `pods.metrics.k8s.io`.** That is the whole api surface of this
file, which is what lets clont run under a ClusterRole with `get,list` on those six and
nothing else. No write verb, no secrets, no exec, no logs.

The last four are optional, and a cluster that refuses them is still fully priced — only
the extra findings go quiet. Namespaces are read for their *labels*, so a showback
table can group by `team` the way the aws one groups by a cost-allocation tag. A role
without them still prices every namespace — `labels()` answers empty and the table groups
by name — so the read is optional on purpose and a 403 there is not a failed pass. Claims
and volumes are the same: without them the volume findings are simply absent and the
on-prem storage gap stays unreconciled, which is better than a cluster that cannot be
priced because a role was tight.

Two things that are easy to get wrong and are decided here:

* **every list is paged.** `list_node` answers 500 at a time on a big cluster and hands
  back a `continue` token; a reader that ignores it silently prices a slice of the fleet.
  Pods are where this actually bites — a 200-node cluster has thousands. The loop stops at
  `MAX_PAGES` rather than trusting the server to terminate it.
* **the raw json is read, not the generated models** (`_preload_content=False`). The typed
  client *validates* a node on deserialize and raises when a required field is missing —
  `architecture`, `bootID`, `machineID`, none of which clont reads — so one unusual node
  would take the whole pass down for a value we never look at. The json is what `nodes.py`
  parses anyway, with `.get()`, where an absent field is simply absent.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from clont.core.errors import ConfigError
from clont.core.logging import get_logger
from clont.providers.k8s.nodes import Node, build_nodes
from clont.providers.k8s.pods import Pod, build_pods
from clont.providers.k8s.pvs import Volume, build_volumes
from clont.providers.k8s.usage import PodUsage, build_usage
from clont.providers.k8s.volumes import Claim, build_claims

log = get_logger("clont.providers.k8s")

PAGE_SIZE = 500
MAX_PAGES = 50  # 25k nodes; past that something is wrong with the token, not the cluster
DEFAULT_TIMEOUT = 30

METRICS_GROUP = "metrics.k8s.io"
METRICS_VERSION = "v1beta1"


class KubernetesNodes:
    """A read-only node reader for one cluster.

    Use it as a context manager: the api client holds a connection pool, and a daemon that
    leaks one per cycle runs out of sockets long before it runs out of clusters.
    """

    def __init__(
        self,
        *,
        kubeconfig: str | None = None,
        context: str | None = None,
        in_cluster: bool = False,
        timeout_seconds: int = DEFAULT_TIMEOUT,
    ) -> None:
        self._kubeconfig = kubeconfig
        self._context = context
        self._in_cluster = in_cluster
        self._timeout = timeout_seconds
        self._api: Any = None
        self._custom: Any = None
        self._client: Any = None
        # the apiserver this session talks to, filled on connect — for the log line
        self.endpoint: str | None = None

    def __enter__(self) -> KubernetesNodes:
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def connect(self) -> None:
        k8s_client, k8s_config, config_exception = _import_kubernetes()
        try:
            if self._in_cluster:
                k8s_config.load_incluster_config()
            else:
                k8s_config.load_kube_config(
                    config_file=self._kubeconfig, context=self._context
                )
        except config_exception as exc:
            where = "in-cluster service account" if self._in_cluster else (
                self._kubeconfig or "the default kubeconfig"
            )
            raise ConfigError(f"cannot load kubernetes config from {where}: {exc}") from None
        self._client = k8s_client.ApiClient()
        self._api = k8s_client.CoreV1Api(self._client)
        self._custom = k8s_client.CustomObjectsApi(self._client)
        self.endpoint = self._client.configuration.host
        log.debug("connected to %s", self.endpoint)

    def close(self) -> None:
        if self._client is None:
            return
        try:
            self._client.close()
        finally:
            self._client = None
            self._api = None
            self._custom = None

    def nodes(self) -> list[Node]:
        """Every node in the cluster, paged, as plain `Node`s."""
        return build_nodes(self._list("nodes", lambda: self._api.list_node))

    def pods(self) -> tuple[list[Pod], list[Pod]]:
        """Every live pod with its requests: the scheduled ones, then the ones waiting."""
        return build_pods(
            self._list("pods", lambda: self._api.list_pod_for_all_namespaces)
        )

    def claims(self) -> list[Claim]:
        """Every pvc in the cluster, or empty when the role cannot list them.

        Optional, like `labels()`: a missing read costs the volume findings and nothing
        else, and dying here would cost the whole split for them.
        """
        try:
            items = self._list(
                "persistentvolumeclaims",
                lambda: self._api.list_persistent_volume_claim_for_all_namespaces,
            )
        except Exception as exc:  # noqa: BLE001 - optional read, see the module docstring
            log.info("%s: persistent volume claims unavailable: %s", self.endpoint, exc)
            return []
        return build_claims(items)

    def volumes(self) -> list[Volume]:
        """Every pv, or empty when the role cannot list them.

        Optional like `claims()`, and it costs a little more when it is missing: without
        the pv there is no way to tell a volume on a datastore from one inside a node's own
        disk, so the on-prem gap stays as large as it was.
        """
        try:
            items = self._list(
                "persistentvolumes", lambda: self._api.list_persistent_volume
            )
        except Exception as exc:  # noqa: BLE001 - optional read, see the module docstring
            log.info("%s: persistent volumes unavailable: %s", self.endpoint, exc)
            return []
        return build_volumes(items)

    def usage(self) -> list[PodUsage]:
        """metrics-server's per-pod readings, or empty when there is nothing to read.

        Optional the same way `labels()` is: a cluster without metrics-server answers 404
        and a role without `metrics.k8s.io` answers 403, and neither is a failed pass — it
        means the sizing half has no evidence and will say so instead of advising.

        **Not paged.** metrics-server serves this list out of memory and implements no
        `continue` token, so a token loop would be a second identical read of the whole
        cluster. The 500-item limit the other reads pass would silently truncate it.
        """
        if self._custom is None:
            raise ConfigError("kubernetes session is not connected")
        try:
            page = self._custom.list_cluster_custom_object(
                METRICS_GROUP,
                METRICS_VERSION,
                "pods",
                _request_timeout=self._timeout,
                _preload_content=False,
            )
            try:
                raw = json.loads(page.data)
            finally:
                page.release_conn()
        except Exception as exc:  # noqa: BLE001 - optional read, see the docstring
            log.info("%s: pod metrics unavailable: %s", self.endpoint, exc)
            return []
        return build_usage(raw.get("items") or [])

    def labels(self) -> dict[str, dict[str, str]]:
        """Namespace -> its labels, or empty when the role cannot list namespaces.

        A missing read is not a failure: the table groups by namespace name and simply has
        no `team` column. Dying here would cost the whole split for a grouping nobody may
        have asked for.
        """
        try:
            items = self._list("namespaces", lambda: self._api.list_namespace)
        except Exception as exc:  # noqa: BLE001 - optional read, see the module docstring
            log.info("%s: namespace labels unavailable: %s", self.endpoint, exc)
            return {}
        out: dict[str, dict[str, str]] = {}
        for item in items:
            meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            name = str((meta or {}).get("name") or "").strip()
            raw = (meta or {}).get("labels")
            if name:
                out[name] = (
                    {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
                )
        return out

    def _list(self, what: str, call: Callable[[], Any]) -> list[dict]:
        """One paged list, as the api's own json.

        `call` hands back the api method instead of being it — the api is None until
        `connect()`, and binding the method at the call site would raise before the check.
        """
        if self._api is None:
            raise ConfigError("kubernetes session is not connected")
        items: list[dict] = []
        token: str | None = None
        for _ in range(MAX_PAGES):
            page = call()(
                limit=PAGE_SIZE,
                _continue=token,
                _request_timeout=self._timeout,
                _preload_content=False,
            )
            try:
                raw = json.loads(page.data)
            finally:
                page.release_conn()
            items.extend(raw.get("items") or [])
            token = (raw.get("metadata") or {}).get("continue") or None
            if not token:
                break
        else:
            log.warning("%s: stopped paging %s after %d pages", self.endpoint, what, MAX_PAGES)
        return items


def _import_kubernetes() -> tuple[Any, Any, type[Exception]]:
    try:
        from kubernetes import client, config
        from kubernetes.config.config_exception import ConfigException
    except ImportError as exc:  # pragma: no cover - depends on the install extra
        raise ConfigError(
            "the kubernetes source needs the client: pip install 'clont[k8s]'"
        ) from exc
    return client, config, ConfigException
