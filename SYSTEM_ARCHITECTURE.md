# System Architecture — FinaX Automated Liquidation Shield & Flash-Repayment Vault

| Field | Value |
|---|---|
| **System** | **FinaX** — Automated Liquidation Shield & Flash-Repayment Vault |
| **Problem Statement** | PS-11, CSI ORIGIN 2026 |
| **Source docs** | [`Problem_Statement_11.pdf`](Problem_Statement_11.pdf) · [`PRD.md`](PRD.md) · [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) |
| **Status** | Complete v1.0 (Full-Stack Architecture) |
| **Chain** | **Arbitrum One** (Chain ID `42161`) |
| **Smart Contracts** | Solidity `0.8.24` (EVM: `Cancun`), OpenZeppelin v5, Foundry |
| **Backend Engine** | Python 3.11+, FastAPI, `web3.py`, APScheduler, Pydantic v2 |
| **AI Co-Pilot** | Google Gemini (`gemini-3-flash`), LangGraph, SQLite |
| **Frontend Console** | Vanilla HTML5 / CSS3 / ES6 JavaScript (Mounted at `/console/`) |

---

## 1. System Context & External Interfaces (C4 Level 1 Flow Diagram)

The following flow diagram illustrates how borrowers, the off-chain keeper engine, the multi-agent AI layer, the on-chain vault, and external DeFi protocols interact on **Arbitrum One**.

```mermaid
flowchart TB
    subgraph Users["Actors & Interfaces"]
        Borrower(["👤 Borrower / Position Owner"])
        Operator(["👨‍💻 Keeper Operator"])
        Console["💻 FinaX Web Console<br/>(HTML5 / CSS3 / ES6 JS)"]
    end

    subgraph OffChain["Off-Chain System (Python / FastAPI)"]
        Backend[["⚙️ FinaX Keeper Backend<br/>(FastAPI + Web3.py)"]]
        Worker[["⏱️ Autonomous Worker<br/>(12s APScheduler loop)"]]
        AgentCrew[["🤖 Multi-Agent Co-Pilot<br/>(LangGraph + Google Gemini)"]]
        Store[("🗄️ SQLite Audit Store<br/>(finax_agent.db)")]
    end

    subgraph OnChain["On-Chain Arbitrum One (Chain ID: 42161)"]
        Vault["🛡️ LiquidationShieldVault.sol<br/>(IFlashLoanSimpleReceiver)"]
        Aave[("🏦 Aave V3 Pool<br/>(Lending + flashLoanSimple)")]
        Uni[("🦄 Uniswap V3<br/>(SwapRouter + QuoterV2)")]
        Oracle[("🔮 Chainlink & Aave Oracles<br/>(Price feeds & Heartbeats)")]
    end

    Borrower -->|"1. EIP-712 Mandate & aToken Approval"| Console
    Operator -->|"2. Monitor Telemetry & Trigger Resets"| Console
    Console <-->|"3. REST API / WebSocket Telemetry"| Backend
    Backend <-->|"4. State & Audit Logging"| Store
    Worker -->|"5. Autonomous Tick Trigger"| Backend
    AgentCrew <-->|"6. Read-Only Context & Proposals"| Backend
    
    Backend -->|"7. RPC Polling (HF, Reserves, Prices)"| Aave
    Backend -->|"8. Price & Staleness Check"| Oracle
    Backend -->|"9. Exact Output Swap Quotes"| Uni
    Backend -->|"10. Broadcast executeProtection()"| Vault

    Vault -->|"11. Sourced Flash Loan (0.05%)"| Aave
    Vault -->|"12. Repay Debt Permissionlessly"| Aave
    Vault -->|"13. transferFrom & Withdraw Collateral"| Aave
    Vault -->|"14. Exact Output Swap (Collateral ➔ Debt)"| Uni
    Vault -->|"15. HealthGuard Multi-Invariant Verification"| Aave
```

---

## 2. Container & Service Architecture (C4 Level 2 Flow Diagram)

The backend cleanly separates the **Control API**, the **Autonomous Background Worker**, the **Shared Protection Service**, and the **Multi-Agent AI Runtime**:

