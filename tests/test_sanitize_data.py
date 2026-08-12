"""Tests for scripts/sanitize_data.py — the PII scrubber that runs before
data/* is committed by the school-loop workflow.

This is the ONLY automated guardrail for the durability restore: the
checkpoint commit carries [skip ci], so these tests are what actually verify
the PII guarantee on every CI run.
"""

import json
import subprocess
import sys

from scripts.sanitize_data import (
    HOME_RE,
    REPO_PREFIX_RE,
    DEFAULT_TRAJECTORY_KEEP,
    sanitize_file,
    scrub_value,
    trim_trajectories,
    trim_consolidations,
    DEFAULT_CONSOLIDATION_KEEP,
)


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


class TestSanitizeConsolidation:
    def test_yaml_round_trip_scrubs_home_repo_and_secret_values(self, tmp_path):
        p = tmp_path / "session.yaml"
        p.write_text(
            "session_id: loop-20260811-120000\n"
            "source: /Users/brandonbennett/school-core/data/sessions/consolidation/loop.yaml\n"
            "note: /Users/brandonbennett/.hermes/private\n"
            "api_key: sk-1234567890abcdef\n"
        )

        n = sanitize_file(p)

        # Repo prefix + home prefix in the source path, home prefix in the
        # note, sensitive field, and token pattern are all counted.
        assert n == 5
        cleaned = p.read_text()
        assert "/Users/" not in cleaned
        assert "data/sessions/consolidation/loop.yaml" in cleaned
        assert "~/.hermes/private" in cleaned
        assert "[REDACTED]" in cleaned
        assert "sk-1234567890abcdef" not in cleaned

    def test_fresh_checkout_can_read_sanitized_consolidation(self, tmp_path, monkeypatch):
        from context_orchestrator import _archival_context

        source = tmp_path / "source" / "data" / "sessions" / "consolidation" / "loop-20260811-120000"
        source.mkdir(parents=True)
        artifact = source / "debugging.yaml"
        artifact.write_text(
            "session_id: loop-20260811-120000\n"
            "domain: debugging\n"
            "patterns:\n"
            "  - edited /Users/brandonbennett/school-core/data/x.py\n"
            "key_learnings: []\n"
            "error_recurrence: {}\n"
        )
        sanitize_file(artifact)

        # Commit the sanitized artifact to a temporary git repository and
        # clone it, matching the next school-loop checkout boundary.
        source_repo = tmp_path / "source-repo"
        source_repo.mkdir()
        tracked = source_repo / "data" / "sessions" / "consolidation" / "loop-20260811-120000" / "debugging.yaml"
        tracked.parent.mkdir(parents=True)
        tracked.write_text(artifact.read_text())
        subprocess.run(["git", "init", "-q", str(source_repo)], check=True)
        subprocess.run(["git", "-C", str(source_repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(source_repo), "config", "user.name", "test"], check=True)
        subprocess.run(["git", "-C", str(source_repo), "add", "data"], check=True)
        subprocess.run(["git", "-C", str(source_repo), "commit", "-qm", "checkpoint"], check=True)
        fresh_repo = tmp_path / "fresh-checkout"
        subprocess.run(["git", "clone", "-q", str(source_repo), str(fresh_repo)], check=True)

        # Prove the next-cycle reader in a fresh Python process against that
        # real checkout, not the current pytest module cache.
        fresh_root = fresh_repo / "data" / "sessions" / "consolidation"
        code = (
            "from pathlib import Path; "
            "import consolidation_writer; "
            f"consolidation_writer.CONSOLIDATION_DIR = Path({str(fresh_root)!r}); "
            "from context_orchestrator import _archival_context; "
            "print(_archival_context('debugging', 'loop-20260811-120000') or '')"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)

        assert "Archival patterns" in result.stdout
        assert "data/x.py" in result.stdout
        assert "/Users/" not in result.stdout


class TestTrimConsolidations:
    def test_keeps_newest_sessions_and_all_domains(self, tmp_path):
        root = tmp_path / "consolidation"
        for session in ("loop-20260811-120000", "loop-20260811-120001", "loop-20260811-120002"):
            session_dir = root / session
            session_dir.mkdir(parents=True)
            (session_dir / "debugging.yaml").write_text("session_id: " + session)
            (session_dir / "testing.yaml").write_text("session_id: " + session)

        removed = trim_consolidations(keep=2, consolidation_dir=root)

        assert removed == 2
        assert not (root / "loop-20260811-120000").exists()
        assert sorted(p.name for p in (root / "loop-20260811-120001").glob("*.yaml")) == ["debugging.yaml", "testing.yaml"]
        assert sorted(p.name for p in (root / "loop-20260811-120002").glob("*.yaml")) == ["debugging.yaml", "testing.yaml"]

    def test_keep_zero_is_clamped(self, tmp_path):
        root = tmp_path / "consolidation" / "loop-20260811-120000"
        root.mkdir(parents=True)
        (root / "debugging.yaml").write_text("session_id: loop-20260811-120000")
        assert trim_consolidations(keep=0, consolidation_dir=root.parent) == 0

    def test_default_keep_is_sane(self):
        assert DEFAULT_CONSOLIDATION_KEEP >= 10


class TestTrimTrajectories:
    """U2: the checkpoint trims trajectory history to the newest N files."""

    def _make_traj_dir(self, tmp_path, n):
        d = tmp_path / "trajectories"
        d.mkdir(exist_ok=True)
        for i in range(n):
            (d / f"20260801_000000_00000{i}--debugging--agent.json").write_text("{}")
        return d

    def test_keeps_newest_n(self, tmp_path):
        d = self._make_traj_dir(tmp_path, 5)
        removed = trim_trajectories(keep=2, traj_dir=d)
        assert removed == 3
        remaining = sorted(p.name for p in d.glob("*.json"))
        assert len(remaining) == 2
        # Newest two (highest timestamp prefix) survive: i=3 and i=4.
        assert remaining == [
            "20260801_000000_000003--debugging--agent.json",
            "20260801_000000_000004--debugging--agent.json",
        ]

    def test_noop_when_under_cap(self, tmp_path):
        d = self._make_traj_dir(tmp_path, 3)
        assert trim_trajectories(keep=5, traj_dir=d) == 0
        assert len(list(d.glob("*.json"))) == 3

    def test_empty_dir(self, tmp_path):
        d = tmp_path / "trajectories"
        d.mkdir(exist_ok=True)
        assert trim_trajectories(keep=2, traj_dir=d) == 0

    def test_keep_zero_is_clamped_to_one(self, tmp_path):
        """keep=0 is a footgun (files[:-0] removes nothing) — clamp to 1."""
        d = self._make_traj_dir(tmp_path, 3)
        removed = trim_trajectories(keep=0, traj_dir=d)
        assert removed == 2
        assert len(list(d.glob("*.json"))) == 1

    def test_missing_dir(self, tmp_path):
        assert trim_trajectories(keep=2, traj_dir=tmp_path / "nope") == 0

    def test_default_keep_is_sane(self):
        assert DEFAULT_TRAJECTORY_KEEP >= 10

    def test_trims_real_repo_dir_without_error(self):
        """Sanity: the default (repo) dir trim path works without touching it."""
        # keep a large cap so this is a no-op in the real repo — never deletes.
        assert trim_trajectories(keep=10_000) >= 0
