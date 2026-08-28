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
    const chip = document.getElementById("chip-system-status");
    chip.className = "chip chip-status bad";
    document.getElementById("txt-system-status").textContent = "UNREACHABLE";
  }
}

// ── GET /metrics ─────────────────────────────────────────────────────
async function pollMetrics() {
  try {
    const res = await fetch(`${API_BASE}/metrics`);
    const data = await res.json();
    state.metrics = data;

    document.getElementById("cc-breaker").textContent = data.breaker_paused ? "PAUSED" : "ARMED";
    document.getElementById("cc-breaker-sub").textContent = `${data.breaker_consecutive_failures} / 3 failures`;
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
    toast("error", "GET /positions failed: " + err.message);
    document.getElementById("walletbar-hint").textContent = "Load failed — see toast.";
  } finally {
    btn.disabled = false;
    btn.textContent = "LOAD POSITION";
  }
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

function buildParamsPayload(borrower) {
  return {
    params: {
      borrower,
      hfTriggerBps: 11500,
      hfTargetBaseBps: 12500,
      volCoeffK: 0,
      hfTargetMaxBps: 14000,
      maxSlippageBps: 300,
      maxCostBps: 500,
      allowedCollaterals: ["0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"],
      nonce: Date.now() % 1_000_000,
      deadline: 2000000000,
    },
    signature: "0x" + "00".repeat(65),
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
    state.protectResult = protectData;

    if (protectData.state === "DECLINED" && protectData.reason && protectData.reason.includes("simulation")) {
      setFlow(5, "failed", "REVERTED");
      setFlow(6, "skipped", "SKIPPED");
      setFlow(7, "skipped", "SKIPPED");
      markAtomic(["flashloan"], "fail");
      showExecResult(`Simulation reverted: ${protectData.reason}. No transaction was submitted — nothing on-chain changed.`);
      return;
    }
    setFlow(5, "done", "PASS");
    setFlow(6, "active", "RUNNING");
    await sleep(250);

    if (protectData.state === "RESTORED") {
      setFlow(6, "done", "DONE");
      setFlow(7, "done", "RESTORED");
      markAtomic(["flashloan", "repay", "release", "swap", "flashrepay", "healthcheck", "restored"], "ok");
      showExecResult(`Position restored. Tx: ${protectData.tx_hash || "—"}`);
    } else {
      setFlow(6, "declined", protectData.state);
      setFlow(7, "skipped", "SKIPPED");
      markAtomic(["flashloan"], protectData.submitted ? "ok" : "pending");
      showExecResult(`Execution stopped in state "${protectData.state}": ${protectData.reason || "see technical details"}.`);
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

// ── Assistant (deterministic template over real state) ────────────────
function renderAssistant() {
  const el = document.getElementById("assistant-text");
  const p = state.position, a = state.assessment, pr = state.protectResult;
  if (!p) { el.textContent = "Load a position and run an assessment to get an explanation."; return; }

  let lines = [];
  lines.push(`Your health factor is ${fmtHf(p.hf)}.`);
  const risk = riskLevelFor(p.state);
  lines.push(`The system currently classifies the position as ${risk.label} (backend state: "${p.state}").`);

  if (a) {
    lines.push(`The current target health factor is ${a.hf_target.toFixed(4)}.`);
    if (a.viable) {
      lines.push(`A protection assessment estimated ${(a.repay_amount / 1e6).toFixed(2)} USDC as the minimum repayment, sourced from ${symbolOf(a.collateral_asset)}, at an estimated cost of ${a.est_cost_bps} basis points.`);
    } else {
      lines.push(`The assessment declined to recommend a rescue: ${a.reason || "not economically viable"}.`);
    }
  } else {
    lines.push(`No assessment has been run yet — visit Protection to request one.`);
  }

  if (pr) {
    if (pr.state === "RESTORED") {
      lines.push(`Execution status: RESTORED. Transaction: ${pr.tx_hash || "—"}.`);
    } else if (pr.submitted === false) {
      lines.push(`Execution status: ${pr.state}. The transaction was not submitted — ${pr.reason || "see Execution tab for detail"}.`);
    } else {
      lines.push(`Execution status: ${pr.state}.`);
    }
  }

  lines.push(`This explanation only restates values already returned by the backend — it does not make or override any protection decision.`);
  el.textContent = lines.join("\n\n");
}

// ── Demo: fictional market crash (never touches live metric cards) ────
async function simulateMarketCrash() {
  const box = document.getElementById("crash-sim");
  box.style.display = "block";
  const prices = [3000, 2850, 2700, 2600];
  const hfs = [1.48, 1.41, 1.30, 1.21];
  document.getElementById("crash-status").textContent = "Simulating…";
  for (let i = 0; i < prices.length; i++) {
    document.getElementById("crash-price").textContent = `$${prices[i].toLocaleString()}`;
    document.getElementById("crash-hf").textContent = hfs[i].toFixed(2);
    await sleep(500);
  }
  document.getElementById("crash-status").textContent = "RISK DETECTED (fictional) — this panel never overwrites live data above.";
}
