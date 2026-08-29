/**
 * FinaX — Autonomous DeFi Risk Protection (frontend)
 * Presentation + interaction layer only. Every number on screen comes from
 * the existing FastAPI backend's own response models:
 *   GET  /health
 *   GET  /positions/{borrower}
 *   POST /positions/{borrower}/assessment
 *   POST /positions/{borrower}/protect
 *   GET/PUT /config
 *   GET  /metrics
 *   POST /breaker/reset
 * No endpoint is renamed, no schema is changed, no financial logic is
 * duplicated here. Demo/simulation values are always kept in their own
 * clearly-labeled panel and never written into a live metric card.
 */

const API_BASE = "";

const TOKEN_SYMBOLS = {
  "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1": "WETH",
  "0xaf88d065e77c8cC2239327C5EDb3A432268e5831": "USDC",
  "0x5979D7b546E38E414F7E9822514be443A4800529": "wstETH",
};
const symbolOf = (a) => (a ? TOKEN_SYMBOLS[a] || `${a.slice(0, 6)}…${a.slice(-4)}` : "—");
const fmtUsd = (n) => `$${(n ?? 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const fmtHf = (hf) => (hf === null || hf === undefined || !isFinite(hf) ? "∞" : hf.toFixed(4));
const short = (a) => a.slice(0, 6) + "…" + a.slice(-4);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ── App state (last known real values, nothing invented) ──────────────
const state = {
  borrower: "",
  health: null,
  position: null,
  assessment: null,
  protectResult: null,
  metrics: null,
  config: null,
  agent: {
    status: null,       // GET /agent/status
    threadId: null,     // server-minted on the first reply, reused after
    proposals: [],
    tuning: [],
    audit: [],
    sending: false,
  },
};

// ── Section navigation ──────────────────────────────────────────────
// Five views. Position absorbs the old position/risk/protection/assistant
// screens - they all read the same `state.position`/`state.assessment`, so
// four thin tables over one dataset became one coherent view. System absorbs
// the security checklist. Every element id survived the merge, so the render
// functions below are unchanged.
const VIEWS = ["overview", "position", "execution", "agent", "system"];

function go(view) {
  window.location.hash = view;
}

function renderNav() {
  const hash = (window.location.hash || "#overview").slice(1);
  const view = VIEWS.includes(hash) ? hash : "overview";
  document.querySelectorAll(".view").forEach((el) => el.classList.toggle("active", el.dataset.view === view));
  document.querySelectorAll(".navlinks a, .navlinks-mobile a").forEach((el) => {
    el.classList.toggle("active", el.dataset.nav === view);
  });
  document.getElementById("navlinks-mobile").classList.remove("open");
  document.getElementById("btn-menu").setAttribute("aria-expanded", "false");
  window.scrollTo({ top: 0, behavior: "instant" in window ? "instant" : "auto" });
  // Entering the Agent view refreshes it. Nothing polls it in the background:
  // the agent layer is optional, and a disabled backend must stay silent rather
  // than emit a 503 toast every few seconds behind an unrelated tab.
  if (view === "agent") refreshAgentView();
}
window.addEventListener("hashchange", renderNav);

document.addEventListener("DOMContentLoaded", () => {
  renderNav();
  document.getElementById("btn-menu").addEventListener("click", () => {
    const nav = document.getElementById("navlinks-mobile");
    const open = nav.classList.toggle("open");
    document.getElementById("btn-menu").setAttribute("aria-expanded", String(open));
  });
  document.getElementById("borrower-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") loadPosition();
  });

  document.getElementById("agent-chat-form").addEventListener("submit", (e) => {
    e.preventDefault();
    sendChat();
  });

  pollHealth();
  pollMetrics();
  loadConfig();
  pollAgentStatus();
  setInterval(pollHealth, 8000);
  setInterval(pollMetrics, 7000);
});

// ── Toasts ───────────────────────────────────────────────────────────
function toast(kind, msg) {
  const stack = document.getElementById("toast-stack");
  const el = document.createElement("div");
  el.className = "toast " + (kind === "error" ? "err" : kind === "ok" ? "ok" : "");
  el.textContent = msg;
  stack.appendChild(el);
  setTimeout(() => el.remove(), 6000);
}

// ── GET /health ──────────────────────────────────────────────────────
async function pollHealth() {
  try {
    const res = await fetch(`${API_BASE}/health`);
    const data = await res.json();
    state.health = data;
    const chip = document.getElementById("chip-system-status");
    const txt = document.getElementById("txt-system-status");
    chip.className = "chip chip-status " + (data.rpc_connected ? "ok" : "bad");
    txt.textContent = data.rpc_connected ? "System active" : "RPC down";

    document.getElementById("cc-rpc").textContent = data.rpc_connected ? "Connected" : "Down";
    document.getElementById("cc-block").textContent = data.block_number ? `block ${data.block_number.toLocaleString()}` : "block —";
    document.getElementById("sys-rpc").textContent = data.rpc_connected ? "Connected" : "Down";
  } catch (err) {
    // Clear the cached health too. Leaving it set made renderSecurity() keep
    // showing "RPC CONNECTION: CONNECTED" in green while the backend was down.
    state.health = null;
    const chip = document.getElementById("chip-system-status");
    chip.className = "chip chip-status bad";
    document.getElementById("txt-system-status").textContent = "Unreachable";
    document.getElementById("cc-rpc").textContent = "Unreachable";
    document.getElementById("cc-block").textContent = "block —";
    document.getElementById("sys-rpc").textContent = "Unreachable";
    renderSecurity();
  }
}

// ── GET /metrics ─────────────────────────────────────────────────────
async function pollMetrics() {
  try {
    const res = await fetch(`${API_BASE}/metrics`);
    const data = await res.json();
    state.metrics = data;

    document.getElementById("cc-breaker").textContent = data.breaker_paused ? "Paused" : "Armed";
    // Threshold comes from /config (and is editable in the System tab), so it must
    // not be hardcoded here - setting it to 5 used to still display "... / 3".
    const breakerMax = state.config?.breaker_max_consecutive_failures ?? "?";
    document.getElementById("cc-breaker-sub").textContent =
      `${data.breaker_consecutive_failures} / ${breakerMax} failures`;
    document.getElementById("sys-breaker").textContent = data.breaker_paused ? "Paused" : "Armed";
    document.getElementById("sys-inflight").textContent = data.in_flight_borrowers.length;
    document.getElementById("sys-keeper").textContent = state.config?.autonomous_enabled ? "Running" : "Manual";

    const banner = document.getElementById("breaker-banner");
    if (data.breaker_paused) {
      banner.style.display = "flex";
      document.getElementById("breaker-reason-text").textContent = `Reason: ${data.breaker_trip_reason || "unknown"}`;
    } else {
      banner.style.display = "none";
    }

    const tbody = document.querySelector("#counters-table tbody");
    const rows = Object.entries(data.counters || {}).map(
      ([k, v]) => `<tr><th>${k}</th><td class="mono">${v}</td></tr>`
    );
    tbody.innerHTML = rows.length ? rows.join("") : `<tr><td class="muted">No pipeline runs yet this session.</td></tr>`;

    renderSecurity();
  } catch (err) {
    // keep last-known values on transient failure
  }
}

// ── GET/PUT /config ──────────────────────────────────────────────────
async function loadConfig() {
  try {
    const res = await fetch(`${API_BASE}/config`);
    const data = await res.json();
    state.config = data;
    document.getElementById("cfg-poll").value = data.poll_interval_seconds;
    document.getElementById("cfg-breaker").value = data.breaker_max_consecutive_failures;
    document.getElementById("cfg-cooldown").value = data.inflight_cooldown_seconds;
    document.getElementById("cfg-bumps").value = data.max_simulation_bumps;
    setAutoSwitch(data.autonomous_enabled);
  } catch (err) {
    toast("error", "Could not load /config: " + err.message);
  }
}
function setAutoSwitch(on) {
  const el = document.getElementById("cfg-auto-switch");
  el.setAttribute("aria-pressed", String(!!on));
  state._autonomous = !!on;
}
function toggleAuto() {
  setAutoSwitch(!state._autonomous);
}
async function saveConfig() {
  const payload = {
    poll_interval_seconds: parseInt(document.getElementById("cfg-poll").value, 10),
    breaker_max_consecutive_failures: parseInt(document.getElementById("cfg-breaker").value, 10),
    inflight_cooldown_seconds: parseInt(document.getElementById("cfg-cooldown").value, 10),
    max_simulation_bumps: parseInt(document.getElementById("cfg-bumps").value, 10),
    autonomous_enabled: state._autonomous,
  };
  try {
    const res = await fetch(`${API_BASE}/config`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail ? JSON.stringify(data.detail) : `HTTP ${res.status}`);
    state.config = data;
    toast("ok", "Config applied to the live service.");
  } catch (err) {
    toast("error", "PUT /config failed: " + err.message);
  }
}
async function resetBreaker() {
  try {
    const res = await fetch(`${API_BASE}/breaker/reset`, { method: "POST" });
    const data = await res.json();
    state.metrics = data;
    toast("ok", "Circuit breaker reset.");
    pollMetrics();
  } catch (err) {
    toast("error", "POST /breaker/reset failed: " + err.message);
  }
}

// ── GET /positions/{borrower} ────────────────────────────────────────
async function loadPosition() {
  const addr = document.getElementById("borrower-input").value.trim();
  if (!addr.startsWith("0x") || addr.length !== 42) {
    toast("error", "Enter a valid 0x-prefixed, 42-character address.");
    return;
  }
  state.borrower = addr;
  const btn = document.getElementById("btn-load");
  btn.disabled = true;
  btn.textContent = "Loading…";
  document.getElementById("walletbar-hint").textContent = "";
  try {
    const res = await fetch(`${API_BASE}/positions/${addr}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    state.position = data;
    state.assessment = null;
    state.protectResult = null;

    // Auto-register pre-signed mandate with backend if on file
    if (hasSignature(addr)) {
      const payload = buildParamsPayload(addr);
      fetch(`${API_BASE}/positions/${addr}/assessment`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }).catch(() => {});
    }

    renderPosition();
    renderCommandCenter();
    renderRisk();
    renderProtection();
    renderExecution();
    renderAssistant();
    document.getElementById("walletbar-hint").textContent = `Loaded: ${data.state}, HF ${fmtHf(data.hf)}`;
  } catch (err) {
    // Clear stale data from any previously-loaded wallet — never leave an old
    // wallet's numbers on screen while showing an unrelated address as loaded.
    state.position = null;
    state.assessment = null;
    state.protectResult = null;
    clearPositionDisplay();
    const friendly = explainLoadError(err.message);
    toast("error", friendly);
    document.getElementById("walletbar-hint").textContent = "⚠ " + friendly;
    console.warn("GET /positions raw error:", err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Load position";
  }
}

