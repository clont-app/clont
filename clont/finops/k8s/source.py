"""One cluster joined to the provider that prices it.

`client.py` reads nodes and pods, `mapping.py` places the nodes on priced iron,
`namespaces.py` divides that iron's cost; this is the piece that knows *what to place them
on* — and it is the only place in the k8s source that touches a provider. The rule it
enforces is the plan's: a cluster is priced through a pool that already exists, so
`priced_by` names an on-prem site (its vms) or an aws account (its instances), and nothing
else can price a node.

**The read is cached on the result, not the connection**, the same way `OnPremProvider`
does it: the namespace split and the workload findings both want one node list per cycle,
and a cluster that is read twice an hour is two api calls for one set of numbers. Nodes,
pods and namespace labels share one session per refresh — three lists on one connection,
not three connections.

A node read that fails is a failed cluster, not a failed site: the caller isolates it the
way bootstrap isolates one unreachable vcenter.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
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
from clont.finops.k8s.namespaces import NamespaceShowback, split
from clont.finops.models import CostRecord
from clont.providers.k8s.client import KubernetesNodes
from clont.providers.k8s.config import KubernetesCluster
from clont.providers.k8s.nodes import Node
from clont.providers.k8s.pods import Pod

log = get_logger("clont.finops.k8s")

PASS_TTL_SECONDS = 300


@dataclass(frozen=True, slots=True)
class ClusterRead:
    """One pass over a cluster: everything the reports need, read on one connection."""

    nodes: list[Node] = field(default_factory=list)
    pods: list[Pod] = field(default_factory=list)
    pending_pods: int = 0                              # scheduled on nothing
    labels: dict[str, dict[str, str]] = field(default_factory=dict)  # namespace -> labels


class KubernetesSource:
    """Nodes of one cluster, mapped onto the iron one provider already prices."""

    def __init__(
        self,
        name: str,
        config: KubernetesCluster,
        provider: Any,
        *,
        reader: Callable[[], ClusterRead] | None = None,
        pass_ttl_seconds: float = PASS_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.config = config
        self.provider = provider
        self._reader = reader or self._read_cluster
        self._ttl = pass_ttl_seconds
        self._clock = clock
        self._last: tuple[float, ClusterRead, ClusterMapping] | None = None

    def read(self, *, refresh: bool = False) -> ClusterRead:
        """The cluster as of this pass, reused within the ttl."""
        return self._pass(refresh=refresh)[0]

    def mapping(self, *, refresh: bool = False) -> ClusterMapping:
        """Which pool pays for each node, reused within the ttl."""
        return self._pass(refresh=refresh)[1]

    def namespaces(
        self, records: list[CostRecord], *, refresh: bool = False
    ) -> NamespaceShowback:
        """This cluster's spend split by namespace, out of records someone else emitted.

        The records are the cycle's own: this adds no spend of its own, it divides what the
        pricing provider already reported — so a namespace table can never make a site's
        total larger than the invoice behind it.
        """
        read, mapped = self._pass(refresh=refresh)
        return split(
            mapped,
            read.pods,
            records,
            labels=read.labels,
            pending_pods=read.pending_pods,
        )

    def _pass(self, *, refresh: bool) -> tuple[ClusterRead, ClusterMapping]:
        if not refresh and self._last is not None and self._clock() - self._last[0] < self._ttl:
            return self._last[1], self._last[2]
        read = self._reader()
        mapped = match(
            self.name,
            read.nodes,
            self.targets(),
            by_name=self.config.match_by_name,
        )
        self._last = (self._clock(), read, mapped)
        return read, mapped

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

    def _read_cluster(self) -> ClusterRead:
        with KubernetesNodes(
            kubeconfig=self.config.kubeconfig,
            context=self.config.context,
            in_cluster=self.config.in_cluster,
            timeout_seconds=self.config.timeout_seconds,
        ) as session:
            nodes = session.nodes()
            pods, pending = session.pods()
            labels = session.labels()
        log.debug(
            "%s: %d node(s), %d pod(s), %d pending", self.name, len(nodes), len(pods), pending
        )
        return ClusterRead(nodes=nodes, pods=pods, pending_pods=pending, labels=labels)