```mermaid
flowchart TD
    subgraph ClientLayer["Client & Dashboard Layer"]
        UI["Web Console UI (/console/)"]
        Wallet["Browser Web3 Wallet (MetaMask)"]
    end

    subgraph FastAPIService["FastAPI Application (Port 8097)"]
        direction TB
        MainApp["app.main:app (FastAPI Lifespan)"]
        
        subgraph Routes["API Route Controllers"]
            RHealth["/health"]
            RPos["/positions/{borrower}"]
            RCfg["/config"]
            RMet["/metrics"]
            RAgent["/agent (chat, crew, proposals)"]
        end

        subgraph CorePipeline["Core Decision & Execution Engine"]
            Svc["ProtectionService (Shared Facade)"]
            Mon["PositionMonitor"]
            Risk["RiskModel (Dynamic Target)"]
            Size["InterventionSizer (Δd*)"]
            Sel["CollateralSelector"]
            Via["ViabilityGate (QuoterV2)"]
            Sim["Simulator (eth_call dry-run)"]
            Sub["Submitter (eth_account)"]
            Inflight["InFlightLock (Idempotency)"]
            Breaker["CircuitBreaker (3-Strike)"]
        end

        subgraph BackgroundWorker["Autonomous Background Task"]
            Sched["APScheduler (12s Block Tick Loop)"]
            WorkerLoop["Worker.tick()"]
        end

        subgraph AgentLayer["Agentic AI Subsystem (LangGraph)"]
            AgentRuntime["AgentRuntime (StateGraph)"]
            Guard["NumberGuard (Provenance Anti-Hallucination)"]
            Tools["Read-Only Tools Surface"]
            SQLiteStore[("SQLite Store: finax_agent.db")]
        end
    end

    subgraph Blockchain["Arbitrum One Blockchain"]
        RPC["Arbitrum RPC (Primary + Fallback)"]
        VaultContract["LiquidationShieldVault.sol"]
    end

    UI <--> Routes
    Wallet -.->|"EIP-712 Sign"| UI
    MainApp --> Routes
    MainApp --> BackgroundWorker
    
    Routes --> Svc
    WorkerLoop --> Svc
    Sched --> WorkerLoop

    Svc --> Inflight
    Svc --> Breaker
    Svc --> Mon
    Svc --> Risk
    Svc --> Size
    Svc --> Sel
    Svc --> Via
    Svc --> Sim
    Svc --> Sub

    RAgent --> AgentRuntime
    AgentRuntime --> Tools
    Tools --> Svc
    AgentRuntime --> Guard
    AgentRuntime --> SQLiteStore

    Mon --> RPC
    Via --> RPC
    Sim --> RPC
    Sub -->|"Signed Tx"| VaultContract
```

---

## 3. The 8-Step Decision Engine Flowchart (Off-Chain Pipeline)

Every 12-second block tick (or on manual assessment), the system evaluates positions through an 8-step deterministic decision tree:

```mermaid
flowchart TD
    Start([12-Second Tick Trigger]) --> Step1["Step 1: Ingest On-Chain Data<br/>(Aave getUserAccountData & Chainlink Feeds)"]
    
    Step1 --> CheckBreaker{"Step 2: Circuit Breaker<br/>State == CLOSED?"}
    CheckBreaker -- "OPEN / PAUSED" --> Halt["Halt Tick: Alert Operator via /metrics"]
    
    CheckBreaker -- "CLOSED (Healthy)" --> CheckInflight{"Step 3: Rescue In-Flight<br/>for Borrower?"}
    CheckInflight -- "YES (Pending Tx)" --> Wait["Wait for Receipt / Cooldown"]
    
    CheckInflight -- "NO" --> Step4["Step 4: Dynamic Risk Assessment<br/>(Compute Market Volatility σ & HF_target)"]
    
    Step4 --> CheckTrigger{"HF <= HF_trigger<br/>(e.g., HF <= 1.15)?"}
    CheckTrigger -- "NO (Safe)" --> StateHealthy["Set State: HEALTHY / WATCH"]
    
    CheckTrigger -- "YES (At Risk)" --> Step5["Step 5: Analytical Sizing Math<br/>(Calculate candidate Δd* to reach HF_target)"]
    
    Step5 --> Step6["Step 6: Collateral Asset Selection<br/>(Rank by Liquidation Threshold & DEX Depth)"]
    
    Step6 --> Step7["Step 7: Economic Viability Gate<br/>(Uniswap QuoterV2 Quote + Gas + Premium)"]
    
    Step7 --> CheckViable{"Total Cost <= MaxCostBps<br/>AND Cost < Value Protected?"}
    CheckViable -- "NO" --> StateDeclined["Set State: DECLINED<br/>Log Unviable Reason"]
    
    CheckViable -- "YES" --> Step8["Step 8: Zero-Gas Simulation<br/>(eth_call Dry-Run on Live Fork)"]
    
    Step8 --> CheckSim{"Simulation Success<br/>AND HF_after >= HF_target?"}
    CheckSim -- "HF Short" --> Bump["Bump Δd* Sizing by ε<br/>(Re-simulate up to 3 times)"]
    Bump --> Step8
    
    CheckSim -- "Revert / Fail" --> StateDeclined
    CheckSim -- "SUCCESS" --> SetLock["Acquire In-Flight Lock<br/>Set State: SUBMITTED"]
    
    SetLock --> Broadcast["Keeper Signs & Broadcasts<br/>executeProtection() Tx"]
    
    Broadcast --> AwaitReceipt{"Await On-Chain<br/>Tx Receipt"}
    AwaitReceipt -- "Success (Block Mined)" --> Restored["Set State: RESTORED<br/>Record Metrics & Clear Lock"]
    AwaitReceipt -- "Revert / Timeout" --> HandleRevert["Increment Breaker Failure Count<br/>Clear Lock & Set State: REVERTED"]
```