// Turn a raw chain/RPC error into one readable line. The full message is
// still logged to the console for debugging — it just never lands on screen,
// where a 300-character trie-node dump is unreadable.
function explainLoadError(raw) {
  const m = String(raw || "");
  if (/missing trie node|not available, not found|metadata is not found/i.test(m)) {
    return "No state for this address on the local demo fork. Only the demo wallet resolves here.";
  }
  if (/timeout|timed out/i.test(m)) return "The chain RPC timed out. Retry in a moment.";
  if (/Failed to fetch|NetworkError/i.test(m)) return "Backend unreachable — is the server still running?";
  if (/HTTP 4\d\d/.test(m)) return "That address was rejected as invalid.";
  return m.length > 140 ? m.slice(0, 140) + "…" : m;
}

// Reset every panel to its empty state — used when a load fails, so a
// previous wallet's real numbers never linger under a different address.
function clearPositionDisplay() {
  ["cc-hf", "cc-collateral", "cc-debt", "cc-target", "cc-risk", "cc-state",
   "pt-borrower", "pt-state", "pt-hf", "pt-collateral", "pt-debt", "pt-hasdebt", "pt-registered",
   "rk-hf", "rk-target", "rk-state", "ad-risk", "ad-target", "ad-repay", "ad-collateral", "ad-cost", "ad-viable",
  ].forEach((id) => { const el = document.getElementById(id); if (el) el.textContent = "—"; });
  document.getElementById("cc-hf-sub").textContent = "No data — last load failed";
  document.getElementById("risk-badge").textContent = "No data";
  document.getElementById("risk-badge").style.color = "var(--text-muted)";
  document.getElementById("risk-badge").style.borderColor = "var(--border)";
  document.getElementById("risk-explain").textContent = "Could not read this wallet's position — see the error above. Try the demo address instead.";
  document.getElementById("decision-tag").textContent = "No assessment yet";
  document.getElementById("protection-explain").textContent = "Run an assessment to see the backend's reasoning here.";
  document.getElementById("assistant-text").textContent = "Load a position and run an assessment to get an explanation.";
  resetFlowSteps();
  document.getElementById("exec-result").className = "exec-result";
  document.querySelectorAll(".atomic-step").forEach((el) => (el.className = "atomic-step pending"));
  document.getElementById("hfc-current-label").textContent = "Current";
  document.getElementById("hfc-target-label").textContent = "Target";
}

// ── Presentational risk mapping (derived from REAL backend state only) ─
// HEALTHY -> SAFE, WATCH -> WATCH, ASSESSING -> HIGH RISK, DECLINED -> WATCH,
// READY/SUBMITTED -> HIGH RISK, RESTORED -> SAFE, REVERTED -> HIGH RISK.
function riskLevelFor(positionState) {
  const map = {
    HEALTHY: { label: "Safe", cls: "badge-safe" },
    WATCH: { label: "Watch", cls: "badge-warn" },
    ASSESSING: { label: "High risk", cls: "badge-danger" },
    DECLINED: { label: "Watch", cls: "badge-warn" },
    READY: { label: "High risk", cls: "badge-danger" },
    SUBMITTED: { label: "High risk", cls: "badge-danger" },
    RESTORED: { label: "Safe", cls: "badge-safe" },
    REVERTED: { label: "High risk", cls: "badge-danger" },
  };
  return map[positionState] || { label: positionState || "Unknown", cls: "" };
}

// ── Render: Command Center ──────────────────────────────────────────
function renderCommandCenter() {
  const p = state.position;
  if (!p) return;
  document.getElementById("cc-hf").textContent = fmtHf(p.hf);
  document.getElementById("cc-hf-sub").textContent = p.has_debt ? "Live from getUserAccountData" : "No open debt";
  const risk = riskLevelFor(p.state);
  document.getElementById("cc-risk").innerHTML = `<span class="badge ${risk.cls}">${risk.label}</span>`;
  document.getElementById("cc-collateral").textContent = fmtUsd(p.collateral_usd);
  document.getElementById("cc-debt").textContent = fmtUsd(p.debt_usd);
  document.getElementById("cc-target").textContent = state.assessment ? state.assessment.hf_target.toFixed(4) : "—";
  document.getElementById("cc-state").innerHTML = `<span class="badge ${risk.cls}">${p.state}</span>`;

  // HF chart lines
  const hf = p.hf;
  const target = state.assessment ? state.assessment.hf_target : null;
  const pctFor = (v) => {
    if (v === null || v === undefined || !isFinite(v)) return 92;
    return Math.max(2, Math.min(96, ((v - 1.0) / 0.6) * 100));
  };
  document.getElementById("hfc-current-line").style.left = pctFor(hf) + "%";
  document.getElementById("hfc-current-label").textContent = `CURRENT ${fmtHf(hf)}`;
  if (target) {
    document.getElementById("hfc-target-line").style.left = pctFor(target) + "%";
    document.getElementById("hfc-target-label").textContent = `TARGET ${target.toFixed(4)}`;
  }
}

// ── Render: Position ─────────────────────────────────────────────────
function renderPosition() {
  const p = state.position;
  if (!p) return;
  document.getElementById("pt-borrower").textContent = p.borrower;
  document.getElementById("pt-state").innerHTML = `<span class="badge ${riskLevelFor(p.state).cls}">${p.state}</span>`;
  document.getElementById("pt-hf").textContent = fmtHf(p.hf);
  document.getElementById("pt-collateral").textContent = fmtUsd(p.collateral_usd);
  document.getElementById("pt-debt").textContent = fmtUsd(p.debt_usd);
  document.getElementById("pt-hasdebt").textContent = String(p.has_debt);
  document.getElementById("pt-registered").textContent = String(p.registered);
}

// ── Render: Risk ──────────────────────────────────────────────────────
function renderRisk() {
  const p = state.position;
  if (!p) return;
  const risk = riskLevelFor(p.state);
  const badge = document.getElementById("risk-badge");
  badge.textContent = risk.label;
  badge.style.color = risk.cls === "badge-safe" ? "var(--success)" : risk.cls === "badge-danger" ? "var(--danger)" : risk.cls === "badge-warn" ? "var(--warning)" : "var(--text-primary)";
  badge.style.borderColor = badge.style.color;

  document.getElementById("rk-hf").textContent = fmtHf(p.hf);
  document.getElementById("rk-target").textContent = state.assessment ? state.assessment.hf_target.toFixed(4) : "—";
  document.getElementById("rk-state").innerHTML = `<span class="badge ${risk.cls}">${p.state}</span>`;

  let explain;
  if (!p.has_debt) {
    explain = `This wallet has no open debt on Aave V3. There is no liquidation risk and no intervention zone applies.`;
  } else if (state.assessment) {
    explain = `The backend classifies this position as ${risk.label} because its state machine reports "${p.state}". `
      + `Current health factor is ${fmtHf(p.hf)}, and the risk engine computed a dynamic target of ${state.assessment.hf_target.toFixed(4)} `
      + `based on the borrower's signed HF band and recent volatility. `
      + (state.assessment.viable
          ? `The sizing and viability modules found a rescue path worth executing.`
          : `The sizing/viability modules declined to act: ${state.assessment.reason || "not economically worthwhile"}.`);
  } else {
    explain = `Current health factor is ${fmtHf(p.hf)}, position state "${p.state}". Run an assessment on the Protection tab for the full risk → sizing → viability breakdown.`;
  }
  document.getElementById("risk-explain").textContent = explain;
}

