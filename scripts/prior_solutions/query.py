#!/usr/bin/env python3
"""Offline query entrypoint for the prior_solutions cocoindex app (Rank 8).

Run via the client:
    uv run --project <app_dir> python query.py "<query>" [k]

Embeds the query with the same SentenceTransformer model the index used, then
runs a vector search against the LanceDB ``prior_solutions`` table. Prints
``TASK:<id>\tSCORE:<float>`` marker lines followed by the snippet between
``----`` and ``====`` markers. Exits non-zero on any failure so the caller
(client) can fall back gracefully.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import cocoindex as coco
from cocoindex.connectors import lancedb
from cocoindex.ops.sentence_transformers import SentenceTransformerEmbedder

MODEL = "sentence-transformers/all-MiniLM-L6-v2"
LANCEDB_URI = str(Path(__file__).parent / "lancedb_data")
TABLE_NAME = "prior_solutions"
EMBED_COLUMN = "embedding"


async def main() -> int:
    if len(sys.argv) < 2:
        sys.stderr.write("usage: query.py <query> [k]\n")
        return 2
    query_text = sys.argv[1]
    k = int(sys.argv[2]) if len(sys.argv) > 2 else 3

    try:
        conn = await lancedb.connect_async(LANCEDB_URI)
    except Exception as exc:
        sys.stderr.write(f"lancedb connect failed: {exc}\n")
        return 1

    try:
        embedder = SentenceTransformerEmbedder(MODEL)
        query_vec = await embedder.embed(query_text)
        table = await conn.open_table(TABLE_NAME)
        search = await table.search(query_vec, vector_column_name=EMBED_COLUMN)
        results = await search.limit(k).to_list()
    except Exception as exc:
        sys.stderr.write(f"query failed: {exc}\n")
        return 1

    for r in results:
        # LanceDB returns cosine distance in [0, 2]; clamp similarity to [0, 1].
        score = max(0.0, min(1.0, 1.0 - float(r.get("_distance", 1.0))))
        task_id = r.get("task_id", "")
        text = r.get("text", "")
        print(f"TASK:{task_id}\tSCORE:{score:.4f}")
        print("----")
        print(text[:600])
        print("====")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
