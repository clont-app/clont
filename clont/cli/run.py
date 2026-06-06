"""`clont run` - start the agent."""

from __future__ import annotations
import os
import typer

from clont.agent.bootstrap import build_agent
from clont.core import config as cfg
from clont.core.logging import configure, resolve_level, set_level


def run(
    config: str = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to config.yaml (overrides $CLONT_CONFIG; default ./clont.yaml).",
        envvar="CLONT_CONFIG",
    ),
    once: bool = typer.Option(
        False, "--once", help="Run a single cycle and exit (for cron/testing)."
    ),
) -> None:
    """Collect from read-only cloud providers, detect events, dispatch to channels."""
    configure()
    if config:
        os.environ["CLONT_CONFIG"] = config
    cfg.ensure()                      # write a default config on first run
    settings = cfg.load()
    set_level(resolve_level(settings.log_level))
    agent = build_agent(settings)
    if once:
        agent.run_once()
    else:
        agent.run_forever()
