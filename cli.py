import shlex
import sys
from routing import route_task, RouteResult
from scoring import ScoreStore, GATES
from director import run_task, evaluate_and_update, available_combos, run_staff, staff_list, sleep, wake, _active_sessions
from sleep_state import load_session, read_library_log, SessionNotFoundError
from trajectory import list_trajectories, count_trajectories
from engram_adapter import engram_available, get_stats as engram_stats, search_trajectories as engram_search
from context_orchestrator import DEFAULT_VAULT
import subprocess


def print_agents(store: ScoreStore):
    for name in sorted(store.list_agents()):
        scores = store.get_all_scores()[name]
        doms = sorted(scores.keys())
        print(f"  {name}")
        for d in doms:
            val = scores[d]
            gate = store.gate_for_score(val)
            print(f"    {d}: {val:.1f} ({gate})")


def print_scores(store: ScoreStore, domain: str = "_default"):
    lb = store.leaderboard(domain)
    if not lb:
        print(f"  No agents with scores for '{domain}'")
        return
    print(f"  Leaderboard for '{domain}':")
    for i, (agent, score) in enumerate(lb, 1):
        gate = store.gate_for_score(score)
        print(f"  {i}. {agent}: {score:.1f} ({gate})")


def print_gates(store: ScoreStore):
    print("  Gates:")
    for name, thr in GATES.items():
        count = len(store.qualifying_agents("_default", name))
        print(f"    {name} (≥{thr}): {count} agents")


def print_history(history: list, n: int = 10):
    if not history:
        print("  No routing history yet.")
        return
    for entry in history[-n:]:
        ts = entry.get("timestamp", "")
        dom = entry["domain"]
        diff = entry["difficulty"]
        agent = entry.get("chosen_agent", "-")
        outcome = entry.get("outcome", "")
        old_s = entry.get("old_score")
        new_s = entry.get("new_score")
        gate_x = entry.get("gate_crossed", "")
        parts = [f"[{ts}]", f"{dom}/{diff}", f"→ {agent}"]
        if outcome:
            parts.append(f"({outcome})")
        if old_s is not None and new_s is not None:
            parts.append(f"{old_s:.1f}→{new_s:.1f}")
        if gate_x:
            parts.append(f"🏅 crossed {gate_x}")
        if entry.get("blocked"):
            parts.append("BLOCKED")
        if entry.get("escalation"):
            parts.append("→ FACULTY")
        print("  " + " ".join(parts))


def do_route(store: ScoreStore, args: list, history: list):
    if len(args) < 2:
        print("  Usage: route <domain> <difficulty> [agent_name]")
        return
    domain, difficulty = args[0], args[1]
    force = args[2] if len(args) > 2 else None

    try:
        route_result = route_task(store, domain, difficulty, force)
    except ValueError as e:
        print(f"  Error: {e}")
        return

    entry = {
        "domain": domain,
        "difficulty": difficulty,
        "chosen_agent": route_result.chosen_agent,
        "blocked": route_result.blocked,
        "escalation": route_result.escalation,
    }

    if route_result.blocked:
        print(f"  No agent above gate {GATES[difficulty]} for '{domain}'. Task blocked.")
        history.append(entry)
        return

    if route_result.escalation:
        print(f"  Escalating to Faculty (Owl Alpha via OpenRouter — cost: $0)")
        history.append(entry)
        return

    print(f"  Chosen: {route_result.chosen_agent} (score {route_result.score:.1f})")
    print(f"  Eligible: {route_result.eligible_count} agent(s)")

    # Prompt user for outcome evaluation
    outcome = input("  Outcome? (s=success, p=partial, f=fail, b=bypass): ").strip().lower()
    task_scores = {"s": 70, "p": 40, "f": 10, "b": 0}
    if outcome not in task_scores:
        print("  Invalid outcome. No score updated.")
        return

    # Route through evaluate_and_update so error-as-0 and gate detection stay in one place
    from director import evaluate_and_update
    mock_result = {
        "agent": route_result.chosen_agent,
        "domain": domain,
        "status": "success" if outcome != "f" else "error" if outcome == "f" else "success",
        "trajectory": None,
    }
    updated = evaluate_and_update(mock_result, task_scores[outcome], store=store)

    delta = updated["new_score"] - updated["old_score"]
    sign = "+" if delta >= 0 else ""

    outcome_labels = {"s": "success", "p": "partial", "f": "fail", "b": "bypass"}
    print(f"  {route_result.chosen_agent} '{domain}': {updated['old_score']:.1f} → {updated['new_score']:.1f} ({sign}{delta:.1f}, {outcome_labels[outcome]})")
    print(f"  Gate: {store.gate_for_score(updated['old_score'])} → {store.gate_for_score(updated['new_score'])}")
    if updated.get("gate_crossed"):
        print(f"  🏅 Crossed gate '{updated['gate_crossed']}' (≥{GATES[updated['gate_crossed']]})")

    entry["outcome"] = outcome_labels[outcome]
    entry["old_score"] = updated["old_score"]
    entry["new_score"] = updated["new_score"]
    entry["gate_crossed"] = updated.get("gate_crossed")
    history.append(entry)


