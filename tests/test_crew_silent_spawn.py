"""A crew that never reports must not consume the whole poll deadline.

WHY THIS EXISTS
---------------
Live run 32330426471 admitted a crew for the first time (issue #342). It burned
~16 minutes and returned ``timeout``. But the arithmetic says that should be
impossible:

    per-turn cap  HERMES_TIMEOUT_PER_TURN_MS = 90_000  (orca_executor.py:313)
    turn budget   {easy:1, medium:3, hard:5, diploma:8} (orca_executor.py:314)

    easy     1 x 90s =  90s  + spawn -> ~120s   vs 900s poll  (780s slack)
    medium   3 x 90s = 270s  + spawn -> ~300s   vs 900s poll  (600s slack)
    hard     5 x 90s = 450s  + spawn -> ~480s   vs 900s poll  (420s slack)
    diploma  8 x 90s = 720s  + spawn -> ~750s   vs 900s poll  (150s slack)

EVERY difficulty fits inside the 900s crew deadline with slack. So the timeout is
NOT too short for legitimate work, and raising CREW_TIMEOUT_SECONDS is the wrong
fix — it would just buy a longer silence.

The actual defect is in ``_poll``'s liveness model. ``_read_status_detail``
returns ``None`` when the status file cannot be read *or* contains no recognised
verb (crew_dispatch.py:177-187), and ``_poll`` treats that identically to
``working``: the loop comment says "working, paused, resolved, unknown, and
absent status all remain live."

So a crew that **never wrote a single status line** — spawn silently failed, the
agent died before its first append, the wrapper never launched — is
indistinguishable from a crew doing useful work. Both consume the full 900s.

That silence is expensive twice over. It burns 900s of a 1800s cycle, and
because admission reserves ``crew_timeout * cap + reserve``, one silent crew can
consume half the cycle budget and starve every remaining issue.

THE FIX: a startup deadline. A crew that has not produced ANY recognised status
within a short grace window is declared ``spawn_silent`` and the poll returns
immediately. This is strictly narrower than the overall timeout: once the crew
has spoken even once, the normal 900s deadline governs and long legitimate work
is unaffected.

Deliberately NOT shortening CREW_TIMEOUT_SECONDS: diploma tasks genuinely need
750s, and cutting the ceiling to catch silent failures would forbid the school's
hardest difficulty. Fail fast on silence, stay patient with work.
"""

import pytest
from typing import Optional

import crew_dispatch


class _Clock:
    """Deterministic clock; sleep advances time instead of blocking."""

    def __init__(self):
        self.t = 1000.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def _poll(tmp_path, monkeypatch, status_text: Optional[str] = None, **kw):
    """Run _poll against a synthetic status file. Returns (status, reason, detail)."""
    crew_id = "crew-silent-test"
    task_dir = tmp_path / crew_id
    task_dir.mkdir(parents=True, exist_ok=True)
    status_file = task_dir / "status.md"
    if status_text is not None:
        status_file.write_text(status_text, encoding="utf-8")

    monkeypatch.setattr(crew_dispatch, "_status_path", lambda cid: status_file)
    clock = _Clock()
    return crew_dispatch._poll(
        crew_id,
        timeout=kw.pop("timeout", 900.0),
        poll_interval=kw.pop("poll_interval", 10.0),
        blocked_grace=kw.pop("blocked_grace", 60.0),
        now_fn=clock.now,
        sleep_fn=clock.sleep,
        **kw,
    ), clock


class TestSilentCrewFailsFast:
    def test_absent_status_file_does_not_burn_the_full_deadline(self, tmp_path, monkeypatch):
        """A crew that never writes a status must not consume 900s.

        REGRESSION: _read_status_detail returns None for a missing file and
        _poll treats None as live, so a crew that never started looked exactly
        like a crew doing work. #342 burned ~16 minutes this way.
        """
        (status, reason, _), clock = _poll(
            tmp_path, monkeypatch, None, timeout=900.0, poll_interval=10.0
        )
        elapsed = clock.t - 1000.0
        assert elapsed < 900.0, (
            f"a silent crew consumed {elapsed}s of a 900s deadline — it must be "
            "declared dead on a startup grace window, not run to full timeout"
        )
        assert status in {"timeout", "failed"}
        assert reason == "spawn_silent", (
            f"silence must be reported distinctly (got {reason!r}) so it is not "
            "confused with a crew that worked and genuinely overran"
        )

    def test_empty_status_file_is_also_silence(self, tmp_path, monkeypatch):
        """A file with no recognised verb is silence, not work."""
        (status, reason, _), clock = _poll(
            tmp_path, monkeypatch, "starting up...\nno verbs here\n",
            timeout=900.0, poll_interval=10.0,
        )
        assert clock.t - 1000.0 < 900.0
        assert reason == "spawn_silent"


class TestWorkingCrewIsUnaffected:
    """The startup deadline must not shorten legitimate long work."""

    def test_a_crew_that_spoke_once_gets_the_full_deadline(self, tmp_path, monkeypatch):
        """`working:` proves the crew is alive — patience must resume.

        A diploma task legitimately needs ~750s. If speaking once did not
        restore the full deadline, this fix would forbid the hardest difficulty.
        """
        (status, reason, _), clock = _poll(
            tmp_path, monkeypatch, "working: implementing the change\n",
            timeout=900.0, poll_interval=10.0,
        )
        elapsed = clock.t - 1000.0
        assert elapsed >= 900.0 - 20.0, (
            f"a crew that reported `working:` was cut off after {elapsed}s; long "
            "legitimate work (diploma ~750s) must still be allowed"
        )
        assert reason != "spawn_silent"

    def test_done_still_returns_immediately(self, tmp_path, monkeypatch):
        (status, reason, detail), clock = _poll(
            tmp_path, monkeypatch,
            "working: started\ndone: branch=fm/x commit=abc123 base=main@def456\n",
        )
        assert status == "done"
        assert reason is None
        assert "branch=fm/x" in detail

    def test_failed_still_returns_immediately(self, tmp_path, monkeypatch):
        (status, reason, _), _ = _poll(tmp_path, monkeypatch, "failed: could not build\n")
        assert status == "failed"

    def test_blocked_still_honours_its_grace_window(self, tmp_path, monkeypatch):
        """blocked_grace must keep working — it is a separate mechanism."""
        (status, reason, _), clock = _poll(
            tmp_path, monkeypatch, "blocked: needs a decision\n",
            timeout=900.0, poll_interval=10.0, blocked_grace=60.0,
        )
        assert status == "blocked"
        assert reason == "blocked"
        elapsed = clock.t - 1000.0
        assert elapsed < 200.0, f"blocked returned after {elapsed}s, grace was 60s"
