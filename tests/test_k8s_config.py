"""The `kubernetes:` block, and the one rule that keeps k8s priced through a pool."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from clont.core.config import Config
from clont.providers.k8s.config import KubernetesCluster

_YAML = """\
onprem:
  dc1:
    rate_card: {hardware_amortization: 10000}
kubernetes:
  lab:
    priced_by: dc1
    kubeconfig: /etc/clont/kubeconfig
    context: lab-admin
"""


def _load(tmp_path, monkeypatch, yaml: str) -> Config:
    cfg_file = tmp_path / "clont.yaml"
    cfg_file.write_text(yaml)
    monkeypatch.setenv("CLONT_CONFIG", str(cfg_file))
    return Config()


def test_a_cluster_loads_against_the_site_that_prices_it(tmp_path, monkeypatch):
    config = _load(tmp_path, monkeypatch, _YAML)
    assert config.kubernetes["lab"].priced_by == "dc1"
    assert config.kubernetes["lab"].context == "lab-admin"
    assert config.kubernetes["lab"].match_by_name is True


def test_priced_by_must_name_a_configured_provider(tmp_path, monkeypatch):
    # "no k8s-only install path" as a load-time error: a cluster with no priced iron under
    # it has nothing to divide, and a typo would otherwise read as a broken node match
    with pytest.raises(ValidationError, match="names no provider"):
        _load(tmp_path, monkeypatch, "kubernetes:\n  lab:\n    priced_by: dc1\n")


def test_in_cluster_takes_no_kubeconfig():
    with pytest.raises(ValidationError, match="service account"):
        KubernetesCluster(priced_by="dc1", in_cluster=True, kubeconfig="/tmp/kubeconfig")
    assert KubernetesCluster(priced_by="dc1", in_cluster=True).kubeconfig is None


def test_a_mistyped_key_is_rejected():
    with pytest.raises(ValidationError):
        KubernetesCluster(priced_by="dc1", kube_config="/tmp/kubeconfig")
