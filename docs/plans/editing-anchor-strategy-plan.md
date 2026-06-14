# Editing Anchor Strategy

**Problem**: The `edit` tool's `oldString` matching is a substring search — it fails with "Found multiple matches" or silently matches the wrong spot when code is reformatted, lines are rearranged, or whitespace changes. The current habit of adding "more surrounding lines" is fragile and tokens-wasteful.

**Goal**: Eliminate fragile text-anchor edits. Every edit should use an anchor that is *structurally unique* and *resilient to formatting changes*.

---

## Tool Selection Decision Tree

```
What are you doing?
│
├─ Renaming a symbol (function, class, variable)?
│  └─ lsp_rename — language-aware, renames all references. Just file + line + char.
│
├─ Changing a function signature, adding params, wrapping calls, restructuring logic?
│  └─ ast_grep_replace — matches AST structure, ignores formatting.
│     Pattern: $X = enrich_prompt($$$)
│     Rewrite: $X = enrich_prompt($$$, vault_path=vault_path)
│
├─ Swapping/add/changing an import line?
│  ├─ ast_grep_replace: pattern "from $MOD import $$$"
│  └─ edit: oldString="from context_orchestrator import enrich_prompt"
│     (import lines are unique per module — semantic anchor)
│
├─ Changing a unique string (docstring, error message, constant value)?
│  └─ edit: oldString=""""Try candidates in score order...""""
│     (docstrings and error strings are structurally unique)
│
├─ Changing a function body (no AST pattern possible)?
│  ├─ edit anchored on: the function signature
│  │  oldString="def run_task("
│  │  (unique per module — semantic anchor)
│  └─ edit anchored on: a unique comment/string inside the function
│     oldString="# Build candidate list sorted by score descending"
│
└─ Generic code block with no unique identifiers nearby?
   ├─ Add a comment to anchor on first, then edit
   └─ Worst case: contiguous lines. Accept fragility. Prefer #1-5.
```

### Priority Order (always try top first):

1. **`lsp_rename`** — rename-only. Zero effort, perfect accuracy.
2. **`ast_grep_replace`** — structural. Immune to all formatting changes.
3. **`edit` with semantic anchor** — import line, function signature, docstring, unique error string, unique constant.
4. **`edit` with anchor comment** — add a unique comment if none exists, then anchor on it.
5. **`edit` with contiguous lines** — fragile, last resort only.

---

## Anchor Type Catalog

### Type A: AST Structural (ast_grep_replace)

Anchors on the *shape* of code, not the text. Survives reformatting, whitespace changes, line breaks.

| Change Type | Pattern | Rewrite |
|---|---|---|
| Add param to call | `$X = enrich_prompt($$$)` | `$X = enrich_prompt($$$, vault_path=vault_path)` |
| Wrap in condition | `context_blob = enrich_prompt(...)` | `context_blob = enrich_prompt(...)` + `if context_blob:` blocks |
| Replace function body | `def $NAME($$$) { $$$ }` | `def $NAME($$$) { $$$ }` |
| Change import source | `from $OLD import $$$` | `from $NEW import $$$` |

**When to use**: Any structural change — adding params, wrapping calls, changing control flow, replacing implementations.

### Type B: Semantic String (edit)

Anchors on things the language guarantees are unique within a scope.

