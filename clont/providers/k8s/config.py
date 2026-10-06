"""Where a cluster is, and which provider's pool pays for it.

    kubernetes:
      lab:
        kubeconfig: /etc/clont/kubeconfig      # or in_cluster: true
        context: lab-admin
        priced_by: dc1                         # an alias under `onprem:` or `aws:`
        usage: metrics-server                  # or prometheus / off
        prometheus_url: http://prom.mon:9090   # required by `usage: prometheus`

**`priced_by` is required, and that is the plan's "no k8s-only install path" written as
code.** clont's k8s number is a continuation of a pool it already priced — node vm ->
cluster -> namespace — so a cluster with no priced iron under it has nothing to divide, and
the right answer is to say so at config load rather than to invent a node price. That it
names a *configured* alias is checked in `core.config`, where both halves are visible.

`match_by_name` exists because the weakest match key is also the one an operator may know
to be wrong: on a fleet where vm names and node names drift, a name match is a
mis-attribution rather than a missing row, and a missing row is the honest failure.

**`usage` only buys sizing advice, never the price.** The namespace table runs on requests
alone, so a cluster with no metrics source is fully priced and simply gets no
`rightsize-workload` row. The two sources are not equivalent and the config says which one
answers: metrics-server gives one instant sample per pass and clont has to accumulate
`usage_min_samples` of them itself, prometheus answers with a real p95 over
`prometheus_window_days` in one query. Default is metrics-server because most clusters
already run it and the read is free.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from clont.providers.k8s.usage import METRICS_SERVER, PROMETHEUS, USAGE_OFF

USAGE_SOURCES = (METRICS_SERVER, PROMETHEUS, USAGE_OFF)


class KubernetesCluster(BaseModel):
    """One cluster to read nodes from. Auth is the kubeconfig's or the pod's, never ours."""

    model_config = ConfigDict(extra="forbid")  # reject unknown keys to catch yaml typos

    priced_by: str                              # provider alias whose pool prices the nodes
    kubeconfig: str | None = None               # default: the usual ~/.kube/config search
    context: str | None = None                  # default: the kubeconfig's current-context
    in_cluster: bool = False                    # a pod's own service account
    timeout_seconds: int = Field(default=30, gt=0, le=300)
    # the last-resort match key: node name == vm name. off means an unnamed node is
    # reported unmapped instead of attributed on a guess
    match_by_name: bool = True

    # where measured usage comes from, i.e. whether sizing advice is possible at all
    usage: str = METRICS_SERVER
    # metrics-server has no history, so this many passes must accumulate before a workload
    # is advised about — the same rule as a vm with too little vcenter history
    usage_min_samples: int = Field(default=24, ge=1)
    prometheus_url: str | None = None
    prometheus_window_days: int = Field(default=14, gt=0, le=90)
    # the subquery step: 14 days at 5 minutes is ~4k points per series, and the cpu query
    # is the expensive one. widen it on a big cluster rather than shortening the window
    prometheus_step_minutes: int = Field(default=5, gt=0, le=60)

    @model_validator(mode="after")
    def _one_auth_source(self) -> KubernetesCluster:
        if not self.priced_by.strip():
            raise ValueError("kubernetes.priced_by is empty: name the site or account that prices these nodes")
        if self.in_cluster and (self.kubeconfig or self.context):
            raise ValueError("in_cluster takes no kubeconfig/context — it reads the pod's service account")
        if self.usage not in USAGE_SOURCES:
            raise ValueError(f"kubernetes.usage must be one of {', '.join(USAGE_SOURCES)}")
        if self.usage == PROMETHEUS and not (self.prometheus_url or "").strip():
            raise ValueError("usage: prometheus needs prometheus_url — there is nothing to query")
        return self
