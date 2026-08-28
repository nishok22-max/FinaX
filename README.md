# Automated Liquidation Shield & Flash-Repayment Vault (PS-11)

Autonomous, atomic liquidation protection for Aave V3 positions on **Arbitrum One**.
Solidity vault + Python/FastAPI keeper backend. See [`PRD.md`](PRD.md),
[`SYSTEM_ARCHITECTURE.md`](SYSTEM_ARCHITECTURE.md), and [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md).

This repo currently implements **Sprint 0 + Phases 0–3** (contracts + fork tests, plus the
backend core: typed `web3.py` clients, config, and Pydantic models). Phases 4–6 (decision
pipeline, FastAPI worker, and end-to-end demo) are next.

## Layout

```
contracts/   Foundry project — LiquidationShieldVault.sol, interfaces, libraries, fork tests
backend/     Python/FastAPI — /health + Phase 3 chain clients (Aave/Uniswap/oracle) & models
```

## Prerequisites

- [Foundry](https://book.getfoundry.sh/getting-started/installation) (`forge`, `anvil`, `cast`)
- An **Arbitrum One** RPC URL (free from Alchemy/Infura/QuickNode/Ankr) — tests run against a mainnet fork
- Python 3.11+ (for the backend scaffold)

## Contracts — build & test (Sprint 0 + Phases 1–2)

```bash
cd contracts
forge install foundry-rs/forge-std OpenZeppelin/openzeppelin-contracts --no-git
cp .env.example .env      # then set ARBITRUM_RPC_URL (and optionally FORK_BLOCK)
forge build               # uses evm_version = "cancun" (OpenZeppelin v5 needs MCOPY)
forge test -vvv
```

> **Verified:** on a live Arbitrum One fork, `forge build` passes and **13/13 tests pass**
> (Gate A permissions 3/3, Gate B sizing, happy path, 8 revert paths).
>
> **RPC note:** Foundry 1.8's fork setup probes the endpoint with `anvil_nodeInfo`. Public
> endpoints (e.g. `https://arb1.arbitrum.io/rpc`, the current `.env` default) tolerate this;
> some managed providers (Alchemy) return HTTP 400 and the fork fails to instantiate. Use a
> public endpoint, or an older Foundry, until that's resolved.

Run a single suite:

```bash
forge test --match-contract PoC_AavePermissionsTest -vvv   # Sprint 0 Gate A (Aave permissions)
forge test --match-contract SizingParityTest -vvv          # Sprint 0 Gate B (sizing)
forge test --match-contract HappyPathTest -vvv             # Phase 1
forge test --match-contract RevertPathsTest -vvv           # Phase 2
```

> Tests fork Arbitrum One and use `deal`/`prank` cheatcodes to build positions with **real**
> Aave V3 / Uniswap V3 / Chainlink state — free, no real funds. If `deal(USDC, ...)` misbehaves
> on your fork block, switch to `USDC_e` (see `addresses.json`).

## Backend — boot & test (Phases 0 + 3)

```bash
cd backend
python -m venv .venv && source .venv/Scripts/activate   # Windows Git Bash; use bin/activate on *nix
pip install -e ".[dev]"
cp .env.example .env      # set ARBITRUM_RPC_URL (a public endpoint or an anvil fork)
uvicorn app.main:app --reload
# GET http://127.0.0.1:8000/health   → reports rpc_connected + block_number
```

Run the backend tests:

```bash
pytest -q          # model unit tests always run; fork-integration tests need ARBITRUM_RPC_URL
```

> The Phase 3 chain clients (`app/chain/`) read real Aave HF, reserve config, Aave-oracle
> prices, Chainlink freshness, and live Uniswap quotes through typed `web3.py` wrappers with
> primary+fallback RPC failover. The fork-integration tests (`tests/test_chain_reads.py`) skip
> automatically when no RPC is configured, or when `web3` can't load its native deps.

## What's implemented

| Item | Status |
|---|---|
| Sprint 0 Gate A — Aave permission PoC (`PoC_AavePermissions.t.sol`) | ✅ |
| Sprint 0 Gate B — sizing parity (`SizingParity.t.sol`) | ✅ |
| `LiquidationShieldVault.sol` — flash-loan → repay → transferFrom+withdraw → swap → repay → HealthGuard | ✅ |
| Multi-invariant HealthGuard (HF, debt↓, leverage↓, cost bound) | ✅ |
| EIP-712 signed `RiskParams`, nonce/deadline, access control, reentrancy | ✅ |
| Happy-path + revert-path fork tests | ✅ |
| Deploy script | ✅ |
| Backend `/health` scaffold | ✅ |
| Backend core — config/addresses/ABIs, typed Aave/Uniswap/oracle/ERC20 clients, RPC failover, Pydantic models (Phase 3) | ✅ |
| Decision pipeline · FastAPI worker · e2e demo (Phases 4–6) | ⏳ next |

## Security notes

- **Non-custodial:** repay is permissionless; collateral moves only via the borrower's **aToken
  allowance/`permit`** (`transferFrom → withdraw`). Credit delegation is not used.
- **Borrower key never touches the backend.** The keeper key only *triggers* and is bounded by the
  borrower's signed `RiskParams`.
- The vault is unaudited PoC code for the hackathon; do not use with real funds without an audit.
