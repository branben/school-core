"""
Tests for U3: PR Creator (pr_creator.py)

Run: python -m pytest tests/test_pr_creator.py -v
"""

import json
from unittest.mock import patch

import pytest

from pr_creator import _slugify, branch_name, create_pr_for_issue


# ── Slugify ────────────────────────────────────────────────────────────────

class TestSlugify:
    def test_basic_title(self):
        assert _slugify("Fix login bug") == "fix-login-bug"

    def test_removes_special_chars(self):
        assert _slugify("Bug: crash @ startup!") == "bug-crash-startup"

    def test_truncates_to_max_len(self):
        long = "a" * 100
        assert len(_slugify(long, max_len=40)) <= 40

    def test_strips_leading_trailing_hyphens(self):
        assert _slugify("--hello--") == "hello"

    def test_empty_title_returns_empty(self):
        assert _slugify("") == ""


# ── Branch Name ────────────────────────────────────────────────────────────

class TestBranchName:
    def test_format(self):
        name = branch_name(42, "Fix login bug")
        assert name == "school/issue-42-fix-login-bug"

    def test_with_special_chars(self):
        name = branch_name(100, "Bug: crash @ startup!")
        assert "school/issue-100" in name

    def test_long_title_truncated(self):
        very_long = "Implement a very long feature that just keeps going and " * 10
        name = branch_name(999, very_long)
        assert name.startswith("school/issue-999-")
        assert len(name) <= 80  # "school/issue-999-" (16) + 40 slug + some room


# ── Create PR for Issue (mocked gh CLI) ───────────────────────────────────
#
# NOTE: pr_creator was rewritten to commit via the GitHub API instead of a
# local checkout. The flow is now:
#   _resolve_base_branch  -> _gh(["repo","view",...])          (gh)
#   _resolve_base_sha     -> _gh(["api", .../commits/<b>])     (gh)
#   _create_or_reuse_branch -> _gh_api(POST git/refs)          (gh api)
#   read branch ref, read tree, blob -> tree -> commit -> ref  (gh api)
#   gh pr create                                               (gh)
# Tests therefore patch `_gh` (the renamed low-level runner) and `_gh_api`
# separately, rather than the old single `_gh_command` seam.

