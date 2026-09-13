"""Tests for the read-only Obsidian vault client — including the path-safety
privacy confinement that keeps agents away from personal folders.

Run: python -m pytest tests/test_obsidian_client.py -v
"""
import sys

import pytest

from scripts.obsidian_client import _is_safe_path


class TestPathSafety:
    """Only non-personal vault areas are readable."""

    @pytest.mark.parametrize(
        "path,expected",
        [
            # Allowed project/skill/reference areas
            ("01-Projects/School/school-core/docs/adr/0001.md", True),
            ("02-Agents/_MATRIX.md", True),
            ("03-Skills/Code Review/ce-code-review.md", True),
            ("04-Reference/Papers/paper.md", True),
            ("docs/glossary/terms.md", True),
            ("omniroute/omniroute-mcp-2026-spec-proposal.html", True),
            # Allowed root-level index files
            ("Welcome.md", True),
            ("AGENTS.md", True),
            ("CONTEXT.md", True),
            ("_CLAUDE.md", True),
            ("index.md", True),
            ("Makefile", True),
            ("log.md", True),
            # Vault root is fine (listing)
            ("", True),
            ("/", True),
            # Hard-blocked personal areas
            ("00-Inbox/something.md", False),
            ("05-Daily/2026-09-13.md", False),
            ("06-Archive/old.md", False),
            ("01-Projects/Brandon Career/resume.md", False),
            # Unknown / other
            ("private/notes.md", False),
            ("logs/", False),
            ("Untitled.md", False),
            ("Sound Royale.md", False),
        ],
    )
    def test_is_safe_path(self, path, expected):
        assert _is_safe_path(path) is expected

    def test_safe_prefix_listing_allowed(self):
        assert _is_safe_path("03-Skills/")
        assert _is_safe_path("04-Reference/Papers/")

    def test_blocked_is_substring_safe(self):
        # A path that merely *starts with* a blocked word elsewhere is fine.
        assert _is_safe_path("05-Daily-notes.md") is False  # top-level file not whitelisted
        assert _is_safe_path("docs/00-Inbox-notes.md") is True

    def test_blocked_prefix_checks_directory_boundary(self):
        # "05-Daily" must not block "05-Daily-notes" (different dir).
        # Our BLOCKED_PREFIXES uses exact dir match; "05-Daily-notes.md" is a
        # top-level file and not whitelisted, so still blocked — just by the
        # root-file whitelist, not the prefix.
        assert _is_safe_path("05-Daily-notes.md") is False


class TestSearchFiltering:
    """Search results must never surface personal-folder snippets."""

    def _fake_search_data(self):
        return [
            {"filename": "01-Projects/School/school-core/docs/adr/0001.md",
             "matches": [{"context": "safe project note"}]},
            {"filename": "05-Daily/2026-09-13.md",
             "matches": [{"context": "PRIVATE daily note (leak!)"}]},
            {"filename": "00-Inbox/random.md",
             "matches": [{"context": "PRIVATE inbox note (leak!)"}]},
        ]

    def test_search_filters_personal_results(self):
        """_is_safe_path is applied per-result in simple_search."""
        from scripts.obsidian_client import ObsidianClient

        class FakeResp:
            def json(self):
                return self._data
            def raise_for_status(self):
                pass
            _data = self._fake_search_data()

        class FakeClient(ObsidianClient):
            def __init__(self):
                # Bypass real init (no key needed in a unit test).
                self.api_key = "test"
                self.socks5 = ""
                self.base_url = "https://x"
            def _post(self, url_path, body):
                return FakeResp()

        res = FakeClient().simple_search("whatever", top_k=10)
        assert len(res) == 1
        assert "05-Daily" not in res[0]["filename"]
        assert "00-Inbox" not in res[0]["filename"]
        assert res[0]["snippet"] == "safe project note"

    def test_search_top_k_applied_after_filtering(self):
        """top_k counts only safe results, not raw API results."""
        from scripts.obsidian_client import ObsidianClient

        class FakeResp:
            def json(self):
                return self._data
            def raise_for_status(self):
                pass
            _data = [
                {"filename": "05-Daily/a.md", "matches": []},
                {"filename": "03-Skills/x.md", "matches": [{"context": "c1"}]},
                {"filename": "03-Skills/y.md", "matches": [{"context": "c2"}]},
            ]

        class FakeClient(ObsidianClient):
            def __init__(self):
                self.api_key = "test"
                self.socks5 = ""
                self.base_url = "https://x"
            def _post(self, url_path, body):
                return FakeResp()

        res = FakeClient().simple_search("q", top_k=1)
        assert len(res) == 1
        assert res[0]["filename"] == "03-Skills/x.md"