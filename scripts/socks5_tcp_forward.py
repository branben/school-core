#!/usr/bin/env python3
"""socks5_tcp_forward.py — forward a local TCP port through a SOCKS5 proxy to a remote host:port.

Lets tools that dial a fixed host:port (e.g. Orca's ws://100.x.y.z:6768) reach that
host through our SOCKS5 tunnel (Obsidian bridge) by listening on localhost:PORT and
piping bytes to the proxy.

Usage:
    socks5_tcp_forward.py --listen 127.0.0.1:6768 --target 100.81.210.96:6768 [--socks5 localhost:1080]
"""
from __future__ import annotations

import argparse
import socket
import struct
import threading
import time

_CFG: dict = {"socks5_addr": "localhost", "socks5_port": 1080}

try:
    from scripts.obsidian_client import _socks5_connect
    SOCKS_IMPL = "obsidian"
except Exception:
    SOCKS_IMPL = "inline"


def _socks5_tunnel(target_host: str, target_port: int, proxy_host: str, proxy_port: int) -> socket.socket:
    """Open a TCP connection to target through the SOCKS5 proxy (no auth)."""
    if SOCKS_IMPL == "obsidian":
        return _socks5_connect(target_host, target_port, f"{proxy_host}:{proxy_port}")
    # inline SOCKS5 CONNECT (fallback)
    s = socket.create_connection((proxy_host, proxy_port), timeout=10)
    s.sendall(b"\x05\x01\x00")  # version, 1 method, no-auth
    resp = recv_exact(s, 2)
    if len(resp) < 2 or resp[1] != 0x00:
        s.close()
        raise RuntimeError(f"SOCKS5 no-auth rejected: {resp.hex()}")
    host_ip = socket.inet_aton(target_host) if is_ipv4(target_host) else None
    if host_ip:
        req = b"\x05\x01\x00\x01" + host_ip + struct.pack(">H", target_port)
    else:
        hostb = target_host.encode()
        req = b"\x05\x01\x00\x03" + bytes([len(hostb)]) + hostb + struct.pack(">H", target_port)
    s.sendall(req)
    reply = recv_exact(s, 4)
    if len(reply) < 4 or reply[1] != 0x00:
        s.close()
        raise RuntimeError(f"SOCKS5 CONNECT failed: {reply[1]}")
    atyp = reply[3]
    if atyp == 0x01:
        recv_exact(s, 4 + 2)
    elif atyp == 0x03:
        ln = recv_exact(s, 1)[0]
        recv_exact(s, ln + 2)
    elif atyp == 0x04:
        recv_exact(s, 16 + 2)
    return s


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def is_ipv4(s: str) -> bool:
    try:
        socket.inet_aton(s)
        return True
    except OSError:
        return False


def pipe(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            data = a.recv(16384)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        try:
            a.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            b.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        a.close()
        b.close()


def handle(client: socket.socket, target_host: str, target_port: int) -> None:
    try:
        up = _socks5_tunnel(target_host, target_port, _CFG["socks5_addr"], _CFG["socks5_port"])
    except Exception as e:
        print(f"[forward] connect to {target_host}:{target_port} via socks failed: {e}", flush=True)
        client.close()
        return
    t1 = threading.Thread(target=pipe, args=(client, up), daemon=True)
    t2 = threading.Thread(target=pipe, args=(up, client), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()


def main() -> None:
    ap = argparse.ArgumentParser(description="Forward local TCP through SOCKS5 to a target host:port")
    ap.add_argument("--listen", default="127.0.0.1:6768", help="local listen addr:port")
    ap.add_argument("--target", required=True, help="target host:port (e.g. 100.81.210.96:6768)")
    ap.add_argument("--socks5", default=f"{_CFG['socks5_addr']}:{_CFG['socks5_port']}", help="socks5 proxy addr:port")
    args = ap.parse_args()
    _CFG["socks5_addr"], _CFG["socks5_port"] = args.socks5.rsplit(":", 1)
    _CFG["socks5_port"] = int(_CFG["socks5_port"])

    lhost, lport = args.listen.rsplit(":", 1)
    lport = int(lport)
    tgt_host, tgt_port = args.target.rsplit(":", 1)
    tgt_port = int(tgt_port)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((lhost, lport))
    srv.listen(16)
    print(f"[forward] listening on {lhost}:{lport} -> {tgt_host}:{tgt_port} via {_CFG['socks5_addr']}:{_CFG['socks5_port']}", flush=True)
    while True:
        client, _ = srv.accept()
        threading.Thread(target=handle, args=(client, tgt_host, tgt_port), daemon=True).start()
        time.sleep(0.01)


if __name__ == "__main__":
    main()