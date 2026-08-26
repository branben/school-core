# OmniRoute Streaming Corruption — A/B Reproduction Recipe

## Finding (2026-08-24, second-sourced by two agents)

OmniRoute (`localhost:20128`, OpenAI-compatible gateway) returns **token-shredded
SSE content on `stream:true`** for some upstream routes, while `stream:false` is
always clean. Crew silence was caused by this: the crew's terminal `done:` status
line comes from streamed model text, arrives as `dones0grq5axXXGd6:...` (no verb
match), and FirstMate marks `silent_agent`.

- `stream:false` → clean `STATUS_OK` (every route, incl gpt-4o)
- `stream:true` → `STATUSn8Xd9ZoU3z_OK` / `STATUSEOevJ3GhH5_OK` (junk between tokens)
- **Routing-dependent:** `auto/best-coding` is an alias → resolves to gpt-4o /
  gemini-3.6-flash / gemini-2.5-flash / gpt-oss per request. Some corrupt, some
  empty, some clean. **Pinning to `auto/coding:reliable` is NOT safe** — it still
  routed to a corrupting backend in a second agent's repro.
- `obfuscation` field appeared in gpt-4o chunks for one agent; absent for another.
  Direction (OmniRoute injects vs strips an OpenAI artifact) was UNPROVEN — do not
  assert it.

## A/B test (run from a host-privileged shell)

```bash
KEY="<OMNIROUTE_API_KEY>"   # or rely on x-api-key from env
PROMPT='Write exactly this line and nothing else: STATUS_OK'

# non-streaming (expect clean)
curl -m 20 -s -X POST localhost:20128/v1/chat/completions \
  -H "x-api-key: $KEY" -H "Content-Type: application/json" \
  -d "{\"model\":\"auto/best-coding\",\"messages\":[{\"role\":\"user\",\"content\":\"$PROMPT\"}],\"max_tokens\":20,\"stream\":false}" \
  -o /tmp/ns.json -w "http=%{http_code}\n"
python3 -c "import json;print(repr(json.load(open('/tmp/ns.json'))['choices'][0]['message']['content']))"

# streaming (expect shredded on gpt-4o route)
curl -m 20 -s -X POST localhost:20128/v1/chat/completions \
  -H "x-api-key: $KEY" -H "Content-Type: application/json" \
  -d "{\"model\":\"auto/best-coding\",\"messages\":[{\"role\":\"user\",\"content\":\"$PROMPT\"}],\"max_tokens\":20,\"stream\":true}" \
  -o /tmp/st.txt -w "http=%{http_code}\n"
```

## SSE delta reconstruction (detect corruption)

```python
import json, re
raw = open("/tmp/st.txt", encoding="utf-8", errors="replace").read()
parts, ups, obf = [], set(), False
for line in raw.splitlines():
    if not line.startswith("data: "): continue
    c = line[6:].strip()
    if not c or c == "[DONE]": continue
    try: obj = json.loads(c)
    except: continue
    if "obfuscation" in json.dumps(obj).lower(): obf = True
    m = obj.get("model")
    if m and any(k in m for k in ["gpt","gemini","claude","oss","nvidia","llama"]):
        ups.add(m)
    d = obj.get("choices", [{}])[0].get("delta", {})
    if "content" in d and d["content"]:
        parts.append(d["content"])
full = "".join(parts)
print("upstream:", "|".join(sorted(ups)) or "?")
print("obfuscation field seen:", obf)
print("reconstructed:", repr(full))
print("clean STATUS_OK:", full == "STATUS_OK")
print("noise-interspersed:", bool(re.search(r"STATUS.{2,}[A-Za-z0-9]", full)))
```

## Fix
Force `stream:false` for crew calls. The lever is Hermes's `auto` provider request
shape (the thing issuing the chat completion), NOT `executor.py` (crews go through
`hermes chat --query`, not `executor.py` directly). Locate where the `auto` provider
sets `stream` and disable it for crew profiles. `stream:false` was clean on every
route including gpt-4o — this is the verified-safe fix.
