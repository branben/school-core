"""Tests for scripts/sanitize_data.py — the PII scrubber that runs before
data/* is committed by the school-loop workflow.

This is the ONLY automated guardrail for the durability restore: the
checkpoint commit carries [skip ci], so these tests are what actually verify
the PII guarantee on every CI run.
"""

import json

from scripts.sanitize_data import HOME_RE, REPO_PREFIX_RE, sanitize_file, scrub_value


class TestScrubValue:
    def test_mac_home_path(self):
        assert (
            scrub_value("/Users/brandonbennett/school-core/data/trajectories/x.json")
            == "data/trajectories/x.json"
        )

    def test_github_runner_path(self):
        assert (
            scrub_value(
                "/home/runner/work/school-core/school-core/data/trajectories/y.json"
            )
            == "data/trajectories/y.json"
        )

    def test_bare_home_path_becomes_tilde(self):
        assert scrub_value("/Users/brandonbennett/.hermes/thing") == "~/.hermes/thing"

    def test_recursive_dict_and_list(self):
        value = {
            "trajectory": "/Users/brandonbennett/school-core/data/t/a.json",
            "meta": {"note": "home is /Users/brandonbennett"},
            "items": ["/home/runner/work/school-core/school-core/data/t/b.json"],
            "count": 3,
        }
        out = scrub_value(value)
        assert out["trajectory"] == "data/t/a.json"
        assert out["meta"]["note"] == "home is ~"
        assert out["items"][0] == "data/t/b.json"
        assert out["count"] == 3

    def test_basename_survives(self):
        """Board.py only uses Path(...).name — the scrubbed form must keep it."""
        from pathlib import Path

        scrubbed = scrub_value("/Users/brandonbennett/school-core/data/trajectories/x.json")
        assert Path(scrubbed).name == "x.json"

    def test_no_false_positive(self):
        assert scrub_value("data/trajectories/x.json") == "data/trajectories/x.json"
        assert scrub_value("plain text") == "plain text"


class TestSanitizeFile:
    def test_json_round_trip(self, tmp_path):
        p = tmp_path / "last_run.json"
        p.write_text(
            json.dumps(
                [
                    {
                        "issue": 5,
                        "trajectory": "/Users/brandonbennett/school-core/data/trajectories/x.json",
                    }
                ]
            )
        )
        n = sanitize_file(p)
        # Both HOME_RE (/Users/brandonbennett) and REPO_PREFIX_RE match here
        assert n == 2
        data = json.loads(p.read_text())
        assert data[0]["trajectory"] == "data/trajectories/x.json"

    def test_no_hits_returns_zero(self, tmp_path):
        p = tmp_path / "clean.json"
        p.write_text(json.dumps([{"issue": 1}]))
        assert sanitize_file(p) == 0

    def test_missing_file_graceful(self, tmp_path):
        assert sanitize_file(tmp_path / "nope.json") == 0

    def test_non_json_text_fallback(self, tmp_path):
        p = tmp_path / "artifact.txt"
        p.write_text("path is /Users/brandonbennett/school-core/data/foo")
        n = sanitize_file(p)
        assert n == 2
        assert "/Users/" not in p.read_text()


class TestRegexes:
    def test_mac_regex(self):
        assert REPO_PREFIX_RE.sub(
            "data/", "/Users/brandonbennett/school-core/data/trajectories/x.json"
        ) == "data/trajectories/x.json"

    def test_runner_regex(self):
        assert REPO_PREFIX_RE.sub(
            "data/", "/home/runner/work/school-core/school-core/data/trajectories/x.json"
        ) == "data/trajectories/x.json"

    def test_home_regex(self):
        assert HOME_RE.sub("~", "kept /Users/brandonbennett and /Users/other") == (
            "kept ~ and ~"
        )
