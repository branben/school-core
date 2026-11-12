"""Tests for scripts/failure_shape_lookup.py (0% coverage)."""

import sys
import pytest

from scripts.failure_shape_lookup import lookup, print_shape, SHAPES


class TestLookup:
    def test_no_match_returns_empty(self):
        assert lookup("zzzzzz") == []

    def test_exact_name_match(self):
        shapes = lookup("empty grep is not proof of absence")
        assert len(shapes) == 1
        assert shapes[0][0] == 1

    def test_keyword_match(self):
        shapes = lookup("grep")
        assert any(s[0] == 1 for s in shapes)

    def test_multiple_keywords_pick_best(self):
        shapes = lookup("empty grep ledger")
        # Should match shape 1 and 2; both have same score so both returned
        nums = [s[0] for s in shapes]
        assert 1 in nums
        assert 2 in nums

    def test_interactive_not_in_lookup(self):
        # --interactive is a main() concern, not lookup()
        shapes = lookup("interactive")
        assert shapes == []

    def test_proof_line_indexed(self):
        shapes = lookup("recorder was not recording")
        assert any(s[0] == 2 for s in shapes)

    def test_repair_number_by_counting(self):
        shapes = lookup("repair a number by re-guessing")
        assert any(s[0] == 6 for s in shapes)


class TestPrintShape:
    def test_prints_header_and_fix(self, capsys):
        print_shape(1, "Shape One", "do this")
        out = capsys.readouterr().out
        assert "Shape 1" in out
        assert "Shape One" in out
        assert "do this" in out


class TestMain:
    def test_no_args_prints_help(self, monkeypatch, capsys):
        import scripts.failure_shape_lookup as mod
        monkeypatch.setattr(mod.sys, "argv", ["failure_shape_lookup.py", "-h"])
        rc = mod.main()
        assert rc == 0
        assert "Usage" in capsys.readouterr().out

    def test_symptom_matches_and_exits_zero(self, monkeypatch, capsys):
        import scripts.failure_shape_lookup as mod
        monkeypatch.setattr(mod.sys, "argv", ["failure_shape_lookup.py", "empty", "grep"])
        rc = mod.main()
        assert rc == 0
        assert "Shape" in capsys.readouterr().out

    def test_symptom_no_match_exits_one(self, monkeypatch, capsys):
        import scripts.failure_shape_lookup as mod
        monkeypatch.setattr(mod.sys, "argv", ["failure_shape_lookup.py", "zzzzzz", "zzzz"])
        rc = mod.main()
        assert rc == 1
        assert "No shape matched" in capsys.readouterr().out

    def test_interactive_mode(self, monkeypatch, capsys):
        import scripts.failure_shape_lookup as mod
        monkeypatch.setattr(mod.sys, "argv", ["failure_shape_lookup.py", "--interactive"])
        inputs = iter(["empty grep", "zzzzzz", KeyboardInterrupt()])
        def fake_input(prompt):
            val = next(inputs)
            if isinstance(val, BaseException):
                raise val
            return val
        monkeypatch.setattr("builtins.input", fake_input)
        rc = mod.main()
        assert rc == 0
        out = capsys.readouterr().out
        assert "Shape" in out
