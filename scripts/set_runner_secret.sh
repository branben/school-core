#!/usr/bin/env bash
# Set the RUNNER_ADMIN_TOKEN repo secret in one command, with verification
# before the write and clipboard cleanup after.
#
#   1. Create the fine-grained PAT in the GitHub UI and COPY it:
#        Settings → Developer settings → Fine-grained personal access tokens
#        (repo only; "Administration" repository permissions, read-only)
#   2. Run:  scripts/set_runner_secret.sh
#      (or  scripts/set_runner_secret.sh --paste   to type it instead)
#   3. Follow the printed follow-ups: verify CI, then revoke the old token.
#
# Security notes:
#   - The token is read from the clipboard or a hidden prompt — never a
#     command-line argument (argv is visible in `ps`), never written to disk.
#   - The secret is only written after the runners API accepts the token, so
#     an invalid or under-scoped token can never replace the live secret.
#   - The clipboard is cleared after use.
#   - Follow-up steps live in docs/security/runner-token-rotation.md.
#
# Auth split: the runners-API check runs under the NEW token (GH_TOKEN scoped
# to that one command). The secret write runs under the operator's regular
# `gh` keyring auth, because the prescribed PAT is Administration-read ONLY —
# it cannot write secrets, by design.
set -euo pipefail

REPO=${REPO:-$(git -C "$(dirname "$0")/.." remote get-url origin |
  sed -E 's#.*github\.com[:/]##; s#\.git$##')}

if [[ "${1:-}" == "--paste" ]]; then
  read -rsp "Paste new runner token (hidden): " TOKEN
  printf '\n'
else
  TOKEN=$(pbpaste 2>/dev/null || true)
  if [[ -z "$TOKEN" ]]; then
    read -rsp "Clipboard empty — paste token (hidden): " TOKEN
    printf '\n'
  fi
fi

if [[ ${#TOKEN} -lt 40 ]]; then
  echo "✗ token looks too short (<40 chars) — did the copy include the whole value?" >&2
  exit 1
fi
case "$TOKEN" in
  github_pat_*) : ;;
  *) echo "⚠ fine-grained PATs start with github_pat_ — verify you created the right kind" >&2 ;;
esac

trap 'unset TOKEN' EXIT

ONLINE=$(GH_TOKEN="$TOKEN" gh api "repos/$REPO/actions/runners" \
  --jq '[.runners[] | select(.name == "school-core-mac" and .status == "online")] | length' \
  2>/dev/null) || {
  echo "✗ runners API rejected this token — NOT setting the secret." >&2
  echo "  Likely causes: missing 'Administration' repository permission (read-only)," >&2
  echo "  wrong repo selected, or expired/revoked token." >&2
  exit 1
}

# Keyring auth, forced: GH_TOKEN stripped so a stray env var can't hijack the
# write, and the read-only PAT can't be (mis)used for it either.
printf '%s' "$TOKEN" | env -u GH_TOKEN gh secret set RUNNER_ADMIN_TOKEN --repo "$REPO" >/dev/null

printf '' | pbcopy 2>/dev/null || true   # clear the clipboard

echo "✓ RUNNER_ADMIN_TOKEN set on $REPO (runners API verified; school-core-mac online: $ONLINE)"
echo "  Clipboard cleared. Next:"
echo "  1. gh workflow run ci.yml --repo $REPO --ref main   # watch integration-gate"
echo "  2. Revoke the OLD token (only after the gate is green) — runbook step 6:"
echo "     docs/security/runner-token-rotation.md"