class TestCreatePR:
    @patch("pr_creator._gh_api")
    @patch("pr_creator._gh")
    def test_dry_run_returns_fake_url(self, mock_gh, mock_api):
        issue = {"issue_number": 1, "title": "Test", "domain": "debugging", "difficulty": "easy"}
        task_result = {"response": "print('hello')", "agent": "foundry-coder-7b"}
        url = create_pr_for_issue(issue, task_result, "user/test", dry_run=True)
        assert url == "https://github.com/user/test/pull/0"
        mock_gh.assert_not_called()
        mock_api.assert_not_called()

    @patch("pr_creator._gh_api")
    @patch("pr_creator._gh")
    def test_empty_response_returns_none(self, mock_gh, mock_api):
        issue = {"issue_number": 1, "title": "Test", "domain": "debugging", "difficulty": "easy"}
        task_result = {"response": "", "agent": "test"}
        url = create_pr_for_issue(issue, task_result, "user/test")
        assert url is None
        mock_gh.assert_not_called()
        mock_api.assert_not_called()

    @patch("pr_creator._gh_api")
    @patch("pr_creator._gh")
    def test_branch_creation_failure_returns_none(self, mock_gh, mock_api):
        # base branch + base sha resolve fine, then the ref POST fails.
        mock_gh.side_effect = [
            json.dumps({"defaultBranch": "main"}),  # repo view
            "abc123\n",                             # base sha
        ]
        mock_api.return_value = None                # POST git/refs fails
        issue = {"issue_number": 5, "title": "Fix crash", "domain": "debugging", "difficulty": "medium"}
        task_result = {"response": "def fix(): pass", "agent": "foundry-coder-7b"}
        url = create_pr_for_issue(issue, task_result, "user/test")
        assert url is None

    @patch("pr_creator._gh_api")
    @patch("pr_creator._gh")
    def test_successful_pr_creation(self, mock_gh, mock_api, tmp_path):
        mock_gh.side_effect = [
            json.dumps({"defaultBranch": "main"}),          # repo view
            "basesha123\n",                                 # base sha
            "https://github.com/user/test/pull/42",         # gh pr create
        ]
        mock_api.side_effect = [
            {"ref": "refs/heads/school/issue-10-fix-the-thing"},  # create ref
            {"object": {"sha": "branchsha456"}},                  # read branch ref
            {"sha": "treesha789"},                                # read tree
            {"sha": "blobsha111"},                                # create blob
            {"sha": "newtree222"},                                # create tree
            {"sha": "newcommit333"},                              # create commit
            {"ref": "refs/heads/school/issue-10-fix-the-thing"},  # update ref
        ]
        issue = {"issue_number": 10, "title": "Fix the thing", "domain": "debugging", "difficulty": "medium"}
        task_result = {"response": "def fix(): return 42\n", "agent": "foundry-coder-7b"}
        url = create_pr_for_issue(issue, task_result, "user/test", work_dir=str(tmp_path))
        assert url == "https://github.com/user/test/pull/42"

    @patch("pr_creator._gh_api")
    @patch("pr_creator._gh")
    def test_pr_creation_uses_correct_args(self, mock_gh, mock_api, tmp_path):
        mock_gh.side_effect = [
            json.dumps({"defaultBranch": "main"}),          # repo view
            "basesha123\n",                                 # base sha
            "https://github.com/user/test/pull/99",         # gh pr create
        ]
        mock_api.side_effect = [
            {"ref": "refs/heads/school/issue-15-add-feature"},
            {"object": {"sha": "branchsha456"}},
            {"sha": "treesha789"},
            {"sha": "blobsha111"},
            {"sha": "newtree222"},
            {"sha": "newcommit333"},
            {"ref": "refs/heads/school/issue-15-add-feature"},
        ]
        issue = {"issue_number": 15, "title": "Add feature", "domain": "code-implementation", "difficulty": "easy"}
        task_result = {"response": "# new feature\nprint('done')", "agent": "owl-alpha"}
        url = create_pr_for_issue(issue, task_result, "user/test", work_dir=str(tmp_path))
        assert url == "https://github.com/user/test/pull/99"

        # The LAST _gh call is `gh pr create` — assert its args carry repo + label.
        pr_args = mock_gh.call_args_list[-1][0][0]
        assert "pr" in pr_args and "create" in pr_args
        assert "--repo" in pr_args
        assert "user/test" in pr_args[pr_args.index("--repo") + 1]
        assert "--label" in pr_args
        assert "school-automated" in pr_args[pr_args.index("--label") + 1]

    @patch("pr_creator._gh_api")
    @patch("pr_creator._gh")
    def test_handles_repo_view_failure(self, mock_gh, mock_api):
        # Every gh call fails; _resolve_base_branch falls back, and the
        # subsequent API work cannot proceed, so no PR URL is returned.
        mock_gh.return_value = None
        mock_api.return_value = None
        issue = {"issue_number": 20, "title": "Broken", "domain": "debugging", "difficulty": "hard"}
        task_result = {"response": "# output", "agent": "foundry-coder-7b"}
        url = create_pr_for_issue(issue, task_result, "user/test")
        assert url is None


# ── Guards: blob-failure vs diff-emptiness are distinct faults ──────────────


