"""In-process coverage tests for src/tools/cli.py."""

from src.core.scoring import ScoreStore
from src.tools import cli


def _store(tmp_path):
    s = ScoreStore(str(tmp_path / "scores.json"))
    s.update_score("alpha", "math", 70)
    s.update_score("beta", "math", 40)
    return s


def test_print_agents(tmp_path, capsys):
    cli.print_agents(_store(tmp_path))
    out = capsys.readouterr().out
    assert "alpha" in out and "beta" in out


def test_print_scores_unknown_domain_uses_default_roster(tmp_path, capsys):
    # A fresh ScoreStore ships a default agent roster, so any domain returns a
    # populated leaderboard (the empty path only occurs when roster is empty).
    cli.print_scores(ScoreStore(str(tmp_path / "s.json")), "nope")
    assert "Leaderboard for 'nope'" in capsys.readouterr().out


def test_print_scores_populated(tmp_path, capsys):
    cli.print_scores(_store(tmp_path), "math")
    out = capsys.readouterr().out
    assert "Leaderboard for 'math'" in out and "alpha" in out


def test_print_gates(tmp_path, capsys):
    cli.print_gates(_store(tmp_path))
    assert "Gates:" in capsys.readouterr().out


def test_print_history_empty(capsys):
    cli.print_history([])
    assert "No routing history yet" in capsys.readouterr().out


def test_print_history_entries(capsys):
    entries = [
        {"timestamp": "t1", "domain": "math", "difficulty": "hard",
         "chosen_agent": "alpha", "outcome": "success", "old_score": 50.0,
         "new_score": 60.0, "gate_crossed": "L2"},
        {"timestamp": "t2", "domain": "math", "difficulty": "hard",
         "chosen_agent": "beta", "blocked": True, "escalation": True},
    ]
    cli.print_history(entries, n=10)
    out = capsys.readouterr().out
    assert "math/hard" in out and "→ alpha" in out and "50.0→60.0" in out
    assert "BLOCKED" in out and "→ FACULTY" in out


def test_do_route_usage(tmp_path, capsys):
    cli.do_route(_store(tmp_path), [], [])
    assert "Usage:" in capsys.readouterr().out


def test_do_route_value_error(tmp_path, capsys):
    cli.do_route(_store(tmp_path), ["math", "impossible"], [])
    assert "Error:" in capsys.readouterr().out


def test_do_route_blocked(tmp_path, capsys, monkeypatch):
    class Blocked:
        chosen_agent = "x"
        blocked = True
        escalation = False
        score = 0.0
        eligible_count = 0
    monkeypatch.setattr(cli, "route_task", lambda *a, **k: Blocked())
    history = []
    cli.do_route(_store(tmp_path), ["math", "easy"], history)
    out = capsys.readouterr().out
    assert "Task blocked" in out and len(history) == 1


def test_do_route_invalid_outcome(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "?")
    cli.do_route(_store(tmp_path), ["math", "easy", "alpha"], [])
    assert "Invalid outcome" in capsys.readouterr().out


def test_do_route_success(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "s")
    history = []
    cli.do_route(_store(tmp_path), ["math", "easy"], history)
    out = capsys.readouterr().out
    assert "Eligible" in out


def test_do_vault(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_check_vault_index", lambda: True)
    cli.do_vault([])
    assert "CocoIndex" in capsys.readouterr().out


def test_do_engram_disabled(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "engram_available", lambda: False)
    cli.do_engram([])
    assert "engram" in capsys.readouterr().out.lower()


def test_repl_exits_on_eof(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "sys", __import__("sys"))
    import io
    import sys as _sys
    monkeypatch.setattr(_sys, "stdin", io.StringIO(""))
    cli.repl()
    # just needs to not raise and terminate cleanly