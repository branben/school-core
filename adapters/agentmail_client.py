"""AgentMail adapter — concrete implementation of AgentMailAdapter.

Wraps the AgentMail REST client (agentmail_client.py) behind the
AgentMailAdapter interface.
"""

from __future__ import annotations

from typing import Optional

from adapters.base import AgentMailAdapter, AgentMailError


class AgentMailClientAdapter(AgentMailAdapter):
    """Concrete AgentMail adapter that wraps the shared agentmail_client module.

    This adapter uses the consolidated ``agentmail_client.py`` for key
    resolution, inbox resolution, and the request helper. Previously three
    surfaces (school_mail.py, src/agentmail_poller.py, scripts/school_inbound.py)
    each carried their own copy of this plumbing — they now share one transport.
    """

    def __init__(self, base_url: str = "https://api.agentmail.to"):
        self._base_url = base_url

    def send_message(
        self,
        to: list[str],
        subject: str,
        text: str,
    ) -> dict:
        """Send a message via the AgentMail API."""
        try:
            from agentmail_client import req, resolve_dest_inbox

            inbox = resolve_dest_inbox()
            return req(
                "POST",
                f"/inboxes/{inbox}/messages/send",
                {"to": to, "subject": subject, "text": text},
            )
        except Exception as e:
            raise AgentMailError(f"Failed to send message: {e}") from e

    def get_inboxes(self) -> list[dict]:
        """Get available inboxes."""
        try:
            from agentmail_client import req
            res = req("GET", "/inboxes")
            return res.get("inboxes", []) if isinstance(res, dict) else []
        except Exception as e:
            raise AgentMailError(f"Failed to get inboxes: {e}") from e

    def resolve_dest_inbox(self) -> str:
        """Resolve the destination inbox."""
        try:
            from agentmail_client import resolve_dest_inbox
            return resolve_dest_inbox()
        except Exception as e:
            raise AgentMailError(f"Failed to resolve inbox: {e}") from e
