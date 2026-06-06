"""Root CLI — the control plane for the daemon.

clont is a long-running agent; the CLI exists only to start it and to validate
config. The container ENTRYPOINT is `clont run`.

    clont run                 # start the agent loop (the daemon)
    clont config check        # validate config / verify read-only access
"""

from __future__ import annotations

import typer

from clont.cli import config as config_cmd
from clont.cli.run import run as run_cmd

app = typer.Typer(
    name="clont",
    help="Multi-cloud monitoring & FinOps agent (read-only daemon).",
    no_args_is_help=True,
)

# The agent loop is the primary entry point (container ENTRYPOINT: `clont run`).
app.command(name="run")(run_cmd)

# Config validation - useful in CI or a k8s init container.
app.add_typer(config_cmd.app, name="config")
