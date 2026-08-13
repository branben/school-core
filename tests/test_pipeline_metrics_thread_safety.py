"""Regression coverage for concurrent pipeline telemetry recording."""

from concurrent.futures import ThreadPoolExecutor

from pipeline_metrics import PipelineMetrics


def test_concurrent_recording_preserves_counts_and_snapshot_shape():
    metrics = PipelineMetrics(max_events=10_000)
    workers = 8
    records_per_worker = 125

    def record_many(worker: int) -> None:
        for _ in range(records_per_worker):
            metrics.record_call("parallel_review")
            metrics.record_model(f"judge-{worker % 2}", prompt_chars=3, output_chars=5)
            metrics.record_verification(commands=1)
            metrics.record_context("serena", hit=True, latency_ms=1.0)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(record_many, range(workers)))

    snapshot = metrics.snapshot()
    total = workers * records_per_worker

    assert snapshot["calls"]["parallel_review"] == total
    assert snapshot["model"]["call_count"] == total
    assert snapshot["model"]["prompt_chars"] == total * 3
    assert snapshot["model"]["output_chars"] == total * 5
    assert snapshot["verification"]["commands"] == total
    assert snapshot["context"]["sources"]["serena"] == {
        "hits": total,
        "misses": 0,
        "latency_ms": float(total),
    }
    assert snapshot["model"]["roles"] == {
        "judge-0": total // 2,
        "judge-1": total // 2,
    }
