# Product Requirements Document — Automated Liquidation Shield & Flash-Repayment Vault

| Field | Value |
|---|---|
| **Product name** | **FinaX** — Automated Liquidation Shield & Flash-Repayment Vault |
| **Problem Statement ID** | 11 |
| **Event** | CSI ORIGIN 2026 |
| **Document status** | Complete v1.0 (Sprint 0 + Phases 0–6 Shipped) |
| **Date** | 2026-08-29 |
| **Target chain** | **Arbitrum One** (chain ID 42161) |
| **Lending protocol** | **Aave V3** (Arbitrum deployment) |
| **Flash liquidity** | **Aave V3 flash loans** (`flashLoanSimple`, 0.05% premium) |
| **Swap venue** | **Uniswap V3** (Arbitrum) |
| **Price source** | **Chainlink** price feeds (Arbitrum) + Aave oracle |
| **Backend** | **Python 3.11+ / FastAPI** (async keeper service via `web3.py`) |
| **AI Co-Pilot** | **Google Gemini + LangGraph** (Multi-Agent StateGraph with NumberGuard) |
| **Frontend** | **Vanilla HTML5/CSS3/ES6 JS Web Console** (mounted at `/console/`) |
| **Deliverable scope** | Full-Stack: Solidity Vault + Python/FastAPI Keeper + LangGraph Multi-Agent Layer + Web Console UI + Fork Demo Suite |

---

## 1. Overview

Overcollateralized lending protocols let users borrow against deposited collateral. During sharp market moves, a borrower's **Health Factor (HF)** can fall below the protocol's liquidation threshold faster than the borrower can react. Once liquidatable, third-party liquidators repay part of the debt and seize collateral plus a **liquidation penalty (bonus)**, causing avoidable capital loss — even when the position could have been saved by a timely, correctly-sized repayment.

This product is an **autonomous, on-chain liquidation-protection system** for Aave V3 positions on Arbitrum. It continuously monitors a delegated position, **predicts** liquidation risk *before* the position becomes liquidatable, computes the **minimum effective intervention** needed to restore the position above a **dynamic, volatility-aware safety threshold**, and executes the full restructuring **atomically** in a single transaction:

```
Monitor Position → Predict Liquidation Risk → Calculate Optimal Repayment → Source Flash Liquidity
→ Repay Debt → Unlock Collateral → Swap → Repay Flash Loan → Restore Safe Position
```

If any step in the atomic sequence cannot complete under acceptable economic conditions, the transaction **reverts**, leaving the user's position unchanged rather than partially or worse-restructured.

> This is **not** a liquidation-alert bot and **not** a naïve auto-repay bot. It is autonomous on-chain risk management with atomic, capital-minimizing position restructuring and an intelligent AI explanation co-pilot.

---

## 2. Goals & Non-Goals

### 2.1 Goals
- **G1 — Proactive protection:** Intervene *before* HF crosses the liquidation threshold, not merely alert afterward.
- **G2 — Minimum effective intervention:** Consume the least user capital required to reach the target HF ($\Delta d^*$).
- **G3 — Atomicity:** The entire flash-loan → repay → withdraw → swap → repay-flash sequence succeeds or the tx reverts.
- **G4 — Economic rationality:** Never execute an intervention whose cost (flash-loan premium + swap slippage + gas) is unjustified relative to the liquidation loss avoided.
- **G5 — Dynamic safety buffer:** Target a safety margin above the liquidation boundary that adapts to volatility, not a single fixed constant.
- **G6 — Autonomy:** Operate under human-defined risk preferences but decide *when* and *how much* to intervene without per-event human action.
- **G7 — Non-custodial safety:** The protection mechanism must not itself introduce additional insolvency, excessive leverage, or unjustified capital loss.
- **G8 — Explainable AI Co-Pilot:** Provide transparent, plain-English risk narration, mandate tuning recommendations, and smart contract error explanation without giving the AI execution power over funds.

### 2.2 Scope & Disciplines
- **Single lending protocol focus:** Aave V3 on Arbitrum One (protocol-agnostic adapter is future work).
- **Core protection path:** Focus on dominant collateral/debt pairs (e.g. WETH/wstETH/USDC) with generalized multi-collateral selection (FR-5).
- **Scope discipline:** "Math proposes, simulation validates, Solidity enforces." No model output participates in risk, sizing, or viability calculations; the LLM orchestrates and explains, while all numbers are validated against live on-chain facts via `NumberGuard`.
- **Zero Custodial Hold:** The contract is stateless between blocks and holds no user balances across transactions.

---

## 3. Personas & Use Cases

