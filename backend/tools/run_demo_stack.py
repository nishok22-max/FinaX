"""Bring the whole demo stack up cleanly — one command, no overlapping processes.

The problem this solves
-----------------------
Restarting by hand kept producing three distinct failures, all of which *look* like code bugs:

  * **Port already in use.** Killing a shell does not kill the ``uvicorn`` child on Windows, so a
    previous API keeps holding 8097. The new one dies with ``[Errno 10048]`` and the browser keeps
    talking to the *old* process — which may be running older code against a different database.
  * **Two agent databases.** Starting the API with ``AGENT_DB_PATH`` set to anything other than the
    ``.env`` value silently splits state: proposals created in one run vanish in the next.
  * **A stale fork.** Public Arbitrum RPC retains only minutes of state (~0.25s blocks), so a
    long-running ``anvil --fork-url`` eventually fails every read with "metadata is not found".

So this script always: kills stragglers first, then starts anvil, seeds, signs, and starts the API
in one deterministic order. It never leaves two of anything running.

Usage:
    python tools/run_demo_stack.py              # full rebuild: fork + seed + sign + API
    python tools/run_demo_stack.py --no-seed    # keep the current fork state, just restart the API
    python tools/run_demo_stack.py --stop       # kill everything and exit
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ANVIL_PORT = 8548
API_PORT = 8097
FORK_URL = os.environ.get("ANVIL_FORK_URL", "https://arb1.arbitrum.io/rpc")

#: Seeders in the only order that produces consistent state. `seed_demo` must run first: it
#: deploys the vault and writes VAULT_ADDRESS into .env, which every later step depends on.
#: `seed_extra_positions.py` is deliberately absent — it is superseded and conflicts.
SEEDERS = [
    "seed_demo.py",
    "seed_all_demo_wallets.py",
    "seed_more_wallets.py",
]


def _anvil() -> str:
    found = shutil.which("anvil")
    if found:
        return found
    fallback = Path.home() / ".foundry" / "bin" / "anvil.exe"
    if fallback.exists():          # installed but not on PATH — the usual case on Windows
        return str(fallback)
    sys.exit("anvil not found. Install Foundry, or add ~/.foundry/bin to PATH.")


def kill_port(port: int) -> None:
    """Free a TCP port, including children that outlived their parent shell."""
    if sys.platform == "win32":
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
        pids = {
            m.group(1)
            for line in out.splitlines()
            if f":{port} " in line and "LISTENING" in line
            for m in [re.search(r"(\d+)\s*$", line.strip())] if m
        }
        for pid in pids:
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
            print(f"  killed pid {pid} holding :{port}")
    else:
        subprocess.run(["bash", "-c", f"lsof -ti tcp:{port} | xargs -r kill -9"],
                       capture_output=True)


def rpc(url: str, method: str, timeout: float = 3.0) -> dict | None:
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": [], "id": 1}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def wait_for(fn, what: str, tries: int = 30, delay: float = 2.0) -> None:
    for _ in range(tries):
        if fn():
            print(f"  {what} ready")
            return
        time.sleep(delay)
    sys.exit(f"timed out waiting for {what}")


def run(script: str) -> None:
    print(f"--- {script} ---")
    r = subprocess.run([sys.executable, str(ROOT / "tools" / script)], cwd=ROOT)
    if r.returncode != 0:
        sys.exit(f"{script} failed with exit code {r.returncode}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-seed", action="store_true", help="keep fork state; restart the API only")
    ap.add_argument("--stop", action="store_true", help="kill anvil and the API, then exit")
    args = ap.parse_args()

    print("stopping anything already running")
    kill_port(API_PORT)
    if not args.no_seed:
        kill_port(ANVIL_PORT)
    if args.stop:
        print("stopped.")
        return

    if not args.no_seed:
        print(f"starting anvil fork on :{ANVIL_PORT}")
        subprocess.Popen(
            [_anvil(), "--fork-url", FORK_URL, "--hardfork", "shanghai",
             "--port", str(ANVIL_PORT), "--chain-id", "42161", "--no-rate-limit"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ROOT,
        )
        wait_for(lambda: rpc(f"http://127.0.0.1:{ANVIL_PORT}", "eth_blockNumber") is not None,
                 "anvil")
        for s in SEEDERS:
            run(s)
        # Signatures commit to the vault address, so they are regenerated after every redeploy.
        print("--- sign_demo_mandates.py --write ---")
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "sign_demo_mandates.py"),
                            "--write"], cwd=ROOT)
        if r.returncode != 0:
            sys.exit("signing failed")

    # No AGENT_DB_PATH override here on purpose: the API must use the .env value, or proposals
    # land in a database the next run cannot see.
    print(f"starting API on :{API_PORT}")
    subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(API_PORT)],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    wait_for(lambda: rpc(f"http://127.0.0.1:{API_PORT}", "x") is not None
             or _http_ok(f"http://127.0.0.1:{API_PORT}/agent/status"), "API")

    print(f"\nconsole: http://localhost:{API_PORT}/console/")
    print("stop everything with:  python tools/run_demo_stack.py --stop")


def _http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=3):
            return True
    except Exception:
        return False


if __name__ == "__main__":
    main()
