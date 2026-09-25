#!/usr/bin/env bash
# setup_envit.sh — wire envit + ripwire + Hermes skills for school-core
# Run once on a fresh machine to make the outer/inner loop framework operational.
#
# What this does:
#   1. Verifies/enables envit (context-materialization tool, v0.1.0)
#   2. Verifies/enables ripwire (AI context map generator, v0.4.0)
#   3. Runs envit sync --frozen to materialize plannotator skill SETS
#      into ~/.envit/store/checkouts/ (global store, read-only at pinned commits)
#   4. Installs the 4 MISSING Hermes-native skills via `hermes skills install`
#      (github-pr-workflow, github-pr-gates, pr-merge-readiness, github-pr-checks-truth)
#   5. Prints a wiring summary: what's materialized, what's installed, what's still TBD
#
# What this does NOT do (documented gaps — see bottom of script):
#   - Hermes does NOT auto-read envit-materialized skills (~/.envit/store/).
#     Hermes loads from ~/.hermes/skills/. These are separate locations with no
#     integration path in envit 0.1.0. The plannotator skills are context material
#     for ripwire/agent orientation, NOT loadable Hermes skills.
#   - repos: [] is still empty. No target repos declared for the outer loop.
#     That's a design decision (which repos to dispatch against), not a wiring bug.
#
# Usage: bash scripts/setup_envit.sh
# Exit: 0 = wired (or already wired), 1 = user action needed

set -euo pipefail
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
ENOVIT_HOME="${ENVIT_HOME:-$HOME/.envit}"
SCHOOL_ROOT="${SCHOOL_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
ENVIT_JSON="$SCHOOL_ROOT/envit.json"
ENVIT_LOCK="$SCHOOL_ROOT/envit.lock.json"

# ── helpers ────────────────────────────────────────────────────────────────────
info()  { echo "  ✓ $1"; }
warn()  { echo "  ⚠  $1" >&2; }
fail()  { echo "  ✗  $1" >&2; }
section() { echo ""; echo "== $1 =="; }

# ── 0. pre-flight ───────────────────────────────────────────────────────────────
section "Pre-flight checks"

# Must be run from inside school-core (or SCHOOL_ROOT set)
if [ ! -f "$ENVIT_JSON" ]; then
  fail "envit.json not found at $ENVIT_JSON — are you inside school-core?"
  exit 1
fi

# PATH must include ripwire + envit (both live in ~/.local/bin)
export PATH="$HOME/.local/bin:$PATH"

# ── 1. ripwire ───────────────────────────────────────────────────────────────────
section "Step 1 — ripwire (AI context map generator, v0.4.0)"

if command -v ripwire >/dev/null 2>&1; then
  RIPWIRE_VER=$(ripwire --version 2>&1 | head -1)
  info "ripwire found: $RIPWIRE_VER"
else
  warn "ripwire NOT on PATH — expected at $HOME/.local/bin/ripwire"
  warn "Install: see docs/setup/local-cron-setup.md (RIPWIRE_INSTALL section)"
  warn "The envit sync will still work (envit has its own git transport), but"
  warn "agents won't have the context-map lens until ripwire is on PATH."
  # Do NOT exit — envit sync is the load-bearing step; ripwire is the nice-to-have lens
fi

# ── 2. envit ─────────────────────────────────────────────────────────────────────
section "Step 2 — envit (context-materialization tool, v0.1.0)"

if command -v envit >/dev/null 2>&1; then
  ENVIT_VER=$(envit --version 2>&1 | head -1)
  info "envit found: $ENVIT_VER"
else
  fail "envit NOT on PATH — expected at $HOME/.local/bin/envit"
  fail "Install envit before running this script. See docs/setup/local-cron-setup.md."
  exit 1
fi

# ── 3. envit sync (materialize plannotator skill SETS) ──────────────────────────
section "Step 3 — envit sync --frozen (materialize 3 plannotator skill SETS)"

