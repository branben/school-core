#!/usr/bin/env python3
"""Tier-2 module-map generator for the Prime step (stdlib only).

Renders a single self-contained ``.html`` with a module-layout view, a
request-path sketch, and a key-files/gotchas skeleton.  This is a *starter*:
it draws the spatial frame (module boxes/arrows) and leaves the "why" to the
agent.

Usage:
    python3 scripts/html_module_map.py <repo-path> [--symbol SYMBOL] [--out OUT]

Default output: <repo>/docs/site/module-map.html (parents created).
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
from pathlib import Path

CODE_SUFFIXES = (".py", ".js", ".ts", ".tsx")

# Directory names that never participate in the module surface.
SKIP_DIR_NAMES = {
    ".git",
    ".venv",
    "node_modules",
    "data",
    "__pycache__",
    ".pytest_cache",
    ".beads",
}

IMPORT_RE = re.compile(r"^import (\w+)|^from (\w+)")

MAX_ENTRY_FILES = 8
MAX_KEY_FILES = 3
MAX_BOX_FILES = 5


# ---------------------------------------------------------------------------
# repo scanning
# ---------------------------------------------------------------------------

def skip_dir(rel_parent: Path, name: str) -> bool:
    """Should directory ``name`` (child of the module at rel ``rel_parent``)
    be excluded from scanning?  Hidden dirs are never module surface."""
    if name in SKIP_DIR_NAMES:
        return True
    if name.startswith("."):
        return True
    if rel_parent.name == "docs" and name == "site":
        return True
    return False


def iter_code_files(root: Path):
    """Yield (abs_path, rel_path) for every code file under root,
    descending to any depth but skipping excluded directories."""
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        dirnames[:] = sorted(d for d in dirnames if not skip_dir(rel, d))
        for fname in sorted(filenames):
            if fname.endswith(CODE_SUFFIXES):
                yield Path(dirpath) / fname, rel / fname


def top_level_boxes(root: Path) -> list[Path]:
    return sorted(
        d for d in root.iterdir()
        if d.is_dir() and not skip_dir(Path("."), d.name)
    )


def py_files_under(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        dirnames[:] = sorted(d for d in dirnames if not skip_dir(rel, d))
        for fname in sorted(filenames):
            if fname.endswith(".py"):
                files.append(Path(dirpath) / fname)
    return files


def sorted_by_size(files: list[Path]) -> list[Path]:
    return sorted(files, key=lambda p: p.stat().st_size, reverse=True)


def repo_module_names(root: Path) -> set[str]:
    """Names that count as "repo-local" for import cross-referencing."""
    names = {d.name for d in top_level_boxes(root)}
    for _, rel in iter_code_files(root):
        if rel.suffix == ".py":
            names.add(rel.stem)
    return names


def local_imports(path: Path, local_names: set[str]) -> list[str]:
    """Repo-local module names imported by ``path`` (heuristic, cheap)."""
    found = set()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        m = IMPORT_RE.match(line)
        if m:
            found.add(m.group(1) or m.group(2))
    return sorted(n for n in found if n in local_names)


def find_symbol(root: Path, symbol: str) -> list[tuple[Path, str]]:
    """(rel_path, top-level box) for every file defining ``symbol``."""
    esc = re.escape(symbol)
    patterns = [
        re.compile(rf"^\s*def\s+{esc}\b", re.MULTILINE),
        re.compile(r"^\s*class\s+" + esc + r"\b", re.MULTILINE),
        re.compile(r"^\s*function\s+" + esc + r"\b", re.MULTILINE),
        re.compile(r"^\s*export\b[^\n]*\b" + esc + r"\b", re.MULTILINE),
    ]
    hits = []
    for path, rel in iter_code_files(root):
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(p.search(content) for p in patterns):
            box = rel.parts[0] if len(rel.parts) > 1 else "(root)"
            hits.append((rel, box))
    return hits


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

CSS = """\
  :root{
    --bg:#0a0a0f; --panel:#111118; --panel2:#1a1a24; --border:#222230;
    --text:#e8e6e3; --muted:#8a8694; --accent:#c8ff00; --blue:#5b8def;
  }
  *{box-sizing:border-box; margin:0;}
  body{
    background:var(--bg); color:var(--text);
    font-family:-apple-system, 'Segoe UI', Roboto, sans-serif;
    padding:2rem clamp(1rem, 3vw, 3rem); line-height:1.5;
  }
  header{margin-bottom:2rem; border-bottom:1px solid var(--border); padding-bottom:1rem;}
  h1{font-size:1.35rem; font-weight:650; letter-spacing:-.01em;}
  h1 .accent{color:var(--accent);}
  header p{color:var(--muted); font-size:.82rem; margin-top:.35rem;}
  .meta{font-family:ui-monospace, SFMono-Regular, Menlo, monospace; font-size:.7rem; color:var(--muted);}
  section{margin-bottom:2.5rem;}
  section > h2{
    font-size:.78rem; text-transform:uppercase; letter-spacing:.09em;
    color:var(--muted); border-bottom:1px solid var(--border);
    padding-bottom:.4rem; margin-bottom:1rem;
  }
  .symbol-note{
    background:var(--panel); border:1px solid var(--border); border-radius:8px;
    padding:.6rem .8rem; font-size:.82rem; margin-bottom:1rem;
  }
  .symbol-note b{color:var(--accent); font-weight:600;}
  .grid{display:grid; grid-template-columns:repeat(auto-fill, minmax(220px, 1fr)); gap:.8rem;}
  .box{background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:.8rem .9rem;}
  .box.symbol{border-color:var(--accent); box-shadow:0 0 0 1px var(--accent) inset;}
  .box .name{font-weight:650; font-size:.95rem;}
  .box.symbol .name{color:var(--accent);}
  .box .stats{font-size:.7rem; color:var(--muted); margin:.15rem 0 .5rem;}
  .box ul{list-style:none;}
  .box li{font-family:ui-monospace, SFMono-Regular, Menlo, monospace; font-size:.72rem; color:var(--muted);}
  .box li .hit{color:var(--accent);}
  .module{
    background:var(--panel); border:1px solid var(--border); border-radius:10px;
    padding:.9rem 1rem; margin-bottom:1rem;
  }
  .module h3{font-size:.85rem; font-weight:650; margin-bottom:.4rem;}
  .module h3 span{color:var(--muted); font-weight:400; font-size:.72rem;}
  table{border-collapse:collapse; width:100%; font-size:.78rem;}
  th,td{text-align:left; padding:.28rem .5rem; border-bottom:1px solid var(--border);}
  th{color:var(--muted); font-size:.68rem; text-transform:uppercase; letter-spacing:.05em;}
  td.fn{font-family:ui-monospace, SFMono-Regular, Menlo, monospace; color:var(--blue);}
  td.n{color:var(--muted); text-align:right;}
  .arrows{margin-top:.6rem; display:flex; flex-wrap:wrap; gap:.4rem .8rem;}
  .arrows .arrow{
    font-family:ui-monospace, SFMono-Regular, Menlo, monospace; font-size:.74rem;
    background:var(--panel2); border:1px solid var(--border); border-radius:6px;
    padding:.3rem .6rem; color:var(--muted);
  }
  .arrows .arrow .from{color:var(--text);}
  .arrows .arrow .go{color:var(--accent);}
  .arrows .arrow .imp{color:var(--blue);}
  .empty{color:var(--muted); font-size:.78rem; font-style:italic;}
  .key-table td.fn .hit2{color:var(--accent);}
  .gotchas{list-style:none;}
  .gotchas li{
    background:var(--panel); border:1px dashed var(--border); border-radius:8px;
    padding:.6rem .8rem; margin-bottom:.5rem; font-size:.8rem;
  }
  .gotchas li .ph{color:var(--accent);}
  .note{color:var(--muted); font-size:.74rem; margin-top:.8rem; font-style:italic;}
  @media (max-width: 640px){ body{padding:1rem;} }
