#!/usr/bin/env python3
"""
autonomous_loop.py — The Agent School scheduler that makes it run on its own.

This is the engine that keeps agents working continuously without human prompting.
It maintains a task queue, generates work items, dispatches them to the best
agent, and tracks everything through the activity log + decision log.

Task sources (in priority order):
  1. Curriculum tasks — synthetic exercises matched to each agent's level
  2. Weak-domain targeting — agents get tasks in their weakest domains
  3. Gate-prep — agents near a gate threshold get tasks aimed at crossing it
  4. Staff maintenance — periodic janitor + score-auditor runs
  5. Exploration — random domain/difficulty combos to discover capability

  6. (Optional) GitHub issues — poll a repo for actionable issues via --issues

Usage:
  python autonomous_loop.py                    # run until stopped (curriculum)
  python autonomous_loop.py --rounds 10        # run 10 rounds then stop
  python autonomous_loop.py --interval 30       # 30s between rounds
  python autonomous_loop.py --fast              # no delay between rounds
  python autonomous_loop.py --dry-run           # show what would happen, don't execute
  python autonomous_loop.py --issues --repo user/repo  # poll GitHub issues instead of curriculum
"""

import argparse
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from scoring import ScoreStore, GATES
from director import run_task, evaluate_and_update, run_staff
from activity_log import get_log, ActivityType
from decision_log import get_decision_log, DecisionType
from issue_bridge import bridge_issues, load_config as load_github_config


def agent_role(agent: str, max_score: float) -> str:
    """Determine the school role based on max score."""
    if max_score >= 75:
        return "Faculty"
    if max_score >= 50:
        return "Teacher"
    if max_score >= 25:
        return "Senior Student"
    return "Student"



# ── Curriculum: synthetic task generators per domain ──

