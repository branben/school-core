---
title: OpenAI video asset retention facts for Braintrust span payload design
date: 2026-09-30
context: braintrustdata/braintrust-sdk-python#304 — what to record on the span when instrumenting OpenAI video generation
sources: primary only (platform.openai.com docs + openai/openai-python source)
---

# OpenAI video asset retention — primary-source findings

All rows below are read from OpenAI's own docs (`platform.openai.com`, including the
`.md` markdown mirrors OpenAI publishes for each page) or from the generated source in
`openai/openai-python`. No blog posts, aggregators, or third-party tutorials were used.

## Findings table

| # | Question | Answer | Confidence | Source |
|---|---|---|---|---|
| 0 | **Does the Videos API still exist?** | **No — it was shut down 2026-09-24, six days before this memo's date.** Deprecation announced 2026-03-24; shutdown date `2026-09-24` for the Videos API and for `sora-2`, `sora-2-pro`, `sora-2-2025-10-06`, `sora-2-pro-2025-10-06`, `sora-2-2025-12-08`. "No one-to-one replacement API is available." The video-generation guide is retained "for historical reference." | MEASURED (primary) | https://platform.openai.com/docs/deprecations.md §`2026-03-24: Sora 2 video generation models and Videos API`; banner on https://platform.openai.com/docs/guides/video-generation.md |
| 0b | Does the SDK still ship it? | Yes, and every Sora method is decorated `@typing_extensions.deprecated("The Sora API is scheduled to permanently shut down on September 24, 2026.")` | MEASURED (primary) | `src/openai/resources/videos.py` (openai-python `main`) |
| Q1a | What does `expires_at` mean on the Video model? | "Unix timestamp (seconds) for when the downloadable assets expire, **if set**." It is `Optional[int] = None` — may be absent. Same wording in the API reference and in the SDK docstring. | MEASURED (primary) | `src/openai/types/video.py` (`expires_at: Optional[int] = None`); https://platform.openai.com/docs/api-reference/videos |
| Q1b | How long after creation does the video become unavailable (synchronous path)? | **The docs state a maximum of 1 hour.** Verbatim: "Download URLs are valid for a maximum of 1 hour after generation. If you need long-term storage, copy the file to your own storage system promptly." | MEASURED (primary) | https://platform.openai.com/docs/guides/video-generation.md §"Download the MP4" |
| Q1c | How long on the Batch path? | **24 hours after the batch completes.** "Batch-generated videos are available for download for up to `24` hours after the batch completes." | MEASURED (primary) | https://platform.openai.com/docs/guides/batch.md §`/v1/videos` |
| Q1d | Is the 1-hour/24-hour figure tied to `expires_at`, or is the field the authority? | **UNPROVEN linkage.** The docs give the durations as prose about *download URLs*; they never say "we populate `expires_at` to that value." INFERENCE (labelled): `expires_at` is the machine-readable form of the retention window, but I could not source that sentence. What would settle it: one live `GET /v1/videos/{id}` on a retired-API credential showing `expires_at - completed_at ≈ 3600`, or an OpenAI doc line tying the two together. Not obtainable now (see row 0). | UNPROVEN | — |
| Q1e | Is the video itself stored, or only the URL? | Ambiguous in the docs, and it matters. The 1-hour sentence is about *download URLs*; the 24-hour batch sentence is about *download availability*. Nothing states whether the underlying asset outlives the URL, and `expires_at`'s docstring scopes it to "downloadable assets." DELETE `/videos/{video_id}` exists "to remove videos you no longer need from OpenAI's storage," which implies storage persists beyond a download window but does not say for how long. | UNPROVEN | https://platform.openai.com/docs/guides/video-generation.md §"Maintain your library" |
| Q2 | Is there a stable, publicly fetchable URL? | **No.** Retrieval is exclusively the authenticated `GET /videos/{video_id}/content` with `Authorization: Bearer $OPENAI_API_KEY`. There is no CDN or public video URL anywhere in the guide or the API reference. Every example uses `-L "https://api.openai.com/v1/videos/video_abc123/content" -H "Authorization: ..."`. By contrast, images in the same guide are served from `cdn.openai.com` — the asymmetry is explicit. | MEASURED (primary) | https://platform.openai.com/docs/guides/video-generation.md; no `url` field exists on the `Video` model (`src/openai/types/video.py`) |
| Q3a | What does `download_content` return? | `HttpxBinaryResponseContent` — a binary/streaming response object, not JSON. Docstring: "Download the generated video bytes or a derived preview asset. Streams the rendered video content." Guide: "This endpoint streams the binary video data and returns standard content headers." Request sends `Accept: application/binary`. | MEASURED (primary) | `src/openai/resources/videos.py` `download_content`; https://platform.openai.com/docs/guides/video-generation.md |
| Q3b | `VideoSeconds` allowed values | `Literal["4", "8", "12"]`. `VideoCreateParams.seconds` docstring: "Clip duration in seconds (allowed values: 4, 8, 12). Defaults to 4 seconds." Note the *response* type is `Union[str, VideoSeconds]` — duration comes back as a **string**, not an int. | MEASURED (primary) | `src/openai/types/video_seconds.py`; `src/openai/types/video_create_params.py` |
| Q3c | `VideoSize` allowed values | `Literal["720x1280", "1280x720", "1024x1792", "1792x1024"]`. Create-param docstring: "Output resolution formatted as width x height (allowed values: 720x1280, 1280x720, 1024x1792, 1792x1024). Defaults to 720x1280." | MEASURED (primary) | `src/openai/types/video_size.py`; `src/openai/types/video_create_params.py` |
| Q3d | Documented byte/size cap on the download | **None.** No primary source states a maximum MP4 size or byte limit for `/videos/{id}/content`. | UNPROVEN (negative finding) | — |
| Q3e | **Docs/SDK conflict on duration and resolution** | The guide contradicts the SDK. Guide: "Use `sora-2-pro` when you need 1080p exports in `1920x1080` or `1080x1920`" and "Both `sora-2` and `sora-2-pro` support `16`- and `20`-second generations." Neither `1920x1080`, `1080x1920`, `"16"`, nor `"20"` appears in `VideoSize` / `VideoSeconds`. The guide's own Batch example posts `"size":"1080x1920","seconds":"16"` — values the SDK's Literals reject. **Do not derive a duration/resolution enum from the guide; the SDK Literals are what the wire format shipped in, and they are also the narrower of the two claims.** Whether the Literals were simply not regenerated is not stated anywhere. | MEASURED conflict (both sides primary) | guide §"Models" + Batch snippet vs. `src/openai/types/video_{size,seconds}.py` |
| Q4a | Is a thumbnail documented? | **Yes.** "For each completed video, you can also download a **thumbnail** and a **sprite sheet**. These are lightweight assets useful for previews, scrubbers, or catalog displays." They are **not** a field on the `Video` model — the model has no `thumbnail`/`url`/`poster` member. | MEASURED (primary) | https://platform.openai.com/docs/guides/video-generation.md §"Download supporting assets"; absence in `src/openai/types/video.py` |
| Q4b | How is it retrieved? | Same authenticated endpoint, `variant` query param: `GET /videos/{video_id}/content?variant=thumbnail` (→ `.webp`) or `?variant=spritesheet` (→ `.jpg`). Default `variant=video`. SDK signature: `download_content(video_id, *, variant: Literal["video","thumbnail","spritesheet"] = omit)`. | MEASURED (primary) | guide §"Download supporting assets"; `src/openai/resources/videos.py` |
| Q4c | Do thumbnails inherit the 1-hour/24-hour window? | **UNPROVEN.** They come off the same `/content` endpoint, so a shared expiry is the likely reading — that is INFERENCE, not sourced. The "download URLs are valid for a maximum of 1 hour" sentence sits immediately above the supporting-assets section and says "download URLs" generally, which is weak support but not a statement about thumbnails specifically. | UNPROVEN | — |