def do_run(store: ScoreStore, args: list):
    if len(args) < 3:
        print("  Usage: run <domain> <difficulty> <prompt>")
        return
    domain = args[0]
    difficulty = args[1]
    prompt = " ".join(args[2:])

    batch = not sys.stdin.isatty()

    print(f"  Routing '{domain}/{difficulty}'...")
    result = run_task(prompt, domain, difficulty, store=store)

    if result["status"] == "blocked":
        print(f"  No agent above gate {GATES.get(difficulty, '?')} for '{domain}'. Task blocked.")
        return

    # All candidates failed — auto-scored already, nothing to evaluate
    if result["status"] == "error" and not result.get("trajectory"):
        print(f"  All agents failed. Last error: {result['error']}")
        return

    # Single agent error with trajectory — still show for transparency
    if result["error"] and result.get("trajectory"):
        print(f"  Error from {result['agent']}: {result['error']}")
        print(f"  (auto-scored as failure)")
        print(f"  Trajectory saved: {result['trajectory']}")
        return

    print(f"  Agent: {result['agent']}")
    print(f"  Response ({len(result['response'])} chars):")
    for line in result["response"].splitlines()[:15]:
        print(f"    {line}")
    if len(result["response"].splitlines()) > 15:
        print(f"    ... ({len(result['response'].splitlines()) - 15} more lines)")

    print(f"  Trajectory saved: {result['trajectory']}")

    if batch:
        # Auto-evaluate: got a response = success
        evaluate_and_update(result, 70, store=store)
        old = result.get("old_score")
        new = result.get("new_score")
        crossed = result.get("gate_crossed")
        if old is not None and new is not None:
            delta = new - old
            sign = "+" if delta >= 0 else ""
            print(f"  [batch] Auto-scored: {old:.1f} → {new:.1f} ({sign}{delta:.1f})")
            if crossed:
                print(f"  🏅 Crossed gate '{crossed}' (≥{GATES[crossed]})")
        return

    # Collect human evaluation for scoring update
    eval_in = input("  Evaluation? (s=success, p=partial, f=fail, b=bypass, skip=n): ").strip().lower()
    task_scores = {"s": 70, "p": 40, "f": 10, "b": 0}
    if eval_in in task_scores:
        evaluate_and_update(result, task_scores[eval_in], store=store)
        old = result.get("old_score")
        new = result.get("new_score")
        crossed = result.get("gate_crossed")
        if old is not None and new is not None:
            delta = new - old
            sign = "+" if delta >= 0 else ""
            print(f"  Score updated: {old:.1f} → {new:.1f} ({sign}{delta:.1f})")
            if crossed:
                print(f"  🏅 Crossed gate '{crossed}' (≥{GATES[crossed]})")


