"""One cluster joined to the provider that prices it.

`client.py` reads nodes, `mapping.py` places them; this is the piece that knows *what to
place them on* — and it is the only place in the k8s source that touches a provider. The
rule it enforces is the plan's: a cluster is priced through a pool that already exists, so
`priced_by` names an on-prem site (its vms) or an aws account (its instances), and nothing
else can price a node.

**The read is cached on the result, not the connection**, the same way `OnPremProvider`
does it: the namespace split and the workload findings both want one node list per cycle,
and a cluster that is read twice an hour is two api calls for one set of numbers.

A node read that fails is a failed cluster, not a failed site: the caller isolates it the
way bootstrap isolates one unreachable vcenter.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from clont.core.errors import ConfigError
from clont.core.logging import get_logger
from clont.core.models import Cloud
from clont.finops.k8s.mapping import (
    ClusterMapping,
    Priced,
    match,
    targets_from_instances,
    targets_from_site,
)
from clont.providers.k8s.client import KubernetesNodes
from clont.providers.k8s.config import KubernetesCluster
from clont.providers.k8s.nodes import Node

log = get_logger("clont.finops.k8s")

PASS_TTL_SECONDS = 300


class KubernetesSource:
    """Nodes of one cluster, mapped onto the iron one provider already prices."""

    def __init__(
        self,
        name: str,
        config: KubernetesCluster,
        provider: Any,
        *,
        reader: Callable[[], list[Node]] | None = None,
        pass_ttl_seconds: float = PASS_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.config = config
        self.provider = provider
        self._reader = reader or self._read_cluster
        self._ttl = pass_ttl_seconds
        self._clock = clock
        self._last: tuple[float, ClusterMapping] | None = None

    def mapping(self, *, refresh: bool = False) -> ClusterMapping:
        """Which pool pays for each node, reused within the ttl."""
        if not refresh and self._last is not None and self._clock() - self._last[0] < self._ttl:
            return self._last[1]
        result = match(
            self.name,
            self._reader(),
            self.targets(),
            by_name=self.config.match_by_name,
        )
        self._last = (self._clock(), result)
        return result

    def targets(self) -> list[Priced]:
        """Everything the priced provider holds that a node could be running on."""
        cloud = getattr(self.provider, "cloud", None)
        alias = getattr(self.provider, "alias", self.config.priced_by)
        if cloud is Cloud.ONPREM:
            return targets_from_site(alias, self.provider.inventory())
        if cloud is Cloud.AWS:
            # imported here: the aws sweep is boto3 work, and an on-prem-only run of this
            # module should not pay for importing it
            from clont.finops.aws import inventory as aws_inventory

            return targets_from_instances(alias, aws_inventory.build(self.provider).running)
        raise ConfigError(
            f"kubernetes {self.name}: priced_by {self.config.priced_by!r} is a "
            f"{cloud or 'unknown'} provider, which cannot price nodes"
        )

    def preflight(self) -> list[str]:
        """What stops this cluster from being priced (empty == it is ready).

        A cluster that answers with nodes and maps none of them is the interesting failure:
        the kubeconfig is fine, the pool is fine, and the two do not refer to the same iron
        — usually `priced_by` pointing at the wrong site.
        """
        result = self.mapping(refresh=True)
        if not result.nodes:
            return [f"{self.name}: the cluster reports no nodes"]
        if not result.matched:
            return [
                f"{self.name}: none of {result.nodes} node(s) is on anything "
                f"{self.config.priced_by} prices — check priced_by"
            ]
        return []

    def _read_cluster(self) -> list[Node]:
        with KubernetesNodes(
            kubeconfig=self.config.kubeconfig,
            context=self.config.context,
            in_cluster=self.config.in_cluster,
            timeout_seconds=self.config.timeout_seconds,
        ) as session:
            nodes = session.nodes()
        log.debug("%s: %d node(s)", self.name, len(nodes))
        return nodes