### Video model fields (complete, for span-payload reference)

From `src/openai/types/video.py` — `id`, `object` (`"video"`), `created_at`, `completed_at`,
`status` (`queued|in_progress|completed|failed`), `progress` (int %), `model`, `prompt`,
`seconds`, `size`, `error`, `expires_at`, `remixed_from_video_id`.
**Notably absent: any `url`, any `thumbnail`, any `bytes`/`size_bytes`.**

---

## What this means for the span payload

Three concrete shapes. No winner is picked here — that call is a human's, made separately.
What follows is the evidence each option rests on and what cuts against it.

### Option A — record a thumbnail reference (download it, attach as a Braintrust attachment)

*Shape:* at span end, `download_content(video_id, variant="thumbnail")`, attach the `.webp`,
log `image_size_bytes` + `mime_type` alongside the video metadata.

**Supports it**
- A thumbnail is a documented, first-class asset (Q4a) — you are not inventing a rendering of
  something. It exists precisely for "previews … and catalog displays," which is the span's job.
- It rides the **exact same `download_content` plumbing this repo already handles for images**
  (`braintrust/oai/tracing.py` ~1765-1795: log `url` as-is, materialize an Attachment only on
  inline base64). The incremental shape is `variant="thumbnail"`, not a new subsystem.
- A viewer that cannot play a 12s 1080p MP4 inline *can* show a still, so the span stays useful
  after the asset expires.
- Cost is bounded and small relative to the MP4 (Q4c does not threaten this; the "lightweight"
  framing is in the guide).

**Undermines it**
- **The endpoint is deprecated and dead as of 2026-09-24 (row 0).** Any code that calls
  `download_content` at runtime only runs against a retired API. If #304 is about the *current*
  API surface, this option is dead on arrival and the effort should go elsewhere.
- It is a **synchronous, network-touching, extra-latency** step inside span finalization — a new
  failure mode on the tracing hot path (API down, key bad, video still `in_progress` because the
  caller instrumented at `create` time, not at completion).
