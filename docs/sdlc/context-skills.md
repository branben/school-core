# Declared context + skills: phase mapping

> The "accurate skills at each phase" layer for the SDLC loop. Three tools
> provide it, all declared in [`envit.json`](../../envit.json):
>
> - **envit** — declarative context/skills for agents (virtualenv for repo
>   context). Committed manifest + lockfile reproduce the same context and
>   skills on any machine/CI/cloud lane.
> - **ripwire** — the ripgrep of AI context: ranked deterministic call graph,
>   blast radius, tests-to-run, quality deltas. Ships the phase-shaped skills
>   this repo declares.
> - **plannotator `effective-html` + `guides`** — the authoring skills for the
>   "proper documentation type" contracts (`phase-map.md`): wireframe,
>   prototype, plan, diagram, pitch-doc, and the Guided Review export.

Complementary to [`../templates/phase-map.md`](../templates/phase-map.md) (which
maps phase → proper documentation type → authoring skill). This page maps
phase → **agent-native skills** that make the loop's *implementation work*
accurate and verifiable.

## The three layers, not in tension

| Layer | Role | Where it lives |
|---|---|---|
| **Plannotator `effective-html` / `guides`** (authoring contract) | Proper documentation types + guided review | `docs/templates/` (canonical contracts) + declared in `envit.json` |
| **envit** (declaration) | Declare WHICH repos+skills a project keeps; reproduce pinned | `envit.json` + `envit.lock.json` (committed) |
| **ripwire** (agent context) | Provide the per-moment code skills (blast radius, test gate, quality delta) | skills declared under `skills` in `envit.json` |

## Phase → skill mapping (agent-native)

| Phase | Ripwire skill | Opening move | Verifies |
|---|---|---|---|
| **Prime** | `ripwire-orient` | `--recall` / `--report` / `--for` | what matters in a cold repo |
| **Plan** | `ripwire-before-you-build` | `--seams` / `--recall` / `--for` | what to touch, size estimate, interface contracts |
| **Plan / Implement** | `ripwire-navigate` | `--callers` / `--callees` / `--impact` / `--connect` | who calls this, is it safe to change |
| **Implement** | `ripwire-reuse-first` | `--exemplar` / `--clones` | reuse over duplication; copy the repo's own pattern |
| **Validate** | `ripwire-change-check` | `--affected` / `--situ` / `--test-gate` / `--quality-delta` | which tests must run for THIS change; did I make it worse |
| **Validate** | `ripwire-write-tests` | `--seams` + `--callers` | test the right seams |
| **Review / PR** | `ripwire-change-check` | `--pr-context` / `--merge-scout` / `--edit-check` | am I ready to push; is it safe to merge |
| **Security** | `ripwire-security-scan` | `--lint` + `--scan-skills` | is third-party skill/config safe to use |
| **Handoff** | `ripwire-handoff` | handoff bundle | the next agent gets the same state, not re-grepped |