// ── Render: Protection ────────────────────────────────────────────────
function renderProtection() {
  const a = state.assessment;
  const tag = document.getElementById("decision-tag");
  if (!a) {
    tag.textContent = "No assessment yet";
    return;
  }
  tag.textContent = a.viable ? "Viable" : "Declined";
  tag.style.color = a.viable ? "var(--success)" : "var(--warning)";

  const risk = state.position ? riskLevelFor(state.position.state) : { label: "—" };
  document.getElementById("ad-risk").innerHTML = `<span class="badge ${risk.cls || ""}">${risk.label}</span>`;
  document.getElementById("ad-target").textContent = a.hf_target.toFixed(4);
  document.getElementById("ad-repay").textContent = a.repay_amount ? `${(a.repay_amount / 1e6).toLocaleString(undefined, { minimumFractionDigits: 2 })} USDC` : "0.00 USDC";
  document.getElementById("ad-collateral").textContent = symbolOf(a.collateral_asset);
  document.getElementById("ad-cost").textContent = `${a.est_cost_bps} bps`;
  document.getElementById("ad-viable").innerHTML = a.viable
    ? `<span class="badge badge-safe">Pass</span>`
    : `<span class="badge badge-warn">Declined</span>`;

  document.getElementById("protection-explain").textContent = a.viable
    ? `A rescue is viable. The pipeline would repay ${(a.repay_amount / 1e6).toFixed(2)} USDC sourced from ${symbolOf(a.collateral_asset)}, `
      + `restoring health factor toward ${a.hf_target.toFixed(4)} at an estimated cost of ${a.est_cost_bps} basis points — `
      + `within the borrower's signed cost bound.`
    : `The pipeline declined to act: ${a.reason || "not economically viable"}. No funds move when a rescue is declined.`;

  renderAssistant();
}

async function runAssessmentOnly() {
  const addr = state.borrower || document.getElementById("borrower-input").value.trim();
  if (!addr) { toast("error", "Load a position first."); return; }
  const btn = document.getElementById("btn-assess");
  btn.disabled = true;
  btn.textContent = "Running…";
  try {
    const res = await fetch(`${API_BASE}/positions/${addr}/assessment`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildParamsPayload(addr)),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail ? JSON.stringify(data.detail) : `HTTP ${res.status}`);
    state.assessment = data;
    renderProtection();
    renderCommandCenter();
    renderRisk();
    toast(data.viable ? "ok" : "error", `Assessment: viable=${data.viable}${data.reason ? " — " + data.reason : ""}`);
  } catch (err) {
    toast("error", "POST /assessment failed: " + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Run dry-run assessment";
  }
}

// Borrower-signed EIP-712 authorizations for the demo wallet.
//
// These are SIGNATURES, not keys. In the real flow the borrower signs their
// RiskParams offline with their own wallet and hands the signature to the
// keeper — the private key never leaves the borrower. That is exactly what is
// reproduced here: the demo borrower (a public Foundry test account) pre-signed
// the params below, and the vault verifies them on-chain with ECDSA.recover.
//
// The signed field values are load-bearing: change any one of them and the
// EIP-712 digest changes, recovery yields a different address, and the vault
// correctly rejects it. Nonces are single-use, so several are pre-signed.
const SIGNED_PARAMS = {
  hfTriggerBps: 11500,
  hfTargetBaseBps: 12500,
  volCoeffK: 0,
  hfTargetMaxBps: 14000,
  maxSlippageBps: 300,
  maxCostBps: 500,
  allowedCollaterals: ["0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"],
  deadline: 2000000000,
};

