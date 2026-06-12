"""AWS finops collectors (one module per service).
"""

from __future__ import annotations
from clont.finops.aws import (  # noqa: F401 - imported for registration
    commitments,
    compute_optimizer,
    cost_explorer,
    idle,
    idle_elb,
    idle_nat,
    idle_rds,
    offhours,
    snapshots,
    tags,
    utilization,
    waste,
)