CURRICULUM = {
    "python-testing": {
        "easy": [
            "Write a pytest test for a function that checks if a string is a palindrome.",
            "Write a pytest test for a function that computes factorial(n).",
            "Write a pytest test for a function that reverses a list in-place.",
            "Write a pytest test for a function that finds the max value in a list.",
            "Write a pytest test for a function that checks if a number is prime.",
        ],
        "medium": [
            "Write pytest tests for a Stack class with push, pop, peek, and is_empty. Include edge cases.",
            "Write pytest tests for a function that parses a CSV string into a list of dicts.",
            "Write pytest tests for a caching decorator that memoizes function results.",
            "Write pytest tests for a function that merges two sorted lists into one sorted list.",
            "Write pytest tests for a URL query string parser.",
        ],
        "hard": [
            "Write a comprehensive pytest test suite for a REST API client class. Mock HTTP responses.",
            "Write pytest tests for a concurrent task queue with thread safety guarantees.",
            "Write pytest property-based tests (using hypothesis) for a sorting algorithm.",
            "Write pytest tests for an event bus system with subscribe/publish/unsubscribe.",
            "Write pytest tests for a rate limiter that allows N requests per time window.",
        ],
    },
    "python-coding": {
        "easy": [
            "Write a Python function called `is_palindrome` that takes a string and returns True if it's a palindrome.",
            "Write a Python function that computes the nth Fibonacci number iteratively.",
            "Write a Python function that counts the frequency of each word in a string.",
            "Write a Python function that removes duplicates from a list while preserving order.",
            "Write a Python function that flattens a nested list.",
        ],
        "medium": [
            "Implement a LRU cache class with get and put methods. O(1) for both operations.",
            "Implement a function that finds all anagrams of a word in a dictionary.",
            "Implement a simple CSV parser that handles quoted fields and commas within quotes.",
            "Implement a binary search tree with insert, search, and in-order traversal.",
            "Implement a function that computes the edit distance between two strings.",
        ],
        "hard": [
            "Implement a thread-safe task queue with priority support and graceful shutdown.",
            "Implement a simple HTTP server from scratch using only the socket module.",
            "Implement a basic regular expression engine supporting . * | and grouping.",
            "Implement a memory-efficient streaming JSON parser.",
            "Implement a graph data structure with BFS, DFS, and shortest path (Dijkstra).",
        ],
    },
    "code-review": {
        "easy": [
            "Review this code for bugs: `def divide(a, b): return a / b`",
            "Review this code for style issues: `x=1;y=2;z=x+y;print(z)`",
            "Review this function for edge cases: `def get_item(lst, i): return lst[i]`",
            "Review this code for naming issues: `def f(x): return [i for i in x if i > 0]`",
            "Review this code for error handling: `import json; data = json.loads(input())`",
        ],
        "medium": [
            "Review this authentication middleware for security vulnerabilities: `def auth(request): if request.headers.get('X-Token') == 'secret': return True`",
            "Review this database query function for SQL injection risks: `def get_user(name): return db.execute(f'SELECT * FROM users WHERE name = {name}')`",
            "Review this caching implementation for race conditions: `cache = {}; def get(key): if key not in cache: cache[key] = expensive_compute(key); return cache[key]`",
            "Review this file processing function for resource leaks: `def process(path): f = open(path); data = f.read(); return parse(data)`",
            "Review this API endpoint for input validation issues: `@app.route('/api/users/<id>'): def get_user(id): return db.get_user(id)`",
        ],
        "hard": [
            "Conduct a Fagan Inspection of this microservice architecture: a payment processing system with 3 services (order, payment, notification) communicating via HTTP. Identify all failure modes.",
            "Review this distributed locking implementation for correctness under network partitions: [code for Redis-based lock with TTL]",
            "Review this event-sourcing system for consistency issues: [code for event store with projections]",
            "Review this OAuth2 implementation for security vulnerabilities: [code for authorization code flow]",
            "Review this concurrent data structure for memory safety: [code for lock-free queue]",
        ],
    },
    "code-implementation": {
        "easy": [
            "Implement a function that returns the sum of all even numbers in a list.",
            "Implement a function that counts vowels in a string.",
            "Implement a function that returns the intersection of two lists.",
            "Implement a function that checks if two strings are anagrams.",
            "Implement a function that returns the nth triangular number.",
        ],
        "medium": [
            "Implement a simple key-value store with set, get, delete, and list_keys operations.",
            "Implement a function that evaluates a mathematical expression string (supports +, -, *, /, parentheses).",
            "Implement a paginator class that takes a list and page size, with methods: has_next, next_page, has_prev, prev_page.",
            "Implement a retry decorator that retries a function up to N times with exponential backoff.",
            "Implement a function that groups a list of dicts by a specified key.",
        ],
        "hard": [
            "Implement a simple in-memory database with support for SELECT, WHERE, ORDER BY, and LIMIT on JSON data.",
            "Implement a basic compiler for arithmetic expressions: lexer, parser, and evaluator.",
            "Implement a connection pool with max size, timeout, and health checking.",
            "Implement a consistent hash ring for distributed key-value storage.",
            "Implement a simple MapReduce framework that works on local files.",
        ],
    },
    "debugging": {
        "easy": [
            "Find the bug: `def factorial(n): return n * factorial(n - 1)`",
            "Find the bug: `def average(nums): return sum(nums) / len(nums)`",
            "Find the bug: `for i in range(len(lst)): if lst[i] == target: return i`  # returns wrong type",
            "Find the bug: `def remove_duplicates(lst): return list(set(lst))`  # loses order",
            "Find the bug: `def is_odd(n): return n % 2 == 1`  # fails for negative numbers",
        ],
        "medium": [
            "Debug this function that should find the longest palindromic substring but returns incorrect results for some inputs.",
            "Debug this binary search implementation that infinite-loops on certain inputs.",
            "Debug this function that merges overlapping intervals but misses some cases.",
            "Debug this tree traversal that skips some nodes.",
            "Debug this string compression function that produces incorrect output for certain patterns.",
        ],
        "hard": [
            "Debug a memory leak in this event handler system: handlers are registered but never cleaned up.",
            "Debug a deadlock in this two-lock resource manager: threads acquire locks in different orders.",
            "Debug a race condition in this concurrent counter: multiple threads increment but count is wrong.",
            "Debug this recursive descent parser that fails on left-recursive grammars.",
            "Debug this caching layer that serves stale data after updates.",
        ],
    },
    "triage-category": {
        "easy": [
            "Classify this issue: 'The app crashes when I click the submit button'",
            "Classify this issue: 'Add dark mode to the settings page'",
            "Classify this issue: 'The login page loads slowly on mobile'",
            "Classify this issue: 'Update the README with installation instructions'",
            "Classify this issue: 'The API returns 500 when sending empty body'",
        ],
        "medium": [
            "Classify this issue: 'Refactor the authentication module to use JWT tokens'",
            "Classify this issue: 'The database connection pool exhausts under load'",
            "Classify this issue: 'Add rate limiting to the public API endpoints'",
            "Classify this issue: 'The CI pipeline fails intermittently on Windows'",
            "Classify this issue: 'Migrate from REST to GraphQL for the user service'",
        ],
        "hard": [
            "Classify this issue: 'Design a new event-driven architecture for the notification system'",
            "Classify this issue: 'Implement distributed tracing across all microservices'",
            "Classify this issue: 'The system needs to handle 10x current load with same latency'",
            "Classify this issue: 'Evaluate and migrate from monolith to microservices'",
            "Classify this issue: 'Design a zero-downtime deployment strategy for the database'",
        ],
    },
}