- **P1 — Leveraged borrower (primary):** Holds a borrow position on Aave V3 Arbitrum (e.g., collateral WETH / wstETH, debt USDC). Wants automated protection without watching charts 24/7.
- **P2 — Keeper operator (system):** Runs the off-chain monitoring agent that submits protection transactions when conditions are met.
- **P3 — Protocol / liquidity venues (external):** Aave V3 pool, Uniswap V3 pools, Chainlink oracles — integration surfaces, not users.

**Primary use case:** A borrower opts in by delegating the minimum required permissions to the `LiquidationShieldVault`. The keeper monitors HF; when risk crosses the intervention trigger, the vault atomically restructures the position back above the target HF, financed by an Aave flash loan and a Uniswap V3 collateral swap, at no worse than the user's approved slippage and cost bounds.

---

## 4. System Architecture

Two cooperating components: an **on-chain executor** (trust-minimized Solidity contract, holds the atomicity guarantee) and an **off-chain brain** — a **Python/FastAPI backend** (monitoring, prediction, sizing, submission) that drives the contract via `web3.py`.

```
┌──────────────── OFF-CHAIN (Keeper Backend — Python / FastAPI) ────────────────────┐
│  Position Monitor  →  Risk/Volatility Model  →  Intervention Sizer                 │
│      │  (Aave getUserAccountData, Chainlink feeds, DEX quotes via Quoter)          │
│      ▼                                                                             │
│  Economic-Viability Gate  →  Tx Builder & Simulator  →  Signer/Submitter (private) │
└───────────────────────────────────────┬───────────────────────────────────────────┘
                                         │  executeProtection(params)
                                         ▼
┌──────────────────────────── ON-CHAIN (Arbitrum One) ──────────────────────────────┐
│  LiquidationShieldVault (IFlashLoanSimpleReceiver)                                  │
│   1. Aave Pool.flashLoanSimple(debtAsset, amount)                                   │
│   2. executeOperation():                                                            │
│        a. Pool.repay(debtAsset, amount, user)      // reduce debt                   │
│        b. Pool.withdraw(collateral, released)      // unlock collateral (on-behalf) │
│        c. UniswapV3 SwapRouter.exactOutput(...)     // collateral → debtAsset        │
│        d. approve + return amount+premium to Pool  // repay flash loan              │
│        e. require(HF_after ≥ HF_target)            // else revert                   │
└────────────────────────────────────────────────────────────────────────────────────┘
```

### 4.1 On-chain: `LiquidationShieldVault`
- Implements Aave's `IFlashLoanSimpleReceiver.executeOperation`.
- **Repays the borrower's debt permissionlessly** (`Pool.repay(asset, amount, rateMode, onBehalfOf=borrower)` — anyone may repay another user's debt) and **moves collateral via the borrower's aToken allowance** using the `transferFrom → withdraw` pattern (see §4.2). The vault never takes custody beyond the single transaction.
- **Ordering is load-bearing:** repay **before** collateral withdrawal, so the borrower's Health Factor is already improved when the aToken transfer's `finalizeTransfer` health check runs.
- Swaps released collateral to the debt asset via Uniswap V3 `SwapRouter` (`exactOutput` preferred so exactly the flash repayment amount is produced; residual collateral returned to user).
- Enforces the **multi-invariant no-worse guarantee** (see §4.3), not just `HF_after ≥ HF_target`, else `revert`.
- Access-controlled: only the authorized keeper (or the user directly) can trigger; user-signed risk parameters bound every execution.
- **Single contract, logical modules:** implemented as one `LiquidationShieldVault.sol` with internal functions (`_validateParams`, `_requestFlashLoan`, `_repayDebt`, `_withdrawCollateral`, `_executeSwap`, `_verifyHealthFactor`, `_repayFlashLoan`); libraries are extracted later only if size demands it. Logical modularity > physical contract modularity for v1.

### 4.2 Aave V3 Permission Model (verify before implementing)

> The vault's two Aave operations are **authorized differently**. This must be validated with a small on-fork proof-of-concept before writing the production vault (see Implementation Plan Sprint 0).

| Operation | Authorization required | Mechanism |
|---|---|---|
| **Repay borrower's debt** | **None** (permissionless) | `Pool.repay(debtAsset, amount, rateMode, onBehalfOf=borrower)`; vault only needs to hold + approve the debt asset (from the flash loan). |
| **Withdraw borrower's collateral** | **Borrower's aToken allowance** (ERC-20 `approve`, or EIP-2612 `permit`) | `Pool.withdraw` has **no `onBehalfOf`** — it burns the *caller's* aTokens. So: borrower approves the vault on the collateral aToken → vault `aToken.transferFrom(borrower, vault, releaseAmount)` → `Pool.withdraw(collateral, releaseAmount, vault)`. |