---

## 4. On-Chain Atomic Execution Flowchart (`executeOperation`)

All actions on-chain execute inside **Aave's `flashLoanSimple` callback within a single transaction**. If any invariant check fails, the transaction reverts completely, guaranteeing **zero capital loss**:

```mermaid
flowchart TD
    TxIn["Keeper calls: executeProtection(RiskParams, sig, repayAmount, ...)"] --> EntryChecks{"1. AccessControl & ParamVerifier<br/>• Caller == Keeper or Borrower?<br/>• EIP-712 Signature Valid?<br/>• Nonce Unused & Deadline Not Expired?"}
    
    EntryChecks -- "Invalid" --> Rev0["revert NotAuthorized / BadSignature / Expired"]
    
    EntryChecks -- "Valid" --> FlashReq["2. Request Aave Flash Loan<br/>Pool.flashLoanSimple(debtAsset, repayAmount)"]
    
    FlashReq --> Callback["3. Aave Invokes executeOperation() Callback"]
    
    Callback --> CallCheck{"4. Sender Assertions<br/>• msg.sender == Aave Pool?<br/>• initiator == address(this)?"}
    CallCheck -- "No" --> Rev1["revert CallerNotPool / BadInitiator"]
    
    CallCheck -- "Yes" --> RepayDebt["5. Repay Borrower Debt (Permissionless)<br/>Pool.repay(debtAsset, repayAmount, borrower)<br/>➔ Health Factor Immediately Increases"]
    
    RepayDebt --> WithdrawCol["6. Pull & Withdraw Collateral<br/>aToken.transferFrom(borrower, vault, releaseAmount)<br/>Pool.withdraw(collateralAsset, releaseAmount, vault)<br/>➔ finalizeTransfer Passes"]
    
    WithdrawCol --> SwapDex["7. Uniswap V3 exactOutputSingle<br/>Swap collateralAsset ➔ debtAsset<br/>Enforce amountInMaximum <= Oracle Cap"]
    
    SwapDex --> SwapCheck{"8. Swap Succeeded within<br/>Slippage & Price Bounds?"}
    SwapCheck -- "No (Slippage / Liquidity)" --> Rev2["revert CostExceeded / Uniswap Revert"]
    
    SwapCheck -- "Yes" --> ReturnRes["9. Return Residual Collateral to Borrower<br/>IERC20(collateral).safeTransfer(borrower, leftover)"]
    
    ReturnRes --> RepayFlash["10. Approve & Repay Flash Loan<br/>IERC20(debtAsset).approve(Pool, repayAmount + 0.05% premium)"]
    
    RepayFlash --> HealthGuard{"11. HealthGuard Multi-Invariant Verification<br/>① HF_after >= HF_target?<br/>② debt_after < debt_before?<br/>③ leverage_after <= leverage_before?<br/>④ total_cost_bps <= maxCostBps?"}
    
    HealthGuard -- "Any Invariant Fails" --> Rev3["revert HealthBelowTarget / DebtNotReduced / LeverageIncreased"]
    
    HealthGuard -- "All Invariants Pass" --> Settle["12. Emit ProtectionExecuted Event<br/>Transaction Committed (Position Restored)"]
```

---

## 5. LangGraph Multi-Agent Co-Pilot State Flowchart

The AI Agent subsystem provides natural language explanations and parameter tuning recommendations. It operates on a **strict unidirectional state graph** with **zero execution privileges**:

