"""`clont run` - start the agent (or run one cycle and summarize it)."""

from __future__ import annotations
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import typer

from clont.agent.bootstrap import build_agent
from clont.core import config as cfg
from clont.core.logging import configure, resolve_level, set_level
from clont.reporting.summary import SummaryFormat, render, summarize


def _resolve_format(fmt: str | None, output: str) -> SummaryFormat:
    """Explicit `--format` wins; otherwise infer from the output extension."""
    if fmt:
        try:
            return SummaryFormat(fmt.lower())
        except ValueError as exc:
            raise typer.BadParameter(f"unknown format {fmt!r} (json|text)") from exc
    if output != "-" and Path(output).suffix.lower() == ".json":
        return SummaryFormat.JSON
    return SummaryFormat.TEXT


def _write(output: str, text: str) -> None:
    """Write to `output` ('-' is stdout), atomically so a failure mid-write
    never leaves a half-written summary behind."""
    if output == "-":
        typer.echo(text, nl=False)
        return
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


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
    summary: str = typer.Option(
        None,
        "--summary",
        "-s",
        help="Run one cycle and write a summary of what it saw to this path "
        "('-' for stdout). Implies --once.",
    ),
    fmt: str = typer.Option(
        None,
        "--format",
        help="Summary format: json or text (default: from the output extension).",
    ),
    fail_on_critical: bool = typer.Option(
        False,
        "--fail-on-critical",
        help="With --summary, exit 2 if the cycle produced a critical event.",
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

    if summary:
        summary_format = _resolve_format(fmt, summary)   # validate before collecting
        started = time.monotonic()
        generated_at = datetime.now(UTC)
        batch, _delivered = agent.run_cycle()
        report = summarize(
            batch,
            accounts=list(settings.aws),
            duration_seconds=time.monotonic() - started,
            generated_at=generated_at,
        )
        _write(summary, render(report, summary_format))
        if summary != "-":
            typer.echo(
                f"scanned {len(report.accounts)} account(s): {report.events} event(s) "
                f"({report.critical_events} critical) -> {summary}"
            )
        if fail_on_critical and report.critical_events:
            raise typer.Exit(code=2)
        return

    if once:
        agent.run_once()
    else:
        agent.run_forever()
