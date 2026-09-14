#!/usr/bin/env python3
"""Tier-3 markdown -> self-contained HTML renderer stub (stdlib only).

Conservative, deliberately NOT a full CommonMark engine. Supported
constructs: ATX headings (#..######), unordered lists (-, *), ordered
lists, pipe tables (|...| rows with a |---| separator), fenced code
blocks (``` ... ```), blockquotes (>), bold (**..**), and inline code
(`..`). Everything else falls through to a visibly-marked "raw" block so
unsupported construct content is never silently dropped.

Canonical source discipline: the .md file in git is authoritative; the
generated .html is a disposable view of it. Output is stable for a given
input (no timestamps), so regenerated files diff cleanly.
"""

import argparse
import html
import re
import sys
from pathlib import Path

TEMPLATES = ("spec", "prd-pitch", "pr-writeup")

ATX_RE = re.compile(r"^#{1,6}\s+\S.*$")
UL_BULLET_RE = re.compile(r"^[ \t]*[-*][ \t]+(.*)$")
OL_ITEM_RE = re.compile(r"^[ \t]*\d+[.)][ \t]+(.*)$")
BLOCKQUOTE_RE = re.compile(r"^>[ \t]?(.*)$")

# Signals that a block uses a markdown construct OUTSIDE the supported set.
# Such blocks are rendered as a visibly-marked "raw" block instead of a plain
# paragraph, so nothing is silently dropped and none of it is misparsed.
UNSUPPORTED_MARKERS = [
    re.compile(r"~~"),                       # strikethrough
    re.compile(r"^[^:\n]{1,40}\s:\s.*$"),    # definition-list ("Term : definition")
    re.compile(r"^[-*_]{3,}\s*$"),           # horizontal rule
]

