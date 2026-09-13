"""Tests for scripts/html_render_md.py (Tier-3 markdown -> HTML stub).

Runs the script as a subprocess from the repo root, per the U3 contract.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "html_render_md.py"

FIXTURE_MD = """---
title: "Fixture plan title"
---
# Fixture Heading One

## Section Two

Intro paragraph with **bold** and inline code `var`.

- bullet one
- bullet two

* bullet three

1. first ordered
2. second ordered
3. third ordered

## Section With Table

| Name | Value |
|---|---|
| alpha | 1 |
| beta | 2 |

## Section With Code

```python
x = "<script>alert(1)</script>"
print("hi")
```

## Section With Quote

> This is a blockquote note.

## Unsupported Constructs

Alpha term : definition line here

## Final Section

Plain tail paragraph.
"""

def _run(args, cwd=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT)] + args,
        cwd=cwd or str(REPO_ROOT),
        capture_output=True,
        text=True,
    )


def _extract_atx_heading_texts(md_text):
    texts = []
    for line in md_text.splitlines():
        stripped = line.strip()
        if len(stripped) >= 2 and stripped[0] == "#":
            match = __import__("re").match(r"^#{1,6}\s+(.*)$", stripped)
            if match and match.group(1).strip():
                texts.append(match.group(1).strip())
    return texts


def _render(tmp_path):
    in_path = tmp_path / "fixture.md"
    in_path.write_text(FIXTURE_MD, encoding="utf-8")
    out_path = tmp_path / "out.html"
    result = _run([str(in_path), "--template", "spec", "--out", str(out_path)])
    assert result.returncode == 0, result.stderr
    html_text = out_path.read_text(encoding="utf-8")
    return in_path, out_path, html_text, result


def test_missing_input_fails(tmp_path):
    missing = tmp_path / "does-not-exist.md"
    result = _run([str(missing), "--out", str(tmp_path / "x.html")])
    assert result.returncode != 0
    assert result.stderr.strip() != ""
    assert "not found" in result.stderr.lower()


def test_every_heading_preserved_as_dom_element(tmp_path):
    _, _, html_text, _ = _render(tmp_path)
    headings = _extract_atx_heading_texts(FIXTURE_MD)
    assert len(headings) >= 5
    for title in headings:
        assert title in html_text
    ids = html_text.count('id="sec-')
    assert ids == len(headings)


def test_unsupported_construct_renders_as_raw_and_kept(tmp_path):
    _, _, html_text, _ = _render(tmp_path)
    assert 'class="raw"' in html_text
    assert "raw-title" in html_text
    assert "unsupported markdown" in html_text
    assert "Alpha term : definition line here" in html_text


def test_no_external_resource_refs(tmp_path):
    _, _, html_text, _ = _render(tmp_path)
    assert "http://" not in html_text
    assert "https://" not in html_text
    assert "src=" not in html_text


def test_script_injection_is_escaped(tmp_path):
    _, _, html_text, _ = _render(tmp_path)
    assert "&lt;script&gt;" in html_text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_text
    assert "<script>" not in html_text
    assert "<script>alert" not in html_text
    assert 'onerror=' not in html_text


def test_source_meta_present(tmp_path):
    in_path, _, html_text, _ = _render(tmp_path)
    assert '<meta name="source" content="' in html_text
    assert in_path.name in html_text


def test_output_deterministic(tmp_path):
    in_path = tmp_path / "fixture.md"
    in_path.write_text(FIXTURE_MD, encoding="utf-8")
    out1 = tmp_path / "one.html"
    out2 = tmp_path / "two.html"
    r1 = _run([str(in_path), "--template", "spec", "--out", str(out1)])
    r2 = _run([str(in_path), "--template", "spec", "--out", str(out2)])
    assert r1.returncode == 0 and r2.returncode == 0
    assert out1.read_bytes() == out2.read_bytes()