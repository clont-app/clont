"""AWS monitoring collectors (one module per service).

Importing this package registers every implemented collector via the
`@register` decorator (an import side-effect) so the agent can discover them.
"""

from __future__ import annotations

from clont.monitoring.aws import (
    acm,
    autoscaling,
    ebs,
    ec2,
    ecs,
    eks,
    elasticache,
    elb,
    health,
    rds,
    redshift,
)
