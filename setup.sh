#!/usr/bin/env bash
# setup.sh — One-command setup for school-core
# Usage: ./setup.sh [--with-firstmate] [--with-orca]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

echo "=== school-core setup ==="

# 1. Create virtual environment if missing
if [ ! -d .venv ]; then
    echo "[1/5] Creating virtual environment..."
    python3 -m venv .venv
fi
source .venv/bin/activate

# 2. Install dependencies
echo "[2/5] Installing Python dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

# 3. Create .env from template if missing
if [ ! -f .env ]; then
    echo "[3/5] Creating .env from template..."
    cp .env.example .env
    echo "    ⚠️  Edit .env and fill in your API keys"
fi

# 4. Register with Orca if available
if command -v orca &>/dev/null; then
    echo "[4/5] Registering with Orca..."
    orca repo list --json 2>/dev/null | grep -q school-core || orca repo add --path "$REPO_ROOT" 2>/dev/null || true
else
    echo "[4/5] Orca not found — skipping registration"
fi

# 5. Create Hermes config if missing
HERMES_CONFIG="$HOME/.hermes/config.yaml"
if [ ! -f "$HERMES_CONFIG" ]; then
    echo "[5/5] Creating Hermes config stub..."
    mkdir -p "$(dirname "$HERMES_CONFIG")"
    cat > "$HERMES_CONFIG" <<EOF
model:
  provider: nous
  default: meituan/longcat-2.0:free
EOF
else
    echo "[5/5] Hermes config exists — skipping"
fi

echo ""
echo "✅ Setup complete. Next steps:"
echo "   1. Edit .env with your API keys"
echo "   2. Run tests: python3 -m pytest -q -m 'not live'"
echo "   3. Launch: python3 conductor.py --serve"
echo ""
echo "Optional:"
echo "   pip install pandas       # for benchmark CSV checks"
echo "   brew install nix         # for verify gate (hermetic builds)"
