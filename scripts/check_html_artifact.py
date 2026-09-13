#!/usr/bin/env python3
"""Static lint gate for generated HTML artifacts.

Hard-fails on:
  * external http(s) URLs in href/src/style attributes and inline CSS
  * missing semantic landmarks <header>, <main>, <footer>
  * (tier 3) missing canonical-source <meta name="source"> header
Warns (without changing the exit code) on horizontal-overflow smells:
  * min-width: or width: >= 1201px in inline CSS
  * a <table> not wrapped with an overflow-x note/class

Structural check only: this is a static lint, not an HTML validator --
unclosed or malformed tags are treated as text and still pass.

Exit codes: 0 = clean, 1 = lint failure, 2 = input file missing/unreadable.
Zero dependencies: Python 3.9+ standard library (argparse, re, sys).
"""

import argparse
import os
import re
import sys

URL_RE = re.compile(r"https?://[^\s\"'<>)]+", re.IGNORECASE)
# href/src/style attribute values.
ATTR_RE = re.compile(
    r"\b(href|src|style)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')",
    re.IGNORECASE,
)
STYLE_ATTR_RE = re.compile(
    r"\bstyle\s*=\s*(?:\"([^\"]*)\"|'([^']*)')",
    re.IGNORECASE,
)
STYLE_BLOCK_RE = re.compile(
    r"<\s*style\b[^>]*>(.*?)<\s*/\s*style\s*>",
    re.IGNORECASE | re.DOTALL,
)
HEADER_RE = re.compile(r"<\s*header\b", re.IGNORECASE)
MAIN_RE = re.compile(r"<\s*main\b", re.IGNORECASE)
FOOTER_RE = re.compile(r"<\s*footer\b", re.IGNORECASE)
SOURCE_META_RE = re.compile(
    r"<\s*meta\b[^>]*\bname\s*=\s*[\"']source[\"']", re.IGNORECASE
)
# Pixel widths in inline CSS: min-width: / width:, but not max-width:.
WIDTH_RE = re.compile(r"(?<!max-)\b(min-width|width)\s*:\s*(\d+)\s*px\b", re.IGNORECASE)
TABLE_RE = re.compile(r"<\s*table\b", re.IGNORECASE)
TABLE_WRAP_WINDOW = 800  # chars of preceding text scanned for an overflow-x wrapper/note
MIN_SMELL_WIDTH = 1201


def line_of(text, pos):
    return text.count("\n", 0, pos) + 1


def _attr_value(m, value_groups):
    """Return (value, offset_in_text) for a quoted-attribute regex match."""
    for g in value_groups:
        if m.group(g) is not None:
            return m.group(g), m.start(g)
    return None, None


def lint_text(text, tier):
    """Return (hard_fail_reasons, warnings) for a single document."""
    reasons = []
    warnings = []

    # External-URL policy: any absolute http:// or https:// URL found in an
    # href=, src=, or style= attribute value, or inside a <style> block
    # (inline CSS). Relative URLs, protocol-relative "//host" URLs, other
    # schemes, and plain text occurrences are not treated as external.
    for m in ATTR_RE.finditer(text):
        attr = m.group(1).lower()
        value, _ = _attr_value(m, (2, 3))
        if value is None:
            continue
        for um in URL_RE.finditer(value):
            reasons.append(
                "external %s URL in %s attribute" % (um.group(0), attr)
            )

    for m in STYLE_BLOCK_RE.finditer(text):
        block = m.group(1)
        for um in URL_RE.finditer(block):
            url = um.group(0)
            reasons.append(
                "external %s URL in inline CSS (line %d)"
                % (url, line_of(text, m.start(1) + um.start()))
            )

    for name, pattern in (
        ("<header>", HEADER_RE),
        ("<main>", MAIN_RE),
        ("<footer>", FOOTER_RE),
    ):
        if not pattern.search(text):
            reasons.append("missing semantic landmark %s" % name)

    if tier >= 3 and not SOURCE_META_RE.search(text):
        reasons.append(
            "missing canonical source <meta name=\"source\"> header (tier 3)"
        )

    # Horizontal-overflow smells (warnings only).
    inline_css = []  # (offset_in_text, css_source)
    for m in STYLE_ATTR_RE.finditer(text):
        value, offset = _attr_value(m, (1, 2))
        if value is not None:
            inline_css.append((offset, value))
    for m in STYLE_BLOCK_RE.finditer(text):
        inline_css.append((m.start(1), m.group(1)))

    for offset, css in inline_css:
        for wm in WIDTH_RE.finditer(css):
            if int(wm.group(2)) >= MIN_SMELL_WIDTH:
                warnings.append(
                    "horizontal-overflow smell: %s:%spx in inline CSS (line %d)"
                    % (wm.group(1), wm.group(2), line_of(text, offset + wm.start()))
                )

    for tm in TABLE_RE.finditer(text):
        start = tm.start()
        opening_end = text.find(">", start)
        opening = text[start : opening_end + 1] if opening_end != -1 else "<table"
        window = text[max(0, start - TABLE_WRAP_WINDOW) : start]
        if "overflow-x" not in opening and "overflow-x" not in window:
            warnings.append(
                "horizontal-overflow smell: <table> not wrapped with an "
                "overflow-x note/class (line %d)" % line_of(text, start)
            )

    return reasons, warnings


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="check_html_artifact.py",
        description=(
            "Static lint gate for generated HTML artifacts. Hard-fails on "
            "external http(s) URLs in href/src/style and inline CSS, on missing "
            "semantic landmarks (<header>, <main>, <footer>), and -- at tier 3 -- "
            "on a missing canonical-source <meta name=\"source\"> header. Warns "
            "(exit code unchanged) on horizontal-overflow smells: min-width: / "
            "width: >= 1201px in inline CSS, or a <table> not wrapped with an "
            "overflow-x note/class."
        ),
        epilog=(
            "--tier: minimum strictness level, default 2. Tiers 1 and 2 apply the "
            "same external-URL and landmark checks; tier 3 additionally requires "
            "a canonical-source <meta name=\"source\"> header.\n"
            "\n"
            "Structural check only: this is a static lint, not an HTML validator "
            "-- unclosed tags still pass."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--tier",
        type=int,
        choices=(1, 2, 3),
        default=2,
        help="minimum strictness level 1|2|3 (default: 2)",
    )
    parser.add_argument(
        "files",
        metavar="file.html",
        nargs="+",
        help="HTML artifact(s) to lint",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    missing = [f for f in args.files if not os.path.isfile(f)]
    if missing:
        for f in missing:
            print("check_html_artifact.py: %s: no such file" % f, file=sys.stderr)
        return 2

    had_fail = False
    for path in args.files:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            print("check_html_artifact.py: %s: cannot read: %s" % (path, exc),
                  file=sys.stderr)
            return 2

        reasons, warnings = lint_text(text, args.tier)
        for w in warnings:
            print("WARN %s: %s" % (path, w), file=sys.stderr)
        if reasons:
            had_fail = True
            print("FAIL %s: %s" % (path, "; ".join(reasons)))
        else:
            print("OK %s" % path)

    return 1 if had_fail else 0


if __name__ == "__main__":
    sys.exit(main())