```mermaid
flowchart TD
    StartCrew([Trigger: /agent/crew/run OR Chat]) --> ReadFacts["Node 1: Monitor (Pure Python)<br/>Reads on-chain account data, oracle prices, and debt state"]
    
    ReadFacts --> Analyst["Node 2: Risk Analyst (LLM - Gemini)<br/>Synthesizes volatility, LTV, and liquidation proximity into narrative"]
    
    Analyst --> Strategist["Node 3: Strategy Selector (LLM - Gemini)<br/>Selects high-level strategy strictly from constrained enum"]
    
    Strategist --> StrategyChoice{"Strategy<br/>Choice?"}
    
    StrategyChoice -- "PROTECT_NOW" --> PolicyGate["Node 4: Policy Gate (Pure Python)<br/>Validates against operator ceilings, breaker state & cooldowns"]
    
    StrategyChoice -- "RETUNE_MANDATE" --> Tuner["Node 5: Mandate Tuner (LLM - Gemini)<br/>Identifies buffer bottlenecks & suggests new RiskParams"]
    
    StrategyChoice -- "MONITOR / DECLINE" --> Auditor
    
    PolicyGate -- "Allowed" --> Propose["Node 6: Propose Action (Pure Python)<br/>Writes formal proposal row to SQLite for human review"]
    PolicyGate -- "Blocked" --> Auditor
    
    Tuner --> ProposeTuning["Formats Re-Sign Payload<br/>(For borrower browser signature)"]
    ProposeTuning --> Auditor
    Propose --> Auditor
    
    Auditor["Node 7: Auditor (LLM - Gemini)<br/>Reviews execution trail and outputs executive summary"]
    
    Auditor --> NumberGuard{"NumberGuard Validation<br/>(Pure Python Regex + Provenance)<br/>All numbers match on-chain facts?"}
    
    NumberGuard -- "Unverified Figures Found" --> Degrade["Mark Numbers as Degraded / Unverified"]
    NumberGuard -- "Provenance Verified" --> Finalize["Store Run in SQLite Ledger<br/>Emit Response to Dashboard UI"]
    Degrade --> Finalize
```

---

## 6. End-to-End Sequence Diagram (Full Lifecycle & Reverts)

```mermaid
sequenceDiagram
    autonumber
    actor Borrower as 👤 Borrower
    participant UI as 💻 Web Console
    participant Keeper as ⚙️ Keeper Backend
    participant Vault as 🛡️ Vault Contract
    participant Aave as 🏦 Aave V3 Pool
    participant Uni as 🦄 Uniswap V3
    participant Oracle as 🔮 Chainlink Oracle

    Note over Borrower,UI: Setup & Authorization Phase
    Borrower->>Vault: aToken.approve(Vault, amount)
    Borrower->>UI: Sign EIP-712 RiskParams in Wallet
    UI->>Keeper: Store signed mandate & telemetry preferences

    Note over Keeper,Aave: Continuous 12s Monitoring Loop
    loop Every 12 Seconds
        Keeper->>Aave: getUserAccountData(borrower)
        Keeper->>Oracle: latestRoundData() (Prices & Freshness)
    end

    Note over Keeper,Vault: Market Movement & Trigger Phase
    Oracle-->>Keeper: Collateral Price Drops ➔ HF falls to 1.12 (<= HF_trigger)
    Keeper->>Keeper: 1. Size candidate Δd* to reach HF_target (1.30)
    Keeper->>Uni: 2. QuoterV2.quoteExactOutputSingle (Price impact)
    Keeper->>Keeper: 3. Viability Check (Cost < Value Protected)
    Keeper->>Keeper: 4. Simulate via eth_call (0-Gas Dry Run)

    alt Simulation Fails / Cost Exceeded
        Keeper->>UI: Update State to DECLINED (No gas spent)
    else Simulation Passes
        Keeper->>Vault: executeProtection(RiskParams, sig, repayAmount, ...)
        
        Note over Vault,Aave: Atomic On-Chain Execution
        Vault->>Vault: Verify EIP-712 Signature & Bounds
        Vault->>Aave: flashLoanSimple(USDC, repayAmount)
        Aave->>Vault: executeOperation(USDC, repayAmount, premium)
        
        Vault->>Aave: Pool.repay(USDC, repayAmount, borrower)
        Vault->>Aave: aToken.transferFrom & Pool.withdraw(WETH)
        Vault->>Uni: exactOutputSingle(WETH ➔ USDC, maxIn)
        
        alt Uniswap Swap Slippage OK & Invariants Met
            Uni-->>Vault: Return exact USDC output + refund extra WETH
            Vault->>Borrower: safeTransfer(leftover WETH)
            Vault->>Aave: Approve & Repay Flash Loan + 0.05%
            Vault->>Aave: Assert HF_after >= 1.30
            Vault-->>Keeper: Emit ProtectionExecuted Event (Tx Confirmed)
            Keeper->>UI: Push RESTORED state & update position gauge
        else Slippage Exceeded OR HealthGuard Fails
            Vault-->>Vault: Revert entire transaction (Atomic Revert)
            Vault-->>Keeper: Tx Reverted (Position remains intact on Aave)
            Keeper->>UI: Push REVERTED state & increment circuit breaker
        end
    end
```

