"""Tests for scripts/html_module_map.py (U2, Tier-2 module map).

Real subprocess + real files, no mocks.  Each test builds a small fixture
repo under tmp_path and runs the CLI from the repo root.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "html_module_map.py"


def run_cli(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(repo), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def build_fixture(root: Path) -> None:
    a = root / "a"
    b = root / "b"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    (a / "mod.py").write_text(
        "class Widget:\n    pass\n\n\ndef root_fn():\n    \"\"\"does a thing.\"\"\"\n    return 42\n"
    )
    (b / "util.py").write_text("def helper():\n    return True\n")
    (root / ".git").mkdir(parents=True)
    (root / "node_modules" / "x").mkdir(parents=True)
    (root / ".git" / "config").write_text("[core]\n")
    (root / "node_modules" / "x" / "index.js").write_text("export default 1\n")


def test_empty_repo_outputs_no_urls_no_ignored_dirs(tmp_path):
    repo = tmp_path / "fixture"
    build_fixture(repo)
    out = tmp_path / "out.html"

    proc = run_cli(repo, "--out", str(out))

    assert proc.returncode == 0, proc.stderr
    assert out.exists()
    content = out.read_text()

    assert 'class="module-layout"' in content
    assert 'class="request-path"' in content
    assert "Key files / gotchas" in content
    # self-contained: no external URLs at all
    assert "http://" not in content
    assert "https://" not in content
    # ignored dirs must not appear in the module layout section
    layout = content.split("class=\"module-layout\"", 1)[1]
    assert "node_modules" not in layout
    assert ".git" not in layout
    # module boxes for the fixture dirs are rendered
    assert ">a<" in content or "<div class='name'>a</div>" in content
    assert "<div class='name'>b</div>" in content


def test_symbol_highlights_defining_file(tmp_path):
    repo = tmp_path / "fixture"
    build_fixture(repo)
    out = tmp_path / "symbol.html"

    proc = run_cli(repo, "--symbol", "root_fn", "--out", str(out))

    assert proc.returncode == 0, proc.stderr
    content = out.read_text()
    assert "a/mod.py" in content
    assert "root_fn" in content


def test_symbol_highlight_marks_box(tmp_path):
    repo = tmp_path / "fixture"
    build_fixture(repo)
    out = tmp_path / "mark.html"

    proc = run_cli(repo, "--symbol", "Widget", "--out", str(out))

    assert proc.returncode == 0, proc.stderr
    content = out.read_text()
    assert "a/mod.py" in content
    # the box containing the defining file gets the symbol marker; the
    # box opened by that marker must be a's (it is the name right after
    # the class attribute closes)
    layout = content.split("class=\"module-layout\"", 1)[1]
    marked_at = layout.index("'box symbol'")
    remainder = layout[marked_at:]
    assert "<div class='name'>a</div>" in remainder[:200]


def test_nonexistent_path_fails_nonzero(tmp_path):
    missing = tmp_path / "does-not-exist"
    proc = run_cli(missing)

    assert proc.returncode != 0
    assert "not found" in proc.stderr
    assert str(missing) in proc.stderr


def test_default_output_under_docs_site(tmp_path):
    repo = tmp_path / "fixture"
    build_fixture(repo)

    proc = run_cli(repo)

    assert proc.returncode == 0, proc.stderr
    expected = repo / "docs" / "site" / "module-map.html"
    assert expected.is_file()
    assert 'class="module-layout"' in expected.read_text()