class TestCreatePRGuards:
    """The tree-builder must abort on a no-op commit (every entry matches the
    base tree) — a different fault from blob creation failing.

    The read-tree call (mock_api index 2) is the base tree. To exercise the
    diff-emptiness guard we return a real ``tree`` list there and have the blob
    POST (index 3) hand back the SAME sha the base already holds.
    """

    @patch("pr_creator._gh_api")
    @patch("pr_creator._gh")
    def test_empty_change_is_rejected(self, mock_gh, mock_api, tmp_path):
        base_sha = "sameblob000"
        mock_gh.side_effect = [
            json.dumps({"defaultBranch": "main"}),
            "basesha123\n",
            "https://github.com/user/test/pull/8",
        ]
        mock_api.side_effect = [
            {"ref": "refs/heads/school/issue-8-noop"},
            {"object": {"sha": "branchsha456"}},
            {"sha": "basetree999", "tree": [
                {"path": "school-output/debugging/8/output.md",
                 "mode": "100644", "type": "blob", "sha": base_sha},
            ]},
            {"sha": base_sha},
            {"sha": "newtree222"},
            {"sha": "newcommit333"},
            {"ref": "refs/heads/school/issue-8-noop"},
        ]
        issue = {"issue_number": 8, "title": "Noop", "domain": "debugging", "difficulty": "easy"}
        task_result = {"response": "print('identical')\n", "agent": "owl-alpha"}
        url = create_pr_for_issue(issue, task_result, "user/test", work_dir=str(tmp_path))
        assert url is None
        called_paths = [
            c.kwargs.get("json", {}).get("message")
            for c in mock_api.call_args_list
            if c.args and c.args[0] == "POST" and "commits" in c.args[1]
        ]
        assert not called_paths, "a no-op commit was created"

    @patch("pr_creator._gh_api")
    @patch("pr_creator._gh")
    def test_real_change_still_commits(self, mock_gh, mock_api, tmp_path):
        mock_gh.side_effect = [
            json.dumps({"defaultBranch": "main"}),
            "basesha123\n",
            "https://github.com/user/test/pull/9",
        ]
        mock_api.side_effect = [
            {"ref": "refs/heads/school/issue-9-real"},
            {"object": {"sha": "branchsha456"}},
            {"sha": "basetree999", "tree": [
                {"path": "school-output/debugging/9/output.md",
                 "mode": "100644", "type": "blob", "sha": "oldblob111"},
            ]},
            {"sha": "newblob444"},
            {"sha": "newtree222"},
            {"sha": "newcommit333"},
            {"ref": "refs/heads/school/issue-9-real"},
        ]
        issue = {"issue_number": 9, "title": "Real", "domain": "debugging", "difficulty": "easy"}
        task_result = {"response": "print('changed')\n", "agent": "owl-alpha"}
        url = create_pr_for_issue(issue, task_result, "user/test", work_dir=str(tmp_path))
        assert url == "https://github.com/user/test/pull/9"

    @patch("pr_creator._gh_api")
    @patch("pr_creator._gh")
    def test_unreadable_base_tree_fails_open(self, mock_gh, mock_api, tmp_path):
        mock_gh.side_effect = [
            json.dumps({"defaultBranch": "main"}),
            "basesha123\n",
            "https://github.com/user/test/pull/10",
        ]
        mock_api.side_effect = [
            {"ref": "refs/heads/school/issue-10-unk"},
            {"object": {"sha": "branchsha456"}},
            None,
            {"sha": "newblob444"},
            {"sha": "newtree222"},
            {"sha": "newcommit333"},
            {"ref": "refs/heads/school/issue-10-unk"},
        ]
        issue = {"issue_number": 10, "title": "Unknown", "domain": "debugging", "difficulty": "easy"}
        task_result = {"response": "print('ok')\n", "agent": "owl-alpha"}
        url = create_pr_for_issue(issue, task_result, "user/test", work_dir=str(tmp_path))
        assert url == "https://github.com/user/test/pull/10"


# ── B8 Phase 2 (MIDDLE): ship the captured .patch as a tree blob ──────────