- `expires_at` is `Optional`, so a truncated window is possible in principle (Q1d — UNPROVEN).
- Cost: spans carrying image attachments are heavier than metadata-only spans. Braintrust's
  image precedent presumably assumed inline base64 from the API response, which is *not* what
  videos do (Q2) — you must fetch the thumbnail yourself.

### Option B — record the expiring URL/handle and nothing else

*Shape:* log `video_id`, `expires_at`, `seconds`, `size`, `status`, `model` — no bytes, no URL.

**Supports it**
- **There is no URL to record.** Q2 settles this: retrieval is authenticated-only, and the
  `Video` model has no `url` field. So "record the URL" is not on the table; the best this
  option can do is record *how to fetch it* — id + expiry.
- Cheapest span possible; no hot-path network call, no new failure modes, no storage decision.
- `video_id` + `expires_at` is a **complete, honest description of a handle with a known
  deadline** — replayable by anyone with a key, within 1 hour (Q1b) / 24 hours for Batch (Q1c).
- Consistent with the repo's own image rule: when the API gives you a reference, log the
  reference rather than materializing bytes (`tracing.py` ~1765-1795).

**Undermines it**
- The reference is **worthless in a week**. For a tracing product whose main value is
  retrospective debugging, a 1-hour pointer is close to a pointer to nothing — the span will
  outlive its referent by orders of magnitude. This is the strongest argument against B.
- It records *no* evidence of the output's actual content. A reviewer cannot tell whether the
  render succeeded correctly, only that the job reported `completed`.
- The window differs by path (1h sync vs 24h Batch, Q1b/Q1c), so a span consumer must know
  which one produced it — otherwise `expires_at` is misread as a promise.

### Option C — record metadata only, and explicitly mark the asset as not-captured

*Shape:* same fields as B, plus an explicit signal that no durable copy of the output exists
(e.g. a boolean/enum like `output_asset_ephemeral: true`, or documented span semantics saying
video outputs are not retained).

**Supports it**
- Fully consistent with the evidence: nothing durable exists to capture (Q2), the window is
  ~1 hour (Q1b), and `expires_at` is itself optional (Q1a).
- Zero risk of presenting an ephemeral reference as if it were durable — the failure mode that
  B invites.
- Keeps spans lightweight and the tracing path free of extra I/O.
- Pairs naturally with the existing precedent: the image path logs `image_size_bytes` and
  `mime_type` because it genuinely knows them; here it would log `seconds` and `size`, which
  are *declared request parameters* (Q3b/Q3c), plus `expires_at`.

**Undermines it**
- Delivers **no visual verification at all** — a regression in rendered output (wrong subject,
  wrong palette, corruption) leaves zero trace. For a media-generation integration that is a
  real observability hole, and reviewers may consider it under-instrumenting the point of #304.
- The honest signal is only useful if consumers know to look for it; a missing attachment is
  indistinguishable from an instrumentation bug.

### A note that outranks all three

Row 0 is a scheduling fact, not a design preference: **the Videos API and every Sora 2 model
shut down on 2026-09-24 with no one-to-one replacement** (https://platform.openai.com/docs/deprecations.md).
All the evidence above is now historical. Any option chosen here is being chosen for
(a) spans recorded *before* 2026-09-24 that are still queryable, and (b) whatever replaces the
Videos API, whose retention semantics are unknown and must not be assumed to match. It would be
a mistake to build Option A's network-touching download step against a retired endpoint.

---

## What remains unknown

1. **Whether `expires_at` is populated, and with what value.** The field is `Optional[int]` and
   the docs never state a duration for it. The 1-hour / 24-hour figures are stated about
   "download URLs" / "download availability" in prose, never about the field. (Q1d, UNPROVEN)
2. **Whether the asset outlives the download URL.** The 1-hour sentence is scoped to URLs; whether
   OpenAI retains the rendered MP4 server-side beyond that is unstated. (Q1e, UNPROVEN)
3. **Whether thumbnails/spritesheets share the same expiry window** as the MP4. (Q4c, UNPROVEN)
4. **Any byte-size ceiling** on `GET /videos/{video_id}/content`. No primary source states one. (Q3d)
5. **Which of the docs or the SDK is right about 16s/20s and 1920x1080/1080x1920.** Both are
   primary, they conflict, and no source resolves it. Note the *response* model is
   `Union[str, VideoSeconds]` for `seconds`, so the wire format is looser than the Literal
   either way. (Q3e)
6. **Whether any third-party / successor video API will exist**, and whether its retention model
   differs. The deprecation notice says only "---" (no recommended replacement) for the Videos API.
7. Anything I could only source from a search-result *snippet* rather than the page body. I
   re-fetched every claim above as a full `.md` page or raw SDK file; nothing here rests on a snippet.

**How to settle the unknowns:** with the API retired, live measurement is no longer available.
The remaining route is OpenAI's dated changelog/blog announcement for the 2026-03-24 deprecation,
or a first-party support answer, which would state whether assets already generated survive the
shutdown and for how long. Absent that, treat every UNPROVEN row as unknown rather than assuming
the 1-hour figure generalizes.