"""Test for the /stream SSE endpoint (Task 8 — live SSE stream).

Run: python -m pytest tests/test_sse.py -q
"""

import json
import socket
import threading
from http.server import ThreadingHTTPServer

import pytest

from activity_server import ActivityHandler


@pytest.fixture(scope="module")
def server_port():
    """Start activity server on a random port, yield the port, clean up."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), ActivityHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield port
    # Don't call server.shutdown() here — the SSE stream blocks the handler,
    # so shutdown would block.  The daemon thread is killed when the process
    # exits (pytest reaps it after the module scope).
    server.server_close()


class TestSseEndpoint:
    """Tests for the /stream SSE live-update endpoint."""

    def test_stream_returns_text_event_stream_with_initial_board(
        self, server_port
    ):
        """GET /stream returns Content-Type text/event-stream and an initial
        ``event: board`` frame whose data is valid board JSON with the four
        expected columns."""
        port = server_port
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        # Send a raw HTTP/1.0 request (no chunking complications)
        request = b"GET /stream HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n"
        sock.sendall(request)

        # Read response headers
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk

        # Check Content-Type in headers
        header_text = data.decode("utf-8", errors="replace")
        assert "text/event-stream" in header_text, (
            f"Expected 'text/event-stream' in headers, got: {header_text[:200]}"
        )
        assert "200" in header_text.split("\r\n")[0], (
            "Expected 200 status"
        )

        # Read past headers
        body_start = data[data.index(b"\r\n\r\n") + 4 :]

        # Read the initial SSE frame (should arrive immediately)
        # HTTP/1.0 closes after the response, so read all remaining data
        rest = b""
        sock.settimeout(3)
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                rest += chunk
        except socket.timeout:
            pass

        full_body = body_start + rest
        body_text = full_body.decode("utf-8", errors="replace")

        # Check for initial event: board frame
        assert "event: board" in body_text, (
            f"No 'event: board' in SSE body. Got: {body_text[:300]}"
        )

        # Parse the data line from the first event
        found_payload = None
        for line in body_text.split("\n"):
            if line.startswith("data: "):
                try:
                    found_payload = json.loads(line[6:])
                except json.JSONDecodeError:
                    pass
                break

        assert found_payload is not None, (
            f"No valid JSON data line found in SSE body: {body_text[:300]}"
        )
        assert "columns" in found_payload
        for key in ("todo", "in_progress", "in_review", "done"):
            assert key in found_payload["columns"], (
                f"Expected column '{key}' in board payload"
            )

        sock.close()
