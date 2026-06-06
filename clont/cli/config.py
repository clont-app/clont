"""`clont config ...` commands: validate config and verify RO access."""

from __future__ import annotations
import os
import typer

from clont.core import config as cfg

app = typer.Typer(help="Configuration and access checks.", no_args_is_help=True)


@app.command()
def check(
    config: str = typer.Option(
        None, "--config", "-c", envvar="CLONT_CONFIG", help="Path to config.yaml."
    ),
) -> None:
    """Load and validate the configuration file."""
    if config:
        os.environ["CLONT_CONFIG"] = config
    try:
        c = cfg.load()
    except Exception as exc:  # noqa: BLE001 - surface validation errors to the user
        typer.echo(f"config invalid: {exc}")
        raise typer.Exit(code=1) from exc

    enabled = [
        name
        for name, val in (
            ("slack", c.channels.slack),
            ("discord", c.channels.discord),
            ("telegram", c.channels.telegram),
        )
        if val is not None
    ]
    accounts = ", ".join(c.aws) if c.aws else "none"
    typer.echo(
        f"OK: {len(c.aws)} AWS account(s) [{accounts}]; "
        f"channels: log{''.join('+' + n for n in enabled)}; "
        f"interval={c.interval_seconds}s; log_level={c.log_level}"
    )
