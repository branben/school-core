"""F5: reusable context packet and bounded parallel probes."""

import threading

import context_orchestrator as context
from pipeline_metrics import PipelineMetrics


def test_same_session_reuses_context_packet(monkeypatch, tmp_path):
    context.clear_context_cache()
    calls = {"coco": 0, "serena": 0, "engram": 0, "archival": 0}

    def probe(name, value):
        def _run(*args):
            calls[name] += 1
            return value
        return _run

    monkeypatch.setattr(context, "_cocoindex_context", probe("coco", "coco"))
    monkeypatch.setattr(context, "_serena_context", probe("serena", "serena"))
    monkeypatch.setattr(context, "_engram_context", probe("engram", "engram"))
    monkeypatch.setattr(context, "_archival_context", probe("archival", "archival"))

    first_metrics = PipelineMetrics()
    first = context.enrich_prompt(
        "python-testing", "Write parser tests", session_id="loop-1",
        repo_path=tmp_path, vault_path=tmp_path, metrics=first_metrics,
    )
    second_metrics = PipelineMetrics()
    second = context.enrich_prompt(
        "python-testing", "Write parser tests", session_id="loop-1",
        repo_path=tmp_path, vault_path=tmp_path, metrics=second_metrics,
    )

    assert second == first
    assert calls == {"coco": 1, "serena": 1, "engram": 1, "archival": 1}
    assert second_metrics.snapshot()["context"]["sources"]["cache"]["hits"] == 1


def test_repo_identity_invalidates_cached_packet(monkeypatch, tmp_path):
    context.clear_context_cache()
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    head = git_dir / "HEAD"
    head.write_text("commit-a")
    calls = []
    monkeypatch.setattr(context, "_cocoindex_context", lambda *args: calls.append("coco") or "coco")

    context.enrich_prompt(
        "python-testing", "same task", session_id="loop-3",
        repo_path=tmp_path, vault_path=tmp_path,
    )
    head.write_text("commit-b")
    context.enrich_prompt(
        "python-testing", "same task", session_id="loop-3",
        repo_path=tmp_path, vault_path=tmp_path,
    )

    assert calls == ["coco", "coco"]


def test_context_sources_probe_in_parallel_with_deterministic_render_order(
    monkeypatch, tmp_path,
):
    context.clear_context_cache()
    barrier = threading.Barrier(4, timeout=2)

    def blocking(value):
        def _run(*args):
            barrier.wait()
            return value
        return _run

    monkeypatch.setattr(context, "_cocoindex_context", blocking("coco"))
    monkeypatch.setattr(context, "_serena_context", blocking("serena"))
    monkeypatch.setattr(context, "_engram_context", blocking("engram"))
    monkeypatch.setattr(context, "_archival_context", blocking("archival"))

    rendered = context.enrich_prompt(
        "python-testing", "Write parser tests", session_id="loop-2",
        repo_path=tmp_path, vault_path=tmp_path,
    )

    assert rendered.index("coco") < rendered.index("serena")
    assert rendered.index("serena") < rendered.index("engram")
    assert rendered.index("engram") < rendered.index("archival")