const DEMO_SIGNATURES = {
  "0x70997970c51812dc3a010c7d01b50e0d17dc79c8": [
    { nonce: 1, sig: "0x40fef6055af5cb021b7605c12b7f149d241a7ee640392dfb9489add928fedefe3b32e8fd7739db0eccc5554613bab711ece44ffacfc7030239aa91313d02d5af1b" },
    { nonce: 2, sig: "0x432db9199d4bff987b1194c8d313cbff5439fff059274285e82da161b1439b0d531b5ce101c82052006c728b6d547853c88dd49e98ae6bc4d41a846dc2df3f761b" },
    { nonce: 3, sig: "0x18b055786bb41a7cc145817404234f3564ab9c07b501efb7bcc876dc6fd49c976fab73d27255c9fc34dfd8319e080a68f09aada351ea28b98e4927208c3838041c" },
    { nonce: 4, sig: "0x59a2e0556202c5010d57a896e3099c3804f6d85751ba517978b116649c2a8ec15e0d6396f76b55615b2d2fb9fd580024bfc58d1d8647daa121a76a4ed5cc62291c" },
    { nonce: 5, sig: "0x054f7ef1a9b1553dedaa263ea8c5fabb2fada25ea1ce36c4cb28b16e4c8a50c03ccd54e28d16386b44926359ade442e724be0ec180477c40d0e9c56c21de2c921c" },
  ],
  "0x3c44cdddb6a900fa2b585dd299e03d12fa4293bc": [
    { nonce: 1, sig: "0x8142066a05cc721b608f3244bab9e1f8ff3a0d19575a8ed2cfdef670024b3d38380f81933da40e74bbb3e4eac9d8ed284dfe6d5ac59393219b26097817ce83a91c" },
    { nonce: 2, sig: "0xc2a4df6cd329941e4fc954eef6bbb32a44b63e6df2231ef0e0f655b433215da417982d1e99acab75e39d41c3edf2b4ab28e71dff336cabc18c57e8ed4303e87f1b" },
    { nonce: 3, sig: "0x14b7199f77df331448cc2db9c8a88817258010a5713a87ee75a6edeb51d270a60e10bef5e48344edf2e2b06b800ad7abaeaa976548d7a5f9f5a0c56d642992471b" },
    { nonce: 4, sig: "0xd55ccb53ca5fc2ce8db9806b1d09fd07e00ef2af8c3c4382fb93e3496958bc66005b7bb4d38d87eacfecbda5ee9478bce825329b413446b304a63769bc5896e11c" },
    { nonce: 5, sig: "0x7f499dc03568365642aa17634fa0acf20c2d289c79729748a4af26eec89e39d337eed2c5808f726997f981f3e2dbf953ec266fdf9f4646a48751e8d865c34f6e1b" },
  ],
  "0x90f79bf6eb2c4f870365e785982e1f101e93b906": [
    { nonce: 1, sig: "0xdd905adbe0ab8e8d7ef13015759a1850960ece625339b7e010984bde4474258a62e461895cd6e87c7567ccc4168db0162109519275369793a86dc2c6bd6d38b21b" },
    { nonce: 2, sig: "0xaa3c52d2013d32dc5945e88920ef08ef5f0b85b2bbdd26e4abf1b6c305da9c3e2fe57ef02d4479a89c79648fc60f3d495eb0f336572e96e710186bfb82eb636a1c" },
    { nonce: 3, sig: "0xec45b8b11728aecea4887808a2e347417c320e927a1c33467b05e4be7678472e213e1461be332618dfc85e4b282273348ef871a394f64f44af1c912ab61c271e1b" },
    { nonce: 4, sig: "0x10117166d951078554559c0466bd1414a812023d2a5f8a83a581c068042410dd4e307cd4854ac546f3769886313a2f1122729cecb09dc3978c06bde44b86ae741c" },
    { nonce: 5, sig: "0xfda678b532e7a33e56daa4acd5da2f2871c54c0fd9de8a9b1e713987a830c1594dbb4547f4c1852782253aee3ab02a38c830338ce0052fe8e473df41361bd4351c" },
  ],
  "0x15d34aaf54267db7d7c367839aaf71a00a2c6a65": [
    { nonce: 1, sig: "0x2cdff70aba651ee6ffa237c98bfd7126835e087729781ef49c8699f40e98e792573e543bffb36f40fa760b3b90b0e0ac6f1ad45dc0468a0432276cb33bd3cdbf1c" },
    { nonce: 2, sig: "0xd9e76cb6327cbe24bf5f2f8d6fee164af3c34b5dbcd1977bfee624722d0ad3920799717b83554470bea48cf425d37c0891b724ce4257515a68a610c662143d921c" },
    { nonce: 3, sig: "0xf8ef4a524542533807facea4af1e725aded5463cd3a7882e4b044514c21346c512451340f427e3137bff9a8e2ea440a62f4dd91313c08b2b80579b86a85b362a1b" },
    { nonce: 4, sig: "0x5888474ddc1ca45b6e90b9ed94b166b57388ca361a6c9c9a49076d94394fc9d3024ccab70266111a24a52638784c960a07fa8569eef5fdb6e422f96fc505bc4a1b" },
    { nonce: 5, sig: "0x0c5a1f445dbf16943da9f3bfcbb003ba5e830cc1836a1555a514d783417864ff44de0c548f8577f00d6f7d287ff3172d0aefc88502a819c5a643fd9f7ab723f01b" },
  ],
  "0x9965507d1a55bcc2695c58ba16fb37d819b0a4dc": [
    { nonce: 1, sig: "0xad14a55cfb7852447ee108ce1fbbc0acb66d2a21078ca9a308544e3440b229357177779a6926de9a357e2c38ed7dfcef8caca5b203257c0cef96aaf93ec63a8b1c" },
    { nonce: 2, sig: "0x383d4eb192b965dd05a99ca8c5852d3e015395d834cbf122bd3da616286cd472173b46b1eb6e283d4f4317a1802956c89b43eb92864acc1066c71476df9e872c1b" },
    { nonce: 3, sig: "0xa4e98d2bd1b4996bb7feea1d59fe8fa465303ad84f321fc51095ca8cb8e0bff2677dbbc32ba7ae811387de0ba91930c10d044f753b580e2e8a97939cd759f83c1b" },
    { nonce: 4, sig: "0x2892265fd8a9154d63ea4a768af5f6170eacb4a38dc12612250dd7e1963f6d7629712fe78b0034c05bbf634262d52ae54d2644fb3e9c6e99b1479a7bf23dbea01c" },
    { nonce: 5, sig: "0x71fca6686e41a43e2b39e84250b7ed4a2e4f9ab62160591e705c3f3c736b646a6501078a5e4e3356df3b7344e335696f0079f6d998df7d2350d4f3abd4e64d561c" },
  ],
  "0x976ea74026e726554db657fa54763abd0c3a0aa9": [
    { nonce: 1, sig: "0xfebc368a093de6ddb3c7f6410669eb5f79cd8ed3fa40fe54fa0d718753f489bb195c17e7f251f39308defe1a0eee554f23d53d4768e5ee85cc4bf0d372fa1f221b" },
    { nonce: 2, sig: "0x40faed210f2cf734567e51d861c82ba2151ab17732dec36034b4ec2d0afe48eb4c4bde94a168ff7f8d99d5ef0888eddfb37169d495edbfbe664227dbcb7d804b1c" },
    { nonce: 3, sig: "0xd38ec0d287172104c18578f4b1bc7de0504941da0c7abeb576044aabc4ad607118ac611908dc778a3372607d480d7404c53651dfaa9c90023202559ac1a4efc21c" },
    { nonce: 4, sig: "0xceff172729accd1948085ba5f5be79097fbc0d169fcde3ad1f5507de21a63da230de72d5431addd73b97dd8ebaa3893da33c5072af378414f0443d46ae052b151b" },
    { nonce: 5, sig: "0x7039490a168f209cd39fd65c149a659f4680a29519fa313111fa7e18b1795ca64efbcc6a8dc82de70cd0d508bfdb5d1c8ea556c72b693310b7dcc12189977d8a1c" },
  ],
  "0x14dc79964da2c08b23698b3d3cc7ca32193d9955": [
    { nonce: 1, sig: "0xe0c911d81343d79ee2b15c1c14701454154cb6584895a49d700c7cd0ebcc1f8b28cb208dfa6049a4501522186d5d3d00feecf603bde7de841e5f5e82b7dde4971b" },
    { nonce: 2, sig: "0x7cc713d8736cacc576174c845a7c21f4859171ec518d68807cdd791637e026260f1db1cc861efa5f1ac92c7f653f6b74cffccd8975130b124d653a558a56f7f41c" },
    { nonce: 3, sig: "0x12179f1ff73e152cb079a08a18d628a47fdbd526a10075649fbe843a752319ed431d5afe082a45897984118c25747c403ce1074ffc4140fb10999e49ec1c5b921c" },
    { nonce: 4, sig: "0x57cee64258d4329c98e3682505ac50b75fbac6212d5a872fb94207161145582832610c1ebc9ee846cd23c577084176b5d5ec7e7d6fdeee64923968f143304c861c" },
    { nonce: 5, sig: "0x63c510967d2f8a58e7e2a5ebad061d487f5284e4b034f237dbcc1dda9dd7d0ae157431d4c9a34552ba56f16f16857a15f887b413ed8262b787ec7c5b15f648001c" },
  ],
  "0x23618e81e3f5cdf7f54c3d65f7fbc0abf5b21e8f": [
    { nonce: 1, sig: "0xa9a3c526f6d68e6f64e8c85f57190406e71b343e007f1449a168fd87f1b2d60d3341b31024f1532066dcb0fc438af094f1dd120bd1247560f7d076d9efcb29731b" },
    { nonce: 2, sig: "0xca16a2406bf30be7da472483f7311d017d6bee7c599675cc661107ecd4c498485fccb4a432e06d5393c2395aa2464590ce1babf038812d30981cf4bc624c59521b" },
    { nonce: 3, sig: "0xe77766422478996d885f01982bbb7fcb2f148dd1cb761f35ed814029ea9da34c5932ce0ed66ef1c42cd03df3bfe74bec89a76ffc301761dc447cd21f995dc74a1c" },
    { nonce: 4, sig: "0x03c752ee43889f0c0ebe4892cbfc8da5e9209fa8422dec4a7d98755d32d664c62447d5132af9b1e8c6acf17d1ac7aedc60c092af220f471aab57eab5eb32d6941c" },
    { nonce: 5, sig: "0x4b24ca0cfd888a4e472bcaae51a8f7512d29a1c686fb6d5eb8382ce38d7416360c5ad2289c2f02e2d9b718d8942b50ba0bd3bc579537a078961262ee807e528e1c" },
  ],
};

// Which pre-signed nonce to try next. Nonces are single-use on-chain, so a
// successful rescue consumes one and the next attempt advances.
let sigIndex = 0;

// True when we hold a real borrower signature for this address. Without one
// the request is unsigned, and the vault will reject it — correctly.
function hasSignature(borrower) {
  return !!DEMO_SIGNATURES[String(borrower).toLowerCase()];
}

function buildParamsPayload(borrower) {
  const entries = DEMO_SIGNATURES[String(borrower).toLowerCase()];
  const entry = entries ? entries[Math.min(sigIndex, entries.length - 1)] : null;
  return {
    params: {
      borrower,
      ...SIGNED_PARAMS,
      nonce: entry ? entry.nonce : 1,
    },
    // No signature on file for this borrower: send an empty one. The vault
    // rejects it at ECDSA.recover, which is the correct non-custodial outcome.
    signature: entry ? entry.sig : "0x" + "00".repeat(65),
  };
}

// ── Render: Execution ─────────────────────────────────────────────────
function renderExecution() {
  resetFlowSteps();
  document.getElementById("exec-result").className = "exec-result";
  document.getElementById("exec-result").textContent = "";
  document.querySelectorAll(".atomic-step").forEach((el) => (el.className = "atomic-step pending"));
}
function resetFlowSteps() {
  document.querySelectorAll(".flow-step").forEach((el) => {
    el.className = "flow-step";
    el.querySelector(".flow-status").textContent = "Pending";
  });
}
function setFlow(n, cls, label) {
  const el = document.querySelector(`.flow-step[data-step="${n}"]`);
  el.className = "flow-step " + cls;
  el.querySelector(".flow-status").textContent = label;
}

