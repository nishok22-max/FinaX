# Implementation Plan — Automated Liquidation Shield & Flash-Repayment Vault

| Field | Value |
|---|---|
| **System** | **FinaX** — Automated Liquidation Shield & Flash-Repayment Vault |
| **Problem Statement** | PS-11, CSI ORIGIN 2026 |
| **Source docs** | [`Problem_Statement_11.pdf`](Problem_Statement_11.pdf) · [`PRD.md`](PRD.md) · [`SYSTEM_ARCHITECTURE.md`](SYSTEM_ARCHITECTURE.md) |
| **Status** | Complete v1.0 (All Phases 0–6 Implemented & Verified) |
| **Chain** | Arbitrum One (chain ID 42161) |
| **On-chain** | Solidity 0.8.24 + Foundry · Aave V3 · Uniswap V3 · OpenZeppelin v5 |
| **Backend** | Python 3.11+ · FastAPI · `web3.py` · Pydantic v2 · APScheduler |
| **AI Co-Pilot** | Google Gemini · LangGraph · LangChain Core · NumberGuard · SQLite |
| **Frontend** | Vanilla HTML5 / CSS3 / ES6 JavaScript Web Console (FastAPI Static Mount) |

> This plan details the completed implementation of the full-stack system: Solidity vault contracts, Python/FastAPI keeper backend, multi-agent AI co-pilot, and the frontend web console.

---

## 1. Build Strategy & Completed Phases

```
Sprint 0  Architecture validation → Aave permission PoC + sizing check (GATE) [COMPLETED]
Phase 0   Repo & tooling          → Foundry + FastAPI scaffold + fork RPC       [COMPLETED]
Phase 1   Contracts (happy path)  → LiquidationShieldVault atomic sequence     [COMPLETED]
Phase 2   Contract hardening      → HealthGuard, 8 revert paths, EIP-712       [COMPLETED]
Phase 3   Backend core            → web3.py clients, failover, Pydantic models [COMPLETED]
Phase 4   Decision pipeline       → monitor, risk, sizing, selector, viability [COMPLETED]
Phase 5   FastAPI service         → API/worker split, in-flight, breaker, sim  [COMPLETED]
Phase 5b  Agentic Co-Pilot layer  → LangGraph StateGraph, Gemini, NumberGuard  [COMPLETED]
Phase 6   UI Console & E2E Demo   → Web console (/console/), seed demo scripts [COMPLETED]
```

Each phase ends in a **runnable, tested** state. Phases 1–2 and 3–4 can proceed in parallel once Phase 0 pins the ABI/interfaces — **but only after Sprint 0 clears the two validation gates.**

---

## 2. Repository Layout

