"""Pre-sign EIP-712 RiskParams mandates for the anvil demo borrowers.

Why this exists
---------------
``frontend/finax.js`` ships real borrower signatures so the console can drive the full
non-custodial path without ever holding a key. Until now only anvil account #1 had them, so every
other demo wallet was registered with an all-zero placeholder and the vault correctly refused it
at ``ECDSA.recover`` (selector ``0xf645eedf``). That made seven of the eight wallets undemoable.

A signature cannot be shared between borrowers: the EIP-712 digest commits to the borrower
address, the params, the nonce, AND the domain separator (vault address + chain id). So each
wallet needs its own, and they must be regenerated if the vault is redeployed to a new address.

This is the same thing a real borrower does offline with their own wallet — it is not server-side
signing. The keeper never sees these keys; the output is signatures only, pasted into the
frontend exactly as account #1's already are.

Usage:
    python tools/sign_demo_mandates.py            # print the JS block
    python tools/sign_demo_mandates.py --write    # rewrite DEMO_SIGNATURES in finax.js
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

# Running `python tools/x.py` puts tools/ on sys.path, not the backend root, so the app package
# is not importable. The sibling seeders dodge this by parsing .env by hand; this one needs the
# real RiskParams model and vault ABI, so it puts the root on the path instead.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eth_account import Account  # noqa: E402
from web3 import AsyncWeb3  # noqa: E402

from app.config.arbitrum import vault_abi  # noqa: E402
from app.config.settings import settings  # noqa: E402
from app.core.models import RiskParams  # noqa: E402

MNEMONIC = "test test test test test test test test test test test junk"
WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"

#: Must match `SIGNED_PARAMS` in finax.js byte for byte — every field is inside the digest, so a
#: single mismatch makes the signature recover to a different address and the vault rejects it.
BASE = dict(
    hf_trigger_bps=11500,
    hf_target_base_bps=12500,
    vol_coeff_k=0,
    hf_target_max_bps=14000,
    max_slippage_bps=300,
    max_cost_bps=500,
    allowed_collaterals=[WETH],
    deadline=2000000000,
)

ACCOUNTS = range(1, 9)   # anvil #1..#8 — the seeded demo borrowers
NONCES = range(1, 6)     # nonces are single-use on chain, so pre-sign several


async def main(write: bool) -> None:
    Account.enable_unaudited_hdwallet_features()
    w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(settings.arbitrum_rpc_url))
    vault = w3.eth.contract(
        address=AsyncWeb3.to_checksum_address(settings.vault_address), abi=vault_abi()
    )

    blocks: list[str] = []
    for idx in ACCOUNTS:
        acct = Account.from_mnemonic(MNEMONIC, account_path=f"m/44'/60'/0'/0/{idx}")
        entries: list[str] = []
        for nonce in NONCES:
            params = RiskParams(borrower=acct.address, nonce=nonce, **BASE)
            # The vault computes the digest itself, so this cannot drift from the contract's
            # own RISK_PARAMS_TYPEHASH or domain separator.
            digest = await vault.functions.hashRiskParams(params.to_solidity_tuple()).call()
            sig = Account.unsafe_sign_hash(digest, acct.key).signature.hex()
            if not sig.startswith("0x"):
                sig = "0x" + sig
            entries.append(f'    {{ nonce: {nonce}, sig: "{sig}" }},')
        joined = "\n".join(entries)
        blocks.append(f'  "{acct.address.lower()}": [\n{joined}\n  ],')

    js = "const DEMO_SIGNATURES = {\n" + "\n".join(blocks) + "\n};"

    if not write:
        print(js)
        return

    path = Path(__file__).resolve().parent.parent.parent / "frontend" / "finax.js"
    src = path.read_text(encoding="utf-8")
    pattern = re.compile(r"const DEMO_SIGNATURES = \{.*?\n\};", re.DOTALL)
    if not pattern.search(src):
        raise SystemExit("could not find DEMO_SIGNATURES in finax.js")
    path.write_text(pattern.sub(js, src), encoding="utf-8", newline="")
    print(f"rewrote DEMO_SIGNATURES in {path} — {len(list(ACCOUNTS))} borrowers "
          f"x {len(list(NONCES))} nonces")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="rewrite finax.js in place")
    asyncio.run(main(ap.parse_args().write))