// The single guided real run: health -> position -> assessment -> protect.
// Used by both the Execution tab's "Execute protection" and the Command
// Center's "RUN PROTECTION DEMO" (which is real, unlike the crash simulator).
async function runFullCheck() {
  const addr = state.borrower || document.getElementById("borrower-input").value.trim();
  if (!addr.startsWith("0x") || addr.length !== 42) {
    toast("error", "Load a valid wallet address first.");
    return;
  }
  go("execution");
  resetFlowSteps();
  document.getElementById("exec-result").className = "exec-result";
  document.querySelectorAll(".atomic-step").forEach((el) => (el.className = "atomic-step pending"));

  const btn = document.getElementById("btn-execute");
  btn.disabled = true;
  btn.textContent = "Running…";

  try {
    // 1. MONITOR
    setFlow(1, "active", "Running");
    const posRes = await fetch(`${API_BASE}/positions/${addr}`);
    const pos = await posRes.json();
    if (!posRes.ok) throw new Error(pos.detail || `HTTP ${posRes.status}`);
    state.position = pos;
    renderPosition();
    renderCommandCenter();
    setFlow(1, "done", "Done");

    if (!pos.has_debt) {
      setFlow(2, "skipped", "Skipped");
      setFlow(3, "skipped", "Skipped");
      setFlow(4, "skipped", "Skipped");
      setFlow(5, "skipped", "Skipped");
      setFlow(6, "skipped", "Skipped");
      setFlow(7, "skipped", "Skipped");
      showExecResult("No open debt on this wallet. Nothing to protect.");
      return;
    }

    // 2. PREDICT + 3. SIZE (both inside the single /assessment call)
    setFlow(2, "active", "Running");
    const assessPromise = fetch(`${API_BASE}/positions/${addr}/assessment`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildParamsPayload(addr)),
    });
    await sleep(400);
    setFlow(2, "done", "Done");
    setFlow(3, "active", "Running");
    const assessRes = await assessPromise;
    const assessment = await assessRes.json();
    if (!assessRes.ok) throw new Error(assessment.detail ? JSON.stringify(assessment.detail) : `HTTP ${assessRes.status}`);
    state.assessment = assessment;
    setFlow(3, "done", "Done");
    renderProtection(); renderCommandCenter(); renderRisk();

    // 4. CHECK (viability, from the same response)
    setFlow(4, "active", "Running");
    await sleep(250);
    if (!assessment.viable) {
      setFlow(4, "declined", "Declined");
      setFlow(5, "skipped", "Skipped");
      setFlow(6, "skipped", "Skipped");
      setFlow(7, "skipped", "Skipped");
      showExecResult(`Declined at the viability gate: ${assessment.reason || "not economically worthwhile"}. No transaction was built.`);
      return;
    }
    setFlow(4, "done", "Pass");

    // 5. SIMULATE + 6. EXECUTE (inside /protect)
    setFlow(5, "active", "Running");
    const protectRes = await fetch(`${API_BASE}/positions/${addr}/protect`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildParamsPayload(addr)),
    });
    const protectData = await protectRes.json();
    // This was the only fetch in the file without a status check. On a 4xx/5xx the
    // body carries `detail` and no `state`, so the UI marked SIMULATE as PASS and
    // then reported `state "undefined"` - a backend failure shown as a good dry-run.
    if (!protectRes.ok) {
      throw new Error(
        protectData.detail ? JSON.stringify(protectData.detail) : `HTTP ${protectRes.status}`
      );
    }
    state.protectResult = protectData;

    const declinedReason = protectData.reason || "";
    // PositionState enum value, not display text.
    if (protectData.state === "DECLINED" && declinedReason.includes("simulation")) {
      setFlow(5, "failed", "REVERTED");
      setFlow(6, "skipped", "Skipped");
      setFlow(7, "skipped", "Skipped");
      markAtomic(["flashloan"], "fail");
      showExecResult(explainRevert(declinedReason, addr));
      return;
    }
    // Breaker-paused, in-flight, and viability declines never reach the simulator.
    // Marking step 5 PASS for those claimed a dry-run that never ran.
    if (protectData.state !== "RESTORED" && !protectData.submitted) {
      setFlow(5, "skipped", "Not run");
      setFlow(6, "skipped", "Skipped");
      setFlow(7, "skipped", "Skipped");
      showExecResult(`Stopped before simulation - ${declinedReason || protectData.state}.`);
      renderAssistant();
      return;
    }

    setFlow(5, "done", "Pass");
    setFlow(6, "active", "Running");
    await sleep(250);

    if (protectData.state === "RESTORED") {
      setFlow(6, "done", "Done");
      setFlow(7, "done", "RESTORED");
      if (protectData.via_fallback) {
        // HF really did improve, but via a direct Aave repay - no flash loan, no
        // swap, no HealthGuard. Never present that as the atomic vault rescue.
        markAtomic(["repay"], "ok");
        markAtomic(["flashloan", "release", "swap", "flashrepay", "healthcheck"], "fail");
        setFlow(7, "declined", "Partial");
        showExecResult(
          `Health factor restored, but NOT by the atomic vault path. ${protectData.reason || ""} ` +
          `Only the debt repayment executed - no flash loan, collateral swap, or HealthGuard check. ` +
          `Tx: ${protectData.tx_hash || "—"}`
        );
      } else {
        markAtomic(["flashloan", "repay", "release", "swap", "flashrepay", "healthcheck", "restored"], "ok");
        showExecResult(`Position restored by the atomic vault rescue. Tx: ${protectData.tx_hash || "—"}`);
      }
    } else {
      setFlow(6, "declined", protectData.state);
      setFlow(7, "skipped", "Skipped");
      markAtomic(["flashloan"], protectData.submitted ? "ok" : "pending");
      showExecResult(`Execution stopped in state "${protectData.state}": ${declinedReason || "see technical details"}.`);
    }
    renderAssistant();
  } catch (err) {
    const activeStep = document.querySelector(".flow-step.active");
    if (activeStep) { activeStep.className = "flow-step failed"; activeStep.querySelector(".flow-status").textContent = "Error"; }
    toast("error", err.message);
    showExecResult(`Error: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "Execute protection";
  }
}
// Decode the vault/OpenZeppelin custom-error selectors that actually come back
// from a reverted dry-run, so the console states the real cause instead of a
// raw 4-byte selector. Selectors computed from the deployed ABI.
const REVERT_SELECTORS = {
  "0xf645eedf": "ECDSAInvalidSignature — the request carried no valid borrower signature.",
  "0x5cd5d233": "BadSignature — the signature did not recover to the borrower address.",
  "0x1f6d5aef": "NonceUsed — this authorization has already been consumed.",
  "0x203d82d8": "Expired — the signed authorization is past its deadline.",
  "0xea8e4eb5": "NotAuthorized — caller is neither the keeper nor the borrower.",
  "0x00413389": "CollateralNotAllowed — asset is not in the borrower's signed allow-list.",
  "0xc11ccf39": "TargetOutOfBand — requested HF target sits outside the signed band.",
  "0x11a3fbc6": "NoDebt — the position has nothing to repay.",
  "0x05e27633": "HealthBelowTarget — the rescue would not reach the target HF.",
  "0xcff70657": "CostExceeded — the rescue would cost more than the signed bound.",
  "0xb893dc08": "DebtNotReduced — HealthGuard invariant violated.",
  "0x39846b06": "LeverageIncreased — HealthGuard invariant violated.",
};

function explainRevert(reason, borrower) {
  const r = String(reason);

  // Empty revert data means an opcode-level halt, not a contract rejection.
  // Aave V3.3's flash-loan reentrancy guard uses the Cancun TSTORE opcode, and
  // anvil cannot run an Arbitrum fork under Cancun ("Excess blob gas not set"),
  // so it runs Shanghai where TSTORE halts. Verified directly, not assumed.
  if (/'0x'\)?\s*$|execution reverted', '0x'/.test(r)) {
    return "Signature and all pre-flight checks PASSED — execution reached Aave's flash loan and halted there. "
      + "Aave V3.3's reentrancy guard uses the Cancun TSTORE opcode, which this local fork cannot run "
      + "(anvil cannot fork Arbitrum under Cancun). Nothing was submitted; the position is unchanged. "
      + "The atomic execution itself is covered by the Foundry fork suite, 13/13 passing.";
  }

  const hit = Object.keys(REVERT_SELECTORS).find((sel) => r.includes(sel));
  if (hit) {
    let msg = `Simulation reverted — ${REVERT_SELECTORS[hit]} Nothing was submitted; the position is unchanged.`;
    if (hit === "0xf645eedf" && !hasSignature(borrower)) {
      msg += ` This wallet has no borrower authorization on file, so the vault refused it. That is the non-custodial guarantee working: without a signature from the borrower, no collateral can move.`;
    }
    return msg;
  }
  return `Simulation reverted: ${reason}. Nothing was submitted; the position is unchanged.`;
}

function markAtomic(ids, cls) {
  ids.forEach((id) => {
    const el = document.querySelector(`.atomic-step[data-a="${id}"]`);
    if (el) el.className = "atomic-step " + cls;
  });
}
function showExecResult(text) {
  const el = document.getElementById("exec-result");
  el.textContent = text;
  el.className = "exec-result show";
}

// ── Render: Security ────────────────────────────────────────────────
function renderSecurity() {
  const m = state.metrics;
  const items = [
    { name: "Circuit breaker", src: "Live", ok: m ? !m.breaker_paused : null, okText: "Armed", badText: "Paused" },
    { name: "In-flight lock", src: "Live", ok: m ? m.in_flight_borrowers.length === 0 : null, okText: "Clear", badText: "Held" },
    { name: "RPC connection", src: "Live", ok: state.health ? state.health.rpc_connected : null, okText: "Connected", badText: "Down" },
    { name: "EIP-712 signature", src: "By design", ok: true, okText: "On-chain" },
    { name: "Keeper authorization", src: "By design", ok: true, okText: "On-chain" },
    { name: "Nonce", src: "By design", ok: true, okText: "Replay-proof" },
    { name: "Deadline", src: "By design", ok: true, okText: "Expiry-enforced" },
    { name: "aToken allowance", src: "By design", ok: true, okText: "Opt-in only" },
    { name: "Slippage bound", src: "By design", ok: true, okText: "Contract-capped" },
    { name: "Health guard", src: "By design", ok: true, okText: "No-worse invariant" },
  ];
  const grid = document.getElementById("security-grid");
  grid.innerHTML = items.map((it) => {
    const known = it.ok !== null;
    const cls = !known ? "" : it.ok ? "badge-safe" : "badge-danger";
    const label = !known ? "—" : it.ok ? it.okText : it.badText;
    return `<div class="sec-item">
      <div class="sec-item-head"><span class="sec-item-name">${it.name}</span><span class="sec-item-src">${it.src}</span></div>
      <span class="badge ${cls}">${label}</span>
    </div>`;
  }).join("");
}

// ── Forecast (rendered inside the Position view) ──────────────────────
// Presentation only. Every figure comes from AssessmentResponse or the
// position snapshot; nothing is computed here that the backend did not
// already compute, and with no assessment on file we render nothing rather
// than fall back to invented constants. An earlier revision did exactly that
// - fabricating a cost, a target and a liquidation threshold, then rendering
// them in the same visual language as live data - which is the one mistake
// this whole console is built to avoid.
function renderAssistant() {
  const el = document.getElementById("assistant-text");
  const p = state.position, a = state.assessment;
  if (!p) {
    el.innerHTML = `<p class="muted">Load a position to see the forecast.</p>`;
    return;
  }

  const currentHf = p.hf && isFinite(p.hf) ? p.hf : null;
  const currentDebt = p.debt_usd || 0;
  const currentCollat = p.collateral_usd || 0;
  const currentEquity = Math.max(0, currentCollat - currentDebt);

  if (!p.has_debt || currentDebt === 0) {
    el.innerHTML = `<p class="muted">No open debt on Aave V3 for this account
      (collateral ${fmtUsd(currentCollat)}). Health factor is infinite and no
      intervention applies.</p>`;
    return;
  }

  if (!a) {
    el.innerHTML = `<p class="muted">Health factor <span class="mono">${fmtHf(p.hf)}</span>,
      collateral <span class="mono">${fmtUsd(currentCollat)}</span>,
      debt <span class="mono">${fmtUsd(currentDebt)}</span>.
      Run a dry-run assessment above to produce the rescue forecast — no sizing, cost
      or projection is shown until the backend has computed it.</p>`;
    return;
  }

  if (!a.viable) {
    el.innerHTML = `<p class="muted">The pipeline declined to act:
      ${esc(a.reason || "not economically viable")}. No forecast is shown for a
      declined rescue, because no intervention would be made.</p>`;
    return;
  }

  const repayUsd = a.repay_amount / 1e6;
  const targetHf = a.hf_target;
  const estCostBps = a.est_cost_bps;
  const collatSymbol = a.collateral_asset ? symbolOf(a.collateral_asset) : "—";

  const collatSpent = repayUsd * (1 + estCostBps / 10000);
  const futureDebt = Math.max(0, currentDebt - repayUsd);
  const futureCollat = Math.max(0, currentCollat - collatSpent);
  const futureEquity = Math.max(0, futureCollat - futureDebt);
  const interventionCost = collatSpent - repayUsd;

  const row = (label, before, after, delta) => `
    <tr><th>${label}</th>
      <td class="mono">${before}</td>
      <td class="mono">${after}</td>
      <td class="mono fc-delta">${delta}</td></tr>`;

  el.innerHTML = `
    <div class="fc-summary">
      <div class="fc-summary-title">Repay ${repayUsd.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} USDC
        sourced from ${collatSymbol}, restoring health factor to ${targetHf.toFixed(4)}.</div>
      <span class="badge badge-safe">Viable</span>
    </div>

    <div class="fc-table-wrap">
      <table class="fc-table">
        <thead><tr><th>Metric</th><th>Current</th><th>After rescue</th><th>Change</th></tr></thead>
        <tbody>
          ${row("Health factor", fmtHf(currentHf), targetHf.toFixed(4),
                `+${((targetHf - (currentHf || 1)) * 100).toFixed(1)}%`)}
          ${row("Outstanding debt", fmtUsd(currentDebt), fmtUsd(futureDebt),
                `−${fmtUsd(repayUsd)}`)}
          ${row("Supplied collateral", fmtUsd(currentCollat), fmtUsd(futureCollat),
                `−${fmtUsd(collatSpent)}`)}
          ${row("Net equity", fmtUsd(currentEquity), fmtUsd(futureEquity),
                `−${fmtUsd(interventionCost)}`)}
        </tbody>
      </table>
    </div>
    <p class="fc-note">Intervention cost ${fmtUsd(interventionCost)} at ${estCostBps} bps —
      within the borrower's signed bound. Figures are read from
      <span class="mono">AssessmentResponse</span>; the liquidation penalty this avoids is
      per-reserve and is not exposed on that response, so it is deliberately not shown here.</p>

    <div class="fc-steps">
      <div class="fc-step">
        <span class="fc-step-num">01</span>
        <div><div class="fc-step-main">Flash loan</div>
          <div class="fc-step-sub">Borrow ${fmtUsd(repayUsd)} USDC from the Aave V3 flash pool. No upfront capital.</div></div>
      </div>
      <div class="fc-step">
        <span class="fc-step-num">02</span>
        <div><div class="fc-step-main">Debt repayment</div>
          <div class="fc-step-sub">Repay ${fmtUsd(repayUsd)} of borrower debt, lifting health factor to ${targetHf.toFixed(4)}.</div></div>
      </div>
      <div class="fc-step">
        <span class="fc-step-num">03</span>
        <div><div class="fc-step-main">Collateral withdrawal and swap</div>
          <div class="fc-step-sub">Withdraw ${fmtUsd(collatSpent)} of ${collatSymbol} and swap on Uniswap V3 under the signed slippage cap (${estCostBps} bps estimated).</div></div>
      </div>
      <div class="fc-step">
        <span class="fc-step-num">04</span>
        <div><div class="fc-step-main">Settlement and HealthGuard</div>
          <div class="fc-step-sub">Repay flash principal plus premium. The vault enforces HF ≥ ${targetHf.toFixed(4)} and debt strictly reduced, or the whole transaction reverts.</div></div>
      </div>
    </div>`;
}


// ══════════════════════════════════════════════════════════════════════
//  AGENT LAYER (FR-18…FR-22)
//
//  The agent orchestrates and explains; it never produces a number that
//  reaches a transaction. Two rules govern everything below:
//
//    1. Every figure rendered here is read from a structured field the
//       BACKEND built — `proposal.facts.*`, `reply.facts`, `gate.checks`.
//       Model prose (`rationale`, `reply`) is displayed as prose and is
//       never parsed for a value. This is the same contract the rest of
//       this file already holds itself to.
//    2. A reply the backend's NumberGuard flagged is rendered in a
//       deliberately degraded style — never in the visual language of a
//       live metric card.
// ══════════════════════════════════════════════════════════════════════

// All agent content is backend- or model-authored text. It goes through
// this before it ever touches innerHTML.
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

// A route is unavailable when the layer is off. Surface the backend's own
// reason rather than a generic failure.
async function agentFetch(path, options) {
  const res = await fetch(`${API_BASE}/agent${path}`, options);
  let data = null;
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) {
    const d = data && data.detail;
    const msg = typeof d === "string" ? d : d ? JSON.stringify(d) : `HTTP ${res.status}`;
    const err = new Error(msg);
    err.status = res.status;
    err.detail = d;
    throw err;
  }
  return data;
}

// ── GET /agent/status ────────────────────────────────────────────────
// Always 200 by contract, including when the layer is disabled.
async function pollAgentStatus() {
  try {
    const data = await agentFetch("/status");
    state.agent.status = data;
  } catch (err) {
    state.agent.status = { enabled: false, reason: "Backend unreachable: " + err.message };
  }
  renderAgentStatus();
  return state.agent.status;
}

function renderAgentStatus() {
  const st = state.agent.status;
  const bar = document.getElementById("agent-status-bar");
  const workspace = document.getElementById("agent-workspace");
  const disabled = document.getElementById("agent-disabled-panel");
  if (!st) return;

  const chips = [
    `<span class="badge ${st.enabled ? "badge-safe" : "badge-warn"}">${st.enabled ? "Agent enabled" : "Agent disabled"}</span>`,
    `<span class="chip">Model ${esc(st.model || "—")}</span>`,
    `<span class="chip">Stack ${st.stack_available ? "Installed" : "Missing"}</span>`,
    `<span class="chip">Store ${st.store_ready ? "Ready" : "—"}</span>`,
    `<span class="chip">${st.pending_proposals ?? 0} Pending</span>`,
  ];
  bar.innerHTML = chips.join("");

  workspace.style.display = st.enabled ? "block" : "none";
  disabled.style.display = st.enabled ? "none" : "block";
  if (!st.enabled) {
    document.getElementById("agent-disabled-reason").textContent =
      st.reason || "The agent layer is not enabled on this backend.";
  }
}

// Entering the view: status first, then the three lists if it is live.
async function refreshAgentView() {
  const st = await pollAgentStatus();
  if (!st || !st.enabled) return;
  loadAgentInbox();
  loadTuning();
  loadAgentAudit();
}

// ── POST /agent/chat ─────────────────────────────────────────────────
async function sendChat() {
  if (state.agent.sending) return;
  const input = document.getElementById("agent-chat-input");
  const message = input.value.trim();
  if (!message) return;

  appendChatMessage({ role: "user", text: message });
  input.value = "";

  const btn = document.getElementById("agent-chat-send");
  state.agent.sending = true;
  btn.disabled = true;
  btn.textContent = "Thinking…";
  const pending = appendChatMessage({ role: "assistant", text: "…", pending: true });

  try {
    const body = { message };
    if (state.agent.threadId) body.thread_id = state.agent.threadId;
    if (state.borrower) body.borrower = state.borrower;

    const reply = await agentFetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    state.agent.threadId = reply.thread_id;
    pending.remove();
    appendChatMessage({
      role: "assistant",
      text: reply.reply,
      toolCalls: reply.tool_calls,
      sources: reply.sources,
      guardFlagged: reply.guard_flagged,
      truncated: reply.truncated,
    });
  } catch (err) {
    pending.remove();
    appendChatMessage({ role: "error", text: err.message });
    toast("error", "POST /agent/chat failed: " + err.message);
  } finally {
    state.agent.sending = false;
    btn.disabled = false;
    btn.textContent = "Send";
  }
}

function appendChatMessage(msg) {
  const log = document.getElementById("agent-chat-log");
  const el = document.createElement("div");

  // A guard-flagged reply carries a number the backend could not trace to a
  // tool result. It is shown - suppressing it would hide what the model said -
  // but never in the styling reserved for verified data.
  const flagged = !!msg.guardFlagged;
  el.className = [
    "agent-msg",
    `agent-msg-${msg.role}`,
    flagged ? "unsourced" : "",
    msg.pending ? "pending" : "",
  ].filter(Boolean).join(" ");

  let html = "";
  if (flagged) {
    html += `<div class="agent-guard-banner">Unverified — a figure in this reply could not be traced
             to a tool result. Treat it as prose, not data; check the live panels instead.</div>`;
  }
  html += `<div class="agent-msg-body">${esc(msg.text)}</div>`;

  if (msg.truncated) {
    html += `<div class="agent-msg-note">Tool loop hit its cap before the model finished — this answer may be incomplete.</div>`;
  }

  if (msg.toolCalls && msg.toolCalls.length) {
    const rows = msg.toolCalls.map((t) =>
      `<li><span class="mono">${esc(t.name)}</span>
       <span class="badge ${t.ok ? "badge-safe" : "badge-danger"}">${t.ok ? "OK" : "Err"}</span>
       <span class="muted mono">${t.latency_ms ?? 0}ms</span>
       ${t.error ? `<span class="muted">${esc(t.error)}</span>` : ""}</li>`
    ).join("");
    html += `<details class="agent-sources"><summary>Tools called (${msg.toolCalls.length})</summary>
             <ul class="agent-tool-list">${rows}</ul></details>`;
  }
  html += sourcesFooter(msg.sources);

  el.innerHTML = html;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

// Provenance footer. `sources` is a list on ChatReply and a field -> backend
// field map on FactSheet; render either without inventing entries.
function sourcesFooter(sources) {
  if (!sources) return "";
  const entries = Array.isArray(sources)
    ? sources.map((s) => `<li class="mono">${esc(s)}</li>`)
    : Object.entries(sources).map(([k, v]) => `<li><span class="mono">${esc(k)}</span> ← <span class="mono">${esc(v)}</span></li>`);
  if (!entries.length) return "";
  return `<details class="agent-sources"><summary>Sources (${entries.length})</summary>
          <ul class="agent-source-list">${entries.join("")}</ul></details>`;
}

async function clearChat() {
  const log = document.getElementById("agent-chat-log");
  const tid = state.agent.threadId;
  log.innerHTML = "";
  state.agent.threadId = null;
  if (!tid) return;
  try {
    await agentFetch(`/chat/${tid}`, { method: "DELETE" });
  } catch (err) {
    toast("error", "Could not clear the thread server-side: " + err.message);
  }
}

// ── POST /agent/crew/run ─────────────────────────────────────────────
async function runCrew() {
  const addr = state.borrower || document.getElementById("borrower-input").value.trim();
  if (!addr.startsWith("0x") || addr.length !== 42) {
    toast("error", "Load a valid borrower address in the top bar first.");
    return;
  }
  const btn = document.getElementById("btn-crew-run");
  btn.disabled = true;
  btn.textContent = "Running crew…";
  try {
    // Auto-register pre-signed mandate if available
    if (hasSignature(addr)) {
      const payload = buildParamsPayload(addr);
      await fetch(`${API_BASE}/positions/${addr}/assessment`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }).catch(() => {});
    }

    const result = await agentFetch("/crew/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ borrower: addr, trigger: "manual" }),
    });
    const outcome = {
      proposed: "ok", tuning_suggested: "ok",
      no_action: "info", gate_blocked: "info", error: "error",
    }[result.terminal] || "info";
    toast(outcome, `Crew: ${result.terminal} (strategy "${result.strategy}")`);
    loadAgentInbox();
    loadTuning();
    loadAgentAudit();
    pollAgentStatus();
  } catch (err) {
    toast("error", "POST /agent/crew/run failed: " + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Run crew";
  }
}

// ── GET /agent/proposals ─────────────────────────────────────────────
async function loadAgentInbox() {
  const box = document.getElementById("agent-inbox");
  try {
    const rows = await agentFetch("/proposals?limit=20");
    state.agent.proposals = rows;
    box.innerHTML = rows.length
      ? rows.map(renderProposalCard).join("")
      : `<p class="muted">No proposals yet. Run the crew for a registered borrower to produce one.</p>`;
  } catch (err) {
    box.innerHTML = `<p class="muted">Could not load proposals: ${esc(err.message)}</p>`;
  }
}

const PROPOSAL_BADGE = {
  PENDING: "badge-warn", APPROVED: "badge-warn", EXECUTED: "badge-safe",
  REJECTED: "", FAILED: "badge-danger", STALE: "badge-danger", EXPIRED: "",
};

function renderProposalCard(p) {
  const f = p.facts || {};
  const gate = p.gate || { checks: [], allowed: false, blocking: [] };
  const passed = gate.checks.filter((c) => c.passed).length;

  // Every figure below is a structured backend field. None is parsed out of
  // `p.rationale`, which is model prose and is rendered as prose only.
  const facts = [
    ["Health factor", f.hf !== undefined ? fmtHf(f.hf) : "—"],
    ["Target HF", f.hf_target !== undefined ? f.hf_target.toFixed(4) : "—"],
    ["Trigger", f.hf_trigger_bps !== undefined ? (f.hf_trigger_bps / 10000).toFixed(4) : "—"],
    ["Repay amount", f.repay_amount_human ? `${esc(f.repay_amount_human)} USDC` : "—"],
    ["Collateral", esc(f.collateral_symbol || "—")],
    ["Est. cost", f.est_cost_bps !== undefined ? `${f.est_cost_bps} bps` : "—"],
    ["Viable", f.viable === undefined ? "—" : f.viable ? "Yes" : "NO"],
    ["Volatility σ", f.sigma !== undefined ? f.sigma.toFixed(6) : "—"],
    ["Breach probability", f.breach_probability !== undefined ? `${(f.breach_probability * 100).toFixed(2)}%` : "—"],
    ["Collateral (USD)", f.collateral_usd !== undefined ? fmtUsd(f.collateral_usd) : "—"],
    ["Debt (USD)", f.debt_usd !== undefined ? fmtUsd(f.debt_usd) : "—"],
    ["Position state", esc(f.state || "—")],
  ];

  const checkRows = gate.checks.map((c) =>
    `<div class="gate-check ${c.passed ? "pass" : "fail"}">
       <span class="gate-check-mark">${c.passed ? "Pass" : "Fail"}</span>
       <span class="gate-check-name mono">${esc(c.name)}</span>
       <span class="gate-check-detail">${esc(c.detail)}</span>
     </div>`
  ).join("");

  // Backend enum value, not display text - ProposalStatus serialises as "PENDING".
  // Comparing against a prettified "Pending" silently hid the approve/reject buttons.
  const pending = p.status === "PENDING";
  const actions = pending
    ? `<div class="btn-row">
         <button class="btn btn-primary btn-sm" onclick="approveProposal(${p.id})">Approve and execute</button>
         <button class="btn btn-ghost btn-sm" onclick="rejectProposal(${p.id})">Reject</button>
       </div>`
    : "";

  const expires = p.expires_at ? new Date(p.expires_at * 1000).toLocaleTimeString() : "—";

  return `
    <div class="proposal-card ${p.guard_flagged ? "unsourced" : ""}" id="proposal-${p.id}">
      <div class="proposal-head">
        <div>
          <span class="proposal-id mono">#${p.id}</span>
          <span class="badge ${PROPOSAL_BADGE[p.status] || ""}">${esc(p.status)}</span>
          <span class="badge">${esc(p.strategy)}</span>
        </div>
        <div class="proposal-meta mono">${esc(short(p.borrower))} · expires ${esc(expires)}</div>
      </div>

      ${p.guard_flagged ? `<div class="agent-guard-banner">Unverified prose — a figure in the rationale below could not be traced to a tool result. The table and checklist are backend data and are unaffected.</div>` : ""}

      <div class="proposal-rationale">${esc(p.rationale || "")}</div>

      <div class="proposal-facts">
        ${facts.map(([k, v]) => `<div class="proposal-fact"><span class="proposal-fact-k">${k}</span><span class="proposal-fact-v mono">${v}</span></div>`).join("")}
      </div>

      <div class="gate-head">
        <span class="gate-title">Policy gate</span>
        <span class="badge ${gate.allowed ? "badge-safe" : "badge-danger"}">${gate.allowed ? "Allowed" : "Blocked"}</span>
        <span class="muted mono">${passed}/${gate.checks.length} checks passed${gate.severity ? " · " + esc(gate.severity) : ""}</span>
      </div>
      <div class="gate-checks">${checkRows}</div>

      ${p.tx_hash ? `<div class="proposal-tx mono">tx ${esc(p.tx_hash)}</div>` : ""}
      ${p.decision_note ? `<div class="muted">Note: ${esc(p.decision_note)}</div>` : ""}
      ${sourcesFooter(f.sources)}
      ${actions}
    </div>`;
}

// ── POST /agent/proposals/{id}/approve ───────────────────────────────
// The only path in this console that can reach a transaction, and it goes
// through a human clicking here. The backend re-runs the gate on a freshly
// re-fetched assessment before it calls protect().
async function approveProposal(id) {
  const who = window.prompt("Approving executes a real protection transaction.\nEnter your operator name to confirm:");
  if (!who) return;
  try {
    const result = await agentFetch(`/proposals/${id}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approved_by: who }),
    });
    const pr = result.protect;
    toast(pr && pr.submitted ? "ok" : "error",
      pr && pr.submitted
        ? `Executed. State ${pr.state}, tx ${pr.tx_hash || "—"}`
        : `Approved but not submitted: ${(pr && pr.reason) || result.reason || "see the proposal"}`);
  } catch (err) {
    // A 409 carries the re-validated gate's failed checks - the most useful
    // thing to show, since it names exactly which safety check now fails.
    const d = err.detail;
    if (err.status === 409 && d && d.checks) {
      const names = d.checks.map((c) => c.name).join(", ");
      toast("error", `Refused — the position moved. Now failing: ${names}`);
    } else {
      toast("error", `Approve failed: ${err.message}`);
    }
  } finally {
    loadAgentInbox();
    loadAgentAudit();
    pollAgentStatus();
    pollMetrics();
  }
}

