#!/usr/bin/env bash
# bridge_envit_to_hermes.sh — close the Hermes ↔ envit skill integration gap
#
# WHAT THIS DOES:
#   Symlinks plannotator skill SETS from envit's global store
#   (~/.envit/store/checkouts/) into Hermes' skill loader
#   (~/.hermes/skills/) so Hermes agents can load them.
#
# COLLISION SAFETY:
#   4 plannotator skills are IDENTICAL to already-installed Hermes skills
#   (html, html-plan, html-prototype, plannotator-guide) — verified by
#   `diff -q` in the gap characterization. These are SKIPPED (no-op).
#
#   4 plannotator skills have NO Hermes-native counterpart and are
#   SAFE to symlink:
#     html-diagram      (from effective-html)
#     html-wireframe    (from effective-html)
#     design-artifact   (from effective-html)
#     plannotator-tui   (from herdr-annotate)
#
#   These 4 fill genuine gaps in Hermes' skill set.
#
# WHAT THIS DOES NOT DO:
#   - Install the 4 MISSING Hermes-native skills (github-pr-workflow,
#     github-pr-gates, pr-merge-readiness, github-pr-checks-truth).
#     Those are a separate task — they don't exist in any registry;
#     they need to be written or fetched from a non-registry source.
#   - Populate envit.json repos: [] (design decision, not wiring).
#   - Pin envit.lock.json to exact commits (cosmetic reproducibility gap).
#
# Prerequisites: envit sync already run (skills materialized in ~/.envit/store/).
#   Run scripts/setup_envit.sh first if starting from scratch.

set -euo pipefail

HERMES_SKILLS="$HOME/.hermes/skills"
ENVIT_STORE="$HOME/.envit/store/checkouts/github.com/plannotator"
DRY_RUN="${DRY_RUN:-0}"

symlink_skill() {
  local plannotator_repo="$1"   # effective-html | guides | herdr-annotate
  local skill_name="$2"         # html-diagram | html-wireframe | design-artifact | plannotator-tui
  local commit="$3"             # pinned commit hash

  local src="$ENVIT_STORE/$plannotator_repo/$commit/skills/$skill_name"
  local dst="$HERMES_SKILLS/$skill_name"

  if [ ! -d "$src" ]; then
    echo "  ✗  SOURCE MISSING: $src"
    return 1
  fi

  if [ -d "$dst" ]; then
    # Collision check: are they identical?
    local src_skill="$src/SKILL.md"
    local dst_skill="$dst/SKILL.md"
    if [ -f "$src_skill" ] && [ -f "$dst_skill" ]; then
      if diff -q "$src_skill" "$dst_skill" >/dev/null 2>&1; then
        echo "  ↔  SKIP (identical): $skill_name — already installed, same content"
        return 0
      else
        echo "  ✗  COLLISION (different content): $skill_name"
        echo "     envit:  $src_skill"
        echo "     hermes: $dst_skill"
        echo "     DO NOT overwrite — investigate scope difference first."
        return 1
      fi
    else
      echo "  ✗  COLLISION (dir exists, SKILL.md mismatch): $skill_name"
      return 1
    fi
  fi

  if [ "$DRY_RUN" = "1" ]; then
    echo "  ⊙  DRY RUN: would symlink $skill_name → $dst"
    return 0
  fi

  ln -s "$src" "$dst"
  echo "  ✓  symlinked: $skill_name → $dst"
}

section() { echo ""; echo "== $1 =="; }

section "Hermes ↔ envit skill bridge"
echo "  Hermes skills dir:  $HERMES_SKILLS"
echo "  envit store root:   $ENVIT_STORE"
echo "  dry_run:            $DRY_RUN"
echo ""

symlink_skill "effective-html"  "html-diagram"     "d95debbaef15af1d201fc6c10c77cf92b524a0d6"
symlink_skill "effective-html"  "html-wireframe"   "d95debbaef15af1d201fc6c10c77cf92b524a0d6"
symlink_skill "effective-html"  "design-artifact"  "d95debbaef15af1d201fc6c10c77cf92b524a0d6"
symlink_skill "herdr-annotate"  "plannotator-tui"  "7c8f5a177b8285dc56efc471ef04f7ab44a2b4b6"

echo ""
echo "  Done. 4 plannotator skills materialized into Hermes."
echo "  4 identical skills skipped (html, html-plan, html-prototype, plannotator-guide)."
echo "  4 MISSING Hermes-native skills NOT addressed (github-pr-*, pr-merge-readiness, github-pr-checks-truth)."
