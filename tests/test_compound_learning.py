"""Tests for the bounded compound-learning loop."""

import json

import pytest

from compound_learning import CompoundLearningStore


def _evidence():
    return {
        "control": {"bd_id": "school-core-1", "plan_unit": "U2"},
        "runtime": {"dispatcher": "firstmate"},
        "outcome": {"lifecycle": "completed", "failure_edge": "runtime"},
        "raw_prompt": "must not persist",
    }


def _change():
    return {
        "change_id": "repair-timeout-admission",
        "kind": "route_policy",
        "target": "crew_admission",
        "reason": "timeouts repeat on comparable medium tasks",
    }


def test_observe_persists_bounded_bead_trigger(tmp_path):
    store = CompoundLearningStore(tmp_path / "compound.json")

    record = store.observe(
        bead_id="school-core-1",
        trigger="bead_completed",
        evidence=_evidence(),
    )

    assert record["phase"] == "observed"
    assert record["stop_reason"] == "awaiting_comparable_evidence"
    assert record["evidence"]["control"]["bd_id"] == "school-core-1"
    assert "raw_prompt" not in json.dumps(record)
    assert json.loads((tmp_path / "compound.json").read_text())[0]["observation_id"] == "obs-000001"


def test_one_change_budget_and_independent_verification_gate(tmp_path):
    store = CompoundLearningStore(tmp_path / "compound.json")
    record = store.observe(bead_id="b1", trigger="bead_failed", evidence=_evidence())
    proposed = store.propose(record["observation_id"], _change())

    assert proposed["phase"] == "proposed"
    with pytest.raises(ValueError, match="only one"):
        store.propose(record["observation_id"], _change())

    blocked = store.verify(record["observation_id"], accepted=True, evidence={"independent": False})
    assert blocked["phase"] == "blocked"
    assert blocked["stop_reason"] == "blocked"


def test_failed_verification_stops_as_stagnated(tmp_path):
    store = CompoundLearningStore(tmp_path / "compound.json")
    record = store.observe(bead_id="b1", trigger="bead_failed", evidence=_evidence())
    store.propose(record["observation_id"], _change())

    stopped = store.verify(
        record["observation_id"],
        accepted=False,
        evidence={"independent": True, "comparable": True},
    )

    assert stopped["phase"] == "stopped"
    assert stopped["stop_reason"] == "stagnated"


def test_two_independently_verified_comparable_changes_become_eligible(tmp_path):
    store = CompoundLearningStore(tmp_path / "compound.json")
    first = store.observe(bead_id="b1", trigger="bead_failed", evidence=_evidence())
    store.propose(first["observation_id"], _change())
    store.verify(first["observation_id"], accepted=True, evidence={"independent": True})
    first_recorded = store.record(first["observation_id"])
    assert first_recorded["promotion"]["eligible"] is False

    second = store.observe(bead_id="b2", trigger="bead_failed", evidence=_evidence())
    store.propose(second["observation_id"], _change())
    store.verify(second["observation_id"], accepted=True, evidence={"independent": True})
    second_recorded = store.record(second["observation_id"])

    assert second_recorded["promotion"] == {
        "eligible": True,
        "validated_repetitions": 2,
    }
    assert second_recorded["stop_reason"] == "completed"


def test_invalid_trigger_and_unknown_stop_fail_closed(tmp_path):
    store = CompoundLearningStore(tmp_path / "compound.json")
    with pytest.raises(ValueError, match="trigger"):
        store.observe(bead_id="b", trigger="timer", evidence={})
    record = store.observe(bead_id="b", trigger="bead_completed", evidence={})
    with pytest.raises(ValueError, match="stop"):
        store.stop(record["observation_id"], "keep_going")
