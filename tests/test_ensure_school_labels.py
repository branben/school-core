"""Regression tests for _ensure_school_labels.

WHY THIS EXISTS
---------------
Live School Loop run 32319064467 processed three real sound-royale-ny issues
and reached two full two-judge verdicts — then failed to persist any of them::

    [issue_bridge] #340: two-judge review rejected: ... — school-failed
    [github_fetcher] gh error (rc=1): failed to update
        https://github.com/branben/sound-royale-ny/issues/340:
        'school-failed' not found
    failed to update 1 issue

The label DID exist. Measured on that repo:

    gh label list --repo branben/sound-royale-ny --json name -q 'length'  -> 30
    gh api repos/branben/sound-royale-ny/labels --paginate | wc -l        -> 43
    (school-* labels present in the first 30)                            -> 0

`gh label list` is page-limited (30 by default). The school labels sort past
that cutoff, so the existence check saw an incomplete set, believed they were
missing, and tried to create them — which fails because they already exist.
The verdict then could not be applied.

Second, independent bug in the same function: `_LABELS_ENSURED` is a single
process-global flag with no repo key. The bridge is multi-repo (it processes
school-core AND sound-royale-ny in one cycle), so the first repo checked sets
the flag and every later repo skips the check entirely.
"""

import json
from unittest.mock import patch

import issue_bridge


class TestEnsureSchoolLabels:
    def setup_method(self):
        # The memo is module-level; clear it so tests are order-independent.
        issue_bridge._LABELS_ENSURED.clear()

    def teardown_method(self):
        issue_bridge._LABELS_ENSURED.clear()

    def test_paginates_label_list_so_late_sorting_labels_are_seen(self):
        """The existence check must see ALL labels, not the first page.

        REGRESSION: `gh label list --json name` returned 30 of 43 labels on
        sound-royale-ny and none of them were the school-* ones, so the bridge
        tried to create labels that already existed and the verdict update
        failed with "'school-failed' not found".
        """
        calls = []

        def fake_gh(args):
            calls.append(args)
            # Simulate a repo where the school labels DO exist. Whatever
            # mechanism the implementation uses to enumerate labels, it must
            # end up seeing them.
            if "list" in args or "labels" in " ".join(args):
                return json.dumps(
                    [{"name": f"filler-{i}"} for i in range(40)]
                    + [{"name": n} for n, _c, _d in issue_bridge._SCHOOL_LABELS]
                )
            return ""

        with patch.object(issue_bridge, "_gh_command", side_effect=fake_gh):
            issue_bridge._ensure_school_labels("owner/repo")

        create_calls = [c for c in calls if "create" in c]
        assert not create_calls, (
            "attempted to create labels that already exist: "
            f"{[c[2] for c in create_calls]} — the enumeration is truncating"
        )

    def test_enumeration_is_not_page_limited(self):
        """Whatever command enumerates labels must request every page.

        A bare `gh label list` caps at 30. The call must either use --limit with
        a high bound or the paginated API.
        """
        seen_args = []

        def fake_gh(args):
            seen_args.append(args)
            return json.dumps([])

        with patch.object(issue_bridge, "_gh_command", side_effect=fake_gh):
            issue_bridge._ensure_school_labels("owner/repo")

        enumeration = [a for a in seen_args if "create" not in a]
        assert enumeration, "no label enumeration call was made"
        joined = " ".join(enumeration[0])
        assert ("--paginate" in joined) or ("--limit" in joined), (
            f"label enumeration is page-limited: {joined!r} — "
            "it will miss labels on repos with many labels"
        )

    def test_memo_is_per_repo_not_global(self):
        """A multi-repo cycle must check each repo.

        REGRESSION: `_LABELS_ENSURED` was a single global bool. The bridge
        processes school-core AND sound-royale-ny in one run, so the first repo
        set the flag and the second was never checked — meaning a repo missing
        the labels would silently never get them.
        """
        enumerated_repos = []

        def fake_gh(args):
            if "create" not in args and "--repo" in args:
                enumerated_repos.append(args[args.index("--repo") + 1])
            return json.dumps([])

        with patch.object(issue_bridge, "_gh_command", side_effect=fake_gh):
            issue_bridge._ensure_school_labels("owner/first")
            issue_bridge._ensure_school_labels("owner/second")

        assert "owner/second" in enumerated_repos, (
            "second repo was skipped — the memo is global, not per-repo; "
            f"only checked {enumerated_repos}"
        )

    def test_label_failure_never_crashes_the_bridge(self):
        """Label plumbing is non-fatal by design."""
        with patch.object(
            issue_bridge, "_gh_command", side_effect=RuntimeError("gh exploded")
        ):
            issue_bridge._ensure_school_labels("owner/repo")  # must not raise
