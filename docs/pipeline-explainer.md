# The School-Core Pipeline, Explained Like You're a Curious Kid

> A stage-by-stage walkthrough of what the pipeline actually does *today* — real
> code, a kid-level analogy at every step, and an honest note on what's still
> missing at each stage. The pipeline is "Agent School": GitHub issues are the
> students, personas are the roles, reviews are the grading panel, and scores
> are the report card.
>
> **One-line status:** wired end-to-end from issue → context → routing →
> student/crew work → verify gate → CTO+COO review → scoring → verdict card →
> live board. Gaps at the end of every section.
>
> Last reconciled: 2026-08-12 (U10 deterministic crew handshake landed).

---

## Stage 0 — The Inbox: GitHub issues arrive

**What it does.** The scheduled school-loop cron fires `bridge_issues()` every few
minutes. It fetches open issues, skips ones already processed, checks the retry
ledger, and gives each cycle a session ID.

**Code:**

```python
issues = fetch_issues(repo, labels)
processed = _load_processed()
retries = _load_retries()
cycle_session_id = cycle_session_id or f"loop-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
```

**Pedagogy.** This is the teacher's desk tray at the start of the day. Kids
(issues) slide their homework in; the teacher checks her "already graded" stack
first so nobody gets graded twice, and puts a date-stamp on the tray so she
knows which day's pile this is.

**What's missing.** The desk only accepts what's in the tray — there's no
queueing by priority. Issues are processed in fetch order, not by
difficulty/domain balance. Analogy: the teacher grades papers in the order they
land on the desk instead of sorting the math homework from the essays first.

---

## Stage 1 — Context: the student's study guide

**What it does.** `enrich_prompt()` gathers four memory layers before the student
starts: Layer 0 (CocoIndex vault glossary), Layer 1 (Serena LSP symbol
locations), Layer 2 (Engram past trajectories), Layer 3 (archival consolidation
from prior sessions).

**Code:**

```python
if domain in ("code-review", "python-testing", "_default"):
    ctx = _cocoindex_context(prompt, vault, top_k)
    if ctx:
        parts.append(ctx)

if domain in ("code-implementation", "python-coding", "python-testing", ...):
    ctx = _serena_context(prompt, repo_path, top_k)
    if ctx:
        parts.append(ctx)
```

**Pedagogy.** This is the "open-book" part of the test. Before writing the
answer, the student flips through the class glossary (CocoIndex), looks up where
`fix_thing()` actually lives in the code (Serena), reads notes from last week's
similar problem (Engram), and checks the class-wide summary sheet from previous
students (Layer 3).

**What's missing.** Layer 3 is a **read-only** book right now. The student reads
last semester's summary, but nobody writes the summary *for this* semester yet —
the write-side hook that saves `loop-*` sessions' consolidations is the pending
half. Analogy: the teacher hands out the study guide, but no one is assigned to
write the study guide for today's class.

---

## Stage 2 — The Principal routes: who does this work?

**What it does.** `_principal_dispatch()` in `conductor.py` routes the task to the
right persona based on measured competency — a Student (0-24 pts) gets easy
tasks, a Faculty (75+) gets blockers. Routing is competency-based, not random.

**Code:**

```python
def _principal_dispatch(args, store, ...):
    # picks role from COMBO_MAP by domain + the agent's EMA score
    ...
```

**Pedagogy.** This is the school principal deciding which classroom gets which
problem. The kid who aced subtraction gets the hard word problems; the kid who's
still shaky gets practice sheets. Nobody gets homework above their level, and
nobody gets bored with baby stuff.

**What's missing.** The Principal picks by *domain* and *difficulty*, but not by
*learning rate* — a kid who's improving fast is treated the same as one who's
flatlining. The deferred RouterExperience question: should routing key on domain
**and** difficulty **and** growth trajectory? Analogy: the principal promotes
solely on grade-point-average, ignoring that one kid jumped two grade levels
last month.

---

## Stage 3 — Crew dispatch: the group project (U7-U10)

**What it does.** If `CREW_ENABLED=1`, an eligible issue goes to FirstMate, which
spawns a Hermes crewmate in an Orca worktree. `dispatch_crew()` writes a brief,
spawns, polls the status file, validates `report.md` carries branch/commit/base
identity, and tears down — now with the U10 deterministic handshake.

**Code:**

```python
env["FM_STATUS_FILE"] = str(_status_path(crew_id))
env["FM_REPORT_FILE"] = str(_task_dir(crew_id) / "report.md")
# ...spawn → poll → validate identity → teardown
```

**Pedagogy.** This is sending the homework to a tutor's office: a dedicated
assistant (FirstMate) sets up a private workspace (Orca worktree), hands the
student a very specific instruction sheet (the brief), and checks the turn-in
box. The U10 handshake is the tutor's rule: *"If the student leaves without
handing anything in, I mark it `failed`, I do NOT write the answer myself."*

