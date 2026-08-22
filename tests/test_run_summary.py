"""`clont run --summary`: one cycle, then a summary file (the ad-hoc scan path).

Wiring only — `build_agent` is stubbed, so no cloud auth and no network.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from clont.api.uplink import Batch
from clont.cli import run as run_mod
from clont.cli.app import app
from clont.core.models import Cloud
from clont.events.models import Event, EventSeverity

runner = CliRunner()


def _event(severity: EventSeverity) -> Event:
    return Event(
        key=f"k-{severity.value}",
        severity=severity,
        domain="monitoring",
        cloud=Cloud.AWS,
        title="t",
        message="m",
    )


class _FakeAgent:
    def __init__(self, batch: Batch) -> None:
        self._batch = batch
        self.cycles = 0
        self.forever = 0

    def run_cycle(self, force: bool = False) -> tuple[Batch, int]:
        self.cycles += 1
        self.forced = force
        return self._batch, 0

    def run_once(self) -> int:
        self.cycles += 1
        return 0

    def run_forever(self) -> None:
        self.forever += 1


@pytest.fixture
def agent_for(tmp_path, monkeypatch):
    def _make(batch: Batch | None = None) -> _FakeAgent:
        agent = _FakeAgent(batch if batch is not None else Batch())
        monkeypatch.setattr(run_mod, "build_agent", lambda settings: agent)
        cfg = tmp_path / "clont.yaml"
        cfg.write_text(
            "interval_seconds: 300\n"
            "aws:\n"
            "  prod:\n"
            "    role_arn: arn:aws:iam::111111111111:role/clont-readonly\n"
        )
        monkeypatch.setenv("CLONT_CONFIG", str(cfg))
        return agent

    return _make


def test_summary_file_written_and_recap_echoed(agent_for, tmp_path):
    agent = agent_for(Batch(events=[_event(EventSeverity.INFO)]))
    out = tmp_path / "scan.json"

    result = runner.invoke(app, ["run", "--summary", str(out)])

    assert result.exit_code == 0, result.output
    assert agent.cycles == 1
    assert agent.forever == 0                       # --summary implies a single cycle
    data = json.loads(out.read_text())
    assert data["events"] == 1
    assert data["accounts"] == ["prod"]
    assert "1 account(s): 1 event(s) (0 critical)" in result.output


def test_txt_extension_gets_the_shareable_report(agent_for, tmp_path):
    agent_for()
    out = tmp_path / "scan.txt"

    result = runner.invoke(app, ["run", "--summary", str(out)])

    assert result.exit_code == 0, result.output
    assert "CLOUD WASTE REPORT" in out.read_text()


def test_unknown_extension_falls_back_to_text(agent_for, tmp_path):
    agent_for()
    out = tmp_path / "scan.log"

    result = runner.invoke(app, ["run", "--summary", str(out)])

    assert result.exit_code == 0, result.output
    assert out.read_text().startswith("clont scan summary")


def test_stdout_stays_the_operator_dump(agent_for):
    agent_for()

    result = runner.invoke(app, ["run", "--summary", "-"])

    assert result.exit_code == 0, result.output
    assert result.output.startswith("clont scan summary")


def test_report_format_can_be_asked_for_explicitly(agent_for, tmp_path):
    agent_for()
    out = tmp_path / "scan.json"

    result = runner.invoke(app, ["run", "--summary", str(out), "--format", "report"])

    assert result.exit_code == 0, result.output
    assert "CLOUD WASTE REPORT" in out.read_text()


def test_explicit_format_overrides_extension(agent_for, tmp_path):
    agent_for()
    out = tmp_path / "scan.txt"

    result = runner.invoke(app, ["run", "--summary", str(out), "--format", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text())["version"]


def test_unknown_format_rejected_before_collecting(agent_for, tmp_path):
    agent = agent_for()

    result = runner.invoke(app, ["run", "--summary", str(tmp_path / "s.json"), "--format", "yaml"])

    assert result.exit_code != 0
    assert agent.cycles == 0


def test_dash_writes_to_stdout_and_no_file(agent_for, tmp_path):
    agent_for()

    result = runner.invoke(app, ["run", "--summary", "-"])

    assert result.exit_code == 0, result.output
    assert "clont scan summary" in result.output
    assert list(tmp_path.glob("*.json")) == []


def test_parent_directories_created(agent_for, tmp_path):
    agent_for()
    out = tmp_path / "nested" / "dir" / "scan.json"

    result = runner.invoke(app, ["run", "--summary", str(out)])

    assert result.exit_code == 0, result.output
    assert out.exists()
    assert list(out.parent.glob(".*.tmp")) == []     # temp file cleaned up by os.replace


def test_fail_on_critical_exits_2(agent_for, tmp_path):
    agent_for(Batch(events=[_event(EventSeverity.CRITICAL)]))
    out = tmp_path / "scan.json"

    result = runner.invoke(app, ["run", "--summary", str(out), "--fail-on-critical"])

    assert result.exit_code == 2
    assert out.exists()                              # summary still written


def test_fail_on_critical_exits_0_without_criticals(agent_for, tmp_path):
    agent_for(Batch(events=[_event(EventSeverity.WARN)]))

    result = runner.invoke(
        app, ["run", "--summary", str(tmp_path / "scan.json"), "--fail-on-critical"]
    )

    assert result.exit_code == 0, result.output


def test_collector_errors_surface_in_summary(agent_for, tmp_path):
    agent_for(Batch(errors=["finops CostExplorerCollector collect failed: AccessDenied"]))
    out = tmp_path / "scan.json"

    result = runner.invoke(app, ["run", "--summary", str(out)])

    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text())["errors"] == [
        "finops CostExplorerCollector collect failed: AccessDenied"
    ]