"""


def esc(value: object) -> str:
    return html.escape(str(value))


def file_size_kb(path: Path) -> float:
    try:
        return path.stat().st_size / 1024.0
    except OSError:
        return 0.0


def render(root: Path, symbol: str | None) -> str:
    repo_name = root.name

    modules = []  # list of (box_name, module_dir, py_files_sorted)
    root_pys = sorted_by_size(
        [p for p in root.iterdir() if p.is_file() and p.suffix == ".py"]
    )
    if root_pys:
        modules.append(("(root)", root, root_pys))
    for d in top_level_boxes(root):
        pys = sorted_by_size(py_files_under(d))
        if pys:
            modules.append((d.name, d, pys))

    local_names = repo_module_names(root)

    hit_box_names: set[str] = set()
    symbol_rows: list[str] = []
    if symbol:
        hits = find_symbol(root, symbol)
        symbol_rows = [
            "<tr><td class='fn'>{0}</td><td>{1}</td></tr>".format(esc(rel), esc(box))
            for rel, box in hits
        ]
        hit_box_names = {box for _, box in hits}

    # --- module layout boxes -------------------------------------------------
    box_parts = []
    for box_name, mod_dir, pys in modules:
        total_kb = sum(file_size_kb(p) for p in pys)
        marker = " symbol" if symbol and box_name in hit_box_names else ""
        names = [
            "<li>{0}</li>".format(esc(p.relative_to(mod_dir)))
            for p in pys[:MAX_BOX_FILES]
        ]
        box_parts.append(
            "<div class='box{3}'><div class='name'>{0}</div>"
            "<div class='stats'>{1} py · {2:.0f} KB</div><ul>{4}</ul></div>".format(
                esc(box_name), len(pys), total_kb, marker, "".join(names)
            )
        )

    symbol_note = ""
    if symbol:
        if hits := find_symbol(root, symbol):
            files = ", ".join(f"{esc(rel)} ({esc(box)})" for rel, box in hits)
            symbol_note = (
                "<div class='symbol-note'>Root symbol <b>{0}</b> resolves to: "
                "{1}</div>".format(esc(symbol), files)
            )
        else:
            symbol_note = (
                "<div class='symbol-note'>Root symbol <b>{0}</b> was not found "
                "in any .py/.js/.ts/.tsx file.</div>".format(esc(symbol))
            )

    # --- request-path view ----------------------------------------------------
    req_parts = []
    for box_name, mod_dir, pys in modules:
        rows = []
        for p in pys[:MAX_ENTRY_FILES]:
            rows.append(
                "<tr><td class='fn'>{0}</td><td class='n'>{1:.1f} KB</td></tr>".format(
                    esc(p.relative_to(mod_dir)), file_size_kb(p)
                )
            )
        arrows = []
        for p in sorted_by_size(pys):
            if p.name == "cli.py":
                imports = local_imports(p, local_names)
                arrows.append(
                    "<span class='arrow'><span class='from'>{0}</span> "
                    "<span class='go'>--&gt;</span> {1}</span>".format(
                        esc(p.relative_to(mod_dir)),
                        ", ".join(
                            "<span class='imp'>{0}</span>".format(esc(i))
                            for i in imports
                        )
                        or "<span class='imp'>-</span>",
                    )
                )
        total_kb = sum(file_size_kb(p) for p in pys)
        req_parts.append(
            "<article class='module'><h3>{0} <span>({1} py · {2:.0f} KB total)</span></h3>"
            "<table><thead><tr><th>entry modules (largest first)</th>"
            "<th class='n'>size</th></tr></thead><tbody>{3}</tbody></table>"
            "<div class='arrows'>{4}</div></article>".format(
                esc(box_name), len(pys), total_kb, "".join(rows), "".join(arrows)
            )
        )
    req_body = "".join(req_parts) or (
        "<p class='empty'>No top-level modules with .py files found.</p>"
    )

    # --- key files / gotchas --------------------------------------------------
    key_parts = []
    for box_name, mod_dir, pys in modules:
        largest = pys[:MAX_KEY_FILES]
        inits = sorted(p for p in py_files_under(mod_dir) if p.name == "__init__.py")
        if box_name == "(root)":
            inits = sorted(p for p in root.iterdir()
                           if p.is_file() and p.name == "__init__.py")
        rows = []
        for p in largest:
            rows.append(
                "<tr><td class='fn'>{0}</td><td class='n'>{1:.1f} KB</td>"
                "<td>largest</td></tr>".format(
                    esc(p.relative_to(mod_dir)), file_size_kb(p)
                )
            )
        for p in inits:
            rows.append(
                "<tr><td class='fn'>{0}</td><td class='n'>-</td>"
                "<td>__init__.py</td></tr>".format(esc(p.relative_to(mod_dir)))
            )
        if rows:
            key_parts.append(
                "<h3>{0}</h3><table class='key-table'><thead><tr>"
                "<th>file</th><th class='n'>size</th><th>why</th></tr></thead>"
                "<tbody>{1}</tbody></table>".format(esc(box_name), "".join(rows))
            )
    key_body = "".join(key_parts) or (
        "<p class='empty'>No python files found under the repo surface.</p>"
    )

    gotchas = [
        "<li><span class='ph'>FILLED BY THE AGENT 1:</span> "
        "biggest known surprise / gotcha in {0} (config, data layout, "
        "module boundary, hidden state).</li>".format(esc(repo_name)),
        "<li><span class='ph'>FILLED BY THE AGENT 2:</span> "
        "the decision that will bite a newcomer first (import cycle, "
        "filesystem coupling, ordering assumption).</li>",
        "<li><span class='ph'>FILLED BY THE AGENT 3:</span> "
        "anything that looks like a bug but is load-bearing behavior.</li>",
    ]

    html_doc = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>module map · {repo}</title>
<style>
{css}
</style>
</head>
<body>
<header>
  <h1>Module map · <span class="accent">{repo}</span>{sym_hdr}</h1>
  <p class="meta">generated by scripts/html_module_map.py (Tier-2 starter: spatial frame only, "why" left to the agent)</p>
</header>

<section class="module-layout" id="module-layout">
  <h2>Module layout</h2>
  {symbol_note}
  <div class="grid">
    {boxes}
  </div>
</section>

<section class="request-path" id="request-path">
  <h2>Request-path view</h2>
  {request_path}
</section>

<section class="key-files" id="key-files">
  <h2>Key files / gotchas</h2>
  <h3>Key files (largest .py per module + __init__.py)</h3>
  {key_body}
  <h3>Gotchas</h3>
  <ul class="gotchas">
    {gotchas}
  </ul>
  <p class="note">Skeleton only: these slots are filled by the agent during Prime, never auto-generated.</p>
</section>

</body>
</html>
""".format(
        repo=esc(repo_name),
        sym_hdr=" · --symbol " + esc(symbol) if symbol else "",
        css=CSS,
        symbol_note=symbol_note,
        boxes="\n    ".join(box_parts),
        request_path=req_body,
        key_body=key_body,
        gotchas="\n    ".join(gotchas),
    )
    return html_doc


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="html_module_map.py",
        description="Generate a Tier-2 module-map HTML from a repo (stdlib only).",
    )
    parser.add_argument("repo", help="path to the repository to map")
    parser.add_argument("--symbol", default=None,
                        help="root symbol to locate (def/class/function/export)")
    parser.add_argument("--out", default=None,
                        help="output HTML path (default: <repo>/docs/site/module-map.html)")
    args = parser.parse_args(argv)

    root = Path(args.repo)
    if not root.exists():
        parser.error("repository path not found: {0}".format(root))
    if not root.is_dir():
        parser.error("repository path is not a directory: {0}".format(root))

    if args.out:
        out = Path(args.out)
        if not out.is_absolute():
            out = Path.cwd() / out
    else:
        out = root / "docs" / "site" / "module-map.html"
    out.parent.mkdir(parents=True, exist_ok=True)

    rendered = render(root, symbol=args.symbol)
    out.write_text(rendered, encoding="utf-8")
    print("wrote {0} ({1} bytes)".format(out, len(rendered.encode("utf-8"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())