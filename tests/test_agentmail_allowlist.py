"""Tests for the inbound-command allowlist + Slack activity feed.

The /reject and /fix paths in ``_execute_approval`` used to read-modify-write
``~/.hermes/bookbag/<bead>.json`` (and /reject also ran ``bd update --claim``)
with NO sender allowlist — any human who could email the AgentMail inbox could
flip kanban state or inject reviewer notes. These tests pin fail-closed
behaviour: a non-allowlisted sender is blocked *before any side effect*, and an
allowlisted sender proceeds. ``post_activity`` is a best-effort Slack feed that
must never break command processing.
"""

import json
import os
from unittest.mock import patch

from src.agentmail_poller import _execute_approval, _load_allowed_approvers, post_activity


def _reject_reply(sender="human@example.com", bead="bead-1", note="nope"):
    return {"command": "reject", "bead": bead, "note": note, "from": sender,
            "message_id": "m-1", "subject": "[school] verdict"}


def _fix_reply(sender="human@example.com", bead="bead-1", note="fix it"):
    return {"command": "fix", "bead": bead, "note": note, "from": sender,
            "message_id": "m-2", "subject": "[school] verdict"}


# ── fail-closed: a non-allowlisted sender is blocked before ANY side effect ──

def test_reject_blocked_when_sender_not_allowlisted():
    reply = _reject_reply(sender="attacker@example.com")
    # Bookbag present + bd available: absent the guard BOTH would fire.
    with patch("pathlib.Path.exists", return_value=True), \
         patch("pathlib.Path.write_text") as write_text, \
         patch("src.agentmail_poller.subprocess.run") as run:
        result = _execute_approval(reply, "/tmp/repo",
                                   allowed_approvers=("human@example.com",))
    assert "not allowlisted" in result
    assert "blocked" in result.lower()
    write_text.assert_not_called()   # no bookbag mutation
    run.assert_not_called()          # no `bd update --claim`


def test_fix_blocked_when_sender_not_allowlisted():
    reply = _fix_reply(sender="attacker@example.com")
    with patch("pathlib.Path.exists", return_value=True), \
         patch("pathlib.Path.write_text") as write_text:
        result = _execute_approval(reply, "/tmp/repo",
                                   allowed_approvers=("human@example.com",))
    assert "not allowlisted" in result
    write_text.assert_not_called()


def test_empty_allowlist_blocks_everyone():
    reply = _reject_reply(sender="human@example.com")
    with patch("src.agentmail_poller.subprocess.run") as run:
        result = _execute_approval(reply, "/tmp/repo", allowed_approvers=())
    assert "not allowlisted" in result
    run.assert_not_called()


# ── allowlisted sender proceeds ──

def test_reject_proceeds_when_sender_allowlisted():
    reply = _reject_reply(sender="human@example.com")
    with patch("src.agentmail_poller.subprocess.run") as run:
        result = _execute_approval(reply, "/tmp/repo",
                                   allowed_approvers=("human@example.com",))
    assert "REJECTED" in result
    run.assert_called_once()         # bd update --claim ran


def test_fix_proceeds_when_sender_allowlisted():
    reply = _fix_reply(sender="human@example.com")
    result = _execute_approval(reply, "/tmp/repo",
                               allowed_approvers=("human@example.com",))
    assert "FIX REQUESTED" in result


# ── Slack activity feed: best-effort, must never break processing ──

def test_post_activity_noop_when_unconfigured():
    with patch("urllib.request.urlopen") as ur, \
         patch.dict(os.environ, {"SCHOOL_CORE_SLACK_TOKEN": "",
                                 "SCHOOL_CORE_SLACK_CHANNEL": ""}, clear=False):
        # Must not raise even though nothing is configured.
        post_activity({"command": "reject", "bead": "b", "from": "x@example.com"},
                      "❌ REJECT blocked")
    ur.assert_not_called()


def test_post_activity_posts_when_configured():
    with patch("urllib.request.urlopen") as ur, \
         patch.dict(os.environ, {"SCHOOL_CORE_SLACK_TOKEN": "fake-slack-test-token",
                                 "SCHOOL_CORE_SLACK_CHANNEL": "C123456"},
                    clear=False):
        post_activity({"command": "reject", "bead": "b-9", "from": "x@example.com"},
                      "❌ REJECT blocked: not allowlisted")
    ur.assert_called_once()
    request = ur.call_args[0][0]
    payload = json.loads(request.data.decode())
    assert payload["channel"] == "C123456"
    assert "reject" in payload["text"].lower()
    assert "b-9" in payload["text"]
    assert "blocked" in payload["text"].lower()


def test_post_activity_survives_http_error():
    import urllib.error
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("boom")), \
         patch.dict(os.environ, {"SCHOOL_CORE_SLACK_TOKEN": "fake-slack-test-token",
                                 "SCHOOL_CORE_SLACK_CHANNEL": "C123456"},
                    clear=False):
        # A failing feed must NOT raise — command processing continues.
        post_activity({"command": "fix", "bead": "b", "from": "x@example.com"}, "🔧 FIX")


# ── _load_allowed_approvers: the parse IS the fail-closed guarantee ──

def test_load_allowed_approvers_empty_blocks_all():
    with patch.dict(os.environ, {"SCHOOL_CORE_ALLOWED_APPROVERS": ""}, clear=False):
        assert _load_allowed_approvers() == ()


def test_load_allowed_approvers_whitespace_only_blocks_all():
    with patch.dict(os.environ, {"SCHOOL_CORE_ALLOWED_APPROVERS": "  , , "}, clear=False):
        assert _load_allowed_approvers() == ()


def test_load_allowed_approvers_strips_padding():
    with patch.dict(os.environ,
                    {"SCHOOL_CORE_ALLOWED_APPROVERS": " a@x.com , b@x.com "},
                    clear=False):
        assert _load_allowed_approvers() == ("a@x.com", "b@x.com")