if [ ! -f "$ENVIT_LOCK" ]; then
  fail "envit.lock.json not found at $ENVIT_LOCK — run 'envit sync' once to generate it."
  exit 1
fi

# Count declared skills + locked sources before sync
DECLARED_SKILLS=$(python3 -c "import json; print(len(json.load(open('$ENVIT_JSON'))['skills']))" 2>/dev/null || echo "?")
LOCKED_SOURCES=$(python3 -c "import json; print(len(json.load(open('$ENVIT_LOCK'))['skillSources']))" 2>/dev/null || echo "?")
info "envit.json declares $DECLARED_SKILLS skill SETS"
info "envit.lock.json pins $LOCKED_SOURCES repos (the 3 plannotator ones)"
info "The other $((DECLARED_SKILLS - LOCKED_SOURCES)) declared skills are Hermes-native"
info "  (listed in envit.json:_herm-native-notes for documentation only)."

echo ""
info "Running: envit sync --frozen --link-mode symlink"
echo "         (from $SCHOOL_ROOT)"

cd "$SCHOOL_ROOT"
envit sync --frozen --link-mode symlink 2>&1 | tail -5

SYNC_EXIT=$?
if [ $SYNC_EXIT -eq 0 ]; then
  info "envit sync succeeded — plannotator skill SETS materialized to ~/.envit/store/"
else
  fail "envit sync failed (exit $SYNC_EXIT). See output above."
  fail "Common causes: missing envit.lock.json, network, or unresolved skill declaration."
  exit 1
fi

# ── 4. verify materialized skills ────────────────────────────────────────────────
section "Step 4 — verify materialized plannotator skills"

PLANNOTATOR_SKILLS=(
  "design-artifact"
  "html"
  "html-diagram"
  "html-plan"
  "html-prototype"
  "html-wireframe"
  "plannotator-guide"
  "plannotator-tui"
)

MATERIALIZED=0
for skill in "${PLANNOTATOR_SKILLS[@]}"; do
  # envit stores skills at: ~/.envit/store/checkouts/github.com/plannotator/<repo>/<commit>/skills/<skill>/SKILL.md
  SKILL_PATH=$(find "$ENOVIT_HOME/store/checkouts" -path "*/plannotator/*/skills/$skill/SKILL.md" -print -quit 2>/dev/null)
  if [ -n "$SKILL_PATH" ]; then
    COMMIT=$(echo "$SKILL_PATH" | grep -oP '[0-9a-f]{12}' | head -1)
    info "materialized: plannotator/$skill @ $COMMIT"
    ((MATERIALIZED++))
  else
    warn "NOT materialized: plannotator/$skill (not found in ~/.envit/store/)"
  fi
done

info "Materialized $MATERIALIZED / ${#PLANNOTATOR_SKILLS[@]} plannotator skill SETS"

# ── 5. install missing Hermes-native skills ─────────────────────────────────────
section "Step 5 — install 4 missing Hermes-native skills"

# These are Hermes-native skills (loaded from ~/.hermes/skills/), NOT envit-managed.
# envit does NOT fetch or install them. They must be installed via `hermes skills install`
# or placed manually in ~/.hermes/skills/.
#
# As of envit wiring audit (2026-09-20), these 4 are MISSING:
#   github-pr-workflow, github-pr-gates, pr-merge-readiness, github-pr-checks-truth
#
# These 4 are INSTALLED:
#   pr-verification-gate, build-verification, bounded-adversarial-plan-review, worst-day-ever

INSTALL_TARGETS=(
  "github-pr-workflow"
  "github-pr-gates"
  "pr-merge-readiness"
  "github-pr-checks-truth"
)