```
liquidation-shield/
├── contracts/                       # Foundry project (on-chain)
│   ├── src/
│   │   ├── LiquidationShieldVault.sol
│   │   ├── interfaces/
│   │   │   ├── IPool.sol             # Aave V3 (subset used)
│   │   │   ├── IFlashLoanSimpleReceiver.sol
│   │   │   ├── IPoolAddressesProvider.sol
│   │   │   ├── ISwapRouter.sol       # Uniswap V3
│   │   │   └── IQuoterV2.sol
│   │   └── libraries/
│   │       └── HealthMath.sol        # HF helpers / bps math
│   ├── test/
│   │   ├── PoC_AavePermissions.t.sol # Sprint 0 Gate A (permission proof)
│   │   ├── ForkBase.t.sol            # shared fork setup + address book
│   │   ├── HappyPath.t.sol
│   │   ├── RevertPaths.t.sol         # incl. ordering + missing-allowance reverts
│   │   └── SizingParity.t.sol        # cross-checks Δd* vs on-chain HF
│   ├── script/Deploy.s.sol
│   ├── addresses.json                # Arbitrum One address book
│   └── foundry.toml
│
├── backend/                         # Python / FastAPI (off-chain)
│   ├── app/
│   │   ├── main.py                   # FastAPI app + lifespan (starts monitor loop)
│   │   ├── config/
│   │   │   ├── settings.py           # pydantic-settings (.env)
│   │   │   └── arbitrum.py           # pinned addresses, ABIs, fee tiers
│   │   ├── chain/
│   │   │   ├── client.py             # AsyncWeb3 provider(s) + failover
│   │   │   ├── aave.py               # getUserAccountData, repay/withdraw encoders
│   │   │   ├── uniswap.py            # QuoterV2 quotes, exactOutput calldata
│   │   │   └── oracle.py             # Chainlink latestRoundData
│   │   ├── core/
│   │   │   ├── models.py             # Pydantic: RiskParams, Assessment, requests
│   │   │   ├── monitor.py            # poll HF + prices
│   │   │   ├── risk.py               # volatility model → breach probability
│   │   │   ├── sizing.py            # Δd* + dynamic HF_target
│   │   │   ├── selector.py           # collateral ranking (FR-5)
│   │   │   ├── viability.py          # economic gate (FR-11)
│   │   │   ├── simulator.py          # eth_call dry-run + Δd* bump loop
│   │   │   ├── submitter.py          # eth_account sign + send (+ private relay)
│   │   │   ├── state.py              # PositionState enum (FR-16)
│   │   │   ├── inflight.py           # per-borrower in-flight lock + cooldown (FR-16)
│   │   │   ├── breaker.py            # circuit breaker (FR-17)
│   │   │   └── protection_service.py # shared pipeline used by API + worker
│   │   ├── api/
│   │   │   ├── routes_health.py
│   │   │   ├── routes_positions.py   # status, assessment, protect
│   │   │   ├── routes_config.py
│   │   │   └── routes_metrics.py
│   │   ├── scheduler.py              # APScheduler async loop wiring
│   │   └── observability.py          # structured logging + counters
│   ├── tests/
│   │   ├── conftest.py               # anvil-fork fixture
│   │   ├── test_sizing.py            # unit: Δd* math
│   │   ├── test_viability.py
│   │   ├── test_selector.py
│   │   └── test_e2e_fork.py          # full rescue against fork
│   ├── abi/                          # exported from Foundry build
│   ├── pyproject.toml
│   └── .env.example
│
├── docs/  (PRD.md, SYSTEM_ARCHITECTURE.md, this plan — or kept at repo root)
└── README.md
```

---

## 2a. Sprint 0 — Architecture Validation (GATE, do before the real vault)

Two things the design *assumes* must be **proven on a fork** before writing `LiquidationShieldVault.sol`. This is the #1 technical de-risking task.

**Gate A — Aave V3 permission proof-of-concept**
- Write a throwaway Foundry fork test (`test/PoC_AavePermissions.t.sol`) that, as a would-be vault contract:
  1. `Pool.repay(USDC, amount, rateMode, onBehalfOf=borrower)` — confirm a third party can repay the borrower's debt with **no** permission.
  2. Have the borrower `aWETH.approve(vault, x)` (or `permit`), then vault `aWETH.transferFrom(borrower, vault, x)` → `Pool.withdraw(WETH, x, vault)` — confirm collateral moves **only** via aToken allowance, and that `Pool.withdraw` has no `onBehalfOf`.
  3. Confirm **repay-before-withdraw** ordering is required (withdraw-first fails the `finalizeTransfer` health check).
- **Explicitly confirm credit delegation is NOT the mechanism for withdrawal.**

**Gate B — Sizing formula validation**
- In Python (or Foundry), take a real fork position, compute candidate `Δd*`, execute the full path, and confirm the resulting on-chain HF matches within tolerance — accounting for **token decimals, oracle precision, per-asset LT, flash premium, DEX fee, slippage, rounding**. Establish the round-up + re-simulate policy here.

**Exit criteria:** both gates pass on the fork with logged evidence; the exact Aave calls + allowance mechanism and the sizing tolerance are documented for Phase 1/4. **Only then start Phase 1.**

---

## 3. Phase 0 — Repository & Tooling

