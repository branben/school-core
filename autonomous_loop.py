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
        """Execute one round: bridge GitHub issues into the task pipeline."""
        from issue_bridge import bridge_issues

        self.round_count += 1
        result = {"round": self.round_count, "timestamp": datetime.now(timezone.utc).isoformat()}

        # Every 5 rounds, run staff maintenance
        if self.round_count % 5 == 0:
            return self._run_staff_round(result)

        # Bridge GitHub issues (issue_bridge handles agent selection internally)
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