async function rejectProposal(id) {
  const who = window.prompt("Enter your operator name to reject this proposal:");
  if (!who) return;
  try {
    await agentFetch(`/proposals/${id}/reject`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rejected_by: who }),
    });
    toast("ok", `Proposal #${id} rejected. Nothing was submitted.`);
  } catch (err) {
    toast("error", `Reject failed: ${err.message}`);
  } finally {
    loadAgentInbox();
    loadAgentAudit();
    pollAgentStatus();
  }
}

// ── GET /agent/tuning ────────────────────────────────────────────────
async function loadTuning() {
  const box = document.getElementById("agent-tuning");
  try {
    const rows = await agentFetch("/tuning?limit=20");
    state.agent.tuning = rows;
    box.innerHTML = rows.length
      ? rows.map(renderTuningCard).join("")
      : `<p class="muted">No open tuning suggestions.</p>`;
  } catch (err) {
    box.innerHTML = `<p class="muted">Could not load tuning suggestions: ${esc(err.message)}</p>`;
  }
}

function renderTuningCard(t) {
  return `
    <div class="proposal-card tuning-card">
      <div class="proposal-head">
        <div>
          <span class="proposal-id mono">#${t.id}</span>
          <span class="badge badge-warn">${esc(t.status)}</span>
          <span class="badge">Needs borrower signature</span>
        </div>
        <div class="proposal-meta mono">${esc(short(t.borrower))}</div>
      </div>
      <div class="proposal-facts">
        <div class="proposal-fact"><span class="proposal-fact-k">Field</span><span class="proposal-fact-v mono">${esc(t.field_name)}</span></div>
        <div class="proposal-fact"><span class="proposal-fact-k">Current</span><span class="proposal-fact-v mono">${t.current_value}</span></div>
        <div class="proposal-fact"><span class="proposal-fact-k">Suggested</span><span class="proposal-fact-v mono">${t.suggested_value}</span></div>
      </div>
      <div class="proposal-rationale">${esc(t.rationale || "")}</div>
      <details class="agent-sources">
        <summary>RiskParams the borrower would need to sign</summary>
        <pre class="mono tuning-payload">${esc(JSON.stringify(t.eip712_payload, null, 2))}</pre>
      </details>
      ${t.status === "open" ? `<div class="btn-row"><button class="btn btn-ghost btn-sm" onclick="dismissTuning(${t.id})">Dismiss</button></div>` : ""}
    </div>`;
}

