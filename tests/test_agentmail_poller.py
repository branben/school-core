"""
Tests for src/agentmail_poller.py — the inbound /approve /reject /fix parser.

The critical regression here is the quoted-footer guard: school_mail cards end
with a RESPONSE_FOOTER whose lines begin with the command tokens
(`/approve — accept this work...`, `/fix <note> — send it back...`). When a
human replies by quoting the card (default in most email clients), those lines
appear inside the HUMAN's message and must never be parsed as commands.
"""

from src.agentmail_poller import _parse_approval


class TestParseApproval:
    def test_bare_approve(self):
        assert _parse_approval("/approve") == {"command": "approve", "note": ""}

    def test_approve_with_trailing_text(self):
        assert _parse_approval("/approve looks good") == {"command": "approve", "note": ""}

    def test_bare_reject(self):
        assert _parse_approval("/reject") == {"command": "reject", "note": ""}

    def test_fix_with_note(self):
        assert _parse_approval("/fix add a regression test") == {
            "command": "fix", "note": "add a regression test",
        }

    def test_no_slash_approve(self):
        assert _parse_approval("approve") == {"command": "approve", "note": ""}

    def test_footer_line_is_never_a_command(self):
        # Exact footer lines (school_mail.RESPONSE_FOOTER) must not parse.
        assert _parse_approval("/approve — accept this work and merge it") is None
        assert _parse_approval("/reject — mark it rejected") is None
        assert _parse_approval("/fix <note> — send it back with your note") is None

    def test_quoted_card_footer_is_never_a_command(self):
        # Email clients quote the card with '> ' — the footer inside the quote
        # must not be mistaken for a human command.
        quoted = (
            "On Aug 11, the Agent-School wrote:\n"
            "> Reply with one of:\n"
            "> /approve — accept this work and merge it\n"
            "> /reject — mark it rejected\n"
            "> /fix <note> — send it back with your note\n"
            "\n"
            "Thanks!"
        )
        assert _parse_approval(quoted) is None

    def test_quoted_command_with_real_vote_first(self):
        # A real command the human typed, followed by a quoted card, wins.
        reply = "/reject\n\n> /approve — accept this work and merge it\n> ..."
        assert _parse_approval(reply) == {"command": "reject", "note": ""}

    def test_prose_mention_never_matches(self):
        assert _parse_approval("please approve this when you get a chance") is None

    def test_empty_and_none(self):
        assert _parse_approval("") is None
        assert _parse_approval(None) is None
        assert _parse_approval("   \n  \n") is None