def _check_vault_index() -> bool:
    """Quick check if CocoIndex has an active vault index."""
    try:
        result = subprocess.run(
            ["ccc", "search", "test", "--limit", "1"],
            capture_output=True, timeout=10, check=False, text=True,
            cwd=str(DEFAULT_VAULT),
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def do_vault(args: list):
    """Show vault context indexing status."""
    try:
        result = subprocess.run(
            ["ccc", "search", "", "--limit", "1"],
            capture_output=True, timeout=10, check=False, text=True,
            cwd=str(DEFAULT_VAULT),
        )
    except FileNotFoundError:
        print("  CocoIndex CLI (ccc) not found in PATH")
        return

    indexed = result.returncode == 0 and bool(result.stdout.strip())
    if indexed:
        print(f"  Vault: {DEFAULT_VAULT} (indexed and searchable)")
    else:
        print(f"  Vault: {DEFAULT_VAULT} (not indexed — run `ccc index`)")
    print(f"  Context injection: {'on' if indexed else 'off'}")


def do_engram(args: list):
    if not engram_available():
        print("  Engram not available (binary not found in PATH)")
        return
    if not args:
        stats = engram_stats()
        if stats:
            print("  Engram stats:")
            for k, v in stats.items():
                print(f"    {k}: {v}")
        return
    cmd = args[0]
    if cmd == "search":
        query = " ".join(args[1:]) if len(args) > 1 else "trajectory"
        results = engram_search(query, limit=10)
        if not results:
            print(f"  No Engram results for '{query}'")
            return
        print(f"  Engram memories ({len(results)}):")
        for obs_id, title, body in results:
            print(f"    [#{obs_id}] {title[:90]}")
    else:
        print(f"  Usage: engram [search <query>]")


def do_trajectories(args: list):
    domain = args[0] if args else None
    trajs = list_trajectories(domain, limit=10)
    if not trajs:
        print("  No trajectories captured yet.")
        return
    print(f"  Recent trajectories{' for ' + domain if domain else ''}:")
    for t in trajs:
        ts = t.get("timestamp", "?")[:19]
        d = t.get("domain", "?")
        a = t.get("agent", "?")
        ts_score = t.get("task_score", "-")
        err = " ERROR" if t.get("error") else ""
        print(f"    [{ts}] {d:20s} {a:30s} score={ts_score}{err}")


def do_training(domain: str):
    from director import get_training_data
    data = get_training_data(domain, min_score=50)
    if not data:
        print(f"  No training data for '{domain}' (need task_score >= 50).")
        counts = count_trajectories()
        if counts:
            print(f"  Available trajectories by domain: {counts}")
        return
    print(f"  Training data for '{domain}': {len(data)} trajectories above 50")
    print(f"  First prompt: {data[0]['prompt'][:80]}...")


def repl():
    store = ScoreStore()
    history = []

    print("Agent School — Scoring System")
    print(f"  Agents loaded: {len(store.list_agents())}")
    print(f"  Data: {store.file_path}")
    if engram_available():
        print(f"  Engram: connected")
    vault_ok = _check_vault_index()
    if vault_ok:
        print(f"  Vault: {DEFAULT_VAULT} (indexed)")
    else:
        print(f"  Vault: {DEFAULT_VAULT} (not indexed)")
    print("  Type 'help' for commands.")

    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not line:
            continue

        parts = shlex.split(line)
        cmd = parts[0]
        args = parts[1:]

        if cmd == "quit":
            break
        elif cmd == "help":
            print("  Commands:")
            print("    run <domain> <diff> <prompt>        — Execute task via real model")
            print("    route <domain> <diff> [agent]       — Simulate routing + score update")
            print("    agents                              — List all agents with scores & gates")
            print("    scores [domain]                     — Score leaderboard")
            print("    add-agent <name>                    — Register new agent (score=0)")
            print("    set-score <agent> <domain> <val>    — Manual score override")
            print("    gates                               — Gate thresholds & qualifying counts")
            print("    domains                             — List all known domains")
            print("    history [n]                         — Show last n routing decisions")
            print("    trajectories [domain]               — Show captured model runs")
            print("    training [domain]                   — Show training-ready trajectories")
            print("    vault                               — Show vault context indexing status")
            print("    engram [search <query>]             — Engram persistent memory stats/search")
            print("    combos                              — Show agent→OmniRoute mappings")
            print("    staff-list                          — List loaded Staff plugins")
            print("    staff-run [plugin_name]              — Run one or all Staff plugins")
            print("    sleep [session_id] [agent]            — Execute sleep sequence")
            print("    wake [session_id]                    — Execute wake sequence")
            print("    session-status                       — Show active sessions")
            print("    library-log                          — Show sleep/wake audit log")
            print("    export <path>                       — Export scores to JSON")
            print("    quit                                — Exit")
        elif cmd == "agents":
            print_agents(store)
        elif cmd == "scores":
            domain = args[0] if args else "_default"
            print_scores(store, domain)
        elif cmd == "add-agent":
            if not args:
                print("  Usage: add-agent <name>")
                continue
            name = args[0]
            store.add_agent(name)
            print(f"  Added '{name}' with _default score 0")
        elif cmd == "set-score":
            if len(args) < 3:
                print("  Usage: set-score <agent> <domain> <value>")
                continue
            agent, domain, val = args[0], args[1], args[2]
            try:
                store.set_score(agent, domain, float(val))
                print(f"  Set {agent} '{domain}' to {store.get_score(agent, domain):.1f}")
            except ValueError:
                print("  Invalid score value")
        elif cmd == "gates":
            print_gates(store)
        elif cmd == "domains":
            doms = store.domains()
            if doms:
                print("  Known domains:")
                for d in sorted(doms):
                    print(f"    {d}")
            else:
                print("  No domains registered.")
        elif cmd == "history":
            n = int(args[0]) if args else 10
            print_history(history, n)
        elif cmd == "export":
            if not args:
                print("  Usage: export <path>")
                continue
            import shutil
            dst = args[0]
            shutil.copy(str(store.file_path), dst)
            print(f"  Exported scores to {dst}")
        elif cmd == "route":
            do_route(store, args, history)
        elif cmd == "run":
            do_run(store, args)
        elif cmd == "trajectories":
            do_trajectories(args)
        elif cmd == "training":
            domain = args[0] if args else "_default"
            do_training(domain)
        elif cmd == "vault":
            do_vault(args)
        elif cmd == "engram":
            do_engram(args)
        elif cmd == "combos":
            print("  Agent → OmniRoute combo mappings:")
            for agent, combo in sorted(available_combos().items()):
                print(f"    {agent:30s} → {combo}")
        elif cmd == "sleep":
            sid = args[0] if args else "default_session"
            agent = args[1] if len(args) > 1 else "foundry-coder-7b"
            result = sleep(session_id=sid, agent=agent, store=store)
            print(f"  Sleep complete: {sid}")
            print(f"  State saved: {result['state'].session_id}")
            print(f"  Consolidation: {result['consolidation'].tasks_completed} tasks")
        elif cmd == "wake":
            if not args:
                print("  Usage: wake <session_id>")
                continue
            sid = args[0]
            try:
                result = wake(session_id=sid)
                state = result["state"]
                print(f"  Wake complete: {sid}")
                print(f"  Agent: {state.agent}, Tasks queued: {len(state.task_queue)}")
                print(f"  Scores: {state.scores_snapshot}")
            except SessionNotFoundError:
                print(f"  No session found for '{sid}'")
        elif cmd == "session-status":
            if not _active_sessions:
                print("  No active sessions.")
            for sid, info in _active_sessions.items():
                print(f"  {sid}: agent={info['agent']}, tasks={info['tasks_completed']}, building={info['building']}")
        elif cmd == "library-log":
            entries = read_library_log()
            if not entries:
                print("  No library log entries yet.")
            for e in entries[-10:]:
                print(f"  [{e.get('timestamp', '?')[:19]}] {e.get('event', '?')} {e.get('session_id', '?')} — {e.get('details', '')}")
        elif cmd == "staff-list":
            plugins = staff_list()
            if not plugins:
                print("  No Staff plugins loaded.")
            for p in plugins:
                print(f"  {p['name']:20s} trust={p['trust']:10s} health={p['health']}")
        elif cmd == "staff-run":
            plugin_name = args[0] if args else None
            results = run_staff(plugin_name=plugin_name)
            for r in results:
                print(f"  {r['plugin']:20s} {r['status']:10s} {r['summary']}")
                if r.get("metrics"):
                    for k, v in r["metrics"].items():
                        print(f"    {k}: {v}")
        else:
            print(f"  Unknown command '{cmd}'. Type 'help'.")


if __name__ == "__main__":
    repl()
