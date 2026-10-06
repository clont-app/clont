"""Where a cluster is, and which provider's pool pays for it.

    kubernetes:
      lab:
        kubeconfig: /etc/clont/kubeconfig      # or in_cluster: true
        context: lab-admin
        priced_by: dc1                         # an alias under `onprem:` or `aws:`

**`priced_by` is required, and that is the plan's "no k8s-only install path" written as
code.** clont's k8s number is a continuation of a pool it already priced — node vm ->
cluster -> namespace — so a cluster with no priced iron under it has nothing to divide, and
the right answer is to say so at config load rather than to invent a node price. That it
names a *configured* alias is checked in `core.config`, where both halves are visible.

`match_by_name` exists because the weakest match key is also the one an operator may know
to be wrong: on a fleet where vm names and node names drift, a name match is a
mis-attribution rather than a missing row, and a missing row is the honest failure.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


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

    @model_validator(mode="after")
    def _one_auth_source(self) -> KubernetesCluster:
        if not self.priced_by.strip():
            raise ValueError("kubernetes.priced_by is empty: name the site or account that prices these nodes")
        if self.in_cluster and (self.kubeconfig or self.context):
            raise ValueError("in_cluster takes no kubeconfig/context — it reads the pod's service account")
        return self
