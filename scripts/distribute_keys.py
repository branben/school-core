#!/usr/bin/env python3
"""distribute_keys.py — Decrypt secrets.enc and distribute to all key locations.

Usage:
    python3 scripts/distribute_keys.py [--dry-run]

Decrypts school-core/.secrets/secrets.enc using the age private key at
~/.config/age/keys.txt (or the path in $AGE_KEY_FILE), then populates:
  1. school-core/.env (local runtime)
  2. ~/.omniroute/.env (OmniRoute gateway)
  3. ~/.hermes/config.yaml (AgentMail MCP server api_key)
  4. macOS Keychain (gh CLI token - via gh auth)
  5. ~/.hermes/school-core-fm-config/.env (FirstMate config)

Idempotent: running twice is safe (overwrites, doesn't duplicate).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).parent.parent
SECRETS_ENC = REPO_ROOT / ".secrets" / "secrets.enc"
SECRETS_JSON = REPO_ROOT / ".secrets" / "secrets.json"  # transient, gitignored

# Age private key locations (in order of preference)
AGE_KEY_PATHS = [
    Path(os.environ.get("AGE_KEY_FILE", "")) if os.environ.get("AGE_KEY_FILE") else None,
    Path.home() / ".config" / "age" / "keys.txt",
    Path.home() / ".age" / "key.txt",
    REPO_ROOT / ".secrets" / "recipient.txt",
]
AGE_KEY_PATHS = [p for p in AGE_KEY_PATHS if p]


def find_age_key() -> Optional[Path]:
    """Locate the age private key file."""
    for p in AGE_KEY_PATHS:
        if p and p.exists():
            return p
    return None


def decrypt_secrets() -> dict:
    """Decrypt secrets.enc and return the parsed JSON dict."""
    if not SECRETS_ENC.exists():
        print(f"ERROR: {SECRETS_ENC} not found.")
        print("To create it:")
        print("  1. Create .secrets/secrets.json with your keys")
        print("  2. Run: age -r <recipient> -o .secrets/secrets.enc .secrets/secrets.json")
        sys.exit(1)

    key_path = find_age_key()
    if not key_path:
        print("ERROR: No age private key found.")
        print(f"  Searched: {[str(p) for p in AGE_KEY_PATHS if p]}")
        print("  Set AGE_KEY_FILE env var, or run: age-keygen -o ~/.config/age/keys.txt")
        sys.exit(1)

    # Decrypt to temp file first (avoid leaving plaintext on disk longer than needed)
    with tempfile.NamedTemporaryFile(mode='wb', suffix='.json', delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        result = subprocess.run(
            ["age", "--decrypt", "--identity", str(key_path), "-o", str(tmp_path), str(SECRETS_ENC)],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"Decryption failed: {result.stderr}")
            sys.exit(1)

        data = json.loads(tmp_path.read_text())
        if not isinstance(data, dict):
            print("ERROR: secrets.enc must contain a JSON object")
            sys.exit(1)
        return data
    finally:
        # Secure delete
        if tmp_path.exists():
            tmp_path.unlink()


def write_env_file(path: Path, keys: dict, dry_run: bool = False) -> list[str]:
    """Write a .env file from a dict of key->value pairs. Returns list of changes."""
    changes = []
    lines = []

    existing = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, _, v = line.partition('=')
                existing[k.strip()] = v.strip()

    for key, value in keys.items():
        old = existing.get(key, '<absent>')
        if old != str(value):
            changes.append(f"  {key}: {old[:20]}... -> {str(value)[:20]}...")
        lines.append(f"{key}={value}")

    # Preserve comments from existing file
    if path.exists():
        header_lines = []
        for line in path.read_text().splitlines():
            if line.strip().startswith('#'):
                header_lines.append(line)
        if header_lines:
            lines = header_lines + [''] + lines

    content = '\n'.join(lines) + '\n'

    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        os.chmod(path, 0o600)  # owner read/write only

    return changes


def update_hermes_config(secrets: dict, dry_run: bool = False) -> list[str]:
    """Update ~/.hermes/config.yaml with AgentMail API key."""
    changes = []
    hermes_config = Path.home() / ".hermes" / "config.yaml"

    if not hermes_config.exists():
        return ["  ~/.hermes/config.yaml not found - skipping"]

    content = hermes_config.read_text()
    original = content

    # Update AgentMail MCP server api_key
    agentmail_key = secrets.get('AGENTMAIL_API_KEY', '')
    if agentmail_key:
        # Find the agentmail mcp server block and update its api_key
        import re
        pattern = r'(url: https://mcp\.agentmail\.to/mcp\?apiKey=)([^\s\n]+)'
        if re.search(pattern, content):
            content = re.sub(pattern, f'\\g<1>{agentmail_key}', content)
            changes.append(f"  agentmail MCP api_key updated")
        else:
            changes.append(f"  agentmail MCP server block not found - skipping")

    if content != original and not dry_run:
        hermes_config.write_text(content)

    return changes


def distribute_gh_token(secrets: dict, dry_run: bool = False) -> list[str]:
    """Store GitHub token in macOS Keychain (gh CLI already does this on auth)."""
    changes = []
    gh_token = secrets.get('GITHUB_TOKEN', '')
    if not gh_token:
        return []

    # Check if gh is already authed
    result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    if result.returncode == 0 and 'Logged in' in result.stdout:
        changes.append(f"  gh already authenticated (token in keychain)")
    else:
        # Store in keychain via gh auth
        if not dry_run:
            proc = subprocess.run(
                ["gh", "auth", "login", "--with-token"],
                input=gh_token, capture_output=True, text=True
            )
            if proc.returncode == 0:
                changes.append(f"  gh token stored in keychain")
            else:
                changes.append(f"  FAILED to store gh token: {proc.stderr[:100]}")
        else:
            changes.append(f"  [dry-run] would store gh token in keychain")

    return changes


def main():
    dry_run = "--dry-run" in sys.argv

    if dry_run:
        print("=== DRY RUN MODE (no changes) ===\n")

    # 1. Decrypt secrets
    print("Decrypting secrets.enc...")
    secrets = decrypt_secrets()
    print(f"  Found {len(secrets)} keys: {', '.join(sorted(secrets.keys()))}\n")

    # 2. Define key routing (which keys go where)
    routing = {
        'school-core/.env': [
            'OMNIROUTE_API_KEY', 'REQUIRE_API_KEY', 'STORAGE_ENCRYPTION_KEY',
            'OPENROUTER_API_KEY', 'GITHUB_TOKEN', 'AGENTMAIL_API_KEY',
            'AGENTMAIL_SCHOOL_INBOX', 'BEADS_CREDENTIAL_KEY',
        ],
        '~/.omniroute/.env': [
            'OMNIROUTE_API_KEY', 'REQUIRE_API_KEY', 'STORAGE_ENCRYPTION_KEY',
            'OPENROUTER_API_KEY',
        ],
        '~/.hermes/school-core-fm-config/.env': [
            'GITHUB_TOKEN', 'OMNIROUTE_API_KEY',
        ],
    }

    all_changes = {}

    # 3. Distribute .env files
    for location, key_names in routing.items():
        path = Path(location).expanduser()
        subset = {k: secrets[k] for k in key_names if k in secrets}
        if not subset:
            continue

        print(f"Writing {path}...")
        changes = write_env_file(path, subset, dry_run=dry_run)
        if changes:
            all_changes[location] = changes
            for c in changes:
                print(c)
        else:
            print(f"  (no changes needed)")
        print()

    # 4. Update Hermes config (AgentMail MCP)
    print("Updating ~/.hermes/config.yaml (AgentMail MCP)...")
    hermes_changes = update_hermes_config(secrets, dry_run=dry_run)
    if hermes_changes:
        all_changes['~/.hermes/config.yaml'] = hermes_changes
        for c in hermes_changes:
            print(c)
    print()

    # 5. GitHub token to keychain
    print("Updating GitHub token in keychain...")
    gh_changes = distribute_gh_token(secrets, dry_run=dry_run)
    if gh_changes:
        all_changes['keychain:gh'] = gh_changes
        for c in gh_changes:
            print(c)
    print()

    # Summary
    if dry_run:
        print("=== DRY RUN COMPLETE - no files modified ===")
    elif all_changes:
        print(f"=== Distributed keys to {len(all_changes)} locations ===")
    else:
        print("=== All keys already up-to-date ===")


if __name__ == "__main__":
    main()
