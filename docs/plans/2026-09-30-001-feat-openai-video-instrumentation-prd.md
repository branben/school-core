---
title: "feat: instrument the OpenAI Video generation API in braintrust-sdk-python"
created: 2026-09-30
status: proposed
author: lucas (school-core outer loop)
project: agent-school
upstream_issue: https://github.com/braintrustdata/braintrust-sdk-python/issues/304
fork: branben/braintrust-sdk-python @ 696d206a
tags: [openai, videos, sora, instrumentation, tracing, braintrust, prd]
measured: 2026-09-30 · openai 3.16.2 / 2.31.0 / 1.92.0 / 1.77.0 / 1.71.0 wheels extracted and read
---

# Instrument the OpenAI Video generation API

## The one-line ask

`client.videos.*` produces zero Braintrust spans today. It should produce the
same span shape `client.images.generate()` already produces — because Images
already does it, and video is the exact same class of call: you hand a model a
prompt and an asset, it produces media, and the user wants to see it in their
trace.

## Why this is brownfield, not greenfield

The mechanism exists and is used eight times over. `ImagesPatcher` +
`_WrapImages` (`patchers.py:347-363`) wrap `generate`, `edit`, and
`create_variation` through `_ImageBaseWrapper` (`tracing.py:1870`), which
already solves the hard parts of this problem: starting a span, allowlisting
metadata, extracting media into an output summary, and merging timing metrics.

So the pitch is not "add a feature." It is "copy the row that already works."

## The gap, measured

Nine patchers are registered today (`patchers.py:449-461`): agents.sessions,
chat.completions, embeddings, moderations, audio.{speech,transcriptions,
translations}, images, responses. **Zero** references to video anywhere in
`py/src/braintrust/integrations/openai/`.

| OpenAI Videos method | What it does | Instrument? | Why |
|---|---|---|---|
| `create` | submit a generation job | **yes** | the primary generative call |
| `edit` | re-cut an existing video | **yes** | same shape, takes a source video |
| `extend` | lengthen a completed video | **yes** | same shape, takes a source video |
| `remix` | regenerate from a new prompt | **yes** | generative, cheap to add |
| `create_and_poll` | `create` + poll loop | **no** | convenience wrapper — its inner `create`/`poll` calls are each traced |
| `poll` | block until the job reaches a terminal state | **yes** | holds the real generation time; this is where completion is recorded |
| `retrieve` / `list` | status read + CRUD | no | not generative, and `retrieve` would put a span on every poll tick |
| `download_content` | fetch bytes | no | bytes, not a model call |
| `create_character` / `get_character` | character assets | no | out of scope, not generative |

### The `create_and_poll` objection, stated up front

`create_and_poll` calls `self.create(...)` then `self.poll(...)`
(`videos.py:131-163`). Wrapping it as well would emit a parent span saying
`status: "completed"` around a child saying `status: "queued"`.

The repo's `cursor_sdk` integration faces the identical shape and answers it:
it wraps `Agent.send` (`patchers.py:23`) and `Run.wait` (`patchers.py:67`) as
separate patchers, and does **not** wrap `Agent.prompt` — the one-call
convenience method that does `agent.send(message).wait()` (`_agent.py:149`).
Trace the inner pair, leave the wrapper alone.

**Decision: wrap `create` and `poll`, not `create_and_poll`.** A user calling
`create_and_poll` gets two sibling spans: submit latency, then the generation
wait. That second span is where completion is recorded — dropping it would leave
a trace that proves only that the request was accepted.

## What the user sees

A span named `Video Generation`, `Video Poll`, `Video Edit`, `Video Extend`, or
`Video Remix`, with:

- **input** — the prompt, plus the reference video or image as a Braintrust
  `Attachment` (not a raw file handle)
- **metadata** — `provider: openai`, `model`, `seconds`, `size`, and
  `video_id` for the remix/edit lineage
- **output** — video id, `status`, `progress`, `size`, `seconds`, and the
  `error` payload when the job failed
- **metrics** — `duration`, and **no cost**

## The two things that will bite an implementer

**1. One metadata allowlist does not fit four methods.** `create` takes
`prompt, input_reference, model, seconds, size`. `edit` takes only `prompt` +
`video`. `extend` takes `prompt, seconds` + `video`. `remix` takes `video_id` +
`prompt`. A single `(model, seconds, size)` tuple — the natural copy of
`_IMAGE_METADATA_PARAMS` — silently yields `{"provider": "openai"}` and
nothing else for `edit` and `remix`. Per-method allowlists, or accept an empty
span.

**2. There is no `usage` on a video.** `Video` has no `usage` field (verified:
no match in `openai/types/video.py` across 2.31.0 and 3.16.2). The Images base
wrapper merges `_parse_metrics_from_usage(response.get("usage"))`
(`tracing.py:1875`), which for video returns `{}`. Every video span will have
a duration and no cost. That is correct — Sora billing is not token-metered —
but it is a decision to state, not something to discover in production.

## CI is the actual hard part

The nox matrix runs `test_openai` against **four** openai versions
(`pyproject.toml:387-391`): `latest` (3.16.2), 1.92.0, 1.77.0, 1.71.0. I
downloaded and unzipped all of them:

| Version | `openai/resources/videos.py` |
|---|---|
| 3.16.2 (latest) | present |
| 2.31.0 | present |
| 1.92.0 | **absent** |
| 1.77.0 | **absent** |
| 1.71.0 | **absent** |

Under CI, `record_mode="none"` (`src/braintrust/conftest.py:287`), so a video
cassette recorded only under `cassettes/latest/` makes the three old sessions
**fail** with "cassette not found" rather than skip. Every video test needs a
version guard — the precedent already exists at `test_openai.py:1367` and
`test_openai.py:1631` — and `scripts/check-unused-cassettes.py` will flag any
cassette whose test always skips.

## Objections, pre-answered

- **"Isn't this a thin copy-paste of Images?"** Mostly yes, and that is the
  point. The factory `_make_method_patchers` (`patchers.py:33`) already emits
  sync + async + instance patchers from one wrapper callback, so the delta is
  ~120 lines of new code.
- **"Won't wrapping video break users on old openai?"** No. `_import_optional_module`
  returns `None` when the target module is absent, and `applies()` then returns
  False (`base.py:100-105`, `base.py:147`). The patcher is inert on 1.x. The
  *tests* are the part that needs a guard.
- **"Why not instrument CRUD too?"** Because CRUD is not a model call.
  Instrumenting `retrieve` would put a span on every poll tick and bury the
  generative signal that people actually want. `poll` is the exception: it is
  the single blocking call where the generation time is actually spent.
- **"Why is `create_and_poll` skipped? Isn't that the common call?"** It is the
  common call, which is exactly why it must be skipped. It is a convenience
  wrapper whose two inner calls are each traced, so the trace carries both
  numbers. Wrapping it too would add a third span that duplicates the pair.
  `cursor_sdk` makes the same call for `Agent.prompt`.
- **"Related to Google GenAI?"** Yes — issue #236 tracks the same gap for
  `models.generate_videos()`. This is the OpenAI half of one cross-provider
  gap; doing them separately is fine and probably better.

## Done means

- `client.videos.{create,edit,extend,remix}` emit one span each, sync and
  async, under both `OpenAIIntegration.setup()` and `wrap_openai(client)`.
- File inputs (`input_reference`, `video`) log as `Attachment`, not a file
  handle.
- Video tests pass on the `latest` matrix version and skip cleanly on
  1.92.0 / 1.77.0 / 1.71.0.
- No cassette or docs go stale.
