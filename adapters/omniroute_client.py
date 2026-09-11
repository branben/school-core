"""OmniRoute adapter — concrete implementation of OmniRouteAdapter.

Wraps model routing and LLM calls behind the OmniRouteAdapter interface.
Uses the existing executor.py transport layer.
"""

from __future__ import annotations

from typing import Optional

from adapters.base import OmniRouteAdapter, ModelError


class OmniRouteClientAdapter(OmniRouteAdapter):
    """Concrete OmniRoute adapter that routes model calls through executor.py.

    This adapter wraps the existing ``call_model`` function from ``executor.py``
    so that callers can depend on the adapter abstraction rather than importing
    the concrete function directly. This enables easier testing and transport
    swapping (e.g., switching to Nous direct or A2A).
    """

    def __init__(self, router_experience_path: Optional[str] = None):
        self._router_path = router_experience_path

    def call_model(
        self,
        agent_name: str,
        prompt: str,
        system_prompt: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> str:
        """Call a model and return the response text.

        Delegates to ``executor.call_model`` which handles combo selection,
        LoRA adapter resolution, and transport routing (OmniRoute/Nous/A2A).
        """
        try:
            from executor import call_model, ExecutorError
            return call_model(
                agent_name,
                prompt,
                system_prompt=system_prompt,
                timeout=timeout,
            )
        except Exception as e:
            raise ModelError(f"Model call failed for agent '{agent_name}': {e}") from e


class NousDirectAdapter(OmniRouteAdapter):
    """Concrete adapter for direct Nous API calls (bypasses OmniRoute proxy).

    Used when MODEL_PROVIDER=nous. This adapter provides the same interface
    as OmniRouteClientAdapter but routes directly to the Nous API.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        import os
        self._api_key = api_key or os.environ.get("NOUS_API_KEY", "").strip()
        self._model = model or os.environ.get("NOUS_MODEL", "meituan/longcat-2.0:free")
        self._base = base_url or "https://inference-api.nousresearch.com/v1"

    def call_model(
        self,
        agent_name: str,
        prompt: str,
        system_prompt: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> str:
        """Call the Nous API directly."""
        import json
        import os
        import urllib.request
        import urllib.error

        if not self._api_key:
            raise ModelError("NOUS_API_KEY is not set")

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        body = json.dumps({
            "model": self._model,
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": 4096,
            "stream": False,
        }).encode()

        req = urllib.request.Request(
            f"{self._base}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
                "User-Agent": "school-core/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or 120) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:500] if e.fp else str(e)
            raise ModelError(f"Nous HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise ModelError(f"Nous connection failed: {e.reason}") from e

        choices = data.get("choices", [])
        if not choices:
            raise ModelError(f"No choices in Nous response: {json.dumps(data)[:300]}")
        return choices[0]["message"]["content"]
