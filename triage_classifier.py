#!/usr/bin/env python3
"""
Local triage classifier -- rule-based, no model needed.
Runs entirely on CPU, zero tokens, instant.

Classification strategy:
- CATEGORY: labels > title keywords > body keywords
- STATE: labels > body override patterns > label+body heuristics

Usage:
  from triage_classifier import classify_issue
  category, state = classify_issue(title, labels, body)
"""

import re
from typing import Tuple, List


def classify_issue(title: str, labels: List[str], body: str = "") -> Tuple[str, str]:
    """Returns (category, state) for a GitHub issue."""
    label_names = [l.lower() for l in labels]
    title_l = title.lower()
    body_l = body.lower() if body else ""

    # ======== CATEGORY ========
    if any(l in label_names for l in ["t-enhancement", "enhancement"]):
        category = "enhancement"
    elif any(l in label_names for l in ["t-bug", "bug"]):
        category = "bug"
    else:
        bug_kw = sum(1 for k in ["bug", "fix", "crash", "error", "broken", "fail", "regression"] if k in title_l)
        enh_kw = sum(1 for k in ["implement", "add", "feature", "support", "sep-"] if k in title_l)
        category = "bug" if bug_kw > enh_kw else "enhancement"

    # ======== STATE ========
    # Layer 1: Explicit label mappings (always win)
    if "ready-for-agent" in label_names:
        state = "ready-for-agent"
    elif "needs-info" in label_names:
        state = "needs-info"
    elif "kilo-triaged" in label_names or "kilo-duplicate" in label_names:
        state = "ready-for-human"
    # Layer 2: Body overrides (only clear-cut signals)
    elif re.search(r"^tracking implementation", body_l) and len(body_l) < 300:
        state = "needs-triage"
    elif re.search(r"no (?:error )?logs?|no repro|no steps to repro|please provide|need.*more info", body_l):
        state = "needs-info"
    elif re.search(r"duplicate of|already (?:reported|tracked|fixed)", body_l):
        state = "ready-for-human"
    # Layer 3: Label + body-length heuristics
    else:
        has_type = any(l in label_names for l in ["t-enhancement", "t-bug"])
        has_priority = any(l in label_names for l in ["p0", "p1", "p2"])
        has_feature = "enhancement" in label_names or "feature" in label_names
        body_short = len(body_l) < 200

        if has_type and has_priority and not body_short:
            state = "ready-for-agent"
        elif has_type and has_priority:
            state = "needs-triage"
        elif has_type and not body_short:
            state = "ready-for-agent"
        elif has_type:
            state = "needs-triage"
        elif has_feature:
            state = "ready-for-agent"
        elif "bug" in label_names and len(label_names) == 1:
            state = "needs-triage"
        else:
            state = "needs-triage"

    return category, state


if __name__ == "__main__":
    issues = [
        {"num": 874, "gt_category": "enhancement", "gt_state": "ready-for-agent",
         "title": "Implement SEP-2106: Tool schemas conform to JSON Schema 2020-12",
         "labels": ["T-enhancement", "T-macros", "T-model", "P1"],
         "body": "Tracking implementation of SEP-2106. Brings tool schemas to full JSON Schema 2020-12. Needs code changes: Yes (Medium)."},
        {"num": 903, "gt_category": "bug", "gt_state": "needs-triage",
         "title": "ElicitationSchema serde round-trip silently drops enumNames",
         "labels": ["bug"],
         "body": "Deserializing into ElicitationSchema and serializing back loses enumNames. EnumSchema is untagged. Minimal repro."},
        {"num": 891, "gt_category": "enhancement", "gt_state": "needs-triage",
         "title": "Implement SEP-1865: MCP Apps - Interactive User Interfaces for MCP",
         "labels": ["T-enhancement"],
         "body": "Tracking implementation of SEP-1865 for the 2026-07-28 MCP spec release."},
        {"num": 886, "gt_category": "enhancement", "gt_state": "ready-for-agent",
         "title": "Implement SEP-414: W3C Trace Context propagation in _meta",
         "labels": ["T-enhancement", "T-model", "T-transport", "P2"],
         "body": "SEP-414 implementation. Stage: accepted. Priority: P2. Needs code changes. Summary: propagate W3C trace context."},
        {"num": 878, "gt_category": "bug", "gt_state": "needs-triage",
         "title": "Implement SEP-2351: RFC 8414 well-known URI suffix handling",
         "labels": ["T-bug", "T-security", "T-transport", "P1"],
         "body": "SEP-2351: Specify RFC 8414 well-known URI suffix. Client already builds candidates. Summary: clarify metadata discovery."},
        {"num": 3707, "gt_category": "bug", "gt_state": "needs-triage",
         "title": "[BUG] 429 failover: antigravity executor holds requests ~41s",
         "labels": ["kilo-triaged", "kilo-duplicate", "bug"],
         "body": "antigravity executor holds requests ~41s during 429 errors. skip verdict ignored."},
        {"num": 3694, "gt_category": "bug", "gt_state": "needs-info",
         "title": "[BUG] OpenCode Plugin is not loaded",
         "labels": ["bug"],
         "body": "Plugin returns 0 models. No error logs or repro steps."},
        {"num": 3697, "gt_category": "enhancement", "gt_state": "ready-for-agent",
         "title": "[feature] Codex CLI compatibility shim: echo requested model id",
         "labels": ["enhancement"],
         "body": "Problem: wrong model displayed. Proposed Solution: shim. Reference: Soju06/codex-lb."},
        {"num": 3701, "gt_category": "bug", "gt_state": "ready-for-human",
         "title": "Repeated / duplicated characters in CLI tool-use output",
         "labels": ["kilo-triaged", "kilo-duplicate"],
         "body": "Repeated characters in CLI output. Has context comments."},
        {"num": 3706, "gt_category": "bug", "gt_state": "needs-triage",
         "title": "[BUG] Random use gemini-2.0-flash",
         "labels": ["bug"],
         "body": "Random use gemini-2.0-flash. No repro steps or logs."},
    ]

    print("=" * 80)
    print("Local Triage Classifier v3 -- Eval Results")
    print("=" * 80)
    cat_ok = state_ok = 0
    for issue in issues:
        pred_cat, pred_state = classify_issue(issue["title"], issue["labels"], issue.get("body", ""))
        c = pred_cat == issue["gt_category"]
        s = pred_state == issue["gt_state"]
        if c: cat_ok += 1
        if s: state_ok += 1
        print(f"#{issue['num']:>4} GT={issue['gt_category']}/{issue['gt_state']:<20} "
              f"Pred={pred_cat}/{pred_state:<20} {'OK' if c else 'XX':>4} {'OK' if s else 'XX':>4} "
              f"{issue['title'][:45]}")
    n = len(issues)
    print(f"\nCategory: {cat_ok/n:.0%}  State: {state_ok/n:.0%}")