- **Credit delegation is NOT used** — it authorizes *borrowing* on someone's behalf, not collateral withdrawal. The earlier assumption was incorrect.
- **Opt-in:** the borrower's one-time action is an **aToken allowance/permit** per protectable collateral (prefer an exact-amount, expiring `permit` per rescue over a standing infinite approval — see §9).
- **`finalizeTransfer` health check:** the aToken `transferFrom` reverts if it would drop the borrower below HF 1.0, which is why repay must precede withdrawal and the whole sequence must be atomic.

### 4.3 No-Worse Guarantee — HealthGuard invariants

The atomic sequence reverts unless **all** of the following hold post-execution (stronger than an HF-only check):

```
HF_after      ≥ HF_target
debt_after    < debt_before
leverage_after ≤ leverage_before
swap_input    ≤ amountInMaximum        (oracle-bounded)
flash loan fully repaid (amount + premium)
```

### 4.4 Off-chain: Keeper Backend (Python / FastAPI)
- **Language/stack:** **Python 3.11+**, **FastAPI** (served by **uvicorn**, ASGI), **`web3.py`** (`AsyncWeb3`) for all chain interaction, **`eth_account`** for EIP-712 / tx signing, **APScheduler** (async) for the poll loop, **Pydantic v2** for models and config (`pydantic-settings` / `.env`).
- **Control API vs Background Worker are separated.** The FastAPI `lifespan` hook starts an independent **background worker** (the autonomous monitor→…→submit loop). HTTP handlers do **not** run the pipeline inline — `POST /protect` hands the request to a protection service which the worker executes; the request returns an accepted/assessment response. This keeps request latency bounded and the autonomous loop authoritative.
- **Monitor:** async task polls `Pool.getUserAccountData(user)` (returns `totalCollateralBase`, `totalDebtBase`, `availableBorrowsBase`, `currentLiquidationThreshold`, `ltv`, `healthFactor`) each block/interval and reads Chainlink feeds via `web3.py`.
- **Predict:** volatility model estimates probability HF breaches the liquidation threshold within the execution window (N blocks).
- **Size:** computes minimum debt repayment to reach `HF_target` (see §7).
- **Select collateral:** ranks eligible collaterals by liquidity/slippage/resulting-risk (see §6, FR-5).
- **Viability gate:** simulates via `web3.py` `eth_call` (Tenderly optional) and Uniswap V3 `QuoterV2`; proceeds only if net benefit is positive (§8).
- **Submit:** signs and sends the protection tx via `web3.py` (optionally via a private/MEV-protected route on Arbitrum).
- **Control & observability API (FastAPI):** `GET /health`; `GET /positions/{borrower}` (HF + collateral/debt snapshot + risk verdict); `GET /positions/{borrower}/assessment` (dry-run sizing `Δd*`, dynamic `HF_target`, selected collateral, viability verdict — no submit); `POST /positions/{borrower}/protect` (manual trigger, still bounded by the borrower's signed params); `GET`/`PUT /config` (bounded, authed risk parameters); `GET /metrics`.

> **Contracts remain Solidity.** "Entire backend in Python" refers to the off-chain service; the Python/FastAPI backend drives the deployed Solidity `LiquidationShieldVault` through its ABI via `web3.py`.

---

## 5. The Protection Loop (mapped to concrete calls)

| Step | PS phase | Concrete implementation |
|---|---|---|
| 1 | Monitor Position | `Pool.getUserAccountData(user)`; Chainlink `latestRoundData()` |
| 2 | Predict Liquidation Risk | Off-chain volatility model → time-to-liquidation / breach probability |
| 3 | Calculate Optimal Repayment | Closed-form repay amount to reach `HF_target` (§7) |
| 4 | Source Flash Liquidity | `Pool.flashLoanSimple(vault, debtAsset, repayAmount, params, 0)` |
| 5 | Repay Debt | `Pool.repay(debtAsset, repayAmount, rateMode, user)` |
| 6 | Unlock Collateral | `Pool.withdraw(collateralAsset, releaseAmount, vault)` |
| 7 | Swap | Uniswap V3 `SwapRouter.exactOutputSingle/exactOutput` (collateral → debtAsset) |
| 8 | Repay Flash Loan | `approve(pool, repayAmount + premium)`; Aave pulls repayment |
| 9 | Restore Safe Position | `require(getUserAccountData(user).healthFactor ≥ HF_target)` |

---

## 6. Functional Requirements

Each requirement is testable; acceptance criteria are given inline. IDs are referenced in the traceability matrix (§16).

- **FR-1 — Continuous monitoring.** The agent SHALL continuously read the position's HF, collateral, and debt state and detect when HF approaches a configured **intervention threshold** `HF_trigger` (e.g., 1.15) that sits above the protocol liquidation boundary (HF = 1.0).
  *Accept:* Given a position whose HF falls to `HF_trigger`, the agent flags it within one monitoring interval.

- **FR-2 — Liquidation-risk prediction.** The agent SHALL assess risk *before* liquidation is inevitable, using collateral/debt state plus a volatility signal, not merely a threshold crossing.
  *Accept:* For a position trending downward, the agent raises risk while HF is still > 1.0.

- **FR-3 — Minimum-effective intervention sizing.** The system SHALL compute the **smallest** debt-repayment amount that restores HF to the dynamic `HF_target`, avoiding over-repayment.
  *Accept:* Computed `repayAmount` yields `HF_after ∈ [HF_target, HF_target + ε]`; no smaller amount reaches `HF_target`.

- **FR-4 — Collateral source & amount.** The system SHALL determine which collateral to release and how much to convert to fund the repayment.
  *Accept:* `releaseAmount` covers `repayAmount + flashPremium + swap cost` given the quoted price; residual returned to user.

- **FR-5 — Collateral selection (multi-asset).** Where multiple collaterals exist, the system SHALL rank them by value, DEX liquidity, expected slippage, and resulting post-trade HF, and pick the best.
  *Accept:* Given two eligible collaterals, the one producing lower total cost and acceptable resulting HF is selected; rationale logged.

- **FR-6 — DEX liquidity & slippage assessment.** Before executing, the system SHALL verify sufficient Uniswap V3 liquidity and bound expected slippage via `QuoterV2`.
  *Accept:* If the quote implies slippage above `maxSlippageBps`, the intervention is not attempted (or reverts on-chain via `amountInMaximum`).

- **FR-7 — Flash-liquidity sourcing & cost.** The system SHALL source temporary liquidity via Aave `flashLoanSimple` and account for the 0.05% premium in sizing and viability.
  *Accept:* Repayment to Aave equals `amount + premium`; premium is included in cost math.

- **FR-8 — Atomic execution.** Repay, withdraw, swap, and flash-repay SHALL occur in one transaction inside `executeOperation`.
  *Accept:* All effects land in a single tx; no intermediate state is externally observable/exploitable.

- **FR-9 — Revert on failure / no-worse guarantee.** If any step fails or post-conditions are unmet, the tx SHALL revert, leaving the position unchanged.
  *Accept:* On forced swap failure or `HF_after < HF_target`, state is identical to pre-tx (only gas spent by keeper).

- **FR-10 — Dynamic safety buffer.** `HF_target` SHALL be computed from volatility/market conditions, not a single fixed constant.
  *Accept:* Under higher measured volatility, `HF_target` increases (e.g., 1.25 → 1.5), verifiable in logs.

- **FR-11 — Economic-viability gate.** The system SHALL execute only when expected cost < value protected (liquidation penalty + collateral loss avoided).
  *Accept:* A scenario where total cost exceeds capital-at-risk results in no execution; rationale logged.

- **FR-12 — Proactive action.** Wherever information and execution conditions allow, the system SHALL act before HF reaches the liquidation threshold.
  *Accept:* Interventions in the demo fire at HF > 1.0.

- **FR-13 — Autonomous operation under bounded authority.** The system SHALL act autonomously within user-signed risk parameters (`HF_trigger`, `HF_target` policy, `maxSlippageBps`, `maxCostBps`, allowed collaterals) without per-event approval.
  *Accept:* Given valid signed parameters, protection executes without additional user interaction; parameters bound every tx.

- **FR-14 — Self-safety.** The protection sequence SHALL NOT increase leverage, create new insolvency, or cause unjustified capital loss.
  *Accept:* Post-intervention debt and leverage are ≤ pre-intervention; HF strictly improves.

- **FR-15 — Authorization & opt-in.** A position is protectable only after the borrower grants the vault an **aToken allowance / `permit`** for each protectable collateral (§4.2) and signs risk parameters. Debt repayment itself needs no grant.
  *Accept:* Without the aToken allowance, the collateral `transferFrom`/withdraw step reverts; the allowance is revocable.

- **FR-16 — In-flight lock & idempotency.** While a rescue tx for a borrower is submitted and unconfirmed, the system SHALL NOT submit another rescue for that borrower; it waits for the receipt and updates state (RESTORED/REVERTED) before reconsidering.
  *Accept:* Given HF still below trigger across consecutive blocks after a submit, exactly one rescue tx is broadcast until its receipt resolves.

- **FR-17 — Circuit breaker.** The keeper SHALL pause autonomous submission on abnormal conditions — N consecutive tx failures (default 3), stale oracle, inconsistent RPC, invalid DEX quote, or abnormally high gas — and require operator review to resume.
  *Accept:* After 3 consecutive failed rescues (or a stale-oracle signal), the loop stops submitting and surfaces a paused state via `/metrics`; no further gas is spent until reset.

### 6.1 Agentic layer (optional, additive)

FR-18…FR-22 describe an **optional** LLM-driven layer over the requirements above. It is shipped as
a separate install extra and defaults to off; with it absent or disabled, FR-1…FR-17 behave exactly
as specified. It does not amend NG6: the *decision* math remains non-ML (see the NG6 note).

- **FR-18 — Agentic orchestration & explanation.** The system MAY run a multi-agent crew that
  observes position state, narrates risk in natural language, answers operator questions, and
  selects a course of action from a **fixed enumeration**. The crew SHALL NOT compute any numeric
  quantity that reaches a transaction; every figure originates from the deterministic pipeline
  (FR-2, FR-3, FR-10, FR-11).
  *Accept:* No LLM output is an argument to the submission path; disabling the layer changes no
  keeper behaviour.

- **FR-19 — Deterministic policy gate.** Every agent-originated proposal SHALL pass a pure,
  LLM-free policy gate — bounds on repay size, cost, the signed HF band, the collateral allow-list,
  breaker and in-flight state, and rate limits — before it is persisted, and the gate SHALL be
  **re-evaluated against a freshly recomputed assessment** immediately before execution.
  *Accept:* A proposal whose position moved while awaiting approval is rejected as stale rather
  than executed on figures that no longer hold.

- **FR-20 — Human-in-the-loop approval.** An agent-originated proposal SHALL NOT reach the chain
  without an explicit human approval action. Approval SHALL execute through the *same* path as a
  manual request — breaker (FR-17) → in-flight lock (FR-16) → sizing (FR-3) → viability (FR-11) →
  simulation → atomic submit (FR-8). The agent SHALL possess no capability to submit.
  *Accept:* The agent's tool surface contains no submission tool; approval on a paused breaker is
  refused and no gas is spent.

- **FR-21 — Bounded-mandate immutability.** The agent SHALL NOT modify a borrower's signed
  `RiskParams`. A proposed parameter change SHALL be emitted as a **re-sign request** carrying the
  full new `RiskParams` for the borrower to sign (FR-13, FR-15).
  *Accept:* No agent path mutates a registered mandate; execution always replays the stored
  borrower-signed pair verbatim, and a tampered mandate reverts `BadSignature` on chain.

- **FR-22 — Numeric provenance.** Every figure the agent surfaces SHALL be traceable to a named
  backend response field, and any figure that cannot be traced SHALL be **visibly marked as
  unverified** rather than rendered in the same visual language as live data (NFR-5).
  *Accept:* Each agent reply carries a source map; untraceable figures render in a distinct
  degraded style.

---

## 7. Intervention Sizing Math

**Health Factor (Aave):**
```
HF = ( Σ (collateral_i × price_i × liquidationThreshold_i) ) / ( Σ (debt_j × price_j) )
```
All values in the oracle base currency; thresholds as fractions.

**Target repayment (single debt asset, releasing collateral `c`).** Let:
- `C` = weighted collateral term = `Σ collateral_i × price_i × LT_i`
- `D` = total debt value = `Σ debt_j × price_j`
- Repaying `Δd` (in value) reduces debt; releasing collateral `c` of value `Δc` (with `Δc ≈ Δd + premium + swapCost`) reduces the collateral term by `Δc × LT_c`.

We require post-intervention:
```
HF_target ≤ (C − LT_c × Δc) / (D − Δd)
```
Solving for the **minimum** `Δd` that reaches `HF_target` (with `Δc = Δd·(1 + f)`, where `f` bundles flash premium + slippage + fees):

```
Δd* = ( HF_target · D − C ) / ( HF_target − LT_c · (1 + f) )
```
Choosing a collateral with higher `LT_c` reduces the collateral needed and improves the resulting HF — a factor in FR-5 selection.

> **The closed-form `Δd*` is a candidate, not a final answer — math proposes, simulation validates, Solidity enforces.** The real execution carries flash premium, DEX fee, slippage, rounding, oracle precision, token decimals, and per-asset LT that the formula only approximates. The backend therefore runs:
> ```
> compute candidate Δd*  →  round UP (safe direction, token decimals)
>   →  simulate full tx (eth_call)  →  read resulting HF_after
>   →  if HF_after < HF_target: bump Δd* slightly and re-simulate
>   →  if viable and HF_after ≥ HF_target: submit
> ```
> The on-chain HealthGuard (§4.3) is the final authority — it reverts if the enforced amount does not actually reach `HF_target` under real execution.

**Dynamic safety buffer (`HF_target`).**
```
HF_target = HF_liq_boundary (=1.0) + base_margin + k · σ_window
```
where `σ_window` is realized/estimated collateral-vs-debt volatility over the execution window and `k` is a risk-preference coefficient from user parameters. Bounded to `[HF_min_target, HF_max_target]`. `HF_trigger` (when to act) sits below `HF_target` (where to restore) so the system acts early and restores with margin.

---

## 8. Economic-Viability Model

Execute only if:
```
ValueProtected  >  InterventionCost
```
where:
- **ValueProtected** ≈ expected liquidation penalty avoided = `liquidationBonus × debtLiquidatable` + expected extra collateral loss from adverse continuation.
- **InterventionCost** = `flashPremium (0.05% × repayAmount)` + `swapSlippage + poolFee (Uniswap V3 tier)` + `gasCost` (Arbitrum L2 gas + L1 calldata).

The keeper estimates each term from live quotes (`QuoterV2`), Chainlink prices, and current gas; the on-chain contract additionally enforces `amountInMaximum` / `maxCostBps` so an unexpectedly costly execution reverts (belt-and-suspenders with FR-9).

---

## 9. Authorization & Security Model

- **Opt-in, non-custodial:** Debt repayment needs **no** permission; the only grant is an **aToken allowance (prefer EIP-2612 `permit`, exact-amount + expiring) per protectable collateral** so the vault can pull that collateral via `transferFrom → withdraw` (§4.2). Revocable anytime. Vault holds funds only within the single atomic tx. **Credit delegation is not used.**
- **Allowance is the trust anchor:** a standing aToken allowance means the vault *could* pull that collateral, so the vault must be immutable/audited and its code only ever pulls the minimum inside a valid signed-params execution that reverts unless the no-worse invariants (§4.3) hold. Prefer per-rescue `permit` over infinite approval to bound what the keeper can pull between events.
- **Key separation (critical security property):** the **borrower's private key never touches the backend** — the borrower signs `RiskParams` (and any `permit`) client-side. The **keeper key can only *trigger*** `executeProtection`, and is powerless outside the borrower's signed bounds — it cannot choose amounts, collaterals, or recipients beyond the signed params.
- **Access control:** `executeProtection` callable by the authorized keeper or the user; every call is bound by user-signed risk parameters (nonce'd, expiring signatures).
- **Reentrancy:** `nonReentrant` guard; checks-effects-interactions; `executeOperation` validates `msg.sender == aavePool` and `initiator == vault`.
- **Oracle-manipulation resistance:** Sizing uses Aave/Chainlink oracle prices (not spot pool prices) for HF; Uniswap interaction bounded by `amountInMaximum` derived from oracle price ± `maxSlippageBps` to resist sandwiching.
- **MEV:** Optional private submission on Arbitrum; strict slippage bounds cap sandwich profit; `exactOutput` fixes the produced debt amount.
- **Revert guarantees:** The multi-invariant HealthGuard (§4.3) ensures no partial/worse restructuring (FR-9, FR-14).

---

## 10. Technology Stack

| Layer | Choice |
|---|---|
| **Network** | Arbitrum One (L2), fork-tested against live state |
| **Lending** | Aave V3 `Pool` (`getUserAccountData`, `repay`, `withdraw`, `flashLoanSimple`) |
| **Flash loans** | Aave V3 `flashLoanSimple` (0.05% premium) |
| **DEX** | Uniswap V3 `SwapRouter` (`exactOutput`), `QuoterV2` for quotes |
| **Oracles** | Chainlink price feeds (Arbitrum) + Aave protocol oracle |
| **Contracts (on-chain)** | Solidity ^0.8.x, OpenZeppelin (`ReentrancyGuard`, `SafeERC20`, access control) |
| **Contract tooling** | Foundry (forge/anvil) for build, test, and Arbitrum mainnet-fork tests |
| **Backend** | **Python 3.11+**, **FastAPI** + **uvicorn** (ASGI), **`web3.py`** (`AsyncWeb3`), **`eth_account`** (EIP-712/signing), **APScheduler** (async monitor loop), **Pydantic v2** + `pydantic-settings` |
| **Backend API** | FastAPI REST: `/health`, `/positions/{borrower}`, `/positions/{borrower}/assessment`, `/positions/{borrower}/protect`, `/config`, `/metrics` |
| **Backend simulation** | `web3.py` `eth_call` dry-run (Tenderly optional) |
| **Interfaces** | `IPool`, `IFlashLoanSimpleReceiver`, `IPoolAddressesProvider`, `ISwapRouter`, `IQuoterV2` (consumed from Python via ABI/`web3.py`) |

> Concrete Arbitrum One addresses (Aave V3 `PoolAddressesProvider`/`Pool`, Uniswap V3 `SwapRouter`/`QuoterV2`, relevant Chainlink feeds) to be pinned in the Python backend config (`config/arbitrum.py` / `settings.py`, `pydantic-settings`) and Foundry `addresses.json` during implementation.

---

## 11. Non-Functional Requirements

- **NFR-1 — Latency:** Monitor→decision→submit within the execution window (target: act within a few Arbitrum blocks of trigger; Arbitrum ~0.25s block cadence gives margin). The FastAPI backend uses `asyncio`/`AsyncWeb3` so monitoring, simulation, and submission are non-blocking.
- **NFR-2 — Gas budget:** Protection tx gas within a bound where `gasCost ≪ ValueProtected` for realistic positions.
- **NFR-3 — Reliability:** Keeper reconnects to RPC/feeds; missed intervals do not corrupt state; idempotent decisions.
- **NFR-4 — Testability:** Contracts covered by Foundry fork tests; the Python backend covered by `pytest` (unit + fork-integration against Arbitrum state); all FRs exercised end-to-end (§14 demo scenario reproducible).
- **NFR-5 — Observability:** Structured logs for every decision (risk, sizing, selected collateral, viability verdict, tx hash / revert reason).
- **NFR-6 — Security posture:** No plaintext keys in code; keeper signer from env/KMS; contracts pass slither/basic audit checks.

---

## 12. Milestones & Implementation Status

| Milestone | Phase | Scope | Status | Verification |
|---|---|---|---|---|
| **M0** | Sprint 0 | Aave permission PoC (`PoC_AavePermissions.t.sol`) & Sizing parity check | ✅ Shipped | 3/3 fork tests pass |
| **M1** | Phase 0–1 | Repo setup, interfaces, `LiquidationShieldVault.sol` happy path | ✅ Shipped | Fork test passes |
| **M2** | Phase 2 | Contract hardening, HealthGuard invariants, 8 revert paths | ✅ Shipped | 13/13 Foundry suite passes |
| **M3** | Phase 3 | Typed chain clients (`web3.py`), failover, config, Pydantic models | ✅ Shipped | Unit + fork integration tests |
| **M4** | Phase 4 | Decision pipeline (monitor, risk, $\Delta d^*$ sizing, selector, viability) | ✅ Shipped | `test_pipeline_position_fork.py` |
| **M5** | Phase 5 | FastAPI control API + background worker + in-flight lock + circuit breaker | ✅ Shipped | `test_protect_e2e_fork.py` |
| **M6** | Phase 6 | Multi-Agent LangGraph/Gemini layer (FR-18..22) + Web Console UI + Demo suite | ✅ Shipped | E2E fork demo & live tests |

---

## 13. Comprehensive Requirements Verification Matrix (FR-1 to FR-22)

| Requirement | Description | Layer | Implementation File | Status |
|---|---|---|---|---|
| **FR-1** | Continuous monitoring of HF & position state | Backend | [`app/core/monitor.py`](file:///c:/Users/HP/Desktop/nishin/backend/app/core/monitor.py) | ✅ Verified |
| **FR-2** | Liquidation-risk prediction & dynamic threshold | Backend | [`app/core/risk.py`](file:///c:/Users/HP/Desktop/nishin/backend/app/core/risk.py) | ✅ Verified |
| **FR-3** | Minimum-effective intervention sizing ($\Delta d^*$) | Backend / Math | [`app/core/sizing.py`](file:///c:/Users/HP/Desktop/nishin/backend/app/core/sizing.py) | ✅ Verified |
| **FR-4** | Collateral release amount computation | Backend / Contract | [`app/core/sizing.py`](file:///c:/Users/HP/Desktop/nishin/backend/app/core/sizing.py) | ✅ Verified |
| **FR-5** | Multi-collateral ranking & selection | Backend | [`app/core/selector.py`](file:///c:/Users/HP/Desktop/nishin/backend/app/core/selector.py) | ✅ Verified |
| **FR-6** | DEX liquidity & slippage via QuoterV2 | Backend | [`app/chain/uniswap.py`](file:///c:/Users/HP/Desktop/nishin/backend/app/chain/uniswap.py) | ✅ Verified |
| **FR-7** | Flash liquidity sourcing & premium accounting | Contract / Backend | [`LiquidationShieldVault.sol`](file:///c:/Users/HP/Desktop/nishin/contracts/src/LiquidationShieldVault.sol) | ✅ Verified |
| **FR-8** | Single-tx atomic execution (Aave callback) | Contract | [`LiquidationShieldVault.sol`](file:///c:/Users/HP/Desktop/nishin/contracts/src/LiquidationShieldVault.sol) | ✅ Verified |
| **FR-9** | Atomic revert on failure / no-worse guarantee | Contract | [`LiquidationShieldVault.sol`](file:///c:/Users/HP/Desktop/nishin/contracts/src/LiquidationShieldVault.sol) | ✅ Verified |
| **FR-10** | Dynamic volatility-based safety buffer | Backend | [`app/core/risk.py`](file:///c:/Users/HP/Desktop/nishin/backend/app/core/risk.py) | ✅ Verified |
| **FR-11** | Economic-viability gate (cost < value protected) | Backend | [`app/core/viability.py`](file:///c:/Users/HP/Desktop/nishin/backend/app/core/viability.py) | ✅ Verified |
| **FR-12** | Proactive execution before HF < 1.0 | Backend / Worker | [`app/scheduler.py`](file:///c:/Users/HP/Desktop/nishin/backend/app/scheduler.py) | ✅ Verified |
| **FR-13** | Autonomous bounded execution (EIP-712) | Contract / Backend | [`app/core/models.py`](file:///c:/Users/HP/Desktop/nishin/backend/app/core/models.py) | ✅ Verified |
| **FR-14** | Multi-invariant HealthGuard enforcement | Contract | [`LiquidationShieldVault.sol`](file:///c:/Users/HP/Desktop/nishin/contracts/src/LiquidationShieldVault.sol) | ✅ Verified |
| **FR-15** | Authorization & aToken opt-in allowance | Contract / UI | [`LiquidationShieldVault.sol`](file:///c:/Users/HP/Desktop/nishin/contracts/src/LiquidationShieldVault.sol) | ✅ Verified |
| **FR-16** | In-flight lock & idempotency | Backend | [`app/core/inflight.py`](file:///c:/Users/HP/Desktop/nishin/backend/app/core/inflight.py) | ✅ Verified |
| **FR-17** | Circuit breaker & failure halting | Backend | [`app/core/breaker.py`](file:///c:/Users/HP/Desktop/nishin/backend/app/core/breaker.py) | ✅ Verified |
| **FR-18** | Multi-Agent crew orchestration & risk narrative | AI Agent | [`app/agent/graph.py`](file:///c:/Users/HP/Desktop/nishin/backend/app/agent/graph.py) | ✅ Verified |
| **FR-19** | Deterministic policy gate on agent proposals | AI Agent | [`app/agent/policy.py`](file:///c:/Users/HP/Desktop/nishin/backend/app/agent/policy.py) | ✅ Verified |
| **FR-20** | Human-in-the-loop approval route (no agent submit) | AI Agent / API | [`app/api/routes_agent.py`](file:///c:/Users/HP/Desktop/nishin/backend/app/api/routes_agent.py) | ✅ Verified |
| **FR-21** | Bounded-mandate immutability (re-sign requests) | AI Agent | [`app/agent/tools.py`](file:///c:/Users/HP/Desktop/nishin/backend/app/agent/tools.py) | ✅ Verified |
| **FR-22** | NumberGuard numeric provenance anti-hallucination | AI Agent | [`app/agent/guard.py`](file:///c:/Users/HP/Desktop/nishin/backend/app/agent/guard.py) | ✅ Verified |

---

## 14. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Oracle lag vs. fast price move | Volatility-scaled `HF_target`; act proactively at `HF_trigger` > 1.0 (FR-10, FR-12) |
| Insufficient DEX liquidity | `QuoterV2` pre-check + on-chain `amountInMaximum`; decline/revert if unmet (FR-6, FR-9) |
| Network congestion / missed window | Early trigger buffer, Arbitrum low latency, private submission (NFR-1) |
| Partial fill / swap failure | Atomic tx reverts entirely; no worse position (FR-8, FR-9) |
| Flash-loan unavailable / cost spike | Viability gate rejects; cost included in sizing (FR-7, FR-11) |
| MEV/sandwich | Oracle-bounded slippage, `exactOutput`, optional private route (§9) |
| Over-repayment wastes capital | Closed-form minimum `Δd*` (FR-3) |
| LLM hallucination / rogue action | AI has no submission tool; NumberGuard verifies provenance; deterministic pipeline calculates all numbers (FR-18..22) |

---

## 15. Acceptance & Demo Criteria

**Demo scenario (Arbitrum mainnet fork):**
1. Seed leveraged Aave V3 positions (WETH/USDC) using `backend/tools/seed_all_demo_wallets.py`.
2. Simulate price movement or trigger conditions until HF approaches `HF_trigger`.
3. Keeper detects risk **before** HF < 1.0, sizes $\Delta d^*$, checks viability via QuoterV2, and submits `executeProtection`.
4. Vault atomically: flash-borrows USDC → repays debt → withdraws WETH → swaps WETH→USDC on Uniswap V3 → repays flash loan → asserts `HF_after ≥ HF_target`.
5. Web Console UI at `/console/` displays live telemetry, transaction state machine changes, and interactive Gemini agent explanations.

---

*End of PRD — FinaX Automated Liquidation Shield & Flash-Repayment Vault (PS-11, CSI ORIGIN 2026).*
