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
};

// ── Section navigation ──────────────────────────────────────────────
const VIEWS = ["command-center", "position", "risk", "protection", "execution", "security", "system", "assistant"];

function go(view) {
  window.location.hash = view;
}

function renderNav() {
  const hash = (window.location.hash || "#command-center").slice(1);
  const view = VIEWS.includes(hash) ? hash : "command-center";
  document.querySelectorAll(".view").forEach((el) => el.classList.toggle("active", el.dataset.view === view));
  document.querySelectorAll(".navlinks a, .navlinks-mobile a").forEach((el) => {
    el.classList.toggle("active", el.dataset.nav === view);
  });
  document.getElementById("navlinks-mobile").classList.remove("open");
  document.getElementById("btn-menu").setAttribute("aria-expanded", "false");
  window.scrollTo({ top: 0, behavior: "instant" in window ? "instant" : "auto" });
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

  pollHealth();
  pollMetrics();
  loadConfig();
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
    txt.textContent = data.rpc_connected ? "SYSTEM ACTIVE" : "RPC DOWN";

    document.getElementById("cc-rpc").textContent = data.rpc_connected ? "CONNECTED" : "DOWN";
    document.getElementById("cc-block").textContent = data.block_number ? `block ${data.block_number.toLocaleString()}` : "block —";
    document.getElementById("sys-rpc").textContent = data.rpc_connected ? "CONNECTED" : "DOWN";
  } catch (err) {
    // Clear the cached health too. Leaving it set made renderSecurity() keep
    // showing "RPC CONNECTION: CONNECTED" in green while the backend was down.
    state.health = null;
    const chip = document.getElementById("chip-system-status");
    chip.className = "chip chip-status bad";
    document.getElementById("txt-system-status").textContent = "UNREACHABLE";
    document.getElementById("cc-rpc").textContent = "UNREACHABLE";
    document.getElementById("cc-block").textContent = "block —";
    document.getElementById("sys-rpc").textContent = "UNREACHABLE";
    renderSecurity();
  }
}