**Tasks**
- Init Foundry (`forge init contracts`); set `foundry.toml` with `fork_url = ${ARBITRUM_RPC_URL}` and a pinned fork block for deterministic tests.
- Create `addresses.json` with Arbitrum One addresses: Aave V3 `PoolAddressesProvider`/`Pool`, Uniswap V3 `SwapRouter`/`QuoterV2`, WETH/wstETH/USDC tokens, relevant Chainlink feeds. **Pin and verify each address against the official deployment registry before use.**
- Init backend (`pyproject.toml`, Python 3.11+): FastAPI, uvicorn, `web3`, `eth-account`, `pydantic`, `pydantic-settings`, `apscheduler`, `httpx`, plus dev deps `pytest`, `pytest-asyncio`, `ruff`, `mypy`.
- `.env.example`: `ARBITRUM_RPC_URL`, `FORK_BLOCK`, `KEEPER_PRIVATE_KEY` (dev only), `POLL_INTERVAL_MS`, contract addresses.

**Exit criteria:** `forge test` runs (empty), `uvicorn app.main:app` boots `/health`, both connect to the fork.

---

## 4. Phase 1 — Contracts: Happy Path (FR-4, FR-7, FR-8)

**Deliver `LiquidationShieldVault.sol`** implementing `IFlashLoanSimpleReceiver`.

