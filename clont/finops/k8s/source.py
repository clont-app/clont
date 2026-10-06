"""One cluster joined to the provider that prices it.

`client.py` reads nodes and pods, `mapping.py` places the nodes on priced iron,
`namespaces.py` divides that iron's cost, `workloads.py` sizes the pod templates against
measured usage; this is the piece that knows *what to place them on* — and it is the only
place in the k8s source that touches a provider. The rule it enforces is the plan's: a
cluster is priced through a pool that already exists, so `priced_by` names an on-prem site
(its vms) or an aws account (its instances), and nothing else can price a node.

**The read is cached on the result, not the connection**, the same way `OnPremProvider`
does it: the namespace split and the workload findings both want one node list per cycle,
and a cluster that is read twice an hour is two api calls for one set of numbers. Nodes,
pods, namespace labels and pod metrics share one session per refresh — four lists on one
connection, not four connections.

**The usage samples are accumulated here**, because this object is the only thing that
lives longer than a cycle. metrics-server answers with one instant reading, so the ring in
`UsageHistory` is fed exactly once per *refresh* — a cached pass must not push the same
sample twice and make a workload look like it has twice the history it has.

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
from clont.core.models import Cloud, CloudResource, Money
from clont.finops.base import FinOpsTuning
from clont.finops.k8s.mapping import (
    ClusterMapping,
    Priced,
    match,
    targets_from_instances,
    targets_from_site,
)
from clont.finops.k8s.namespaces import NamespaceShowback, split
from clont.finops.k8s.workloads import WorkloadFinding, WorkloadReport, advise
from clont.finops.models import CostRecord, Recommendation
from clont.providers.k8s.client import KubernetesNodes
from clont.providers.k8s.config import KubernetesCluster
from clont.providers.k8s.nodes import Node
from clont.providers.k8s.pods import Pod, WorkloadRef
from clont.providers.k8s.prometheus import Prometheus
from clont.providers.k8s.usage import (
    METRICS_SERVER,
    PROMETHEUS,
    PodUsage,
    UsageHistory,
    WorkloadUsage,
    fold,
)

log = get_logger("clont.finops.k8s")

PASS_TTL_SECONDS = 300


_SERVICE = "kubernetes"

# one cached pass: when it was taken, and the three things every report reads off it
_Pass = tuple[float, "ClusterRead", "ClusterMapping", dict["WorkloadRef", "WorkloadUsage"]]


@dataclass(frozen=True, slots=True)
class ClusterRead:
    """One pass over a cluster: everything the reports need, read on one connection."""

    nodes: list[Node] = field(default_factory=list)
    pods: list[Pod] = field(default_factory=list)
    pending_pods: int = 0                              # scheduled on nothing
    labels: dict[str, dict[str, str]] = field(default_factory=dict)  # namespace -> labels
    usage: list[PodUsage] = field(default_factory=list)  # per pod, as the source gives it


class KubernetesSource:
    """Nodes of one cluster, mapped onto the iron one provider already prices."""

    def __init__(
        self,
        name: str,
        config: KubernetesCluster,
        provider: Any,
        *,
        reader: Callable[[], ClusterRead] | None = None,
        tuning: FinOpsTuning | None = None,
        pass_ttl_seconds: float = PASS_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.config = config
        self.provider = provider
        self._reader = reader or self._read_cluster
        self._tuning = tuning or FinOpsTuning()
        self._ttl = pass_ttl_seconds
        self._clock = clock
        self._history = UsageHistory()
        self._last: _Pass | None = None

    def read(self, *, refresh: bool = False) -> ClusterRead:
        """The cluster as of this pass, reused within the ttl."""
        return self._pass(refresh=refresh)[0]

    def mapping(self, *, refresh: bool = False) -> ClusterMapping:
        """Which pool pays for each node, reused within the ttl."""
        return self._pass(refresh=refresh)[1]

    def usage(self, *, refresh: bool = False) -> dict[WorkloadRef, WorkloadUsage]:
        """Per-workload p95, for the workloads measured long enough to size."""
        return self._pass(refresh=refresh)[2]

    def namespaces(
        self, records: list[CostRecord], *, refresh: bool = False
    ) -> NamespaceShowback:
        """This cluster's spend split by namespace, out of records someone else emitted.

        The records are the cycle's own: this adds no spend of its own, it divides what the
        pricing provider already reported — so a namespace table can never make a site's
        total larger than the invoice behind it.
        """
        read, mapped, _ = self._pass(refresh=refresh)
        return split(
            mapped,
            read.pods,
            records,
            labels=read.labels,
            pending_pods=read.pending_pods,
        )

    def workloads(self, records: list[CostRecord], *, refresh: bool = False) -> WorkloadReport:
        """Pod templates against their measured p95, priced on the nodes they sit on."""
        read, mapped, usage = self._pass(refresh=refresh)
        return advise(
            mapped,
            read.pods,
            usage,
            records,
            tuning=self._tuning,
            source=self.config.usage,
        )

    def recommendations(self, records: list[CostRecord]) -> list[Recommendation]:
        """The sizing report as the same `Recommendation` every other collector emits.

        The alias a k8s finding carries is the **cluster**, not the site: an operator fixes
        a Deployment in a cluster, and which pool paid for it is the `region`. The cloud is
        the pricing provider's, because that is whose money this is.
        """
        report = self.workloads(records)
        # `targets()` has already rejected anything that is not aws or on prem, so by here
        # the provider's cloud is a real one
        cloud = self.provider.cloud
        return [self._rec(finding, cloud) for finding in report.findings]

    def _rec(self, finding: WorkloadFinding, cloud: Cloud) -> Recommendation:
        return Recommendation(
            cloud=str(cloud),
            service=_SERVICE,
            kind=finding.kind,
            resource=CloudResource(
                cloud=cloud,
                service=_SERVICE,
                resource_id=finding.ref,
                region=finding.region,
                alias=self.name,
            ),
            summary=finding.summary,
            estimated_savings=Money(amount=finding.monthly, currency=finding.currency),
            priced_region=finding.region,
            # capacity handed back to the pool is only money once a node can go, and the
            # summary says so — presenting it as a quote would be the dishonest half
            approximate=True,
        )

    def _pass(
        self, *, refresh: bool
    ) -> tuple[ClusterRead, ClusterMapping, dict[WorkloadRef, WorkloadUsage]]:
        if not refresh and self._last is not None and self._clock() - self._last[0] < self._ttl:
            return self._last[1], self._last[2], self._last[3]
        read = self._reader()
        mapped = match(
            self.name,
            read.nodes,
            self.targets(),
            by_name=self.config.match_by_name,
        )
        usage = self._usage_rows(read)
        self._last = (self._clock(), read, mapped, usage)
        return read, mapped, usage

    def _usage_rows(self, read: ClusterRead) -> dict[WorkloadRef, WorkloadUsage]:
        """The p95 per workload, from whichever source this cluster has.

        prometheus answers with the percentile already computed, so there is nothing to
        keep; metrics-server answers with *now*, so the ring is what makes it a p95 — and
        it is fed here, on a real refresh and nowhere else.
        """
        if self.config.usage == PROMETHEUS:
            return fold(
                read.pods,
                read.usage,
                source=PROMETHEUS,
                min_samples=self.config.usage_min_samples,
            )
        if self.config.usage != METRICS_SERVER:
            return {}
        self._history.observe(read.pods, read.usage)
        return self._history.rows(
            read.pods, min_samples=self.config.usage_min_samples
        )

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
            # the metrics api is on the same connection; prometheus is not, and is read
            # outside the session because it is not the cluster's api at all
            usage = session.usage() if self.config.usage == METRICS_SERVER else []
        if self.config.usage == PROMETHEUS:
            usage = self._prometheus().pod_usage()
        log.debug(
            "%s: %d node(s), %d pod(s), %d pending, %d usage row(s)",
            self.name,
            len(nodes),
            len(pods),
            pending,
            len(usage),
        )
        return ClusterRead(
            nodes=nodes, pods=pods, pending_pods=pending, labels=labels, usage=usage
        )

    def _prometheus(self) -> Prometheus:
        return Prometheus(
            self.config.prometheus_url or "",
            window_days=self.config.prometheus_window_days,
            step_minutes=self.config.prometheus_step_minutes,
            timeout_seconds=max(self.config.timeout_seconds, 60),
        )