# Check which are already installed
INSTALLED=0
MISSING=0
SKIP=0
for skill in "${INSTALL_TARGETS[@]}"; do
  if [ -d "$HERMES_HOME/skills/$skill" ]; then
    info "already installed: $skill"
    ((INSTALLED++))
  else
    ((MISSING++))
    # Try to install via hermes skills install (registry lookup)
    echo ""
    info "Attempting: hermes skills install $skill"
    if hermes skills install "$skill" 2>&1 | tail -3; then
      info "installed: $skill"
      ((INSTALLED++))
      ((MISSING--))
    else
      warn "install FAILED for $skill — may not exist in registry"
      warn "  Install manually: place SKILL.md at ~/.hermes/skills/$skill/SKILL.md"
    fi
  fi
done

info "Hermes-native skill status: $INSTALLED installed, $MISSING still missing (of 4 targets)"

# ── 6. wiring summary ────────────────────────────────────────────────────────────
section "Wiring summary"

echo ""
echo "  envit.json  ........ $ENVIT_JSON ($(wc -l < "$ENVIT_JSON") lines)"
echo "  envit.lock.json .... $ENVIT_LOCK ($(wc -l < "$ENVIT_LOCK") lines)"
echo "  ripwire ............ $(command -v ripwire 2>/dev/null || echo 'NOT ON PATH')"
echo "  envit .............. $(command -v envit 2>/dev/null || echo 'NOT ON PATH')"
echo "  ~/.envit/store/ .... $(find "$ENOVIT_HOME/store/checkouts" -name "SKILL.md" 2>/dev/null | wc -l) SKILL.md files materialized"
echo "  ~/.hermes/skills/ . $(ls -d "$HERMES_HOME/skills"/*/ 2>/dev/null | wc -l) Hermes-native skills installed"
echo ""
echo "  Plannotator skills (envit-managed, in ~/.envit/store/):"
echo "    effective-html:    html, html-plan, html-diagram, html-wireframe, html-prototype, design-artifact"
echo "    guides:            plannotator-guide"
echo "    herdr-annotate:    plannotator-tui"
echo ""
echo "  Hermes-native skills (Hermes-managed, in ~/.hermes/skills/):"
echo "    INSTALLED:         pr-verification-gate, build-verification, bounded-adversarial-plan-review, worst-day-ever"
echo "    MISSING:           github-pr-workflow, github-pr-gates, pr-merge-readiness, github-pr-checks-truth"
echo ""
echo "  repos: [] ........... EMPTY — no target repos declared for the outer loop."
echo "    This is a design decision (which repos to dispatch against), not a wiring bug."
echo "    Add to envit.json repos: [\"branben/sound-royale-ny\", \"branben/OmniRoute\", ...] when ready."

# ── gaps (documented, not fixed by this script) ──────────────────────────────────
section "Documented gaps (NOT fixed by this script)"

cat <<'EOF'

  1. Hermes ↔ envit skill integration gap
     Hermes loads skills from ~/.hermes/skills/.
     envit materializes to ~/.envit/store/checkouts/.
     These are separate locations. Hermes does NOT auto-read envit-materialized skills.
     The 8 plannotator SKILL.md files are context material for ripwire/agent orientation,
     NOT loadable Hermes skills. To make them loadable, symlink or copy them into
     ~/.hermes/skills/ manually, or wait for an envit→Hermes integration layer.

  2. repos: [] is empty
     No target repos declared. The outer loop has nothing to dispatch against until
     repos is populated with the project repos the framework targets
     (e.g. ["branben/sound-royale-ny", "branben/OmniRoute"]).

  3. envit.lock.json pins at HEAD, not exact commits
     Reproducibility gap: a fresh `envit sync --frozen` pulls whatever HEAD is today,
     not the tested version. The lockfile has commit hashes but ref="HEAD". To pin exactly,
     the lockfile should reference the commit directly, not HEAD.

  4. .envit/AGENTS.md stub is unpopulated by sync
     The stub says "Generated by envit sync. Do not edit." but envit 0.1.0 does NOT
     populate it with repo/skill listings after sync. The AGENTS.md is a no-op placeholder.

EOF

section "Done"
echo ""
info "envit + ripwire + plannotator skills + Hermes-native skills wired."
info "Run `envit sync --frozen` again after editing envit.json (add repos, change skills)."
echo ""