| Anchor | Example | Why Unique |
|---|---|---|
| Import line | `from context_orchestrator import enrich_prompt` | One per module |
| Function signature | `def run_task(` | One per module (fns can't share name in same scope) |
| Class declaration | `class ScoreStore:` | Same — unique per module |
| Docstring | `"""Try candidates in score order..."""` | Unique across the file |
| Error message | `f"Unknown agent '{force_agent}'"` | Typically unique across the project |
| String constant | `SYSTEM_PROMPTS = {` | Dict literal or constant — unique per module |
| Unique comment | `# Build candidate list sorted by score descending` | Our own convention — we control uniqueness |

**When to use**: Import changes, signature changes, docstring changes, replacing specific string constants.

### Type C: Anchor Comment (edit)

When the target site has no unique identifier, *add one first*, then edit.

```python
# === BAD: no unique anchor nearby
store.set_score(agent, domain, float(val))
...
except ValueError:
    print("  Invalid score value")

# === GOOD: add an anchor comment (in a prior edit or as step 1)
# Update score for agent
store.set_score(agent, domain, float(val))
...
except ValueError:
    print("  Invalid score value")
```

**Rule**: Anchor comments should be minimal, descriptive, and mark decision points, not explain code. `# Update score for agent` not `# This updates the score for the given agent`.

**When to use**: The middle of a large block where no semantic anchors exist and AST patterns can't isolate the target.

### Type D: Contiguous Lines (edit — last resort)

Match N lines of surrounding code. Fragile. Breaks on ANY formatting change.

```python
oldString = """    context_blob = enrich_prompt(domain, prompt)
    if context_blob:
        system_prompt = system_prompt + context_blob

    # Build candidate list sorted by score descending
    if force_agent:
"""
```

One indent change, one blank line added, one comment tweaked — match fails.

**When to use**: Only when no other anchor type can reach the target. Accept that it may break.

---

## Audit: Recent Fragile Edits in This Codebase

### Edit 1: Wiring enrich_prompt into director.py

**What was done**: Added `enrich_prompt()` call in `run_task()`.

**How it was done**: `edit` with contiguous-line anchor on `if not system_prompt:` block:

```
oldString = """    if not system_prompt:
        system_prompt = SYSTEM_PROMPTS.get(domain, DEFAULT_SYSTEM_PROMPT)

    context_blob = enrich_prompt(domain, prompt)
    if context_blob:
        system_prompt = system_prompt + context_blob

    # Build candidate list sorted by score descending
    if force_agent:
"""
```

**How it should have been done**:
- **Option A (AST)**: `ast_grep_replace` pattern: `if not system_prompt:\n    system_prompt = $$$
``` with rewrite that inserts the context injection before `# Build candidate list`. But that's complex for a multi-statement insertion.
- **Option B (Semantic anchor, better)**: Anchor on the unique comment `# Build candidate list sorted by score descending`. The `edit` would replace `# Build candidate list` and the preceding lines, catching any changes to the context injection code. Much tighter than the full 10-line block.
- **Option C (Anchor comment, best)**: Add a unique comment like `# Enrich prompt with vault context` at the insertion point in a prior step, then anchor on that for subsequent edits.

### Edit 2: Adding import for enrich_prompt

**What was done**: Added `from context_orchestrator import enrich_prompt` import.

**How it was done**: `edit` with contiguous-line anchor on the existing import block.

**How it should have been done**: `edit` with semantic anchor on one of the existing import lines (e.g., `from engram_adapter import engram_available, save_trajectory as engram_save, delete_observation`). Append the new import after it. Unique per module.

---

## Conventions

### 1. Prefer AST-first

Before reaching for `edit`, ask: "Can this be expressed as an AST pattern?" If yes, use `ast_grep_replace`. AST patterns survive reformatting, linter fixes, and manual whitespace changes.

### 2. Imports are free semantic anchors

Import lines are guaranteed unique per module. Use them as the anchor for any nearby edit, or for adding/removing adjacent imports.

```
oldString: "from engram_adapter import engram_available, ..."
newString: "from engram_adapter import engram_available, ...\nfrom context_orchestrator import enrich_prompt"
```

### 3. Function/class signatures are free semantic anchors

A function name is unique within its module. `def run_task(` identifies exactly one spot. Use it as the anchor for any change inside that function.

```
oldString: "def run_task("
newString: "def run_task("  # edit replaces the signature and beyond
```

But note: `edit` with just a signature as oldString replaces FROM that signature to the matching oldString content. You need enough content after the signature to be unique. So pair the signature with a unique docstring line or the first unique code line inside.

### 4. Add anchor comments proactively

When editing a function that has no unique internal identifiers, add a `# <purpose>` comment at the edit site first. This makes all future edits to that area easy.

### 5. Never match more than 3 non-unique lines

If your `oldString` is 5+ lines of surrounding code with no unique identifiers, you're doing option 4 (fragile contiguous lines). Stop and find a better anchor.

### 6. One anchor comment per logical section

Don't sprinkle comments everywhere. One per 20-30 line section is plenty. The comment should mark *what* (decision/intent), not *how* (implementation).

---

## What This Unblocks

| Current Pain | After Change |
|---|---|
| `edit` fails after reformatting → wasted retries | AST patterns survive all formatting |
| Need to add "more context" to disambiguate edits | Semantic anchors are self-disambiguating |
| Can't edit near similar code blocks | Anchor comments make any spot addressable |
| Imports get duplicated because edit matched wrong one | Import line as anchor is always unique |
