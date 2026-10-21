"""Tests for a2a_bridge — fake in-process HTTP server, no live stack needed.

Run:  python3 -m unittest tests.test_a2a_bridge -v
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import a2a_bridge as bridge  # noqa: E402

# --- Fake backend -----------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    """Emulates the three stages: dispatch, poll, read."""

    seen_paths: list = []

    def log_message(self, *args):  # silence
        pass

    def _record(self):
        type(self).seen_paths.append(self.path)

    def _send(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self._record()
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        if self.path == "/dispatch":
            sig = self.headers.get("X-Signature-256", "")
            expected = hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
            if sig != expected:
                self._send({"error": "bad signature"}, status=401)
                return
            self._send(
                {
                    "received": True,
                    "matched": 1,
                    "runs_created": ["run-1"],
                    "conversations_continued": [],
                }
            )
            return
        self._send({"error": "not found"}, status=404)

    def do_GET(self):
        self._record()
        if "/runs" in self.path:
            self._send(
                {
                    "runs": [
                        {
                            "id": "run-1",
                            "status": "COMPLETED",
                            "conversation_id": "conv-1",
                        }
                    ],
                    "total": 1,
                    "status_counts": {"COMPLETED": 1},
                }
            )
            return
        if "/events" in self.path:
            self._send(
                {
                    "items": [
                        {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "the answer"}],
                        }
                    ]
                }
            )
            return
        self._send({"error": "not found"}, status=404)


class FakeServer:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return f"http://127.0.0.1:{self.port}"

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


# --- Tests ------------------------------------------------------------------


class TestHmac(unittest.TestCase):
    def test_matches_manual_hmac(self):
        body = b'{"task":"x"}'
        expected = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        self.assertEqual(bridge.compute_hmac_signature("s3cret", body), expected)

    def test_differs_with_wrong_secret(self):
        body = b'{"task":"x"}'
        self.assertNotEqual(
            bridge.compute_hmac_signature("a", body),
            bridge.compute_hmac_signature("b", body),
        )


class TestDispatch(unittest.TestCase):
    def test_posts_signed_body_and_parses_response(self):
        with FakeServer() as base:
            resp = bridge.dispatch(f"{base}/dispatch", "test-secret", "do a thing")
        self.assertTrue(resp["received"])
        self.assertEqual(resp["runs_created"], ["run-1"])

    def test_raises_on_bad_signature(self):
        with FakeServer() as base:
            with self.assertRaises(RuntimeError) as ctx:
                bridge.dispatch(f"{base}/dispatch", "wrong-secret", "x")
        self.assertIn("401", str(ctx.exception))

    def test_raises_on_malformed_json(self):
        class Bad(_Handler):
            def do_POST(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"not json")

        with ThreadingHTTPServer(("127.0.0.1", 0), Bad) as httpd:
            port = httpd.server_address[1]
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            try:
                with self.assertRaises(RuntimeError) as ctx:
                    bridge.dispatch(f"http://127.0.0.1:{port}/x", "s", "x")
                self.assertIn("malformed JSON", str(ctx.exception))
            finally:
                httpd.shutdown()


class TestPoll(unittest.TestCase):
    def test_returns_terminal_run(self):
        with FakeServer() as base:
            run = bridge.poll_run_status(
                base, "k", "org", "auto", "run-1", interval=0.01, timeout=5
            )
        self.assertEqual(run["status"], "COMPLETED")
        self.assertEqual(run["conversation_id"], "conv-1")

    def test_raises_when_run_missing(self):
        class Empty(_Handler):
            def do_GET(self):
                self._send({"runs": [], "total": 0, "status_counts": {}})

        with ThreadingHTTPServer(("127.0.0.1", 0), Empty) as httpd:
            port = httpd.server_address[1]
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            try:
                with self.assertRaises(RuntimeError) as ctx:
                    bridge.poll_run_status(
                        f"http://127.0.0.1:{port}",
                        "k",
                        "org",
                        "auto",
                        "nope",
                        interval=0.01,
                        timeout=5,
                    )
                self.assertIn("not found", str(ctx.exception))
            finally:
                httpd.shutdown()

    def test_poll_uses_correct_route(self):
        # Locks the contract: /v1/{id}/runs — no "automations" segment.
        _Handler.seen_paths.clear()
        with FakeServer() as base:
            bridge.poll_run_status(
                base, "k", "org", "auto", "run-1", interval=0.01, timeout=5
            )
        self.assertTrue(
            any(p.startswith("/v1/auto/runs") for p in _Handler.seen_paths),
            f"unexpected poll paths: {_Handler.seen_paths}",
        )

    def test_times_out_when_never_terminal(self):
        class Running(_Handler):
            def do_GET(self):
                self._send(
                    {
                        "runs": [{"id": "run-1", "status": "RUNNING"}],
                        "total": 1,
                        "status_counts": {"RUNNING": 1},
                    }
                )

        with ThreadingHTTPServer(("127.0.0.1", 0), Running) as httpd:
            port = httpd.server_address[1]
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            try:
                with self.assertRaises(RuntimeError) as ctx:
                    bridge.poll_run_status(
                        f"http://127.0.0.1:{port}",
                        "k",
                        "org",
                        "auto",
                        "run-1",
                        interval=0.01,
                        timeout=0.05,
                    )
                self.assertIn("timeout", str(ctx.exception))
            finally:
                httpd.shutdown()


class TestExtract(unittest.TestCase):
    def test_picks_assistant_text(self):
        # The bridge requests sort_order=TIMESTAMP_DESC, so the first assistant
        # event in the list is the agent's latest answer.
        events = {
            "items": [
                {"role": "assistant", "content": [{"type": "text", "text": "newest"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "older"}]},
            ]
        }
        self.assertEqual(bridge.extract_last_assistant_text(events), "newest")

    def test_returns_none_without_assistant(self):
        self.assertIsNone(bridge.extract_last_assistant_text({"items": []}))
        self.assertIsNone(
            bridge.extract_last_assistant_text(
                {"items": [{"role": "user", "content": [{"type": "text", "text": "q"}]}]}
            )
        )


class TestRead(unittest.TestCase):
    def test_reads_agent_answer(self):
        with FakeServer() as base:
            answer = bridge.read_agent_answer(base, "k", "conv-1")
        self.assertEqual(answer, "the answer")

    def test_raises_when_no_assistant_message(self):
        class NoAssistant(_Handler):
            def do_GET(self):
                self._send({"items": [{"role": "user"}]})

        with ThreadingHTTPServer(("127.0.0.1", 0), NoAssistant) as httpd:
            port = httpd.server_address[1]
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            try:
                with self.assertRaises(RuntimeError) as ctx:
                    bridge.read_agent_answer(
                        f"http://127.0.0.1:{port}", "k", "conv-1"
                    )
                self.assertIn("No assistant message", str(ctx.exception))
            finally:
                httpd.shutdown()


class TestSecrets(unittest.TestCase):
    def test_missing_secret_raises(self):
        old = os.environ.pop("A2A_WEBHOOK_SECRET", None)
        try:
            with self.assertRaises(RuntimeError) as ctx:
                bridge.require_secrets()
            self.assertIn("A2A_WEBHOOK_SECRET", str(ctx.exception))
        finally:
            if old is not None:
                os.environ["A2A_WEBHOOK_SECRET"] = old

    def test_present_secret_passes(self):
        os.environ["A2A_WEBHOOK_SECRET"] = "x"
        os.environ["A2A_SESSION_KEY"] = "y"
        bridge.require_secrets()  # must not raise


if __name__ == "__main__":
    unittest.main()