**What's missing (today's real gap).** The fresh-checkout live proof is still
outstanding — a clean-checkout run that writes `report.md`, reaches terminal
status, and leaves the repo untouched. The 2026-08-12 proof showed the scout
going idle without a report. Analogy: the tutor's procedure is written down, but
we haven't yet watched a brand-new student walk through it from an empty
classroom — the one live rehearsal is still on the calendar (bead
`school-core-wqz`).

---

## Stage 4 — The student works: `run_task()`

**What it does.** `director.run_task()` routes to the domain-specialized role,
makes the model call, and (on the crew path) can accept
`provided_student_output` — the crew's `report.md` becomes the deliverable,
skipping a second model call.

**Code:**

```python
provided_student_output: Optional[str] = None,
# When set (U8 crew path), skip the student model call entirely and use this
# text as the student's response. The review/scoring/bookbag pipeline is unchanged.
```

**Pedagogy.** This is the student actually writing the answer. Normally the
student thinks and writes it themselves; with the crew path, the group project's
write-up IS the answer — the teacher doesn't make them rewrite it just to prove
they did it.

**What's missing.** The model call itself is a black box — there's no per-turn
telemetry on *which* tools/skills the student actually used vs. which were
offered. The deferred work: native skill/tool telemetry that changes routing
policy. Analogy: the teacher sees the final essay but can't see which books the
student actually opened in the library.

---

## Stage 5 — Verify gate: the compiler before the critic

**What it does.** `run_verify_gate()` executes the repo's typecheck/test/lint
commands inside the Nix `verifyShell` *before* any LLM judge speaks. "Compiler
before critic."

**Code:**

```python
commands = _discover_commands(repo_path, project_verify)
if not commands:
    return _skipped_verdict("(discovery)",
        "No typecheck/test/lint commands discovered in repo.")

nix_bin = _find_nix()
if nix_bin is None:
    return _skipped_verdict("(nix)", ...)  # loud SKIPPED, not a fake compile failure
```

**Pedagogy.** This is checking the arithmetic before the essay is graded. If
`2+2=5` on the scratch paper, the teacher doesn't need a long debate about
whether the essay is good — the math already failed. And if the calculator is
broken (no Nix), the teacher says "I can't check this" out loud instead of
pretending the answer is right.

**What's missing.** Under `VERIFY_GATE_STRICT=1` an unrunnable gate escalates to
a real failure — that's wired but **not enabled** in the workflow (commented
out). Analogy: the teacher has a strict-mode policy in the handbook, but today
the class still runs in lenient mode where a broken calculator just means
"skip."

---

## Stage 6 — Two-judge review: CTO + COO

**What it does.** `_run_two_judge_review()` runs adversarial review: the CTO
checks correctness/security, the COO checks completeness/acceptance. **Both must
pass.** For Python domains, the code is actually executed in an Orca sandbox
first.

**Code:**

```python
if task.get("domain") in executable_domains:
    orca = OrcaExecutionManager()  # Raises OrcaUnavailableError if Orca is down
    code = CodeExtractor.extract(output, language=lang)
    if code.strip():
        # run in sandbox → exit 0 = PASS signal, runtime errors = HIGH findings
```

**Pedagogy.** This is the grading panel: the CTO is the science teacher ("does
the experiment actually work? is the wiring safe?"), the COO is the English
teacher ("did you answer the whole prompt? did you cover the edge cases?"). Both
have to sign off — a great idea with broken code still fails, and working code
that ignored the prompt still fails.

**What's missing.** The two-judge gate is **not** replaceable by a single
automated score — that's deferred by design. Also for non-Python repos (TS,
Rust, Go), the sandbox skips and judges read code by eye. Analogy: the science
teacher can't actually run the chemistry demo for languages her lab doesn't
support, so she grades those on the written procedure alone.

---

## Stage 7 — Pre-merge sensor: Entire review

**What it does.** `src/entire_review.py` runs `entire review` as a
**non-blocking** sensor before merge — findings surface on the board and in
notifications, but don't block.

**Pedagogy.** This is the hallway peer-check. A friendly senior student glances
at your work before you submit — their notes get taped to your paper, but they
can't fail you; that's the teacher's job.

**What's missing.** It's intentionally non-blocking. Analogy: the peer-checker
spots a serious bug but the school policy says only the teachers can hold the
paper — so the bug still gets submitted with a sticky note on it.

---

## Stage 8 — Scoring: the report card

**What it does.** `evaluate_and_update()` + `growth_tracker.py` compute a
difficulty-adjusted EMA score per persona, persisted to `data/scores.json`.
Scores are `_default`-anchored with per-domain difficulty multipliers.

**Code:**

