#!/usr/bin/env bash
# setup.sh — One-command setup for school-core
# Usage: ./setup.sh [--dry-run] [--profile-dir DIR]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

DRY_RUN=false
PROFILE_DIR=""
NONINTERACTIVE="${SETUP_NONINTERACTIVE:-0}"

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --profile-dir=*) PROFILE_DIR="${arg#*=}" ;;
        --noninteractive) NONINTERACTIVE=1 ;;
        *) echo "Unknown flag: $arg"; exit 1 ;;
    esac
done

echo "=== school-core setup ==="
if [ "$DRY_RUN" = true ]; then
    echo "  --dry-run: no changes will be made"
fi

# 1. Create virtual environment if missing
if [ ! -d .venv ]; then
    echo "[1/6] Creating virtual environment..."
    [ "$DRY_RUN" = false ] && python3 -m venv .venv
fi
[ "$DRY_RUN" = false ] && source .venv/bin/activate

# 2. Install dependencies (skip in test mode)
echo "[2/6] Installing Python dependencies..."
if [ "${SETUP_SKIP_DEPS:-0}" = "1" ]; then
    echo "    ⏭ Skipping pip install (SETUP_SKIP_DEPS=1)"
else
    [ "$DRY_RUN" = false ] && pip install --quiet --upgrade pip
    [ "$DRY_RUN" = false ] && pip install --quiet -r requirements.txt
fi

# 3. Create .env from template if missing
if [ ! -f .env ]; then
    echo "[3/6] Creating .env from template..."
    [ "$DRY_RUN" = false ] && cp .env.example .env
    echo "    ⚠️  Edit .env and fill in your API keys"
fi

# 4. Check Orca (optional — warn but don't fail)
echo "[4/6] Checking Orca..."
if command -v orca &>/dev/null; then
    echo "    ✓ Orca found"
    [ "$DRY_RUN" = false ] && orca repo list --json 2>/dev/null | grep -q school-core || {
        orca repo add --path "$REPO_ROOT" 2>/dev/null || true
    }
else
    echo "    ⚠️  Orca not found — optional but recommended for worktree mode"
fi

# 5. Copy profile templates → user profiles
echo "[5/6] Setting up persona profiles..."
HERMES_HOME="${PROFILE_DIR:-$HOME/.hermes}"
PROFILES_DIR="$HERMES_HOME/profiles"
if [ "$DRY_RUN" = true ]; then
    echo "    ⏭ Skipping profile creation (--dry-run)"
else
    mkdir -p "$PROFILES_DIR"

    TEMPLATES_DIR="$REPO_ROOT/config/profiles/_TEMPLATES"
    if [ -d "$TEMPLATES_DIR" ]; then
        for template in "$TEMPLATES_DIR"/*/SOUL.md; do
            [ -f "$template" ] || continue
            persona=$(basename "$(dirname "$template")")
            dest="$PROFILES_DIR/$persona/SOUL.md"
            mkdir -p "$(dirname "$dest")"

            if [ -f "$dest" ]; then
                if [ "$NONINTERACTIVE" = 1 ]; then
                    echo "    ⏭ $persona (exists, noninteractive — skipped)"
                else
                    echo "    ⚠️  $persona already exists — overwrite? (y/N)"
                    read -r ans
                    if [[ "$ans" =~ ^[Yy] ]]; then
                        cp "$template" "$dest"
                        echo "    ✓ $persona (overwritten)"
                    else
                        echo "    ⏭ $persona (kept existing)"
                    fi
                fi
            else
                cp "$template" "$dest"
                echo "    ✓ $persona"
            fi
        done
    fi
fi

# 6. Create Hermes config stub (if missing)
HERMES_CONFIG="$HERMES_HOME/config.yaml"
if [ ! -f "$HERMES_CONFIG" ]; then
    echo "[6/6] Creating Hermes config stub..."
    mkdir -p "$(dirname "$HERMES_CONFIG")"
    [ "$DRY_RUN" = false ] && cat > "$HERMES_CONFIG" <<EOF
model:
  provider: nous
  default: meituan/longcat-2.0:free
EOF
else
    echo "[6/6] Hermes config exists — skipping"
fi

echo ""
echo "✅ Setup complete. Next steps:"
echo "   1. Edit .env with your API keys (NOUS_API_KEY=...)"
echo "   2. Run tests: python3 -m pytest -q -m 'not live'"
echo "   3. Launch: python3 conductor.py --serve"
echo ""
echo "Optional:"
echo "   pip install pandas       # for benchmark CSV checks"
echo "   brew install nix         # for verify gate (hermetic builds)"