STYLE = """
  :root{
    --bg:#0a0a0f; --panel:#111118; --panel2:#1a1a24; --border:#222230;
    --text:#e8e6e3; --muted:#8a8694; --accent:#c8ff00; --blue:#5b8def;
    --green:#22c55e; --amber:#f59e0b; --red:#ef4444; --card:#16161e;
  }
  *{box-sizing:border-box; margin:0;}
  body{
    background:var(--bg); color:var(--text);
    font-family:-apple-system,'Segoe UI',sans-serif;
    line-height:1.55; padding:2rem clamp(1rem,3vw,3rem);
    max-width:980px; margin:0 auto;
  }
  header{margin-bottom:2rem; border-bottom:1px solid var(--border); padding-bottom:1.25rem;}
  header h1{font-size:1.4rem; font-weight:650; letter-spacing:-.01em;}
  header .subtitle{color:var(--muted); font-size:.85rem; margin-top:.35rem;}
  .meta{
    font-family:ui-monospace,'SFMono-Regular',monospace; font-size:.7rem;
    color:var(--muted); display:flex; gap:.5rem; flex-wrap:wrap; margin-top:.75rem;
  }
  .meta span{background:var(--panel); border:1px solid var(--border); border-radius:6px; padding:.2rem .5rem;}
  main{display:grid; grid-template-columns:minmax(0,1fr) 230px; gap:2.5rem; align-items:start;}
  article{min-width:0;}
  section{margin-bottom:1.6rem;}
  section h2{
    font-size:.78rem; text-transform:uppercase; letter-spacing:.1em;
    margin-bottom:.7rem; padding-bottom:.35rem; border-bottom:1px solid var(--border);
  }
  section h3{font-size:1rem; font-weight:600; margin:1.1rem 0 .45rem;}
  h4{font-size:.9rem; font-weight:600; margin:1rem 0 .4rem;}
  h5,h6{font-size:.84rem; font-weight:600; margin:.9rem 0 .35rem;}
  article h2[id],article h3[id],article h4[id],article h5[id],article h6[id]{scroll-margin-top:1rem;}
  p,li{color:#d5d2cd; font-size:.92rem;}
  p{margin-bottom:.7rem;}
  ul,ol{padding-left:1.3rem; margin-bottom:.9rem;}
  li{margin-bottom:.3rem;}
  code{
    font-family:ui-monospace,'SFMono-Regular',monospace; font-size:.82em;
    background:var(--panel2); border:1px solid var(--border); border-radius:4px;
    padding:.05rem .3rem; word-break:break-word;
  }
  pre{
    font-family:ui-monospace,'SFMono-Regular',monospace; font-size:.78rem; line-height:1.6;
    background:#0c0c12; border:1px solid var(--border); border-radius:8px;
    padding:.8rem; white-space:pre-wrap; word-break:break-word; color:#c9c6cf;
  }
  pre code{background:none; border:0; padding:0; font-size:inherit;}
  blockquote{
    margin:.75rem 0; padding:.5rem .9rem; border-left:3px solid var(--blue);
    background:var(--panel); border-radius:0 8px 8px 0; color:var(--muted); font-size:.9rem;
  }
  blockquote p{margin:0; color:var(--muted);}
  .table-wrap{margin:.75rem 0 1rem; overflow-x:auto; border:1px solid var(--border); border-radius:8px;}
  table{border-collapse:collapse; font-size:.85rem; width:100%;}
  th{text-align:left; color:var(--muted); font-size:.7rem; text-transform:uppercase; letter-spacing:.06em;}
  th,td{border-bottom:1px solid var(--border); padding:.5rem .6rem; vertical-align:top;}
  tr:last-child td{border-bottom:none;}
  .raw{
    background:#101018; border:1px dashed var(--amber); border-left:3px solid var(--amber);
    border-radius:8px; padding:.7rem .9rem; margin:.7rem 0; color:#d9d0c0;
  }
  .raw-title{color:var(--amber); font-size:.66rem; text-transform:uppercase; letter-spacing:.08em; margin-bottom:.35rem;}
  .raw pre{
    background:transparent; border:0; border-radius:0; padding:0; margin:0;
    color:#d9d0c0; font-size:.78rem; white-space:pre-wrap; word-break:break-word;
  }
  nav.toc{
    position:sticky; top:1rem; background:var(--panel); border:1px solid var(--border);
    border-radius:10px; padding:.7rem .8rem; font-size:.72rem;
  }
  nav.toc h2{font-size:.62rem; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); margin-bottom:.5rem; border:none; padding:0;}
  nav.toc ol{list-style:none; padding:0; margin:0;}
  nav.toc li{margin:.15rem 0;}
  nav.toc a{color:var(--muted); text-decoration:none;}
  nav.toc a:hover{color:var(--accent);}
  nav.toc .lvl2{padding-left:0;}
  nav.toc .lvl3{padding-left:.85rem;}
  nav.toc .lvl4{padding-left:1.7rem;}
  nav.toc .lvl5{padding-left:2.55rem;}
  nav.toc .lvl6{padding-left:2.55rem;}
  .footer{border-top:1px solid var(--border); margin-top:2.5rem; padding-top:1rem; color:var(--muted); font-size:.75rem;}
  @media (max-width: 900px){
    main{grid-template-columns:1fr; gap:1rem;}
    nav.toc{position:static; order:-1;}
  }
  @media (max-width: 640px){
    body{padding:1rem;}
    header h1{font-size:1.15rem;}
  }
"""

TEMPLATE_CSS = {
    "spec": """
  section h2{color:var(--accent);}
  table{font-size:.8rem;}
""",
    "prd-pitch": """
  section h2{color:var(--purple);}
  blockquote{background:transparent;}
""",
    "pr-writeup": """
  section h2{color:var(--blue);}
  section h3{color:#c9a6ff;}
""",
}

