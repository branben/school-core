"""F6-concurrency: prove ScoreStore.save() is not safe under concurrent writes.

The agent-school MCP tools (`school_run`, `school_evaluate`) -> `evaluate_and_update`
-> `ScoreStore.update_score` -> `set_score` -> `save()`, where `save()` does:
    with self.file_path.open("w", ...) as f:
        json.dump(self.scores, f)
with NO lock. Two scoring calls that land in parallel each drive `save()` on the
SAME shared file. `open("w")` *truncates* immediately; if one thread truncates
while another is mid-`json.dump`, the on-disk file is left truncated/garbage.
The next `ScoreStore.load()` then hits a JSONDecodeError and silently re-seeds
from SEED_AGENTS — wiping the entire leaderboard.

NOTE: in-memory `update_score` deltas are NOT lost (the store caches scores in a
shared dict and the GIL serializes the read-modify-write), so the dangerous
surface is specifically the concurrent `open("w")` in save(). This test hammers
save() from many threads and asserts the on-disk file stays valid + reloadable.
TODAY IT FAILS (intermittent truncation) — proving save() needs a lock.
"""

from __future__ import annotations

import threading

from scoring import ScoreStore


def test_concurrent_save_does_not_corrupt_file(tmp_path):
    scores_file = tmp_path / "scores.json"
    scores_file.write_text("{}")
    store = ScoreStore(file_path=str(scores_file))

    # Populate a non-trivial in-memory state so dumps take real time.
    for i in range(200):
        store.set_score(f"agent-{i}", "_default", float(i % 100))

    errors: list[Exception] = []

    def hammer_save():
        try:
            for _ in range(50):
                store.save()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=hammer_save) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # The file must remain valid JSON and reloadable. A torn write (one thread
    # truncating while another dumps) yields a JSONDecodeError on reload — or a
    # partially-written file that the next load() cannot parse.
    try:
        reloaded = ScoreStore(file_path=str(scores_file))
    except Exception as e:
        raise AssertionError(
            f"concurrent save() corrupted scores.json — reload raised {e!r}; "
            f"next load() would silently re-seed from SEED_AGENTS and wipe the "
            f"leaderboard"
        ) from e

    # The reloaded state must reflect the populated scores, not a wiped/empty store.
    assert reloaded.get_score("agent-50", "_default") == 50.0, (
        f"concurrent save() corrupted leaderboard state on reload: "
        f"agent-50={reloaded.get_score('agent-50', '_default')}"
    )
    assert not errors, f"concurrent save() raised: {errors}"