The `ripwire-router` skill is the entry: "not sure which verb fits? `ripwire
<dir> --help-task="<task>"` names ONE command."

## Phase → authoring skill mapping (plannotator `effective-html` + `guides`)

Maps the "proper documentation type" column of `phase-map.md` to the declared
authoring skills. These are the *producers* of the HTML artifacts; ripwire is
the *context* around them.

| Phase (form) | Authoring skill | Produces |
|---|---|---|
| Pre-PRD exploration / PRD alternatives | `html-wireframe` | structural, low-fi directions |
| PRD | `design-artifact` + `html` (router) | Tier-3 pitch-doc render |
| SPEC | `html-plan` | source-commitment-preserving implementation plan |
| Prime | `html-diagram` | Tier-2 module map |
| Prototype / Validate | `html-prototype` | states/boundary/export prototype |
| Review | `plannotator-guide` | Guided Review: chaptered walkthrough of a diff → one portable HTML |

The `html` skill is the router: pick the right form (`html-wireframe` /
`html-prototype` / `html-plan` / `html-diagram`) for the artifact's tier and
purpose, per `TIER.md`.

### Per-phase templates (Effective HTML guide)

Each phase's documentation follows the Effective HTML guide's discipline
("use the examples as references and ask your agent for the artifact you
need") and shares one CSS register. Templates live in `docs/templates/` and
satisfy the shared build + verify contract in `TIER.md`:

| Phase | Canonical `docs/templates/` artifact | Form |
|---|---|---|
| Pre-PRD exploration | `exploration-wireframe.html` | Tier-1 grayscale wireframe, 2-3 directions, keyboard selector |
| PRD alternatives (GF/BF) | `prd-alternatives.html` | Tier-1 side-by-side decision cards → one pick |
| PRD | `prd-pitch.html` | Tier-3 pitch-doc (demo → pitch → objections) |
| Ticket | `ticket-risk-map.html` | risk-map view over beads data (severity cards) |
| SPEC | `spec-implementation-plan.html` | Tier-3 implementation plan render |
| Prime | `prime-module-map.html` | Tier-2 SVG diagram, hot path + entry points |
| Plan | `plan-tweakable.html` | milestones + data-flow + key code + risks + open questions |
| Validate | `validate-prototype.html` | Tier-1 click-through prototype with works/visual boundary |
| Review | `guided-review-guide.schema.json` + `review_guide_example.json` | Guided Review export (validated) |
| PR → human | `pr-writeup.html` | Tier-3 report register render |

Implement stays `.md` (dev log / unknowns-09 pattern); the loop's deviation
log remains markdown by design. Repo map: `docs/templates/phase-map.md`.

## Declaration policy (conservative, matching the repo)

- **Pin frozen.** `envit.json` pins `redhat-et/ripwire` at `v0.6.0` and the
  plannotator skills at their lockfile commits (`"update": "frozen"`). No
  branch-tracking. Deterministic reproduction via `envit sync --frozen`
  (lockfile-only, errors on drift).
- **modelInvocable: false.** Skills are available to agents on explicit ask
  only, never auto-invoked. This is the "third-party skills are untrusted
  until a human/reviewer opts in" rule made concrete: even after the repo
  declares them, the model cannot spontaneously run them.
- **Security gate.** Every staged third-party skill set is scanned with
  ripwire's scanner before use:
  `ripwire --scan-skills <skills-dir>` (exit 0 = clean). All declared sets
  currently scan clean: 9 picked ripwire skills, 6 `effective-html` skills,
  `plannotator-guide`, and the repo's existing `.agents/skills/`.
- **Repos are read-only dependency context.** Declared repo sources are
  materialized read-only at a pinned commit under `.envit/repos/`; never
  edited, never committed (envit gitignores `.envit/`).

## CI / machine surface

The committed **manifest + lockfile** reproduce context and skills on any
machine (`scripts/install_context_tools.sh` then either `envit sync --frozen`
where envit's git transport works, or `scripts/verify_context_lock.py --dest
.envit` which uses system `git` and is deterministic everywhere). The CI
`context` workflow installs the toolchain then runs
`scripts/verify_context_lock.py` + `ripwire --scan-skills` as the verify gate,
so local `data/tools/` and CI can never drift. The binaries themselves are
**not** tracked in git (durable, SHA-verified install under `data/tools/`,
chmod 700), matching the repo's orca-cli precedent — a fresh machine or
runner reproduces them from the script in one step.

## Status

- **Declaration complete:** `envit.json` commits the declared context/skills
  (ripwire 9 phase skills, `effective-html` 6 authoring skills,
  `plannotator-guide` 1 review skill); `envit.lock.json` pins all three repos.
- **Materialization** is machine-side. `envit sync` is preferred where its
  embedded git transport resolves; but envit v0.1.0 has two known bugs, so
  the shared gate is `scripts/verify_context_lock.py` (pure system `git`):
  1. **gix HTTP transport** — internal fetch fails with "An IO error occurred
     when talking to the server" against GitHub HTTPS even when system `git`
     clones the same repo. Reproducible on GitHub Actions runners and in this
     container.
  2. **Tag-object lockfile bug** — for an annotated-tag ref, envit writes the
     *tag object* SHA into the lockfile instead of the peeled commit; a
     `--frozen` restore then fails with "was supposed to be kind commit, but
     was kind tag." `verify_context_lock.py` hard-fails on a tag object in
     the lockfile, so the bug can never ship silently.
  The declaration is valid and portable; the script is the deterministic
  reproduction path (equivalent to `--frozen` semantics: lockfile-only,
  drift = error).
- **Activation path:** `verify_context_lock.py --dest .envit` materializes the
  declared skills into `.envit/skills/` (linked from pinned-checkout repos
  under `.envit/repos/`). That copy is scanned (`ripwire --scan-skills`)
  before use and is not committed to git (`.envit/` ignored). On machines
  where envit's transport works, `envit sync --frozen` is the equivalent
  one-shot path.
- **Still deferred:** `tot` publishing (human-only) and any skill-content
  edits to the repo's tracked `.agents/skills/` (kept minimal; new skills
  stay envit-declared rather than vendored).