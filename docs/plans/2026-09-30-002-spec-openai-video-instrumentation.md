---
title: "spec: OpenAI Videos instrumentation — implementation plan"
created: 2026-09-30
status: proposed
author: lucas (school-core outer loop)
project: agent-school
prd: docs/plans/2026-09-30-001-feat-openai-video-instrumentation-prd.md
upstream_issue: https://github.com/braintrustdata/braintrust-sdk-python/issues/304
repo: ~/school-core/repos/braintrust-sdk-python @ 696d206a
measured: 2026-09-30 · openai wheels 3.16.2 / 2.31.0 / 1.92.0 / 1.77.0 / 1.71.0 extracted; source cited file:line
tags: [spec, openai, videos, implementation, sdlc]
---

# SPEC — OpenAI Videos instrumentation

## Decisions locked before any code

| # | Decision | Alternative rejected | Why |
|---|---|---|---|
| D1 | Wrap `create`, `poll`, `edit`, `extend`, `remix`; **not** `create_and_poll` | wrap `create_and_poll` too, nesting a parent over `create` | `create_and_poll` is a convenience wrapper that delegates to `create` then `poll` (`videos.py:131-163`). `cursor_sdk` sets the house rule for this exact shape: it wraps `Agent.send` (`patchers.py:23`) and `Run.wait` (`patchers.py:67`) separately and does **not** wrap `Agent.prompt`, the one-call convenience method that does `agent.send(message).wait()` (`_agent.py:149`). Wrapping the inner pair keeps the wait visible, where completion is actually recorded. Resolved in `school-core-5c0` |
| D7 | **Before ruling on any wrapper scope, search the existing integrations for the same shape** | ruling from first principles | Process rule, added after D1 was filed wrong. The original D1 excluded `create_and_poll` citing `_delegates_to_wrapped_method` (`patchers.py:485`) as a policy; that helper landed in `43faa35d` (PR #206) and its docstring describes stainless closure helpers, not a nesting policy. A technical coincidence was read as a principle. `cursor_sdk` answered the question directly and was found in one grep |
| D2 | Per-method metadata allowlists | one `(model, seconds, size)` allowlist | `edit`/`remix` accept neither → empty metadata dict, silently |
| D3 | Route all file inputs through `_materialize_logged_file_input` | log the handle raw | raw handles are non-serializable into span input; every existing file-taking wrapper does this (`tracing.py:1927-1928`, `tracing.py:2003`) |
| D4 | Four distinct span names | one `Video Generation` name | Images subclasses pass distinct names (`tracing.py:1941-1953`) |
| D5 | No cost metrics, by design | add a synthetic cost | `Video` has no `usage` field — Sora is not token-metered |
| D6 | No version gate on the patcher; version gate on the tests | gate both | `applies()` already returns False when the target module is absent (`base.py:100-105`) |

## Milestone 1 — wrappers in `tracing.py`

**New module-level constant.** Next to `_IMAGE_METADATA_PARAMS`
(`tracing.py:151-161`):

```python
_VIDEO_GENERATE_METADATA_PARAMS = ("model", "seconds", "size")
_VIDEO_EDIT_METADATA_PARAMS = ()          # edit takes only prompt + video
_VIDEO_EXTEND_METADATA_PARAMS = ("seconds",)
_VIDEO_REMIX_METADATA_PARAMS = ()
```

**New base class**, modelled on `_ImageBaseWrapper` (`tracing.py:1870`) but
**not** inheriting its `create`/`acreate` body, because the four methods differ
in which kwargs carry the prompt and the file:

- `_parse_params` reads `prompt`, routes the file kwarg (`input_reference` for
  `create`, `video` for `edit`/`extend`) through `_materialize_logged_file_input`
  (`tracing.py:260-265`), and filters metadata through
  `_filter_metadata` (`tracing.py:165-176`) with the per-method allowlist.
- `_log_result` merges `_timing_metrics` only — **not**
  `_parse_metrics_from_usage` (D5).
- `_extract_video_output` builds `{id, status, progress, size, seconds,
  model, error}` from the `Video` model. No `usage` key.

**Four concrete classes**, each passing its own name (D4):
`VideoCreateWrapper` → `"Video Generation"`, `VideoEditWrapper` →
`"Video Edit"`, `VideoExtendWrapper` → `"Video Extend"`, `VideoRemixWrapper` →
`"Video Remix"`.

**Four module-level wrapper callbacks**, one per class, matching the existing
`_image_generate_wrapper = _make_base_wrapper_callback(...)` form
(`tracing.py:2219-2221`).

Risk: `_try_to_dict` on a `Video` — confirm it handles the Stainless
`BaseModel` the way images does, and confirm `error` (`VideoCreateError`)
serializes without raising.

## Milestone 2 — patchers in `patchers.py`

Four `_make_method_patchers` calls (`patchers.py:33`) targeting
`openai.resources.videos.videos`:

| method | sync class | async class |
|---|---|---|
| `create` | `Videos` | `AsyncVideos` |
| `edit` | `Videos` | `AsyncVideos` |
| `extend` | `Videos` | `AsyncVideos` |
| `remix` | `Videos` | `AsyncVideos` |

Then `class VideosPatcher(CompositeFunctionWrapperPatcher)` with `name =
"openai.videos"` and all eight sub-patchers, plus `class _WrapVideos` with the
four instance patchers — mirroring `ImagesPatcher` / `_WrapImages`
(`patchers.py:347-363`).

**Both paths, or the guard test fails.** `test_wrap_openai_and_setup_use_same_wrappers`
(`test_openai.py:2787-2814`) asserts the wrapper sets from
`OpenAIIntegration.patchers` and `_WRAP_TARGETS` are equal.

- `integration.py` — add `VideosPatcher` to `OpenAIIntegration.patchers`
- `patchers.py:449` — add `("videos", _WrapVideos)` to `_WRAP_TARGETS`

Note the guard checks wrapper *identity*. Because D1 gives `create_and_poll` no
wrapper at all, there is no way for the guard to pass while the duplication bug
ships — that failure mode was specific to reusing `create`'s wrapper.

**`poll` gets its own patcher.** `Videos.poll` (`videos.py:165`, async `:706`) is
wrapped alongside `create`, so the two span independently. `create_and_poll` is
not in `_WRAP_TARGETS`' method list, and because it calls `self.create` and
`self.poll`, the wrapped inner calls still emit their own spans — the trace for
one `create_and_poll` call is two sibling spans, not a nested pair and not a
single 0.3s line.

## Milestone 3 — tests

**Version guard is mandatory.** The matrix runs 3.16.2 / 1.92.0 / 1.77.0 /
1.71.0 (`pyproject.toml:387-391`) and **only 3.16.2 has
`openai/resources/videos.py`** — verified by unzipping all four wheels. Under
CI, `record_mode="none"` (`src/braintrust/conftest.py:287`), so an unguarded
video test fails on the three old sessions with "cassette not found", not skip.

Use the existing precedent (`test_openai.py:1631`):

```python
pytest.importorskip("openai.resources.videos")
```

That skips before VCR engages, so no phantom cassette is required.

**Cassettes** go under `cassettes/latest/` only, named
`test_openai_videos_create.yaml` etc. Then run
`py/scripts/check-unused-cassettes.py` — the three always-skipping versions
must not produce cassette entries.

**Cases:**

1. `create` → one span, `name == "Video Generation"`, input has `prompt`,
   metadata has `model`/`seconds`/`size`, output has `id`/`status`, metrics
   has `duration` and **no** `tokens`/`cost`.
2. `create` with a reference image → `span["input"]["input_reference"]` is an
   `Attachment` with a `reference.content_type` (D3).
3. `edit` / `extend` → span name correct; **metadata is not empty-but-provider-only**
   — assert the exact expected keys, since D2 is the bug this guards.
4. `remix` → span name, output `remixed_from_video_id`.
5. `wrap_openai(client)` wraps all four (`inspect.getattr_static` on
   `client.videos`, same shape as `test_wrap_openai_wraps_images_methods`,
   `test_openai.py:2758-2767`).
6. `OpenAIIntegration.setup()` patchers resolve `Videos` / `AsyncVideos`
   (same shape as `test_openai.py:2771-2784`).
7. Regression: the existing guard test still passes.

## Milestone 4 — docs

Nothing enumerates instrumented methods — `docs/` holds only `publishing.md`
and `vcr-testing.md`; `generated_types.json` and `openapi/` describe the
Braintrust API, not the OpenAI SDK. So there is **no doc table to update**. The
only in-code staleness surface is the comment at `patchers.py:443-446`, which
already says to do both halves.

## Verification commands

```bash
cd ~/school-core/repos/braintrust-sdk-python/py
nox -s test_openai -- 3.16.2          # latest matrix cell, videos present
nox -s test_openai -- 1.92.0          # must SKIP cleanly, not fail
python scripts/check-unused-cassettes.py
```

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| `_try_to_dict` chokes on `Video.error` | low | test case 1 with a failed-status fixture |
| Attachment upload inflates span size | medium | reuse the audio/image path verbatim; no new behavior |
| Reviewer wants `create_and_poll` traced as a parent span | medium | D1 cites the `cursor_sdk` precedent for the same shape and was resolved by evidence, not taste. State it as a question in the PR: "is inner-pair tracing right here, or do you prefer a parent span?" Either answer is cheap — both spans already exist |
| Reviewer prefers `create_and_poll` skipped and `poll` unwrapped | low | that would restore the defect: a trace proving only that the request was accepted. Raise it in the PR if raised, but do not silently re-break |
| `check-unused-cassettes` flags skip-only versions | medium | `importorskip` before the VCR marker; run the checker |

## Out of scope

- CRUD (`retrieve`, `list`, `delete`, `download_content`) — note `poll` is *not*
  in this list; it is in scope under D1
- `create_character` / `get_character`
- `create_and_poll` as a wrapped method (D1) — its `create` and `poll` inner calls
  are each traced under their own names
- Cost/token metrics (D5)
- The Google GenAI equivalent (upstream #236)
