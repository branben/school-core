#!/usr/bin/env bash
# setup-secrets.sh — Create secrets.enc from a plaintext JSON file
# This is a ONE-TIME setup script. After creating secrets.enc,
# distribute_keys.py handles all future distribution.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS_DIR="$REPO_ROOT/.secrets"
SECRETS_JSON="$SECRETS_DIR/secrets.json"
SECRETS_ENC="$SECRETS_DIR/secrets.enc"
RECIPIENT_FILE="$SECRETS_DIR/recipient.txt"

mkdir -p "$SECRETS_DIR"

# 1. Generate age keypair if not present
if [ ! -f "$RECIPIENT_FILE" ]; then
    echo "Generating age keypair..."
    age-keygen -o "$RECIPIENT_FILE" 2>&1
    echo ""
    echo "⚠️  BACK UP $RECIPIENT_FILE to a secure location!"
    echo "   Without it, you cannot decrypt secrets.enc on a new machine."
fi

RECIPIENT=$(grep "^# public key:" "$RECIPIENT_FILE" | awk '{print $NF}')
echo "Recipient: $RECIPIENT"

# 2. Create secrets.json template if not present
if [ ! -f "$SECRETS_JSON" ]; then
    echo "Creating $SECRETS_JSON template..."
    cat > "$SECRETS_JSON" <<'TEMPLATE'
{
    "OMNIROUTE_API_KEY": "",
    "REQUIRE_API_KEY": "",
    "STORAGE_ENCRYPTION_KEY": "",
    "OPENROUTER_API_KEY": "",
    "GITHUB_TOKEN": "",
    "AGENTMAIL_API_KEY": "",
    "AGENTMAIL_SCHOOL_INBOX": "",
    "BEADS_CREDENTIAL_KEY": ""
}
TEMPLATE
    echo ""
    echo "⚠️  Edit $SECRETS_JSON and fill in your API keys, then re-run this script."
    exit 0
fi

# 3. Validate JSON has at least one non-empty value
HAS_VALUE=$(python3 -c "
import json
with open('$SECRETS_JSON') as f:
    d = json.load(f)
has = any(v.strip() for v in d.values() if isinstance(v, str))
print('yes' if has else 'no')
")

if [ "$HAS_VALUE" = "no" ]; then
    echo "⚠️  $SECRETS_JSON has no values filled in. Edit it and re-run."
    exit 0
fi

# 4. Encrypt
echo "Encrypting to $SECRETS_ENC..."
age -r "$RECIPIENT" -o "$SECRETS_ENC" "$SECRETS_JSON"

# 5. Verify decryption works
echo "Verifying decryption..."
DECRYPTED=$(age --decrypt --identity "$RECIPIENT_FILE" "$SECRETS_ENC")
echo "  ✓ Decryption successful ($(echo "$DECRYPTED" | wc -c) bytes)"

# 6. Optionally delete plaintext
read -p "Delete plaintext $SECRETS_JSON? (y/N) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    rm -f "$SECRETS_JSON"
    echo "  ✓ Plaintext deleted"
fi

# 7. Update .gitignore
GITIGNORE="$REPO_ROOT/.gitignore"
if ! grep -q ".secrets/" "$GITIGNORE" 2>/dev/null; then
    echo "" >> "$GITIGNORE"
    echo "# Encrypted secrets (never commit plaintext)" >> "$GITIGNORE"
    echo ".secrets/secrets.json" >> "$GITIGNORE"
    echo ".secrets/secrets.enc" >> "$GITIGNORE"
    echo "✓ Added .secrets/ to .gitignore"
fi

echo ""
echo "✅ Setup complete."
echo "   - Private key: $RECIPIENT_FILE (BACK THIS UP)"
echo "   - Encrypted:   $SECRETS_ENC (can be safely synced)"
echo "   - Run: python3 scripts/distribute_keys.py --dry-run"
echo "   - Then: python3 scripts/distribute_keys.py"