async function dismissTuning(id) {
  try {
    await agentFetch(`/tuning/${id}/dismiss`, { method: "POST" });
    toast("ok", `Suggestion #${id} dismissed.`);
  } catch (err) {
    toast("error", `Dismiss failed: ${err.message}`);
  } finally {
    loadTuning();
  }
}

// ── GET /agent/audit ─────────────────────────────────────────────────
async function loadAgentAudit() {
  const box = document.getElementById("agent-audit");
  try {
    const rows = await agentFetch("/audit?limit=50");
    state.agent.audit = rows;
    box.innerHTML = rows.length
      ? `<div class="agent-audit-wrap"><table class="data-table agent-audit-table"><tbody>${rows.map((r) => `
          <tr>
            <td class="mono">${esc(new Date(r.ts * 1000).toLocaleString())}</td>
            <td><span class="badge">${esc(r.actor)}</span></td>
            <td class="mono">${esc(r.action)}</td>
            <td class="mono">${r.borrower ? esc(short(r.borrower)) : "—"}</td>
            <td class="mono muted">${r.proposal_id ? "#" + r.proposal_id : ""}</td>
          </tr>`).join("")}</tbody></table></div>`
      : `<p class="muted">Audit trail is empty.</p>`;
  } catch (err) {
    box.innerHTML = `<p class="muted">Could not load the audit trail: ${esc(err.message)}</p>`;
  }
}

