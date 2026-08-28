"""Local JSON-RPC proxy that lets `anvil --fork-url` use an Alchemy archive node.

Why this exists
---------------
Foundry 1.8 probes the fork endpoint with ``anvil_nodeInfo`` to determine the
network family. Public endpoints answer with a JSON-RPC *error* body under
HTTP 200, which Foundry tolerates and falls back from. Alchemy instead returns
HTTP 400, which Foundry treats as fatal:

    Error: failed to determine network family from fork endpoint
    HTTP error 400 ... "Unsupported method: anvil_nodeInfo on ARB_MAINNET"

So forking straight from Alchemy fails, and forking from a public endpoint
gives a node with no archive state — arbitrary addresses then fail with
"missing trie node" / "historical state is not available".

This proxy answers ``anvil_nodeInfo`` locally the way a public node does
(JSON-RPC error, HTTP 200) and forwards every other call upstream to Alchemy,
so anvil gets a working probe *and* real archive state.

Demo tooling only — it is not part of the keeper service.

Usage:
    python tools/alchemy_fork_proxy.py --upstream <ALCHEMY_URL> --port 8545
    anvil --fork-url http://127.0.0.1:8545 --hardfork shanghai --port 8548
"""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Methods anvil probes with that Alchemy rejects at the HTTP layer. Answering
# them as a JSON-RPC error under HTTP 200 matches public-node behaviour.
LOCALLY_ANSWERED = {"anvil_nodeInfo", "anvil_metadata"}

UPSTREAM = ""


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        pass  # quiet; anvil is chatty enough

    def _reply(self, payload: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802  (BaseHTTPRequestHandler API)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)

        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            self._reply(b'{"jsonrpc":"2.0","id":null,'
                        b'"error":{"code":-32700,"message":"Parse error"}}', 200)
            return

        # Single request (not a batch) for a method we shadow locally.
        if isinstance(body, dict) and body.get("method") in LOCALLY_ANSWERED:
            self._reply(json.dumps({
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "error": {"code": -32601,
                          "message": f"Unsupported method: {body['method']}"},
            }).encode(), 200)
            return

        req = urllib.request.Request(
            UPSTREAM, data=raw,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                self._reply(resp.read(), resp.status)
        except urllib.error.HTTPError as exc:
            self._reply(exc.read() or b'{"jsonrpc":"2.0","id":null,'
                                      b'"error":{"code":-32603,"message":"upstream error"}}',
                        200)
        except Exception as exc:  # noqa: BLE001 — proxy must never die on one call
            self._reply(json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32603, "message": f"proxy error: {exc}"},
            }).encode(), 200)


def main() -> None:
    global UPSTREAM
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", required=True, help="Archive RPC URL (e.g. Alchemy)")
    ap.add_argument("--port", type=int, default=8545)
    args = ap.parse_args()
    UPSTREAM = args.upstream

    server = ThreadingHTTPServer(("127.0.0.1", args.port), ProxyHandler)
    print(f"fork proxy listening on http://127.0.0.1:{args.port} -> upstream archive node",
          flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
