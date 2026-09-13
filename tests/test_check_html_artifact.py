"""Tests for scripts/check_html_artifact.py (U4 static lint gate).

Real subprocess runs from the repo root with fixtures under tmp_path.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_html_artifact.py"

GOOD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="source" content="docs/plans/x.md">
<title>Plan</title>
<style>
  body { margin: 0; }
</style>
</head>
<body>
<header><h1>Plan</h1></header>
<main><p>This plan is self-contained.</p></main>
<footer><p>&copy; 2026</p></footer>
</body>
</html>
"""


def run_cli(file_args, *flags, cwd=None):
    cmd = [sys.executable, str(SCRIPT), *flags, *file_args]
    return subprocess.run(
        cmd,
        cwd=cwd or REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_good_doc_passes(tmp_path):
    good = write(tmp_path / "good.html", GOOD_HTML)
    result = run_cli([str(good)])
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"OK {good}" in result.stdout


def test_external_script_url_fails(tmp_path):
    bad = write(
        tmp_path / "bad.html",
        '<script src="https://external.example/x.js"></script>\n' + GOOD_HTML,
    )
    result = run_cli([str(bad)])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAIL" in result.stdout
    assert "https://external.example/x.js" in result.stdout


def test_tier3_requires_canonical_source(tmp_path):
    no_source = write(
        tmp_path / "no_source.html",
        GOOD_HTML.replace('<meta name="source" content="docs/plans/x.md">\n', ""),
    )
    result = run_cli([str(no_source)], "--tier", "3")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "canonical source" in result.stdout


def test_tier2_allows_missing_source_meta(tmp_path):
    no_source = write(
        tmp_path / "no_source.html",
        GOOD_HTML.replace('<meta name="source" content="docs/plans/x.md">\n', ""),
    )
    result = run_cli([str(no_source)])
    assert result.returncode == 0, result.stdout + result.stderr


def test_wide_inline_style_warns_but_passes(tmp_path):
    wide = write(
        tmp_path / "wide.html",
        '<div style="min-width: 1400px">\n' + GOOD_HTML,
    )
    result = run_cli([str(wide)])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARN" in result.stderr
    assert "min-width" in result.stderr


def test_ok_and_fail_lines_in_one_invocation(tmp_path):
    good = write(tmp_path / "good.html", GOOD_HTML)
    bad = write(
        tmp_path / "bad.html",
        '<img src="http://cdn.example.com/x.png">\n' + GOOD_HTML,
    )
    result = run_cli([str(good), str(bad)])
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"OK {good}" in result.stdout
    assert f"FAIL {bad}" in result.stdout


def test_missing_file_returns_2(tmp_path):
    missing = tmp_path / "does_not_exist.html"
    result = run_cli([str(missing)])
    assert result.returncode == 2, result.stdout + result.stderr
    assert str(missing) in result.stderr