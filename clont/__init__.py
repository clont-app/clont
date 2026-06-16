"""clont — a long-running, strictly read-only monitoring and FinOps agent.

Collects metrics, cost data, and recommendations from cloud accounts (AWS today)
and emits health/FinOps events to local channels. Every cloud call is a
Describe/Get/List — the agent never mutates cloud state.
"""

__version__ = "0.1.0"

