"""Diagnose OmniRoute — list models + test adversarial review with error capture."""
import json
import sys
import traceback
import urllib.request
import urllib.error

sys.path.insert(0, ".")
API_KEY = "***REMOVED***"
BASE = "http://localhost:20128/v1"

# ── Step 1: List all models ────────────────────────────────────
print("=" * 60)
print("STEP 1: Available models")
print("=" * 60)
req = urllib.request.Request(
    f"{BASE}/models",
    headers={"Authorization": f"Bearer {API_KEY}", "User-Agent": "OpenCode/1.0"},
    method="GET",
)
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())
    models = data.get("data", data.get("models", []))
    print(f"Total models: {len(models)}")
    for m in sorted(models, key=lambda m: m.get("id", "")):
        mid = m.get("id", "?")
        owned_by = m.get("owned_by", "?")
        print(f"  {mid}  (by: {owned_by})")
except Exception as e:
    print(f"Models endpoint failed: {e}")

# ── Step 2: Test adversarial review call with error capture ────
print("\n" + "=" * 60)
print("STEP 2: Test adversarial review call with large prompt")
print("=" * 60)

from executor import call_model, COMBO_MAP

# Build a realistic large prompt like adversarial review uses
SYSTEM_PROMPT = """You are an adversarial code reviewer. Your ONLY job is to find flaws, gaps, and issues.
[OUTPUT FORMAT - MANDATORY]
You MUST respond with EXACTLY one JSON object and NOTHING else.
Only this exact structure:
{"findings": [{"section": "...", "issue_class": "...", "severity": "CRITICAL|HIGH|MEDIUM|LOW", "citation": "...", "description": "...", "suggestion": "..."}]}
If you find no issues, respond with: {"findings": []}"""

USER_PROMPT = """[TASK]
Domain: code-implementation
Difficulty: medium
Title: extract MIN_ROUNDS/MAX_ROUNDS constants

[STUDENT OUTPUT]
The student refactored the game loop to extract MIN_ROUNDS = 1 and MAX_ROUNDS = 10 as module-level constants in game.py. The implementation replaces all hardcoded 1 and 10 literals with the named constants. The constants are defined at module level with descriptive names. The student also updated the type hints to reference the constants where applicable.

""" + ("x" * 2000)  # Add bulk to simulate real code

print(f"System prompt length: {len(SYSTEM_PROMPT)} chars")
print(f"User prompt length: {len(USER_PROMPT)} chars")

# Test all candidate combos
combos_to_test = [
    "auto/best-free",
    "agy/gemini-3.5-flash-high",
    "agy/claude-sonnet-4-6",
]

for combo in combos_to_test:
    print(f"\n--- Testing: {combo} ---")
    print(f"  In COMBO_MAP: {combo in COMBO_MAP}")
    if combo not in COMBO_MAP:
        print(f"  SKIP — not in COMBO_MAP")
        continue
    
    try:
        # Direct _omniroute_call to get full error details
        from executor import _omniroute_call
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT},
        ]
        result = _omniroute_call(COMBO_MAP[combo], messages, timeout=60)
        choices = result.get("choices", [])
        if choices:
            content = choices[0]["message"]["content"]
            print(f"  SUCCESS — response length: {len(content)} chars")
            print(f"  First 200 chars: {content[:200]}")
            # Check if it's valid JSON
            try:
                parsed = json.loads(content.strip())
                print(f"  JSON parse: OK — keys: {list(parsed.keys())}")
            except json.JSONDecodeError:
                print(f"  JSON parse: FAILED — raw: {content[:300]}")
        else:
            print(f"  No choices in response: {json.dumps(result)[:300]}")
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {e}")
        traceback.print_exc()

# ── Step 3: Health check ───────────────────────────────────────
print("\n" + "=" * 60)
print("STEP 3: OmniRoute health")
print("=" * 60)
req = urllib.request.Request(
    f"{BASE}/models",
    headers={"Authorization": f"Bearer {API_KEY}", "User-Agent": "OpenCode/1.0"},
    method="GET",
)
try:
    with urllib.request.urlopen(req, timeout=5) as resp:
        print(f"Health: OK (status {resp.status})")
except Exception as e:
    print(f"Health: DOWN — {e}")
