# SCH-29 — Hosted adapter: surface bounded, sanitized provider error bodies

**Date:** 2026-10-04
**Reviewer:** Phymora (adversarial)
**Branch:** `school/sch-20-seam-hygiene`
**Verdict:** ship

## Intent

SCH-13 finding B: `_json_request` raises `_CloudApiStatusError` with only
status+path (`smol_cloud_runner.py:486`); the response body is discarded.
Every hosted failure is undiagnosable without a manual curl. Fix: attach a
bounded, sanitized excerpt of the provider error body to the exception and
the journal detail, with the same secret-safety discipline as the rest of
the seam.

## What changed

### `smol_cloud_runner.py`

1. **`_sanitize_error_body(body: bytes) -> str`** — new helper. Parses JSON,
   extracts `error`/`message`/`detail` field, falls back to raw text. Bounds
   to 2000 chars. Redacts `smk_*` keys and `Bearer` tokens.

2. **`_CloudApiStatusError`** — now carries a `detail: str` attribute. The
   message includes the detail when present.

3. **`_json_request`** — passes `_sanitize_error_body(response.body)` as the
   `detail` argument to `_CloudApiStatusError`.

4. **`_upload`, `_download`, `_delete`** — error messages now include the
   sanitized body excerpt.

### `tests/test_smol_cloud_hosted_probes.py`

5. **`RecordingTransport`** — journal entries for HTTP >= 400 now include
   an `error_detail` key with the sanitized body.

### `tests/test_smol_cloud_runner.py`

6. **12 new tests** covering:
   - `_sanitize_error_body`: JSON extraction, message fallback, raw text,
     length bound, API key redaction, Bearer redaction, empty body, malformed JSON
   - `_CloudApiStatusError.detail` carries the sanitized body
   - `_upload`, `_download`, `_delete` errors include the sanitized body

## Verification

- `python3 -m pytest tests/test_smol_cloud_runner.py -q` → **78 passed**
- All 12 new tests pass
- No regressions in existing tests

## Secret safety

- `smk_*` keys redacted via `re.sub(r"smk_[A-Za-z0-9]+", "[REDACTED]", text)`
- `Bearer` tokens redacted via `re.sub(r"Bearer\s+\S+", "Bearer [REDACTED]", text)`
- Length bounded to 2000 chars before redaction
- Journal `error_detail` uses the same `_sanitize_error_body` function

## Simpler alternative considered

**Do nothing** — the operator can always curl the API manually. Rejected: the
whole point of the hosted qualification is to produce diagnosable evidence
without manual intervention. The error body is the primary diagnostic artifact.

**Log only, don't raise** — attach the body to a log message instead of the
exception. Rejected: the exception is the contract; callers catch
`StudentVMBlocked` and need the detail to classify the failure.

## Not claimed

- No provider/GitHub write
- Branch not pushed
- No PR
- Production student coding stays disabled
