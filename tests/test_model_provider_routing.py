"""Test: call_model routes through Nous when MODEL_PROVIDER=nous."""
import os
import sys
import json
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestModelProviderRouting:
    """call_model should route through Nous when MODEL_PROVIDER=nous."""

    def _fresh_executor(self):
        """Reload executor module to pick up env changes."""
        import importlib
        import executor
        importlib.reload(executor)
        return executor

    def test_nous_routes_to_nous_client(self):
        """When MODEL_PROVIDER=nous, call_model should use NousClient, not OmniRoute."""
        os.environ["MODEL_PROVIDER"] = "nous"
        os.environ["NOUS_API_KEY"] = "test-key"
        os.environ.pop("OMNIROUTE_API_KEY", None)

        executor = self._fresh_executor()

        # Patch NousClient to capture the call
        with patch("executor.NousClient") as MockNous:
            mock_instance = MagicMock()
            mock_instance.complete.return_value = "nous response"
            MockNous.return_value = mock_instance

            result = executor.call_model("coder", "hello", system_prompt="sys")

            MockNous.assert_called_once_with(api_key="test-key", model="meituan/longcat-2.0:free")
            mock_instance.complete.assert_called_once()
            assert result == "nous response"

    def test_omniroute_still_works_when_configured(self):
        """When MODEL_PROVIDER=omniroute (default), call_model uses OmniRoute."""
        os.environ["MODEL_PROVIDER"] = "omniroute"
        os.environ["OMNIROUTE_API_KEY"] = "test-omni-key"
        os.environ.pop("NOUS_API_KEY", None)

        executor = self._fresh_executor()

        with patch("executor._omniroute_call") as mock_omni:
            mock_omni.return_value = {"choices": [{"message": {"content": "omni response"}}]}

            result = executor.call_model("coder", "hello", system_prompt="sys")

            mock_omni.assert_called_once()
            assert result == "omni response"

    def test_nous_missing_key_raises(self):
        """When MODEL_PROVIDER=nous but NOUS_API_KEY missing, fail fast."""
        os.environ["MODEL_PROVIDER"] = "nous"
        os.environ.pop("NOUS_API_KEY", None)

        executor = self._fresh_executor()

        with pytest.raises(executor.ExecutorError, match="NOUS_API_KEY"):
            executor.call_model("coder", "hello")

    def test_omniroute_missing_key_still_raises(self):
        """When MODEL_PROVIDER=omniroute but key missing, fail fast (existing behavior)."""
        os.environ["MODEL_PROVIDER"] = "omniroute"
        os.environ.pop("OMNIROUTE_API_KEY", None)

        executor = self._fresh_executor()

        with pytest.raises(executor.ExecutorError, match="OMNIROUTE_API_KEY"):
            executor.call_model("coder", "hello")
