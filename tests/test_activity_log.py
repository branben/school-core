"""Behavioral tests for the School Core deviation timeline."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from activity_log import ActivityLog, ActivityType


def test_record_deviation_persists_structured_learning(tmp_path):
    log = ActivityLog(tmp_path / "activity_log.json")

    entry = log.record_deviation(
        issue="school-core-s6k",
        agent="student-coder",
        summary="Sibling lease uses byte budget, not count cap",
        plan_expected="Inherit the global count cap",
        code_revealed="Production has the count cap disabled",
        decision="Hold the byte-budget lease independently",
        revisit="Revisit if count-cap admission is re-enabled",
        evidence=["crew_dispatch.py:123"],
    )

    assert entry["type"] == ActivityType.DEVIATION.value
    assert entry["issue"] == "school-core-s6k"
    assert entry["plan_expected"] == "Inherit the global count cap"
    assert entry["code_revealed"] == "Production has the count cap disabled"
    assert entry["decision"] == "Hold the byte-budget lease independently"
    assert entry["revisit"] == "Revisit if count-cap admission is re-enabled"
    assert entry["evidence"] == ["crew_dispatch.py:123"]

    saved = json.loads((tmp_path / "activity_log.json").read_text())
    assert saved["entries"] == [entry]


@pytest.mark.parametrize(
    "field",
    ["summary", "plan_expected", "code_revealed", "decision", "revisit"],
)
def test_record_deviation_rejects_missing_learning_fields(tmp_path, field):
    log = ActivityLog(tmp_path / "activity_log.json")
    values = {
        "issue": "school-core-s6k",
        "agent": "student-coder",
        "summary": "A useful summary",
        "plan_expected": "Expected behavior",
        "code_revealed": "Observed behavior",
        "decision": "Chosen behavior",
        "revisit": "Future condition",
    }
    values[field] = "  "

    with pytest.raises(ValueError, match=field):
        log.record_deviation(**values)


def test_timeline_filters_by_issue_and_event_kind(tmp_path):
    log = ActivityLog(tmp_path / "activity_log.json")
    log.start_task("coder", "python-testing", "easy")
    log.record_deviation(
        issue="school-core-s6k",
        agent="coder",
        summary="The verifier is read-only",
        plan_expected="Run the full test suite",
        code_revealed="The verify shell has no pytest",
        decision="Use the declared compile check",
        revisit="Revisit when pytest is provisioned",
    )
    log.record_deviation(
        issue="other",
        agent="browser",
        summary="Runner is offline",
        plan_expected="Run live integration",
        code_revealed="The self-hosted runner is absent",
        decision="Skip live integration and report blocked",
        revisit="Revisit when the runner returns",
    )

    assert len(log.timeline(issue="school-core-s6k")) == 1
    assert [e["type"] for e in log.timeline(kind=ActivityType.DEVIATION.value)] == [
        ActivityType.DEVIATION.value,
        ActivityType.DEVIATION.value,
    ]
    assert len(log.timeline(kind=ActivityType.TASK_START.value)) == 1


def test_record_deviation_cli_can_target_an_isolated_log(tmp_path):
    log_path = tmp_path / "activity_log.json"
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "scripts" / "record_deviation.py"),
            "--log-path", str(log_path),
            "--issue", "cli-test",
            "--agent", "cli-agent",
            "--summary", "CLI smoke",
            "--plan-expected", "Expected",
            "--code-revealed", "Observed",
            "--decision", "Keep local",
            "--revisit", "When changed",
            "--evidence", "cli.py:1",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout)["type"] == "deviation"
    assert json.loads(log_path.read_text())["entries"][0]["issue"] == "cli-test"
