#!/usr/bin/env python3
"""Read-only client for the Obsidian Local REST API over the tailnet.

Live vault reads for the school-core context pipeline. All access is
READ-ONLY and confined to a safe, non-personal subset of the vault — personal
folders (00-Inbox, 05-Daily, 06-Archive) are hard-blocked.

Pure standard library (`socket` + `ssl` + `http.client`), so it runs in the
ephemeral sandbox with no pip installs. SOCKS5 support is a ~30-line CONNECT
handshake; TLS uses an unverified context because the Local REST API plugin
ships a self-signed cert.

Env config:
    OBSIDIAN_API_KEY    API key from Obsidian → Local REST API settings.
                        If unset, tries AGENT_SCHOOL_OBSIDIAN_KEY.
    OBSIDIAN_BASE_URL   Default https://<mac-tailnet-ip>:27124 (over HTTP via
                        SOCKS5 proxy if OBSIDIAN_SOCKS5 set).
    OBSIDIAN_SOCKS5     SOCKS5 proxy, e.g. localhost:1080 (tailscale userspace).

Usage:
    python scripts/obsidian_client.py list [path]
    python scripts/obsidian_client.py read <path>
    python scripts/obsidian_client.py search <query>
    python scripts/obsidian_client.py tag-list
    python scripts/obsidian_client.py bootstrap
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import ssl
import sys
import urllib.parse
from typing import Optional

# --- Safe (non-personal) vault areas — the only places agents may read. ---
SAFE_PREFIXES = (
    "01-Projects",
    "02-Agents",
    "03-Skills",
    "04-Reference",
    "99-Templates",
    "Bases",
    "docs",
    "engram",
    "job-targets",
    "omniroute",
    "scripts",
)
SAFE_ROOT_FILES = {
    "AGENTS.md",
    "CHANGELOG.md",
    "CLAUDE.md",
    "CONTEXT.md",
    "README.md",
    "Welcome.md",
    "_CLAUDE.md",
    "index.md",
    "Makefile",
    "log.md",
}
# Hard block (personal): even if a future allowlist changes, these stay off-limits.
BLOCKED_PREFIXES = (
    "00-Inbox",
    "05-Daily",
    "06-Archive",
    "01-Projects/Brandon Career",
)


def _env_key() -> Optional[str]:
    return os.environ.get("OBSIDIAN_API_KEY") or os.environ.get("AGENT_SCHOOL_OBSIDIAN_KEY")


def _is_safe_path(path: str) -> bool:
    """True if `path` is inside the safe (non-personal) vault subset."""
    p = path.strip("/")
    if not p:
        return True  # vault root listing is fine
    for blk in BLOCKED_PREFIXES:
        if p == blk or p.startswith(blk + "/"):
            return False
    for pref in SAFE_PREFIXES:
        if p == pref or p.startswith(pref + "/"):
            return True
    # Root-level files: only whitelisted ones.
    if "/" not in p and p in SAFE_ROOT_FILES:
        return True
    return False


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    chunks = b""
    while len(chunks) < n:
        part = sock.recv(n - len(chunks))
        if not part:
            raise ConnectionError("socket closed mid-read")
        chunks += part
    return chunks


def _socks5_connect(
    host: str,
    port: int,
    proxies_socks5: Optional[str],
    timeout: float = 20.0,
) -> socket.socket:
    """Return a TCP socket to (host, port), optionally through a SOCKS5 proxy.

    The proxy host:port is e.g. 'localhost:1080' (tailscale userspace
    --socks5-server). Hostnames are resolved by the PROXY (socks5h behavior),
    which is required for tailnet names the container cannot resolve itself.
    """
    if proxies_socks5:
        phost, _, pport = proxies_socks5.rpartition(":")
        sock = socket.create_connection(
            (phost, int(pport or "1080")), timeout=timeout
        )
        # greeting: version 5, 1 method, no-auth
        sock.sendall(b"\x05\x01\x00")
        rep = _recv_exact(sock, 2)
        if rep[0] != 0x05 or rep[1] != 0x00:
            sock.close()
            raise ConnectionError(f"SOCKS5 greeting rejected: {rep!r}")
        hbytes = host.encode()
        req = (
            b"\x05\x01\x00\x03"  # CONNECT, reserved, ATYP=domainname
            + bytes([len(hbytes)]) + hbytes
            + port.to_bytes(2, "big")
        )
        sock.sendall(req)
        head = _recv_exact(sock, 4)
        if head[1] != 0x00:
            sock.close()
            raise ConnectionError(f"SOCKS5 connect failed, code={head[1]}")
        atyp = head[3]
        if atyp == 0x01:
            extra = 4
        elif atyp == 0x03:
            extra = 1 + _recv_exact(sock, 1)[0]
        elif atyp == 0x04:
            extra = 16
        else:
            raise ConnectionError(f"SOCKS5 bad ATYP {atyp}")
        _recv_exact(sock, extra + 2)  # bind address + port
        return sock
    return socket.create_connection((host, port), timeout=timeout)


# Shared unverified TLS context (self-signed cert from the Local REST API).
_TLS_CTX = ssl._create_unverified_context()


class _StubResponse:
    """Minimal duck-typed response mirroring what the CLI callers expect."""

    __slots__ = ("status", "text", "_data")

    def __init__(self, status: int, body: bytes):
        self.status = status
        self.text = body.decode("utf-8", errors="replace")
        self._data = body

    def json(self):
        return json.loads(self._data or b"{}")

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}: {self.text[:300]}")


class ObsidianClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        socks5: Optional[str] = None,
    ):
        self.base_url = (base_url or os.environ.get(
            "OBSIDIAN_BASE_URL",
            "https://100.81.210.96:27124",
        )).rstrip("/")
        self.api_key = api_key or _env_key()
        if not self.api_key:
            raise RuntimeError(
                "No Obsidian API key set. Export OBSIDIAN_API_KEY or "
                "AGENT_SCHOOL_OBSIDIAN_KEY (Obsidian → Local REST API settings)."
            )
        self.socks5 = socks5 or os.environ.get("OBSIDIAN_SOCKS5", "localhost:1080")
        self.timeout = float(os.environ.get("OBSIDIAN_TIMEOUT", "20"))

    def _request(self, method: str, url_path: str, body=None,
                 content_type: str = "application/json") -> _StubResponse:
        url = f"{self.base_url}/{url_path.lstrip('/')}"
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        raw = _socks5_connect(host, port, self.socks5, self.timeout)
        try:
            tls = _TLS_CTX.wrap_socket(raw, server_hostname=host)
        except ssl.SSLError:
            # Plain HTTP (proxy or local testing without TLS).
            tls = raw
        conn = http.client.HTTPSConnection(host, port, timeout=self.timeout,
                                           context=_TLS_CTX)
        conn.sock = tls  # attach the already-connected socket
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = content_type
            payload = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
        else:
            payload = None
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return _StubResponse(resp.status, data)

    def _get(self, url_path: str) -> _StubResponse:
        resp = self._request("GET", url_path)
        resp.raise_for_status()
        return resp

    def _post(self, url_path: str, body: dict) -> _StubResponse:
        resp = self._request("POST", url_path, body=body)
        resp.raise_for_status()
        return resp

    # ---- operations ----

    def list(self, path: str = "") -> None:
        if not _is_safe_path(path):
            raise SystemExit(
                f"BLOCKED: '{path}' is outside the safe vault subset "
                f"(personal folders are off-limits)."
            )
        p = urllib.parse.quote(path) + "/" if path else ""
        resp = self._get(f"vault/{p}")
        data = resp.json()
        files = data.get("files", [])
        print(f"{len(files)} entries in '{path or '/'}':")
        for f in sorted(files):
            print("  ", f)

    def read(self, path: str) -> None:
        if not _is_safe_path(path):
            raise SystemExit(
                f"BLOCKED: '{path}' is outside the safe vault subset "
                f"(personal folders are off-limits)."
            )
        p = urllib.parse.quote(path)
        resp = self._get(f"vault/{p}")
        # Try JSON metadata; fall back to raw text for markdown.
        try:
            data = resp.json()
            if isinstance(data, dict) and "content" in data:
                print(data["content"])
                return
        except ValueError:
            pass
        sys.stdout.write(resp.text)

    def simple_search(self, query: str, top_k: int = 3) -> list:
        """Full-text search; returns list of {filename, snippet} dicts.

        Results are filtered through _is_safe_path, so search can never
        surface personal-folder snippets (00-Inbox, 05-Daily, …) even though
        the API itself indexes the whole vault.
        """
        resp = self._post(
            f"search/simple/?query={urllib.parse.quote(query)}",
            body={},
        )
        data = resp.json()
        if isinstance(data, dict):
            data = data.get("matches", [])
        out = []
        for m in data:
            path = m.get("filename") or m.get("path") or m.get("file") or "note"
            match = m.get("matches") or []
            if not _is_safe_path(path):
                continue
            snippet = ""
            for hit in match[:1]:
                ctx = hit.get("context", "")
                if ctx:
                    snippet = ctx[:300].replace("\n", " ")
            out.append({"filename": path, "snippet": snippet})
            if len(out) >= top_k:
                break
        return out

    def search(self, query: str) -> None:
        for res in self.simple_search(query, top_k=50):
            print(f"\n— {res['filename']}")
            if res["snippet"]:
                print(f"  {res['snippet']}")

    def tag_list(self) -> None:
        resp = self._get("tags/")
        tags = resp.json()
        print(json.dumps(tags, indent=2)[:2000])

    def bootstrap(self) -> None:
        """Print the knowledge-core bootstrap docs in read order."""
        for doc in ("CONTEXT.md", "Welcome.md", "02-Agents/_MATRIX.md"):
            print(f"\n{'='*70}\n== {doc}\n{'='*70}")
            try:
                self.read(doc)
            except Exception as e:
                print(f"(unavailable: {e})")

    def doctor(self) -> int:
        """One-shot bridge diagnostics; returns 0 if the live vault is usable."""
        print(f"base_url : {self.base_url}")
        print(f"socks5   : {self.socks5}")
        print(f"api_key  : {'set (' + self.api_key[:4] + '…)' if self.api_key else 'MISSING'}")
        if not self.api_key:
            return 3

        # 1. SOCKS5 proxy reachable?
        try:
            phost, _, pport = self.socks5.rpartition(":")
            s = socket.create_connection((phost, int(pport or "1080")), timeout=4)
            s.close()
            print("proxy    : OK (socks5 reachable)")
        except Exception as e:
            print(f"proxy    : FAIL — {e}")
            print("          Is bridge_live_vault.sh up / tailscaled running with --socks5-server?")
            return 2

        # 2. Obsidian API reachable + key valid?
        try:
            self._get("")
            print("api      : OK (GET / → 200)")
        except RuntimeError as e:
            print(f"api      : FAIL — {e}")
            return 2
        except Exception as e:
            print(f"api      : FAIL — {e}")
            return 2

        # 3. Safe-path gate on?
        leaky = _is_safe_path("05-Daily/2077-01-01.md")
        if leaky:
            print("guard    : FAIL — personal path not blocked")
            return 1
        print("guard    : OK (personal folders hard-blocked)")

        # 4. Quick live read + search smoke
        try:
            res = self.simple_search("beads", top_k=2)
            print(f"search   : OK ({len(res)} safe results for 'beads')")
        except Exception as e:
            print(f"search   : FAIL — {e}")
            return 2
        print("doctor   : LIVE VAULT USABLE")
        return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Read-only Obsidian vault client (tailnet).")
    ap.add_argument(
        "op",
        choices=["list", "read", "search", "tag-list", "bootstrap", "doctor"],
    )
    ap.add_argument("rest", nargs="*", help="path or query")
    args = ap.parse_args()

    client = ObsidianClient()
    try:
        if args.op == "list":
            client.list(args.rest[0] if args.rest else "")
        elif args.op == "read":
            if not args.rest:
                raise SystemExit("read requires a path.")
            client.read(args.rest[0])
        elif args.op == "search":
            if not args.rest:
                raise SystemExit("search requires a query.")
            client.search(" ".join(args.rest))
        elif args.op == "tag-list":
            client.tag_list()
        elif args.op == "bootstrap":
            client.bootstrap()
        elif args.op == "doctor":
            sys.exit(client.doctor())
    except (SystemExit, KeyboardInterrupt):
        raise
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except ConnectionError as e:
        print(f"connection error (is the SOCKS5 proxy up?): {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"unexpected error: {e}", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    sys.exit(main())