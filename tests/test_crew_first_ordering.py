"""Crew-eligible issues must be offered to the crew before the budget is spent.

WHY THIS EXISTS
---------------
The crew has never completed a real issue. Live run 32319064467 logged, on every
single issue::

    [issue_bridge] #340: crew admission denied (insufficient_cycle_time) — direct path
    [issue_bridge] #339: crew admission denied (insufficient_cycle_time) — direct path

Not a budget bug — an ORDERING bug. Measured from that run:

    pre-bridge overhead (checkout+venv+orca+preflights)   83s
    direct-path cost PER issue (full two-judge review)   634s
    first crew admission check happened at              1351s into an 1800s job
    remaining at that moment                            449s   (needs 930s)

``crew_admission.decide_admission`` requires
``crew_timeout(900) * cap(1) + reserve(30) = 930s`` REMAINING. The admission
check lives INSIDE the per-issue loop (issue_bridge.py ~1162), and the loop
iterates in plain fetch order (``for issue in issues``) with no prioritisation.
So two direct-path issues burn 21 minutes first and the crew is never reachable.

If the crew-eligible issue is offered FIRST, the check sees ~1717s remaining —
enough for a full diploma task (720s) with ~787s spare for grading.

Three agents converged on this independently (lucas, student-ci, student-whymage).

NOTE ON SCOPE: this fixes ADMISSION only. student-whymage's reverse why-tree
proved a second blocker sits behind it — the artifact handshake at
crew_dispatch.py:860-910 has rejected every real issue that reached "done"
(``artifact_evidence_missing``, ``artifact_identity_mismatch``). Reordering gets
us to that experiment; it does not by itself produce a completed crew issue.
"""

from unittest.mock import patch

import issue_bridge


class TestCrewEligibleOrdering:
    def test_crew_eligible_issues_are_offered_first(self):
        """A crew-eligible issue must sort ahead of crew-ineligible ones.

        REGRESSION: the loop consumed issues in fetch order, so direct-path
        work spent the cycle budget before any crew-eligible issue was reached
        and admission always failed with insufficient_cycle_time.
        """
        issues = [
            {"issue_number": 1, "title": "direct only", "domain": "docs",
             "difficulty": "easy"},
            {"issue_number": 2, "title": "crew eligible", "domain": "code-implementation",
             "difficulty": "medium"},
            {"issue_number": 3, "title": "direct only", "domain": "docs",
             "difficulty": "easy"},
        ]

        def eligible(issue):
            return issue["domain"] == "code-implementation"

        ordered = issue_bridge._order_crew_first(issues, eligible)

        assert ordered[0]["issue_number"] == 2, (
            "the crew-eligible issue must be offered first, or the admission "
            f"check runs after the budget is gone; got order "
            f"{[i['issue_number'] for i in ordered]}"
        )

    def test_relative_order_is_otherwise_stable(self):
        """Reordering must not otherwise scramble the queue.

        Issue order encodes retry/priority intent elsewhere in the bridge, so
        the sort must be stable: only the crew-eligible/ineligible partition
        moves.
        """
        issues = [
            {"issue_number": 10, "domain": "docs"},
            {"issue_number": 11, "domain": "docs"},
            {"issue_number": 12, "domain": "code-implementation"},
            {"issue_number": 13, "domain": "docs"},
            {"issue_number": 14, "domain": "code-implementation"},
        ]
        ordered = issue_bridge._order_crew_first(
            issues, lambda i: i["domain"] == "code-implementation"
        )
        nums = [i["issue_number"] for i in ordered]
        assert nums == [12, 14, 10, 11, 13], f"unstable partition: {nums}"

    def test_no_crew_eligible_issues_leaves_order_untouched(self):
        issues = [{"issue_number": n, "domain": "docs"} for n in (7, 8, 9)]
        ordered = issue_bridge._order_crew_first(issues, lambda i: False)
        assert [i["issue_number"] for i in ordered] == [7, 8, 9]

    def test_all_crew_eligible_leaves_order_untouched(self):
        issues = [{"issue_number": n, "domain": "code-implementation"} for n in (7, 8, 9)]
        ordered = issue_bridge._order_crew_first(issues, lambda i: True)
        assert [i["issue_number"] for i in ordered] == [7, 8, 9]

    def test_eligibility_errors_do_not_drop_issues(self):
        """A raising eligibility probe must never lose an issue.

        Capability resolution can fail (live run: "No role found for score
        24.13"). An issue whose eligibility cannot be determined must still be
        processed — treated as ineligible, never silently dropped.
        """
        issues = [
            {"issue_number": 20, "domain": "docs"},
            {"issue_number": 21, "domain": "boom"},
            {"issue_number": 22, "domain": "code-implementation"},
        ]

        def flaky(issue):
            if issue["domain"] == "boom":
                raise RuntimeError("capability policy unavailable")
            return issue["domain"] == "code-implementation"

        ordered = issue_bridge._order_crew_first(issues, flaky)
        assert sorted(i["issue_number"] for i in ordered) == [20, 21, 22], (
            "an issue was dropped when its eligibility probe raised"
        )
        assert ordered[0]["issue_number"] == 22

    def test_empty_list_is_safe(self):
        assert issue_bridge._order_crew_first([], lambda i: True) == []