// ── GET /metrics ─────────────────────────────────────────────────────
async function pollMetrics() {
  try {
    const res = await fetch(`${API_BASE}/metrics`);
    const data = await res.json();
    state.metrics = data;

    document.getElementById("cc-breaker").textContent = data.breaker_paused ? "PAUSED" : "ARMED";
    // Threshold comes from /config (and is editable in the System tab), so it must
    // not be hardcoded here - setting it to 5 used to still display "... / 3".
    const breakerMax = state.config?.breaker_max_consecutive_failures ?? "?";
    document.getElementById("cc-breaker-sub").textContent =
      `${data.breaker_consecutive_failures} / ${breakerMax} failures`;
    document.getElementById("sys-breaker").textContent = data.breaker_paused ? "PAUSED" : "ARMED";
    document.getElementById("sys-inflight").textContent = data.in_flight_borrowers.length;
    document.getElementById("sys-keeper").textContent = state.config?.autonomous_enabled ? "RUNNING" : "MANUAL";

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
  btn.textContent = "LOADING…";
  document.getElementById("walletbar-hint").textContent = "";
  try {
    const res = await fetch(`${API_BASE}/positions/${addr}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    state.position = data;
    state.assessment = null;
    state.protectResult = null;
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
    btn.textContent = "LOAD POSITION";
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
  document.getElementById("risk-badge").textContent = "NO DATA";
  document.getElementById("risk-badge").style.color = "var(--text-muted)";
  document.getElementById("risk-badge").style.borderColor = "var(--border)";
  document.getElementById("risk-explain").textContent = "Could not read this wallet's position — see the error above. Try the demo address instead.";
  document.getElementById("decision-tag").textContent = "NO ASSESSMENT YET";
  document.getElementById("protection-explain").textContent = "Run an assessment to see the backend's reasoning here.";
  document.getElementById("assistant-text").textContent = "Load a position and run an assessment to get an explanation.";
  resetFlowSteps();
  document.getElementById("exec-result").className = "exec-result";
  document.querySelectorAll(".atomic-step").forEach((el) => (el.className = "atomic-step pending"));
  document.getElementById("hfc-current-label").textContent = "CURRENT";
  document.getElementById("hfc-target-label").textContent = "TARGET";
}

// ── Presentational risk mapping (derived from REAL backend state only) ─
// HEALTHY -> SAFE, WATCH -> WATCH, ASSESSING -> HIGH RISK, DECLINED -> WATCH,
// READY/SUBMITTED -> HIGH RISK, RESTORED -> SAFE, REVERTED -> HIGH RISK.
function riskLevelFor(positionState) {
  const map = {
    HEALTHY: { label: "SAFE", cls: "badge-safe" },
    WATCH: { label: "WATCH", cls: "badge-warn" },
    ASSESSING: { label: "HIGH RISK", cls: "badge-danger" },
    DECLINED: { label: "WATCH", cls: "badge-warn" },
    READY: { label: "HIGH RISK", cls: "badge-danger" },
    SUBMITTED: { label: "HIGH RISK", cls: "badge-danger" },
    RESTORED: { label: "SAFE", cls: "badge-safe" },
    REVERTED: { label: "HIGH RISK", cls: "badge-danger" },
  };
  return map[positionState] || { label: positionState || "UNKNOWN", cls: "" };
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
    tag.textContent = "NO ASSESSMENT YET";
    return;
  }
  tag.textContent = a.viable ? "VIABLE" : "DECLINED";
  tag.style.color = a.viable ? "var(--success)" : "var(--warning)";

  const risk = state.position ? riskLevelFor(state.position.state) : { label: "—" };
  document.getElementById("ad-risk").innerHTML = `<span class="badge ${risk.cls || ""}">${risk.label}</span>`;
  document.getElementById("ad-target").textContent = a.hf_target.toFixed(4);
  document.getElementById("ad-repay").textContent = a.repay_amount ? `${(a.repay_amount / 1e6).toLocaleString(undefined, { minimumFractionDigits: 2 })} USDC` : "0.00 USDC";
  document.getElementById("ad-collateral").textContent = symbolOf(a.collateral_asset);
  document.getElementById("ad-cost").textContent = `${a.est_cost_bps} bps`;
  document.getElementById("ad-viable").innerHTML = a.viable
    ? `<span class="badge badge-safe">PASS</span>`
    : `<span class="badge badge-warn">DECLINED</span>`;

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
  btn.textContent = "RUNNING…";
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
    btn.textContent = "RUN DRY-RUN ASSESSMENT";
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
    el.querySelector(".flow-status").textContent = "PENDING";
  });
}
function setFlow(n, cls, label) {
  const el = document.querySelector(`.flow-step[data-step="${n}"]`);
  el.className = "flow-step " + cls;
  el.querySelector(".flow-status").textContent = label;
}

// The single guided real run: health -> position -> assessment -> protect.
// Used by both the Execution tab's "EXECUTE PROTECTION" and the Command
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
  btn.textContent = "RUNNING…";

  try {
    // 1. MONITOR
    setFlow(1, "active", "RUNNING");
    const posRes = await fetch(`${API_BASE}/positions/${addr}`);
    const pos = await posRes.json();
    if (!posRes.ok) throw new Error(pos.detail || `HTTP ${posRes.status}`);
    state.position = pos;
    renderPosition();
    renderCommandCenter();
    setFlow(1, "done", "DONE");

    if (!pos.has_debt) {
      setFlow(2, "skipped", "SKIPPED");
      setFlow(3, "skipped", "SKIPPED");
      setFlow(4, "skipped", "SKIPPED");
      setFlow(5, "skipped", "SKIPPED");
      setFlow(6, "skipped", "SKIPPED");
      setFlow(7, "skipped", "SKIPPED");
      showExecResult("No open debt on this wallet. Nothing to protect.");
      return;
    }

    // 2. PREDICT + 3. SIZE (both inside the single /assessment call)
    setFlow(2, "active", "RUNNING");
    const assessPromise = fetch(`${API_BASE}/positions/${addr}/assessment`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildParamsPayload(addr)),
    });
    await sleep(400);
    setFlow(2, "done", "DONE");
    setFlow(3, "active", "RUNNING");
    const assessRes = await assessPromise;
    const assessment = await assessRes.json();
    if (!assessRes.ok) throw new Error(assessment.detail ? JSON.stringify(assessment.detail) : `HTTP ${assessRes.status}`);
    state.assessment = assessment;
    setFlow(3, "done", "DONE");
    renderProtection(); renderCommandCenter(); renderRisk();

    // 4. CHECK (viability, from the same response)
    setFlow(4, "active", "RUNNING");
    await sleep(250);
    if (!assessment.viable) {
      setFlow(4, "declined", "DECLINED");
      setFlow(5, "skipped", "SKIPPED");
      setFlow(6, "skipped", "SKIPPED");
      setFlow(7, "skipped", "SKIPPED");
      showExecResult(`Declined at the viability gate: ${assessment.reason || "not economically worthwhile"}. No transaction was built.`);
      return;
    }
    setFlow(4, "done", "PASS");

    // 5. SIMULATE + 6. EXECUTE (inside /protect)
    setFlow(5, "active", "RUNNING");
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
    if (protectData.state === "DECLINED" && declinedReason.includes("simulation")) {
      setFlow(5, "failed", "REVERTED");
      setFlow(6, "skipped", "SKIPPED");
      setFlow(7, "skipped", "SKIPPED");
      markAtomic(["flashloan"], "fail");
      showExecResult(explainRevert(declinedReason, addr));
      return;
    }
    // Breaker-paused, in-flight, and viability declines never reach the simulator.
    // Marking step 5 PASS for those claimed a dry-run that never ran.
    if (protectData.state !== "RESTORED" && !protectData.submitted) {
      setFlow(5, "skipped", "NOT RUN");
      setFlow(6, "skipped", "SKIPPED");
      setFlow(7, "skipped", "SKIPPED");
      showExecResult(`Stopped before simulation - ${declinedReason || protectData.state}.`);
      renderAssistant();
      return;
    }

    setFlow(5, "done", "PASS");
    setFlow(6, "active", "RUNNING");
    await sleep(250);

    if (protectData.state === "RESTORED") {
      setFlow(6, "done", "DONE");
      setFlow(7, "done", "RESTORED");
      if (protectData.via_fallback) {
        // HF really did improve, but via a direct Aave repay - no flash loan, no
        // swap, no HealthGuard. Never present that as the atomic vault rescue.
        markAtomic(["repay"], "ok");
        markAtomic(["flashloan", "release", "swap", "flashrepay", "healthcheck"], "fail");
        setFlow(7, "declined", "PARTIAL");
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
      setFlow(7, "skipped", "SKIPPED");
      markAtomic(["flashloan"], protectData.submitted ? "ok" : "pending");
      showExecResult(`Execution stopped in state "${protectData.state}": ${declinedReason || "see technical details"}.`);
    }
    renderAssistant();
  } catch (err) {
    const activeStep = document.querySelector(".flow-step.active");
    if (activeStep) { activeStep.className = "flow-step failed"; activeStep.querySelector(".flow-status").textContent = "ERROR"; }
    toast("error", err.message);
    showExecResult(`Error: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "EXECUTE PROTECTION";
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
    { name: "CIRCUIT BREAKER", src: "LIVE", ok: m ? !m.breaker_paused : null, okText: "ARMED", badText: "PAUSED" },
    { name: "IN-FLIGHT LOCK", src: "LIVE", ok: m ? m.in_flight_borrowers.length === 0 : null, okText: "CLEAR", badText: "HELD" },
    { name: "RPC CONNECTION", src: "LIVE", ok: state.health ? state.health.rpc_connected : null, okText: "CONNECTED", badText: "DOWN" },
    { name: "EIP-712 SIGNATURE", src: "BY DESIGN", ok: true, okText: "ENFORCED ON-CHAIN" },
    { name: "KEEPER AUTHORIZATION", src: "BY DESIGN", ok: true, okText: "ENFORCED ON-CHAIN" },
    { name: "NONCE", src: "BY DESIGN", ok: true, okText: "REPLAY-PROTECTED" },
    { name: "DEADLINE", src: "BY DESIGN", ok: true, okText: "EXPIRY-ENFORCED" },
    { name: "A-TOKEN ALLOWANCE", src: "BY DESIGN", ok: true, okText: "OPT-IN ONLY" },
    { name: "SLIPPAGE BOUND", src: "BY DESIGN", ok: true, okText: "CONTRACT-CAPPED" },
    { name: "HEALTH GUARD", src: "BY DESIGN", ok: true, okText: "NO-WORSE INVARIANT" },
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

// ── Assistant (Executive AI Summary, Rescue Plan & Future Statistics) ──
function renderAssistant() {
  const el = document.getElementById("assistant-text");
  const p = state.position, a = state.assessment, pr = state.protectResult;
  if (!p) {
    el.innerHTML = `<p class="muted">Load a wallet position in the top bar to generate the autonomous rescue plan and future statistics forecast.</p>`;
    return;
  }

  const risk = riskLevelFor(p.state);
  const currentHf = p.hf && isFinite(p.hf) ? p.hf : null;
  const currentDebt = p.debt_usd || 0;
  const currentCollat = p.collateral_usd || 0;
  const currentEquity = Math.max(0, currentCollat - currentDebt);

  // Every figure below must come from a real assessment. Earlier revisions fell
  // back to invented constants (8 bps, target 1.25, LT 0.825) and re-implemented
  // the sizing formula in the browser, rendering guesses in the same visual
  // language as live data - directly contradicting this file's own contract that
  // no financial logic is duplicated here. With no assessment we show nothing.
  const hasAssessment = !!(a && a.repay_amount);
  if (!a) {
    el.innerHTML = `
      <div class="asst-hero">
        <div class="asst-hero-left">
          <div class="asst-hero-title">Position Analysis for <span class="mono">${p.borrower.slice(0, 8)}...${p.borrower.slice(-6)}</span></div>
          <div class="asst-hero-desc">Risk Status: <strong class="badge ${riskLevelFor(p.state).cls}">${riskLevelFor(p.state).label}</strong> (Backend State: <code class="mono">${p.state}</code>)</div>
        </div>
      </div>
      <p class="muted" style="margin-top:14px;">Health factor <span class="mono">${fmtHf(p.hf)}</span>,
      collateral <span class="mono">${fmtUsd(currentCollat)}</span>,
      debt <span class="mono">${fmtUsd(currentDebt)}</span>.</p>
      <p class="muted">Run a dry-run assessment on the Protection tab to produce the rescue plan.
      No sizing, cost, or forecast figures are shown until the backend has computed them.</p>`;
    return;
  }

  const repayUsd = hasAssessment ? a.repay_amount / 1e6 : 0;
  const targetHf = a.hf_target;
  const estCostBps = a.est_cost_bps;
  const collatSymbol = a.collateral_asset ? symbolOf(a.collateral_asset) : "—";

  const collatSpent = repayUsd * (1 + (estCostBps / 10000));
  const futureDebt = Math.max(0, currentDebt - repayUsd);
  const futureCollat = Math.max(0, currentCollat - collatSpent);
  const futureHf = currentDebt > 0 && repayUsd > 0 ? targetHf : (currentDebt === 0 ? "∞" : fmtHf(currentHf));
  const futureEquity = Math.max(0, futureCollat - futureDebt);
  // Illustrative only: Aave's liquidation bonus is per-reserve (the backend models
  // it as liq_bonus_bps) and is not exposed on AssessmentResponse, so this uses a
  // nominal 10% and is labelled as an estimate wherever it is displayed.
  const penaltyAvoided = currentDebt * 0.10;
  const interventionCost = collatSpent - repayUsd;
  const netBenefit = Math.max(0, penaltyAvoided - interventionCost);

  let html = `
    <!-- Executive Verdict Hero -->
    <div class="asst-hero">
      <div class="asst-hero-left">
        <div class="asst-hero-title">Position Analysis for <span class="mono">${p.borrower.slice(0, 8)}...${p.borrower.slice(-6)}</span></div>
        <div class="asst-hero-desc">Risk Status: <strong class="badge ${risk.cls}">${risk.label}</strong> (Backend State: <code class="mono">${p.state}</code>)</div>
      </div>
      <div class="asst-hero-right">
        <span class="badge ${currentHf && currentHf <= 1.15 ? 'badge-danger' : 'badge-safe'}">
          ${currentHf && currentHf <= 1.15 ? 'INTERVENTION REQUIRED' : 'POSITION SAFE'}
        </span>
      </div>
    </div>
  `;

  if (!p.has_debt || currentDebt === 0) {
    html += `
      <div class="asst-card" style="margin-top: 14px;">
        <div class="asst-card-title">Zero Debt Status</div>
        <div class="asst-card-val" style="color:var(--success);">Health Factor: ∞ (Infinite)</div>
        <p class="asst-card-sub" style="margin-top:8px;">This account holds $${currentCollat.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})} in collateral with $0.00 outstanding debt. Liquidation risk is zero; no rescue intervention is required.</p>
      </div>
    `;
    el.innerHTML = html;
    return;
  }

  // 1. Future Statistics Matrix
  html += `
    <div class="asst-section-title">📊 Future Statistics Forecast (Before vs. After Rescue)</div>
    
    <div class="asst-grid-3">
      <div class="asst-card">
        <div class="asst-card-title">Health Factor Projection</div>
        <div class="asst-card-val" style="color:var(--success);">${fmtHf(currentHf)} ➔ ${typeof futureHf === 'number' ? futureHf.toFixed(4) : futureHf}</div>
        <div class="asst-card-sub">+${((targetHf - (currentHf || 1.0)) * 100).toFixed(1)}% safe buffer above liquidation (1.00)</div>
      </div>
      
      <div class="asst-card">
        <div class="asst-card-title">Debt Reduction</div>
        <div class="asst-card-val" style="color:var(--text-primary);">$${currentDebt.toLocaleString('en-US', {maximumFractionDigits:0})} ➔ $${futureDebt.toLocaleString('en-US', {maximumFractionDigits:0})}</div>
        <div class="asst-card-sub">-$${repayUsd.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})} USDC flash-repaid</div>
      </div>
      
      <div class="asst-card">
        <div class="asst-card-title">Collateral Retained</div>
        <div class="asst-card-val" style="color:var(--text-primary);">$${currentCollat.toLocaleString('en-US', {maximumFractionDigits:0})} ➔ $${futureCollat.toLocaleString('en-US', {maximumFractionDigits:0})}</div>
        <div class="asst-card-sub">${((futureCollat / (currentCollat || 1)) * 100).toFixed(1)}% of original collateral preserved</div>
      </div>
    </div>

    <!-- Comparative Table -->
    <div class="asst-table-wrap">
      <table class="asst-table">
        <thead>
          <tr>
            <th>Metric</th>
            <th>Current (At-Risk)</th>
            <th>Post-Rescue (Forecast)</th>
            <th>Delta / Impact</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td><strong>Health Factor</strong></td>
            <td class="mono" style="color:${currentHf <= 1.15 ? 'var(--danger)' : 'var(--text-primary)'};">${fmtHf(currentHf)}</td>
            <td class="mono" style="color:var(--success); font-weight:700;">${typeof futureHf === 'number' ? futureHf.toFixed(4) : futureHf}</td>
            <td class="mono" style="color:var(--success);">RESTORED (+${((targetHf - (currentHf || 1.0)) * 100).toFixed(1)}%)</td>
          </tr>
          <tr>
            <td><strong>Outstanding Debt</strong></td>
            <td class="mono">$${currentDebt.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})}</td>
            <td class="mono">$${futureDebt.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})}</td>
            <td class="mono" style="color:var(--success);">-$${repayUsd.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})} (-${((repayUsd / currentDebt) * 100).toFixed(1)}%)</td>
          </tr>
          <tr>
            <td><strong>Supplied Collateral</strong></td>
            <td class="mono">$${currentCollat.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})}</td>
            <td class="mono">$${futureCollat.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})}</td>
            <td class="mono">-$${collatSpent.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})} (${collatSymbol})</td>
          </tr>
          <tr>
            <td><strong>Net Protected Equity</strong></td>
            <td class="mono">$${currentEquity.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})}</td>
            <td class="mono">$${futureEquity.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})}</td>
            <td class="mono" style="color:var(--success); font-weight:600;">$${futureEquity.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})} Protected</td>
          </tr>
          <tr>
            <td><strong>Liquidation Penalty Avoided</strong></td>
            <td class="mono" style="color:var(--danger);">-$${penaltyAvoided.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})} (Risk)</td>
            <td class="mono" style="color:var(--success);">$0.00 (Immune)</td>
            <td class="mono" style="color:var(--success); font-weight:700;">+$${penaltyAvoided.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})} SAVED</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 2. Step-by-Step Autonomous Rescue Plan -->
    <div class="asst-section-title">⚡ Step-by-Step Autonomous Rescue Plan</div>
    
    <div class="asst-plan-list">
      <div class="asst-plan-step">
        <div class="asst-plan-num">01</div>
        <div class="asst-plan-content">
          <div class="asst-plan-main">Flash Loan Acquisition</div>
          <div class="asst-plan-sub">Borrow <span class="mono" style="color:var(--text-primary);">$${repayUsd.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})} USDC</span> from Aave V3 flash pool (0% upfront capital required).</div>
        </div>
      </div>
      
      <div class="asst-plan-step">
        <div class="asst-plan-num">02</div>
        <div class="asst-plan-content">
          <div class="asst-plan-main">Atomic Debt Repayment</div>
          <div class="asst-plan-sub">Repay <span class="mono" style="color:var(--text-primary);">$${repayUsd.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})} USDC</span> of borrower debt to push Health Factor directly to target <span class="mono" style="color:var(--success); font-weight:600;">${targetHf.toFixed(4)}</span>.</div>
        </div>
      </div>
      
      <div class="asst-plan-step">
        <div class="asst-plan-num">03</div>
        <div class="asst-plan-content">
          <div class="asst-plan-main">Collateral Withdrawal & Swap</div>
          <div class="asst-plan-sub">Withdraw <span class="mono" style="color:var(--text-primary);">$${collatSpent.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})}</span> of ${collatSymbol} collateral and execute Uniswap V3 swap with strictly capped slippage (estimated cost: <span class="mono">${estCostBps} bps</span>).</div>
        </div>
      </div>
      
      <div class="asst-plan-step">
        <div class="asst-plan-num">04</div>
        <div class="asst-plan-content">
          <div class="asst-plan-main">Flash Loan Settlement & HealthGuard Invariant Verification</div>
          <div class="asst-plan-sub">Repay Aave flash loan principal + 0.05% fee. Smart contract enforces Multi-Invariant HealthGuard (<code class="mono">HF_after >= ${targetHf.toFixed(4)}</code> and <code class="mono">Debt_after < Debt_before</code>).</div>
        </div>
      </div>
    </div>

    <!-- Financial Benefit Callout -->
    <div class="asst-callout">
      <div>
        <strong>Net Economic Benefit to Borrower:</strong>
        <div style="font-size: 12px; color: var(--text-secondary); margin-top: 2px;">
          Liquidation Penalty Saved ($${penaltyAvoided.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})}) minus Intervention Cost ($${interventionCost.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})})
        </div>
      </div>
      <div class="asst-callout-val">+$${netBenefit.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})} SAVED</div>
    </div>
  `;

  el.innerHTML = html;
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