---

## 7. Position Lifecycle State Machine

The backend enforces an explicit finite-state machine with concurrency locks:

```mermaid
stateDiagram-v2
    [*] --> HEALTHY: Borrower grants aToken approval & signs RiskParams
    
    HEALTHY --> WATCH: HF <= HF_trigger + buffer (Approaching risk)
    WATCH --> HEALTHY: Collateral price rebounds
    
    WATCH --> ASSESSING: HF <= HF_trigger (Intervention triggered)
    
    ASSESSING --> DECLINED: Unviable / high slippage / no liquidity
    DECLINED --> WATCH: Market conditions change
    
    ASSESSING --> READY: Viability gate & eth_call simulation succeed
    
    READY --> SUBMITTED: Broadcast tx (In-Flight Lock acquired)
    
    SUBMITTED --> RESTORED: Receipt confirmed, HF_after >= HF_target
    SUBMITTED --> REVERTED: Tx reverted on-chain (Atomic revert)
    
    RESTORED --> HEALTHY: Clear In-Flight Lock; resume monitoring
    REVERTED --> WATCH: Clear In-Flight Lock; update circuit breaker
    
    HEALTHY --> [*]: Borrower revokes aToken approval
```

---

## 8. Frontend Web Console & User Interaction Flowchart

```mermaid
flowchart LR
    subgraph Browser["User Browser (Client)"]
        UserAction(["User Enters Dashboard"]) --> ConnectWallet["Connect Web3 Wallet (MetaMask)"]
        ConnectWallet --> GrantApproval["1. One-Time aToken.approve()"]
        GrantApproval --> SignParams["2. Sign EIP-712 RiskParams"]
        SignParams --> ViewGauges["3. Live Telemetry Gauges<br/>(HF, LTV, Collateral, Debt)"]
        ViewGauges --> AskAI["4. Interactive Gemini Chat<br/>('Why is my HF dropping?')"]
        ViewGauges --> TriggerSim["5. On-Demand Simulator<br/>(Test custom price shocks)"]
    end

    subgraph Server["FastAPI Backend (/console/)"]
        APIEndpoints["REST Endpoints<br/>• GET /positions/{borrower}<br/>• GET /metrics<br/>• POST /agent/chat"]
    end

    ViewGauges <-->|"Poll telemetry every 3s"| APIEndpoints
    AskAI <-->|"Multi-turn conversational loop"| APIEndpoints
    TriggerSim <-->|"Dry-run calculation & QuoterV2"| APIEndpoints
```

---

## 9. Mathematical Formulation & Invariant Summary

### 1. Minimal Sizing Formula ($\Delta d^*$)
$$\Delta d^* = \frac{\text{HF}_{\text{target}} \cdot D - C}{\text{HF}_{\text{target}} - \text{LT}_c \cdot (1 + f)}$$

* $D$: Total Debt in base currency ($\sum \text{debt}_j \times \text{price}_j$)
* $C$: Risk-weighted Collateral ($\sum \text{collateral}_i \times \text{price}_i \times \text{LT}_i$)
* $\text{LT}_c$: Liquidation threshold of the chosen collateral asset
* $f$: Fee bundle ($\text{Flash Premium (0.05\%)} + \text{DEX Fee} + \text{Slippage Buffer}$)

### 2. On-Chain HealthGuard Multi-Invariants
Every rescue must satisfy all 4 conditions before transaction completion:
1. $\text{HF}_{\text{after}} \ge \text{HF}_{\text{target}}$
2. $\text{Debt}_{\text{after}} < \text{Debt}_{\text{before}}$
3. $\text{Leverage}_{\text{after}} \le \text{Leverage}_{\text{before}}$
4. $\text{Cost}_{\text{actual}} \le \text{MaxCostBps}$

---

*End of System Architecture — FinaX Automated Liquidation Shield & Flash-Repayment Vault (PS-11).*
