import json

from context_orchestrator import enrich_prompt
from consolidation_writer import write_consolidation
from trajectory import capture_trajectory


def test_new_cycle_receives_layer2_and_layer3_context(tmp_path, monkeypatch):
    """A fresh session can use the previous cycle's trajectory and archive."""
    trajectory_dir = tmp_path / "trajectories"
    consolidation_dir = tmp_path / "consolidation"
    monkeypatch.setattr("trajectory.TRAJECTORY_DIR", trajectory_dir)
    monkeypatch.setattr("consolidation_writer.CONSOLIDATION_DIR", consolidation_dir)

    capture_trajectory(
        domain="python-testing",
        difficulty="medium",
        agent="coder",
        prompt="Write tests for the parser",
        system_prompt="Use TDD",
        response="Added parser edge-case tests",
        task_score=88.0,
    )
    # Two prior sessions for the SAME domain: the enrichment must select the
    # NEWEST one (session ids are timestamped, so reverse-lexical order is
    # recency). The older archive carries a marker phrase that must NOT leak
    # into the new session's context.
    write_consolidation(
        "loop-20260812-090000",
        "python-testing",
        [
            {
                "domain": "python-testing",
                "status": "success",
                "task_score": 80.0,
                "decision": "STALE-ARCHIVE-MARKER should never appear",
            }
        ],
    )
    write_consolidation(
        "loop-20260812-100000",
        "python-testing",
        [
            {
                "domain": "python-testing",
                "status": "success",
                "task_score": 88.0,
                "strategy": "Use focused parser fixtures",
                "decision": "Keep malformed-input tests explicit",
            }
        ],
    )

    monkeypatch.setattr("context_orchestrator._cocoindex_context", lambda *args: None)
    monkeypatch.setattr("context_orchestrator._serena_context", lambda *args: None)
    context = enrich_prompt(
        domain="python-testing",
        prompt="Continue parser test work",
        vault_path=tmp_path,
        session_id="loop-20260812-110000",
    )

    assert "Past similar trajectories" in context
    assert "Added parser edge-case tests" in context
    assert "Archival patterns from past sessions" in context
    assert "Keep malformed-input tests explicit" in context
    # Newest prior same-domain archive wins over the older one.
    assert "STALE-ARCHIVE-MARKER" not in context
