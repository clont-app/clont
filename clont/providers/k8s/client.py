"""The cluster wire: one read, `list_node`, and nothing else.

The `kubernetes` client is an optional dependency (`pip install clont[k8s]`) — an aws-only
install has no business carrying it — so it is imported when a session opens and not at
module import. Same shape as `vsphere.py`.

**Only `list` on `nodes`.** That is the whole api surface of this file, which is what lets
clont run under a ClusterRole with `get,list` on `nodes` and nothing else. The claim is a
test, not a sentence in a readme: no other api object is reachable from here.

Two things that are easy to get wrong and are decided here:

* **nodes are paged.** `list_node` answers 500 at a time on a big cluster and hands back a
  `continue` token; a reader that ignores it silently prices a slice of the fleet. The loop
  stops at `MAX_PAGES` rather than trusting the server to terminate it.
* **the raw json is read, not the generated models** (`_preload_content=False`). The typed
  client *validates* a node on deserialize and raises when a required field is missing —
  `architecture`, `bootID`, `machineID`, none of which clont reads — so one unusual node
  would take the whole pass down for a value we never look at. The json is what `nodes.py`
  parses anyway, with `.get()`, where an absent field is simply absent.
"""

from __future__ import annotations

import json
from typing import Any

from clont.core.errors import ConfigError
from clont.core.logging import get_logger
from clont.providers.k8s.nodes import Node, build_nodes

log = get_logger("clont.providers.k8s")

PAGE_SIZE = 500
MAX_PAGES = 50  # 25k nodes; past that something is wrong with the token, not the cluster
DEFAULT_TIMEOUT = 30


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

    def nodes(self) -> list[Node]:
        """Every node in the cluster, paged, as plain `Node`s."""
        if self._api is None:
            raise ConfigError("kubernetes session is not connected")
        items: list[dict] = []
        token: str | None = None
        for _ in range(MAX_PAGES):
            page = self._api.list_node(
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
            log.warning("%s: stopped paging nodes after %d pages", self.endpoint, MAX_PAGES)
        return build_nodes(items)


def _import_kubernetes() -> tuple[Any, Any, type[Exception]]:
    try:
        from kubernetes import client, config
        from kubernetes.config.config_exception import ConfigException
    except ImportError as exc:  # pragma: no cover - depends on the install extra
        raise ConfigError(
            "the kubernetes source needs the client: pip install 'clont[k8s]'"
        ) from exc
    return client, config, ConfigException