**Key functions**
- `executeProtection(RiskParams p, bytes sig, address debtAsset, uint256 repayAmount, address collateralAsset, uint256 amountInMaximum, uint24 uniFeeTier)` — entrypoint; verifies params (Phase 2), then calls `POOL.flashLoanSimple(address(this), debtAsset, repayAmount, encodedParams, 0)`.
- `executeOperation(asset, amount, premium, initiator, params)` — the atomic body (order is load-bearing):
  1. `require(msg.sender == address(POOL) && initiator == address(this))`.
  2. `_repayDebt`: `POOL.repay(debtAsset, amount, rateMode, borrower)` — permissionless; **improves HF first**.
  3. `_withdrawCollateral`: `aToken.transferFrom(borrower, address(this), releaseAmount)` (uses borrower's aToken allowance/`permit`) **then** `POOL.withdraw(collateralAsset, releaseAmount, address(this))`. Must run **after** repay so the aToken `finalizeTransfer` health check passes.
  4. `_executeSwap`: `SWAP_ROUTER.exactOutputSingle(...)` collateral → debtAsset with `amountInMaximum`.
  5. Return residual collateral to borrower; `IERC20(debtAsset).approve(POOL, amount + premium)`.
  6. `_verifyHealthFactor`: multi-invariant HealthGuard (Phase 2).

**One contract, internal functions (not separate deployed modules):** implement `_validateParams`, `_requestFlashLoan`, `_repayDebt`, `_withdrawCollateral`, `_executeSwap`, `_verifyHealthFactor`, `_repayFlashLoan` inside `LiquidationShieldVault.sol`. Use `SafeERC20`; follow checks-effects-interactions. Extract libraries later only if bytecode size demands it.

> **Uses the Sprint 0 Gate A findings** for the exact repay/`transferFrom`/withdraw calls and allowance mechanism. **v1 primary path: single WETH collateral → USDC debt.**

**Tests (`HappyPath.t.sol`):** on fork, create a WETH-collateral / USDC-debt position at HF≈1.2, call `executeProtection`, assert HF restored ≥ target, residual collateral returned, flash loan repaid.

**Exit criteria:** happy-path fork test green; gas reported.

---

## 5. Phase 2 — Contracts: Guards & Revert Safety (FR-6, FR-9, FR-13, FR-14, FR-15)

**Tasks**
- **AccessControl + ParamVerifier:** EIP-712 domain + `RiskParams` typehash; recover signer, `require(signer == p.borrower)`, check `nonce`/`deadline`, restrict caller to authorized keeper or borrower.
- **HealthGuard — multi-invariant no-worse check** (stronger than HF-only): capture `HF_before/debt_before/leverage_before`, then require **all** of `HF_after ≥ hfTarget` **AND** `debt_after < debt_before` **AND** `leverage_after ≤ leverage_before` **AND** `swap_input ≤ amountInMaximum` **AND** flash loan fully repaid; else revert.
- **Swap bound:** enforce `amountInMaximum` (oracle-derived) so an over-slippage swap reverts (FR-6).
- **Reentrancy:** `nonReentrant` on entrypoint; sender/initiator asserts inside callback.
- **No-worse guarantee:** any failed step reverts the whole tx (inherent to the flash-loan callback) — assert via negative-control tests.

**Tests (`RevertPaths.t.sol`):** force swap slippage beyond bound → revert; tamper signature/nonce/deadline → revert; unauthorized caller → revert; `HF_after < HF_target` (undersized repay) → revert; **withdraw-before-repay ordering** → revert (`finalizeTransfer`); **no aToken allowance** → revert; assert state identical to pre-tx in every revert case.

**Exit criteria:** all revert-path tests green; `SizingParity.t.sol` confirms a `Δd*`-sized repay lands HF in `[HF_target, HF_target+ε]`.

---

## 6. Phase 3 — Backend Core (foundation for FR-1…FR-13)

**Tasks**
- `config/settings.py` (`pydantic-settings`) + `config/arbitrum.py` (addresses, ABIs from `contracts/` build, Uniswap fee tiers).
- `chain/client.py`: `AsyncWeb3(AsyncHTTPProvider)` with primary+fallback RPC and reconnect (NFR-3).
- `chain/aave.py`: typed wrappers for `getUserAccountData`, and calldata encoders for `repay`/`withdraw` (for simulation).
- `chain/uniswap.py`: `QuoterV2.quoteExactOutputSingle`; build `exactOutput` calldata.
- `chain/oracle.py`: Chainlink `latestRoundData` + staleness check.
- `core/models.py`: Pydantic `RiskParams` (mirrors Solidity struct), `ProtectRequest`, `AssessmentResponse` (per architecture §8).

**Tests:** unit tests hitting the fork read real `getUserAccountData` and a live quote.

**Exit criteria:** backend can read HF, prices, and quotes from the fork through typed clients.

---

## 7. Phase 4 — Decision Pipeline (FR-1, FR-2, FR-3, FR-5, FR-10, FR-11)

Implement `core/` modules as pure, unit-testable functions fed by the chain clients.

- **`monitor.py` (FR-1):** async poll of HF + prices at `POLL_INTERVAL`; emit `PositionState`.
- **`risk.py` (FR-2, FR-10):** **simple rolling standard deviation** `σ_window` (no ML); breach probability; compute dynamic `HF_target = 1.0 + base + k·σ` (clamped). Demonstrate low σ → 1.25, high σ → 1.40.
- **`sizing.py` (FR-3):** closed-form `Δd* = (HF_target·D − C) / (HF_target − LT_c·(1+f))` (PRD §7) as a **candidate**; convert to token units and **round UP** (decimals-aware). Final amount is confirmed/bumped by the simulator in Phase 5 — math proposes, simulation validates, Solidity enforces.
- **`selector.py` (FR-5):** rank eligible collaterals by liquidity/slippage (`QuoterV2`) and resulting HF; pick best; log rationale.
- **`viability.py` (FR-11):** `cost = flashPremium + swapSlippage+fee + gas`; proceed only if `cost < valueProtected` (PRD §8).

**Tests:** `test_sizing.py` (numeric parity with the contract's `SizingParity`), `test_selector.py`, `test_viability.py` — deterministic unit tests with fixture inputs; one fork-backed case per module.

**Exit criteria:** given a fork position, the pipeline produces a correct `AssessmentResponse` (repay amount, target, collateral, viability) with no submission.

---

## 8. Phase 5 — FastAPI Service, Simulation & Submission (FR-8, FR-12, FR-13, FR-15, FR-16, FR-17)

**Tasks**
- `core/simulator.py`: build the full `executeProtection` tx and `eth_call` dry-run against the fork; verify success and `HF_after ≥ target`; if short, **bump `Δd*` and re-simulate** before spending gas.
- `core/submitter.py`: borrower's EIP-712 `RiskParams`/`permit` signature is **client-supplied**; the keeper signs and sends the tx via `web3.py` (`eth_account`); optional private/MEV-protected relay. **Borrower key never touches the backend.**
- **`core/state.py` — explicit `PositionState` enum** (`HEALTHY, WATCH, ASSESSING, DECLINED, READY, SUBMITTED, RESTORED, REVERTED`) per architecture §7.
- **`core/inflight.py` (FR-16):** per-borrower in-flight lock + cooldown — set on submit, block new rescues until the receipt resolves (`RESTORED`/`REVERTED`), then a short cooldown before re-arming.
- **`core/breaker.py` (FR-17):** circuit breaker — pause autonomous submission after N consecutive failures (default 3) or on stale oracle / inconsistent RPC / invalid quote / gas spike; require operator reset; expose paused state via `/metrics`.
- **Control API vs Background Worker split:** `api/` routes call a shared **Protection Service**; they do **not** run the pipeline inline. `POST /protect` enqueues/dry-runs and returns; the **worker** (APScheduler loop) drives autonomous ticks: `breaker → in-flight → monitor → risk → decision → sizing → viability → simulate/bump → submit → await receipt → update state/breaker`.
- `api/` routes: `GET /health`; `GET /positions/{borrower}`; `GET /positions/{borrower}/assessment`; `POST /positions/{borrower}/protect` (bounded by signed params, FR-13/15); `GET`/`PUT /config`; `GET /metrics`.
- `main.py`: FastAPI app; `lifespan` startup launches the worker; shutdown cancels cleanly.
- `observability.py`: structured logs + counters for every decision (NFR-5).

**Exit criteria:** `POST /protect` on the fork executes a full atomic rescue; the autonomous loop fires the same path when HF crosses `HF_trigger`; a second trigger while a rescue is in-flight does **not** double-submit; 3 forced failures trip the breaker.

---

## 9. Phase 5b — Multi-Agent AI Co-Pilot Layer (FR-18…FR-22)

**Tasks**
- `app/agent/graph.py`: LangGraph StateGraph connecting 4 specialized nodes:
  1. **Risk Analyst**: Ingests on-chain data and formats natural language risk narratives.
  2. **Strategist**: Selects action from constrained enum (`PROTECT_NOW`, `MONITOR`, `RETUNE_MANDATE`, `DECLINE`).
  3. **Mandate Tuner**: Evaluates volatility vs user buffer and generates re-sign parameter payloads (FR-21).
  4. **Auditor**: Reviews decisions against deterministic audit policies and logs records.
- `app/agent/guard.py`: **`NumberGuard`** anti-hallucination provenance layer verifying all LLM numbers against on-chain facts (FR-22).
- `app/agent/tools.py`: Read-only tool surface (`inspect_position`, `explain_vault_error`, `propose_mandate_tuning`, `explain_position_state`). **Contains no submission tool (FR-20).**
- `app/agent/store.py`: Async SQLite ledger (`finax_agent.db`) for proposal history and audit logs.
- `app/agent/chat.py`: Interactive conversational assistant running a multi-turn tool loop.
- `app/api/routes_agent.py`: Endpoints for `/agent/status`, `/agent/chat`, `/agent/crew/run`, `/agent/proposals`, `/agent/proposals/{id}/approve`.

**Exit criteria:** The agent layer runs end-to-end, explains risk, proposes re-tuning, and respects all policy bounds without execution permissions.

---

## 10. Phase 6 — Frontend Web Console & Packaged Demo Suite

**Tasks**
- `frontend/index.html`: Modern semantic HTML structure with navigation for Overview, Position Inspector, Execution Simulator, AI Agent Chat, and System Telemetry.
- `frontend/finax.css`: Premium dark-mode design system with responsive grid layout, custom CSS variables, and glassmorphism.
- `frontend/finax.js`: Dynamic client-side logic for real-time polling (`GET /positions`, `GET /metrics`), interactive simulation, and Gemini agent chat.
- `app/main.py`: Mount static frontend at `/console/` with automatic root redirect (`/ -> /console/`).
- `backend/tools/seed_all_demo_wallets.py`: Automated demo script setting up WETH/USDC supplies and borrows on Arbitrum fork.
- `backend/tools/generate_demo_mandates.py`: Pre-signs EIP-712 demo mandates with test private keys.
- `backend/tools/verify_agent_live.py`: Autonomous test script verifying Gemini LLM integration and chat responses.

**Exit criteria:** The Web Console boots seamlessly on `/console/`, presents live position gauges, runs dry-run simulations, and communicates with the AI co-pilot.

---

## 11. Task → Requirement Traceability (Full Scope)

| Requirement | Phase(s) | Primary artifact(s) | Status |
|---|---|---|---|
| FR-1 Continuous monitoring | 4, 5 | `core/monitor.py`, scheduler | ✅ Shipped |
| FR-2 Risk prediction | 4 | `core/risk.py` | ✅ Shipped |
| FR-3 Min-effective sizing | 4, 2 | `core/sizing.py`, `SizingParity.t.sol` | ✅ Shipped |
| FR-4 Collateral amount/source | 1 | `CollateralManager` in vault | ✅ Shipped |
| FR-5 Collateral selection | 4 | `core/selector.py` | ✅ Shipped |
| FR-6 DEX liquidity/slippage | 2, 4 | `SwapExecutor` `amountInMaximum`, `chain/uniswap.py` | ✅ Shipped |
| FR-7 Flash sourcing & cost | 1, 4 | `FlashLoanReceiver`, `core/viability.py` | ✅ Shipped |
| FR-8 Atomic execution | 1 | `executeOperation` in `LiquidationShieldVault.sol` | ✅ Shipped |
| FR-9 Revert / no-worse | 2 | HealthGuard, `RevertPaths.t.sol` | ✅ Shipped |
| FR-10 Dynamic safety buffer | 4 | `core/risk.py` (`HF_target`) | ✅ Shipped |
| FR-11 Economic-viability gate | 4 | `core/viability.py` | ✅ Shipped |
| FR-12 Proactive action | 4, 5 | `HF_trigger` in monitor/scheduler | ✅ Shipped |
| FR-13 Autonomous bounded authority | 2, 5 | ParamVerifier, signed-params flow | ✅ Shipped |
| FR-14 Self-safety | 2 | multi-invariant HealthGuard, revert tests | ✅ Shipped |
| FR-15 Authorization / opt-in | Sprint 0, 2, 5 | Gate A permission PoC, EIP-712 verify, aToken allowance | ✅ Shipped |
| FR-16 In-flight lock & idempotency | 5 | `core/inflight.py`, `core/state.py` | ✅ Shipped |
| FR-17 Circuit breaker | 5 | `core/breaker.py` | ✅ Shipped |
| FR-18 Agentic orchestration & narrative | 5b | `app/agent/graph.py` | ✅ Shipped |
| FR-19 Deterministic policy gate | 5b | `app/agent/policy.py` | ✅ Shipped |
| FR-20 Human-in-the-loop approval | 5b | `app/api/routes_agent.py` | ✅ Shipped |
| FR-21 Bounded-mandate immutability | 5b | `app/agent/tools.py` | ✅ Shipped |
| FR-22 NumberGuard numeric provenance | 5b | `app/agent/guard.py` | ✅ Shipped |

---

## 12. Testing & Verification Summary

- **Contracts:** Foundry fork tests (`13/13 passing`):
  * Gate A: `PoC_AavePermissionsTest` (3/3)
  * Gate B: `SizingParityTest` (1/1)
  * Phase 1: `HappyPathTest` (1/1)
  * Phase 2: `RevertPathsTest` (8/8)
- **Backend Quality Gates:** `ruff check`, `mypy --strict`, and `pytest` clean.
- **Agent Integration:** `verify_agent_live.py` confirms live LLM connection, tool calling, and NumberGuard enforcement.
- **Frontend Console:** Verified at `http://127.0.0.1:8097/console/` with full telemetry and simulation features.

---

## 13. Environments & Deployment

| Tier | Network | Cost | Status |
|---|---|---|---|
| Dev + integration + **demo** | **Arbitrum One mainnet fork** (`anvil --fork-url $ARBITRUM_RPC_URL`) | Free | **Active** on port `8548` |
| Backend & Web Console | Python 3.11+ / FastAPI / Uvicorn | Localhost | **Active** on port `8097` |

---

*End of Implementation Plan — FinaX Automated Liquidation Shield & Flash-Repayment Vault (PS-11).*