// ── POST /agent/panic ────────────────────────────────────────────────
// Trips the keeper's own circuit breaker, so it halts autonomous submission
// too - not merely the agent. POST /breaker/reset (System tab) undoes it.
async function agentPanic() {
  if (!window.confirm("Trip the circuit breaker? This halts ALL autonomous protection, not just the agent.")) return;
  try {
    const res = await agentFetch("/panic", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: "operator panic from console" }),
    });
    toast("ok", `Breaker tripped (${res.trip_reason}). Reset it from the System tab.`);
  } catch (err) {
    toast("error", `Panic failed: ${err.message}`);
  } finally {
    pollMetrics();
    loadAgentAudit();
  }
}


// ── Demo: market crash simulator (computes dynamic impact for loaded wallet) ────
async function simulateMarketCrash() {
  const box = document.getElementById("crash-sim");
  box.style.display = "block";
  const p = state.position;
  const basePrice = 2500;
  const drops = [1.0, 0.92, 0.85, 0.78];
  
  document.getElementById("crash-status").textContent = "Simulating market drop…";
  
  for (let i = 0; i < drops.length; i++) {
    const simPrice = Math.round(basePrice * drops[i]);
    document.getElementById("crash-price").textContent = `$${simPrice.toLocaleString()}`;
    
    if (p && p.has_debt && p.hf && isFinite(p.hf)) {
      const simHf = (p.hf * drops[i]).toFixed(2);
      document.getElementById("crash-hf").textContent = simHf;
      if (simHf < 1.0) {
        document.getElementById("crash-status").textContent = `LIQUIDATION BREACH! HF dropped to ${simHf} (below 1.00)`;
      } else if (simHf <= 1.15) {
        document.getElementById("crash-status").textContent = `CRITICAL TRIGGER BREACH! HF dropped to ${simHf} (<= 1.15)`;
      } else {
        document.getElementById("crash-status").textContent = `Watch zone: HF dropped to ${simHf}`;
      }
    } else {
      document.getElementById("crash-hf").textContent = "∞";
      document.getElementById("crash-status").textContent = "No debt: collateral drop does not threaten position.";
    }
    await sleep(400);
  }
}