TEMPLATE_NOTE = {
    "spec": "spec template — implementation-plan register: commitments, risks and "
            "milestones keep the source's decisions visible; open questions stay open.",
    "prd-pitch": "prd-pitch template — demo → pitch → objections order mirrors the "
                 "source's heading order; nothing is reordered by the renderer.",
    "pr-writeup": "pr-writeup template — what / why / how / tested register; the render "
                  "keeps the source's headings as the structure.",
}

TEMPLATE_TITLE = {
    "spec": "Implementation plan",
    "prd-pitch": "PRD pitch",
    "pr-writeup": "PR writeup",
}


def esc(text):
    """Escape for HTML text and attribute contexts."""
    return html.escape(text, quote=True)


def build_slug(text, used, maxlen=54):
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"\s+", "-", slug.strip())
    slug = re.sub(r"-{2,}", "-", slug)
    slug = slug[:maxlen].rstrip("-")
    slug = slug or "section"
    if slug in used:
        n = 2
        while "{}-{}".format(slug, n) in used:
            n += 1
        slug = "{}-{}".format(slug, n)
    used.add(slug)
    return slug


def inline(s):
    """Escape, then apply the only supported inline spans: **bold** and `code`."""
    s = esc(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!`)`([^`]+)`(?!`)", r"<code>\1</code>", s)
    return s


def table_cell(cell):
    c = cell.strip()
    m = re.fullmatch(r"`([^`]+)`", c)
    if m:
        return "<code>{}</code>".format(esc(m.group(1)))
    return inline(c)


def is_table_separator(line):
    if "-" not in line:
        return False
    cols = [c for c in line.strip().strip("|").split("|")]
    return bool(cols) and all(re.fullmatch(r":?-{1,}:?", c.strip()) for c in cols)


def render_table(rows):
    header_row = rows[0]
    cols = [c.strip() for c in header_row.strip().strip("|").split("|")]
    cols = [c for c in cols if c or len(cols) == 1]
    head = "<tr>{}</tr>".format("".join("<th>{}</th>".format(esc(c)) for c in cols))
    body_rows = []
    for row in rows[2:]:
        cells = [c for c in row.strip().strip("|").split("|")]
        while len(cells) < len(cols):
            cells.append("")
        body_rows.append(
            "<tr>{}</tr>".format("".join("<td>{}</td>".format(table_cell(c)) for c in cells[: len(cols)]))
        )
    return '<div class="table-wrap"><table>{}{}</table></div>'.format(head, "".join(body_rows))


def parse_fence(lines, i):
    m = re.match(r"^```(.*)$", lines[i].strip())
    lang = m.group(1).strip() if m else ""
    j = i + 1
    while j < len(lines) and not lines[j].strip().startswith("```"):
        j += 1
    body = "\n".join(lines[i + 1 : j])
    lang_attr = ' class="language-{}"'.format(esc(lang)) if lang else ""
    block = "<pre><code{}>{}</code></pre>".format(lang_attr, esc(body))
    return block, min(j + 1, len(lines))


def render_para(lines):
    return "<p>{}</p>".format(inline(" ".join(l.strip() for l in lines)))


def uses_unsupported_markdown(text):
    return any(pat.search(line) for pat in UNSUPPORTED_MARKERS for line in text.splitlines())


def render_raw(lines):
    body = "".join("{}\n".format(esc(l)) for l in lines).rstrip("\n")
    return (
        '<div class="raw"><div class="raw-title">raw — unsupported markdown '
        "(kept verbatim, not interpreted)</div><pre>{}</pre></div>"
    ).format(body)


def render_blockquote(inner_lines):
    paras = []
    buf = []
    for raw in inner_lines:
        if raw.strip():
            buf.append(raw)
        else:
            if buf:
                paras.append(render_para(buf))
                buf = []
    if buf:
        paras.append(render_para(buf))
    return "<blockquote>{}</blockquote>".format("".join(paras))


def render(md_lines):
    """Render block-level markdown. Returns (list of HTML blocks, list of heading slugs)."""
    out = []
    headings = []  # (level, slug, visible_text)
    used_slugs = set()
    i = 0
    n = len(md_lines)

    while i < n:
        line = md_lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        if stripped.startswith("```"):
            block, i = parse_fence(md_lines, i)
            out.append(block)
            continue

        m_h = ATX_RE.match(stripped)
        if m_h:
            hashes = re.match(r"^#+", stripped).group(0)
            level = min(len(hashes), 6)
            title = m_h.group(0)[len(hashes) :].strip()
            slug = "sec-{}".format(build_slug(title, used_slugs))
            out.append('<h{} id="{}">{}</h{}>'.format(level, slug, inline(title), level))
            headings.append((level, slug, title))
            i += 1
            continue

        if stripped.startswith(">"):
            quotes = []
            j = i
            while j < n and md_lines[j].strip().startswith(">"):
                mm = BLOCKQUOTE_RE.match(md_lines[j])
                quotes.append(mm.group(1) if mm else "")
                j += 1
            out.append(render_blockquote(quotes))
            i = j
            continue

        if stripped.startswith("|"):
            j = i
            while j < n and md_lines[j].strip().startswith("|"):
                j += 1
            candidates = md_lines[i:j]
            if len(candidates) >= 2 and is_table_separator(candidates[1]):
                out.append(render_table(candidates))
                i = j
                continue
            out.append(render_raw([line]))
            i += 1
            continue

        m_ul = UL_BULLET_RE.match(line)
        m_ol = OL_ITEM_RE.match(line)
        if m_ul or m_ol:
            kind = "ul" if m_ul else "ol"
            item_re = UL_BULLET_RE if kind == "ul" else OL_ITEM_RE
            items = []
            j = i
            while j < n:
                mm = item_re.match(md_lines[j])
                if not mm:
                    break
                items.append(mm.group(1).strip())
                j += 1
            body = "".join("<li>{}</li>".format(inline(it)) for it in items)
            out.append("<{0}>{1}</{0}>".format(kind, body))
            i = j
            continue

        # Plain paragraph (or unsupported construct): group contiguous unblank,
        # non-special lines. If the block hits an unsupported markdown marker,
        # keep it verbatim in a marked "raw" block instead of a plain paragraph.
        j = i
        while j < n and md_lines[j].strip() and not (
            ATX_RE.match(md_lines[j].strip())
            or md_lines[j].strip().startswith(("```", ">", "|"))
            or UL_BULLET_RE.match(md_lines[j])
            or OL_ITEM_RE.match(md_lines[j])
        ):
            j += 1
        para_lines = md_lines[i:j]
        if uses_unsupported_markdown("\n".join(para_lines)):
            out.append(render_raw(para_lines))
        else:
            out.append(render_para(para_lines))
        i = j

    return out, headings


def extract_title(md_lines):
    for ln in md_lines:
        s = ln.strip()
        if s.startswith("title:"):
            raw = s[6:].strip().strip("\"'").strip()
            if raw:
                return esc(raw)[:120]
    for ln in md_lines:
        stripped = ln.strip()
        m = re.match(r"^#\s+(.*)$", stripped)
        if m and m.group(1).strip():
            return inline(m.group(1).strip())
    return ""


def strip_yaml_front(md_lines):
    if md_lines and md_lines[0].strip() == "---":
        for j in range(1, len(md_lines)):
            if md_lines[j].strip() == "---":
                return md_lines[j + 1 :]
    return md_lines


def build_toc(headings):
    if not headings:
        return ""
    links = []
    for level, slug, title in headings:
        links.append(
            '<li class="lvl{}"><a href="#{}">{}</a></li>'.format(min(level, 6), slug, inline(title))
        )
    return '<nav class="toc" aria-label="sections"><h2>Contents</h2><ol>{}</ol></nav>'.format(
        "".join(links)
    )


def build_body_blocks(blocks):
    """Group optional heading + contiguous content into <section> elements."""
    groups = []
    cur = []
    for blk in blocks:
        if blk.startswith("<h"):
            if cur:
                groups.append(cur)
                cur = []
            cur.append(blk)
        else:
            cur.append(blk)
    if cur:
        groups.append(cur)

    out = []
    for g in groups:
        if len(g) == 1 and g[0].startswith("<h"):
            out.append(g[0])
        else:
            out.append("<section>{}</section>".format("\n".join(g)))
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="html_render_md.py",
        description=(
            "Render a markdown file as a self-contained, canonical-source HTML view "
            "(Tier-3 stub). Stdlib only; unsupported markdown is kept verbatim in a "
            "marked 'raw' block."
        ),
    )
    ap.add_argument("input", help="path to the source .md file")
    ap.add_argument("--template", choices=TEMPLATES, default="spec", help="output template register")
    ap.add_argument("--out", help="output .html path (default: <input>.html next to the input)")
    ap.add_argument("--title", default="", help="override the page title (default: source front-matter or first h1)")
    args = ap.parse_args(argv)

    in_path = Path(args.input)
    if not in_path.is_file():
        print("error: input file not found: {}".format(in_path), file=sys.stderr)
        return 2

    try:
        md_text = in_path.read_text(encoding="utf-8")
    except OSError as exc:
        print("error: cannot read {}: {}".format(in_path, exc), file=sys.stderr)
        return 2

    template = args.template
    title = (args.title or "").strip() or extract_title(md_text.splitlines()) or in_path.stem

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        out_base = in_path.with_suffix(".html")
        if str(in_path).startswith("docs/plans/"):
            # Default convention: plans render into docs/site/ so the site
            # directory stays the only docs output bucket.
            site_dir = Path("docs/site")
            site_dir.mkdir(parents=True, exist_ok=True)
            out_base = site_dir / out_base.name
        out_path = out_base

    source_rel = in_path.as_posix()

    md_lines = md_text.splitlines()
    body_frag = strip_yaml_front(md_lines)
    rendered, headings = render(body_frag)

    toc_html = build_toc(headings)
    body_blocks_html = build_body_blocks(rendered)

    page_title = "{} · {}".format(TEMPLATE_TITLE[template], title)

    doc = []
    doc.append("<!DOCTYPE html>")
    doc.append('<!-- Generated view. Canonical source: the markdown file in <meta name="source">')
    doc.append("     below is authoritative and diffable; this .html is a render of it and must")
    doc.append('     not be hand-edited as the source of truth. Regenerate to update. -->')
    doc.append('<html lang="en">')
    doc.append("<head>")
    doc.append('<meta charset="UTF-8">')
    doc.append('<meta name="viewport" content="width=device-width, initial-scale=1.0">')
    doc.append('<meta name="source" content="{}">'.format(esc(source_rel)))
    doc.append('<title>{}</title>'.format(esc(page_title)))
    doc.append("<style>{}{}</style>".format(STYLE, TEMPLATE_CSS[template]))
    doc.append("</head>")
    doc.append("<body>")
    doc.append("<header>")
    doc.append("<h1>{}</h1>".format(title))
    doc.append('<p class="subtitle">{}</p>'.format(TEMPLATE_NOTE[template]))
    doc.append(
        '<div class="meta"><span>{}</span><span>template: {}</span></div>'.format(
            esc(source_rel), esc(template)
        )
    )
    doc.append("</header>")
    doc.append('<main>{}<article>'.format(toc_html))
    doc.append(body_blocks_html)
    doc.append("</article></main>")
    doc.append(
        '<div class="footer">Generated view of <code>{}</code> — the markdown file remains '
        "authoritative. Regenerate with "
        '<code>python3 scripts/html_render_md.py {}</code></div>'.format(esc(source_rel), esc(str(in_path)))
    )
    doc.append("</body>")
    doc.append("</html>")
    doc.append("")

    out_path.write_text("\n".join(doc), encoding="utf-8")
    print("wrote {}".format(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())