```python
# data/scores.json
"foundry-coder-0.5b": {
    "_default": 15.0,
    "_difficulty_code-implementation": 1.0,
    "code-implementation": 40.56
}
```

**Pedagogy.** This is the report card. A hard problem solved is worth more than
an easy one (difficulty multiplier), and the score is an average over time (EMA)
— one bad week doesn't wreck the semester, and one lucky guess doesn't make you
a genius.

**What's missing.** Scores are a single number per persona — there's no per-skill
sub-report showing *which* tools or skills improved. Analogy: the report card
has one "math" grade but no breakdown of "fractions vs. geometry," so the
teacher can't tell what to assign next.

---

## Stage 9 — Bookbag + AgentMail: the verdict letter home

**What it does.** `bookbag.py` persists each task's verdict record with
file-lock protocol; `school_mail.py` sends the operator a card with CTO/COO
verdicts and `/approve` `/reject` `/fix` actions; `src/agentmail_poller.py`
watches the inbox.

**Code:**

```python
def _plain_verdict(accepted: bool) -> str:
    if accepted:
        return "The work passed both teacher reviews and is ready to merge."
    return "The work did not pass review — see the findings below before deciding."
```

**Pedagogy.** This is the note sent home in the backpack. The parent (you, the
operator) reads "passed both teacher reviews" and stamps APPROVE — or writes
"fix the test" and sends it back. The bookbag is the permanent file folder so
nothing gets lost.

**What's missing.** The async Phase 2 — teachers reviewing bookbags in their own
worktree terminals — is **not** built; today review is sync-inline, and the
poller's `/approve` path expects the inline verdict shape. Analogy: right now the
parent gets the note and the teacher is still standing in the room; the plan
where the teacher takes the paper home and reviews it overnight (with the parent
able to respond from anywhere) is still future work.

---

## Stage 10 — The board: the hallway display

**What it does.** `board.py` generates self-contained HTML kanban (a Lifecycle
column per status: retry/blocked/crew-in-flight/school-failed);
`activity_server.py` serves `/api/board.json` + SSE so the board updates live
every 15s.

**Code:**

```python
def _build_last_run_map(last_run: list[dict]) -> dict[int, dict]:
    # most recently appended entry wins per issue
    for entry in reversed(last_run):
        ...
```

**Pedagogy.** This is the classroom hallway chart — each kid's paper moves from
"In Progress" to "Under Review" to "Done" (or "Needs Work"). Visitors can watch
it update live through the window instead of walking in and interrupting class.

**What's missing.** The board is deliberately a read model — it never chooses the
next persona or changes routing. And there's no retention policy: the board
state grows forever (deferred compaction question). Analogy: the hallway chart is
a mirror, not a teacher — it shows the class but can't decide who gets which
homework, and the paper trail behind it is never archived.

---

## The full picture

```text
GitHub issue
  → Stage 0  bridge_issues()          (fetch, dedupe, retry ledger, session id)
  → Stage 1  enrich_prompt()          (Layer 0-3 context, non-blocking)
  → Stage 2  _principal_dispatch()    (competency-based persona routing)
  → Stage 3  dispatch_crew()          (FirstMate → Orca → Hermes, U10 handshake)
  → Stage 4  run_task()               (student model call / provided_student_output)
  → Stage 5  run_verify_gate()        (Nix verifyShell — compiler before critic)
  → Stage 6  _run_two_judge_review()  (Orca sandbox + CTO + COO, both must pass)
  → Stage 7  entire_review()          (non-blocking pre-merge sensor)
  → Stage 8  evaluate_and_update()    (difficulty-adjusted EMA → data/scores.json)
  → Stage 9  bookbag + AgentMail      (verdict card → /approve /reject /fix)
  → Stage 10 board + activity_server  (kanban HTML + SSE, live 15s)
```

### Open gaps, one list

| Gap | Stage | Bead / plan |
|-----|-------|-------------|
| Layer 3 write-side consolidation under `loop-*` ids | 1 | deferred (read side wired) |
| RouterExperience keying on domain/difficulty/growth | 2 | deferred design question |
| Fresh-checkout live crew proof | 3 | `school-core-wqz` (U10, in progress) |
| Skill/tool use telemetry feeding routing | 4 | deferred follow-up |
| `VERIFY_GATE_STRICT=1` not enabled in workflow | 5 | commented out in school-loop.yml |
| Async teacher review (Phase 2) + dead-letter handling | 9 | deferred |
| Board state retention/compaction policy | 10 | deferred |
| Per-skill scoring breakdown | 8 | deferred |

> Note: the docs in this repo are kept honest by design — `campus.md` marks what
> is operational vs. aspirational. This page follows the same rule: every
> "Wired" claim above is backed by code that exists on `main`; every gap is
> named, not papered over.
