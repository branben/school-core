"""cocoindex app: index prior student solutions for retrieval (Rank 8).

Indexes the markdown artifacts under ``docs/solutions/<task-id>/*.md`` into a
local LanceDB table (zero external services — embedded, no server) so the
director/CE runner can retrieve structurally-similar prior solutions when a
student starts a new task. Embeddings use SentenceTransformer
(all-MiniLM-L6-v2); the model downloads on first run inside this app's uv venv,
isolated from the repo.

Build the index:
    uv run cocoindex update main.py

Query (from the client):
    uv run python query.py "<query>" [k]
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
from dataclasses import dataclass
from typing import Annotated, AsyncIterator
from numpy.typing import NDArray

import cocoindex as coco
from cocoindex.connectors import lancedb, localfs
from cocoindex.ops.sentence_transformers import SentenceTransformerEmbedder
from cocoindex.ops.text import RecursiveSplitter
from cocoindex.resources.chunk import Chunk
from cocoindex.resources.file import FileLike, PatternFilePathMatcher
from cocoindex.resources.id import IdGenerator

SOLUTIONS_DIR = pathlib.Path(__file__).parent.parent.parent / "docs" / "solutions"
LANCEDB_URI = str(pathlib.Path(__file__).parent / "lancedb_data")
TABLE_NAME = "prior_solutions"
EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
TOP_K = 5

LANCE_DB = coco.ContextKey[lancedb.LanceAsyncConnection]("prior_solutions_db")
EMBEDDER = coco.ContextKey[SentenceTransformerEmbedder]("embedder", detect_change=True)
_splitter = RecursiveSplitter()


@dataclass
class DocEmbedding:
    id: int
    task_id: str
    filename: str
    text: str
    embedding: Annotated[NDArray, EMBEDDER]


@coco.lifespan
async def coco_lifespan(builder: coco.EnvironmentBuilder) -> AsyncIterator[None]:
    builder.settings.db_path = pathlib.Path(__file__).parent / "cocoindex_store"
    conn = await lancedb.connect_async(LANCEDB_URI)
    builder.provide(LANCE_DB, conn)
    builder.provide(EMBEDDER, SentenceTransformerEmbedder(EMBED_MODEL))
    yield


@coco.fn
async def process_chunk(
    chunk: Chunk,
    task_id: str,
    filename: str,
    id_gen: IdGenerator,
    table: lancedb.TableTarget[DocEmbedding],
) -> None:
    table.declare_row(
        row=DocEmbedding(
            id=await id_gen.next_id(chunk.text),
            task_id=task_id,
            filename=filename,
            text=chunk.text,
            embedding=await coco.use_context(EMBEDDER).embed(chunk.text),
        ),
    )


@coco.fn(memo=True)
async def process_file(
    file: FileLike,
    table: lancedb.TableTarget[DocEmbedding],
) -> None:
    text = await file.read_text()
    chunks = _splitter.split(text, chunk_size=2000, chunk_overlap=500, language="markdown")
    id_gen = IdGenerator()
    await coco.map(process_chunk, chunks, file.file_path.path.parent.name, str(file.file_path.path), id_gen, table)


@coco.fn
async def app_main() -> None:
    target_table = await lancedb.mount_table_target(
        LANCE_DB,
        table_name=TABLE_NAME,
        table_schema=await lancedb.TableSchema.from_class(
            DocEmbedding, primary_key=["id"]
        ),
    )
    files = localfs.walk_dir(
        SOLUTIONS_DIR,
        recursive=True,
        path_matcher=PatternFilePathMatcher(included_patterns=["**/*.md"]),
    )
    await coco.mount_each(process_file, files.items(), target_table)


app = coco.App(
    coco.AppConfig(name="prior_solutions"),
    app_main,
)