class AutonomousScheduler:
    """Generates and dispatches tasks to keep the school running autonomously."""

    def __init__(self, store: ScoreStore = None, dry_run: bool = False,
                 issue_mode: bool = False, repo: str = "", labels: list = None):
        self.store = store or ScoreStore()
        self.dry_run = dry_run
        self.issue_mode = issue_mode
        self.repo = repo
        self.issue_labels = labels
        self.activity_log = get_log()
        self.decision_log = get_decision_log()
        self.round_count = 0
        self.task_counter = 0

    def _pick_task_for_agent(self, agent: str) -> dict:
        """Pick the best task for an agent based on their current state."""
        scores = self.store.get_all_scores().get(agent, {})
        default_score = scores.get("_default", 0)
        role = agent_role(agent, default_score)

        # Strategy 1: If agent is close to a gate, give them tasks at that difficulty
        gate_thresholds = sorted(GATES.items(), key=lambda x: x[1])
        target_difficulty = "easy"
        for gate_name, gate_thresh in gate_thresholds:
            if default_score < gate_thresh:
                # Agent is below this gate — try to reach it
                gap = gate_thresh - default_score
                if gap < 15:
                    # Close! Give them tasks at this gate's difficulty
                    target_difficulty = gate_name if gate_name != "diploma" else "hard"
                    break
                else:
                    # Not close — work at one level below
                    idx = gate_thresholds.index((gate_name, gate_thresh))
                    if idx > 0:
                        target_difficulty = gate_thresholds[idx - 1][0]
                    break

        # Strategy 2: Find weakest domain and target it
        domain_scores = {k: v for k, v in scores.items() if k != "_default"}
        if domain_scores:
            weakest_domain = min(domain_scores, key=domain_scores.get)
            strongest_domain = max(domain_scores, key=domain_scores.get)
        else:
            weakest_domain = random.choice(list(CURRICULUM.keys()))
            strongest_domain = weakest_domain

        # Strategy 3: Mix it up — sometimes give strong-domain tasks (confidence building)
        # and sometimes weak-domain tasks (growth)
        if random.random() < 0.6:
            target_domain = weakest_domain
        else:
            target_domain = random.choice(list(CURRICULUM.keys()))

        # Get curriculum for this domain + difficulty
        domain_curriculum = CURRICULUM.get(target_domain, {})
        difficulty_tasks = domain_curriculum.get(target_difficulty, [])
        if not difficulty_tasks:
            # Fallback to any available difficulty
            for d in ["easy", "medium", "hard"]:
                difficulty_tasks = domain_curriculum.get(d, [])
                if difficulty_tasks:
                    target_difficulty = d
                    break

        if not difficulty_tasks:
            # Ultimate fallback
            return {
                "domain": "_default",
                "difficulty": "easy",
                "prompt": "Write a Python function that returns the sum of two numbers.",
            }

        # Pick a random task from the curriculum
        prompt = random.choice(difficulty_tasks)

        return {
            "domain": target_domain,
            "difficulty": target_difficulty,
            "prompt": prompt,
        }

    def _select_agent(self) -> str:
        """Select which agent should work next."""
        agents = [a for a in self.store.list_agents()
                  if not a.startswith("ses_") and not a.startswith("staff:")]

        if not agents:
            return None

        # Weight agents: prefer those with fewer recent tasks (round-robin-ish)
        # but also prefer lower-scored agents (they need more practice)
        weights = []
        for agent in agents:
            score = self.store.get_score(agent, "_default")
            # Lower score = higher weight (needs more practice)
            # Also factor in role — students get more tasks
            role = agent_role(agent, score)
            role_weight = {"Student": 3, "Senior Student": 2, "Teacher": 1.5, "Faculty": 1, "Unenrolled": 0.5}
            w = (100 - score) * role_weight.get(role, 1)
            weights.append(max(w, 1))

        # Weighted random selection
        total = sum(weights)
        r = random.uniform(0, total)
        cumulative = 0
        for agent, w in zip(agents, weights):
            cumulative += w
            if r <= cumulative:
                return agent

        return agents[-1]

    def run_round(self) -> dict:
        """Execute one round: pick an agent, pick a task, dispatch it."""
        self.round_count += 1
        result = {"round": self.round_count, "timestamp": datetime.now(timezone.utc).isoformat()}

        # Every 5 rounds, run staff maintenance
        if self.round_count % 5 == 0:
            return self._run_staff_round(result)

        # Select agent
        agent = self._select_agent()
        if not agent:
            result["status"] = "no_agents"
            return result

        # Select task
        task = self._pick_task_for_agent(agent)
        self.task_counter += 1
        task_id = f"auto_{self.task_counter}"

        # Log the decision: which agent was selected and why
        score = self.store.get_score(agent, "_default")
        role = agent_role(agent, score)
        self.decision_log.log(
            DecisionType.AGENT_SELECTED,
            agent=agent,
            context={"round": self.round_count, "agent_score": score, "role": role},
            choice={"selected": agent, "domain": task["domain"], "difficulty": task["difficulty"]},
            expected=f"Agent in {role} role working on {task['domain']} at {task['difficulty']} level",
            task_id=task_id,
        )

        # Log self-directed activity
        self.activity_log.self_directed(
            agent=agent,
            action=f"chose to work on {task['domain']} ({task['difficulty']})",
            domain=task["domain"],
        )

        if self.dry_run:
            result.update({
                "status": "dry_run",
                "agent": agent,
                "domain": task["domain"],
                "difficulty": task["difficulty"],
                "prompt": task["prompt"][:80] + "...",
            })
            return result

        # Execute the task
        try:
            task_result = run_task(
                prompt=task["prompt"],
                domain=task["domain"],
                difficulty=task["difficulty"],
                force_agent=agent,
                store=self.store,
            )
        except Exception as e:
            self.activity_log.task_error(agent=agent, domain=task["domain"], error=str(e))
            result.update({"status": "error", "agent": agent, "error": str(e)})
            return result

        # Process result
        if task_result["status"] == "blocked":
            result.update({"status": "blocked", "agent": agent, "domain": task["domain"]})
            return result

        if task_result["status"] == "error":
            result.update({
                "status": "error",
                "agent": agent,
                "domain": task["domain"],
                "error": task_result.get("error", "unknown"),
            })
            return result

        # Auto-evaluate
        updated = evaluate_and_update(task_result, 70, store=self.store)

        new_score = updated.get("new_score", 0)
        old_score = updated.get("old_score", 0)
        delta = new_score - old_score
        gate_crossed = updated.get("gate_crossed")

        result.update({
            "status": "success",
            "agent": agent,
            "domain": task["domain"],
            "difficulty": task["difficulty"],
            "old_score": old_score,
            "new_score": new_score,
            "delta": delta,
            "gate_crossed": gate_crossed,
        })

        return result

    def _run_staff_round(self, result: dict) -> dict:
        """Run staff plugins as a maintenance round."""
        self.activity_log._add({
            "type": ActivityType.STAFF_RUN.value,
            "agent": "staff:scheduler",
            "description": "Scheduled staff maintenance round",
            "status": "in_progress",
        })

        if not self.dry_run:
            staff_results = run_staff()
        else:
            staff_results = [{"plugin": "dry_run", "status": "dry_run", "summary": "dry run"}]

        result.update({
            "status": "staff_round",
            "staff_results": staff_results,
        })
        return result

    def _run_issue_round(self) -> dict:
        """Poll repo for actionable GitHub issues and bridge them into task pipeline."""
        self.round_count += 1
        result = {"round": self.round_count, "timestamp": datetime.now(timezone.utc).isoformat()}

        try:
            bridged = bridge_issues(
                repo=self.repo,
                labels=self.issue_labels,
                dry_run=self.dry_run,
            )
        except Exception as e:
            result.update({"status": "error", "error": str(e)})
            return result

        if not bridged:
            result.update({"status": "idle", "reason": "no new issues"})
            return result

        result.update({
            "status": "success",
            "issues_bridged": len(bridged),
            "results": bridged,
        })
        return result

    def run(self, rounds: int = None, interval: float = 5.0, callback=None):
        """
        Main loop. Runs indefinitely (or for `rounds` rounds).
        `callback(result)` is called after each round for live updates.
        """
        mode_str = "ISSUE" if self.issue_mode else "CURRICULUM"
        print(f"🎓 Autonomous School Loop starting")
        print(f"   Agents: {len(self.store.list_agents())}")
        print(f"   Domains: {len(self.store.domains())}")
        print(f"   Interval: {interval}s")
        print(f"   Mode: {'DRY RUN | ' if self.dry_run else ''}{mode_str}")
        if self.issue_mode:
            print(f"   Repo: {self.repo}")
            print(f"   Labels: {self.issue_labels}")
        print(f"   Rounds: {'∞' if rounds is None else rounds}")
        print()

        round_num = 0
        try:
            while rounds is None or round_num < rounds:
                round_num += 1

                if self.issue_mode:
                    result = self._run_issue_round()
                else:
                    result = self.run_round()

                # Print summary
                status = result["status"]
                if self.issue_mode:
                    if status == "success":
                        issues = result.get("issues_bridged", 0)
                        print(f"  [{round_num}] Bridged {issues} issue(s)")
                    elif status == "idle":
                        print(f"  [{round_num}] Idle — {result.get('reason', '')}")
                    else:
                        print(f"  [{round_num}] ISSUE ERROR: {result.get('error', '?')[:60]}")
                elif status == "success":
                    agent = result["agent"]
                    delta = result.get("delta", 0)
                    sign = "+" if delta >= 0 else ""
                    gate = f" 🎓 {result['gate_crossed']}!" if result.get("gate_crossed") else ""
                    print(f"  [{round_num}] {agent}: {result['domain']}/{result['difficulty']} "
                          f"→ {result['old_score']:.1f}→{result['new_score']:.1f} ({sign}{delta:.1f}){gate}")
                elif status == "staff_round":
                    summaries = [f"{r['plugin']}:{r['status']}" for r in result.get("staff_results", [])]
                    print(f"  [{round_num}] Staff: {', '.join(summaries)}")
                elif status == "dry_run":
                    print(f"  [{round_num}] DRY RUN: {result['agent']} → {result['domain']}/{result['difficulty']}")
                elif status == "error":
                    print(f"  [{round_num}] ERROR: {result.get('agent', '?')} — {result.get('error', '?')[:60]}")
                elif status == "blocked":
                    print(f"  [{round_num}] BLOCKED: {result.get('agent', '?')} — no qualifying agent")

                if callback:
                    callback(result)

                # Sleep between rounds (unless fast mode)
                if interval > 0 and (rounds is None or round_num < rounds):
                    time.sleep(interval)

        except KeyboardInterrupt:
            print(f"\n⏹ Stopped after {round_num} rounds")

        # Print final stats
        print(f"\n📊 Final Stats ({round_num} rounds, {self.task_counter} tasks):")
        lb = self.store.leaderboard("_default")[:5]
        for i, (agent, score) in enumerate(lb, 1):
            gate = self.store.gate_for_score(score)
            print(f"   {i}. {agent}: {score:.1f} ({gate})")

        # Print decision correlations
        print(f"\n🔍 Decision Correlations:")
        for dtype in [DecisionType.AGENT_SELECTED.value, DecisionType.ANCHOR_CHOSEN.value]:
            corr = self.decision_log.correlate(agent=None, decision_type=dtype)
            if corr["count"] > 0:
                print(f"   {dtype}: {corr['count']} decisions, "
                      f"{corr['improvement_rate']}% improved, "
                      f"avg Δ={corr['avg_score_delta']:+.1f}")


def main():
    parser = argparse.ArgumentParser(description="Autonomous Agent School Loop")
    parser.add_argument("--rounds", type=int, default=None, help="Number of rounds (default: infinite)")
    parser.add_argument("--interval", type=float, default=5.0, help="Seconds between rounds (default: 5)")
    parser.add_argument("--fast", action="store_true", help="No delay between rounds")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without executing")
    parser.add_argument("--issues", action="store_true", help="Poll GitHub issues instead of curriculum tasks")
    parser.add_argument("--repo", default="", help="Repository to poll (owner/repo)")
    parser.add_argument("--labels", help="Comma-separated label filter for issues")
    args = parser.parse_args()

    interval = 0 if args.fast else args.interval
    labels = args.labels.split(",") if args.labels else None
    scheduler = AutonomousScheduler(
        dry_run=args.dry_run,
        issue_mode=args.issues,
        repo=args.repo,
        labels=labels,
    )
    scheduler.run(rounds=args.rounds, interval=interval)


if __name__ == "__main__":
    main()