class TestCreatePRCrewPatch:
    """When crew_used and patch_path are set, create_pr_for_issue must commit
    the captured diff as an EXTRA blob in the tree (path
    school-output/<domain>/<num>/changes.patch) so it is reviewable on GitHub
    without being applied to the codebase. The patch is treated purely as
    bytes/text — never interpreted — so binary hunks survive (git diff
    --binary). This reuses the single-blob path (_blobSha) and does NOT touch
    the PR body disclaimer (build_pr_body already states the patch is NOT what
    the PR was built from).
    """

    @patch("pr_creator._gh_api")
    @patch("pr_creator._gh")
    def test_crew_patch_is_committed_as_blob(self, mock_gh, mock_api, tmp_path):
        # Write a real .patch file (binary-safe: git diff --binary is ASCII).
        patch = tmp_path / "changes.patch"
        patch.write_text(
            "diff --git a/x b/x\nindex 111..222 100644\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n- old\n+ new\n",
            encoding="utf-8",
        )
        mock_gh.side_effect = [
            json.dumps({"defaultBranch": "main"}),
            "basesha123\n",
            "https://github.com/user/test/pull/11",
        ]
        mock_api.side_effect = [
            {"ref": "refs/heads/school/issue-11-crew"},
            {"object": {"sha": "branchsha456"}},
            {"sha": "basetree789"},                    # read branch tree
            {"sha": "outblob111"},                     # output.md blob
            {"sha": "patchblob222"},                   # crew patch blob
            {"sha": "newtree333"},
            {"sha": "newcommit444"},
            {"ref": "refs/heads/school/issue-11-crew"},
        ]
        issue = {"issue_number": 11, "title": "Crew", "domain": "debugging", "difficulty": "easy"}
        task_result = {"response": "print('done')\n", "agent": "owl-alpha"}
        url = create_pr_for_issue(
            issue, task_result, "user/test",
            crew_used=True, patch_path=str(patch), work_dir=str(tmp_path),
        )
        assert url == "https://github.com/user/test/pull/11"

        # The tree POST must carry BOTH the output.md entry and the patch blob.
        # _gh_api is called as (method, path, body), so the body dict is args[2].
        tree_post = next(
            c for c in mock_api.call_args_list
            if c.args and c.args[0] == "POST" and "trees" in c.args[1]
        )
        entries = tree_post.args[2]["tree"]
        paths = {e["path"] for e in entries}
        assert "school-output/debugging/11/output.md" in paths
        patch_entry = next(
            e for e in entries if e["path"].endswith("changes.patch")
        )
        assert patch_entry["type"] == "blob"
        assert patch_entry["sha"] == "patchblob222"
        assert patch_entry["mode"] == "100644"

    @patch("pr_creator._gh_api")
    @patch("pr_creator._gh")
    def test_no_patch_when_not_crew_or_missing(self, mock_gh, mock_api, tmp_path):
        # crew_used but patch_path is None (e.g. capture returned None).
        mock_gh.side_effect = [
            json.dumps({"defaultBranch": "main"}),
            "basesha123\n",
            "https://github.com/user/test/pull/12",
        ]
        mock_api.side_effect = [
            {"ref": "refs/heads/school/issue-12-crew"},
            {"object": {"sha": "branchsha456"}},
            {"sha": "basetree789"},
            {"sha": "outblob111"},
            {"sha": "newtree333"},
            {"sha": "newcommit444"},
            {"ref": "refs/heads/school/issue-12-crew"},
        ]
        issue = {"issue_number": 12, "title": "Crew", "domain": "debugging", "difficulty": "easy"}
        task_result = {"response": "print('done')\n", "agent": "owl-alpha"}
        url = create_pr_for_issue(
            issue, task_result, "user/test",
            crew_used=True, patch_path=None, work_dir=str(tmp_path),
        )
        assert url == "https://github.com/user/test/pull/12"

        tree_post = next(
            c for c in mock_api.call_args_list
            if c.args and c.args[0] == "POST" and "trees" in c.args[1]
        )
        paths = {e["path"] for e in tree_post.args[2]["tree"]}
        assert not any(p.endswith("changes.patch") for p in paths)

    @patch("pr_creator._gh_api")
    @patch("pr_creator._gh")
    def test_patch_blob_failure_aborts(self, mock_gh, mock_api, tmp_path):
        # The patch file exists, but the blob POST for it returns None
        # (infra fault). The whole PR must abort rather than ship without the
        # captured diff.
        patch = tmp_path / "changes.patch"
        patch.write_text("diff --git a/x b/x\n", encoding="utf-8")
        mock_gh.side_effect = [
            json.dumps({"defaultBranch": "main"}),
            "basesha123\n",
        ]
        mock_api.side_effect = [
            {"ref": "refs/heads/school/issue-13-crew"},
            {"object": {"sha": "branchsha456"}},
            {"sha": "basetree789"},
            {"sha": "outblob111"},   # output.md blob OK
            None,                    # crew patch blob FAILS
        ]
        issue = {"issue_number": 13, "title": "Crew", "domain": "debugging", "difficulty": "easy"}
        task_result = {"response": "print('done')\n", "agent": "owl-alpha"}
        url = create_pr_for_issue(
            issue, task_result, "user/test",
            crew_used=True, patch_path=str(patch), work_dir=str(tmp_path),
        )
        assert url is None
        # No commit was ever attempted — the function returned at the patch blob guard.
        commit_calls = [
            c for c in mock_api.call_args_list
            if c.args and c.args[0] == "POST" and "commits" in c.args[1]
        ]
        assert not commit_calls
