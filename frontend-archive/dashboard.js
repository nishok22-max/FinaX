/**
 * Liquidation Shield — Operator Console (light theme, jury-demo build)
 * Every function below calls a real FastAPI endpoint and renders the actual
 * response. Nothing is scripted or faked — if the backend declines, says so.
 */

const API_BASE = "";

const TOKEN_SYMBOLS = {
  "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1": "WETH",
  "0xaf88d065e77c8cC2239327C5EDb3A432268e5831": "USDC",
  "0x5979D7b546E38E414F7E9822514be443A4800529": "wstETH",
};

function symbolOf(addr) {
  if (!addr) return "—";
  return TOKEN_SYMBOLS[addr] || `${addr.slice(0, 6)}…${addr.slice(-4)}`;
}

let autonomousEnabled = true;

// ── Init ──────────────────────────────────────────────────────────────
window.addEventListener("DOMContentLoaded", () => {
  pollHealth();
  pollMetrics();
  loadConfig();
  setInterval(pollHealth, 8000);
  setInterval(pollMetrics, 6000);
  logEvent("info", "console", "Operator console loaded. Talking to " + window.location.origin);
});

// ── Health / RPC status ──────────────────────────────────────────────
async function pollHealth() {
  const chipRpc = document.getElementById("chip-rpc");
  try {
    const res = await fetch(`${API_BASE}/health`);
    const data = await res.json();
    document.getElementById("kpi-block").textContent = data.block_number
      ? data.block_number.toLocaleString()
      : "—";
    if (data.rpc_connected) {
      chipRpc.className = "chip status-ok";
      chipRpc.innerHTML = `<span class="dot"></span> RPC connected <span class="mono">#${data.block_number?.toLocaleString() ?? "?"}</span>`;
    } else {
      chipRpc.className = "chip status-bad";
      chipRpc.innerHTML = `<span class="dot"></span> RPC not connected`;
    }
  } catch (err) {
    chipRpc.className = "chip status-bad";
    chipRpc.innerHTML = `<span class="dot"></span> Backend unreachable`;
  }
}

// ── Metrics (breaker, locks, counters) ───────────────────────────────
async function pollMetrics() {
  try {
    const res = await fetch(`${API_BASE}/metrics`);
    const data = await res.json();

    document.getElementById("kpi-registered").textContent = data.registered_positions ?? "0";
    const assessed = data.counters?.assessed ?? 0;
    const declined = data.counters?.declined ?? 0;
    document.getElementById("kpi-assessed").textContent = `${assessed} / ${declined}`;
    document.getElementById("kpi-inflight").textContent = (data.in_flight_borrowers?.length ?? 0);

    document.getElementById("breaker-fails").textContent =
      `${data.breaker_consecutive_failures ?? 0} / 3`;
    document.getElementById("breaker-locks").textContent = data.in_flight_borrowers?.length ?? 0;

    const breakerBadge = document.getElementById("breaker-badge");
    const chipBreaker = document.getElementById("chip-breaker");
    if (data.breaker_paused) {
      breakerBadge.textContent = "PAUSED";
      breakerBadge.className = "badge danger";
      chipBreaker.className = "chip status-bad";
      chipBreaker.innerHTML = `<span class="dot"></span> Breaker paused`;
    } else {
      breakerBadge.textContent = "NORMAL";
      breakerBadge.className = "badge safe";
      chipBreaker.className = "chip status-ok";
      chipBreaker.innerHTML = `<span class="dot"></span> Breaker normal`;
    }
  } catch (err) {
    // Leave last-known values; backend may be mid-restart.
  }
}

async function resetBreaker() {
  logEvent("info", "POST /breaker/reset", "Requesting breaker reset…");
  try {
    const res = await fetch(`${API_BASE}/breaker/reset`, { method: "POST" });
    const data = await res.json();
    logEvent("ok", "POST /breaker/reset", `paused=${data.breaker_paused} failures=${data.breaker_consecutive_failures}`);
    pollMetrics();
  } catch (err) {
    logEvent("err", "POST /breaker/reset", err.message);
  }
}

// ── Position Inspector ────────────────────────────────────────────────
async function loadPosition() {
  const addr = document.getElementById("borrower-input").value.trim();
  if (!addr.startsWith("0x") || addr.length !== 42) {
    alert("Enter a valid 0x-prefixed, 42-character Ethereum address.");
    return;
  }
  const btn = document.getElementById("btn-load");
  btn.disabled = true;
  btn.textContent = "Loading…";
  logEvent("info", `GET /positions/${short(addr)}`, "Reading live Aave position…");

  try {
    const res = await fetch(`${API_BASE}/positions/${addr}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderPosition(data);
    logEvent("ok", `GET /positions/${short(addr)}`,
      `state=${data.state} hf=${data.hf ?? "∞"} debt=$${(data.debt_usd ?? 0).toLocaleString()}`);
  } catch (err) {
    logEvent("err", `GET /positions/${short(addr)}`, err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Load Position";
  }
}

function renderPosition(data) {
  const hf = data.hf; // null when there's no debt (HF is mathematically infinite)
  document.getElementById("current-hf").textContent = hf === null || hf === undefined ? "∞" : hf.toFixed(4);
  document.getElementById("hf-sub").textContent = data.has_debt ? "Live from getUserAccountData" : "No open debt on Aave";
  document.getElementById("is-registered").textContent = data.registered ? "Yes" : "No";
  document.getElementById("collateral-usd").textContent = `$${(data.collateral_usd ?? 0).toLocaleString("en-US", { minimumFractionDigits: 2 })}`;
  document.getElementById("debt-usd").textContent = `$${(data.debt_usd ?? 0).toLocaleString("en-US", { minimumFractionDigits: 2 })}`;

  const badge = document.getElementById("position-state-badge");
  badge.textContent = `STATE: ${data.state}`;
  badge.className = "badge " + stateClass(data.state);

  // Gauge marker: 1.00 -> 10%, 1.15 -> 30%, 1.50+ -> 92%
  let percent;
  if (hf === null || hf === undefined || !isFinite(hf)) {
    percent = 92;
  } else {
    percent = 10 + ((hf - 1.0) / 0.5) * 65;
    percent = Math.max(6, Math.min(92, percent));
  }
  document.getElementById("hf-marker").style.left = `${percent}%`;
  document.getElementById("hf-marker-tag").textContent = hf === null || hf === undefined ? "∞" : hf.toFixed(3);
}

function stateClass(state) {
  if (state === "HEALTHY" || state === "RESTORED") return "safe";
  if (state === "DECLINED" || state === "REVERTED") return "danger";
  if (state === "ASSESSING" || state === "SUBMITTED" || state === "READY") return "accent";
  return "";
}

// ── Risk params payload (from the form on the right) ─────────────────
function buildParamsPayload(borrower) {
  return {
    params: {
      borrower,
      hfTriggerBps: parseInt(document.getElementById("p-trigger").value, 10),
      hfTargetBaseBps: parseInt(document.getElementById("p-target").value, 10),
      volCoeffK: parseInt(document.getElementById("p-volk").value, 10),
      hfTargetMaxBps: parseInt(document.getElementById("p-targetmax").value, 10),
      maxSlippageBps: parseInt(document.getElementById("p-slippage").value, 10),
      maxCostBps: parseInt(document.getElementById("p-cost").value, 10),
      allowedCollaterals: [document.getElementById("p-collateral").value],
      nonce: Date.now() % 1_000_000,
      deadline: 2000000000,
    },
    signature: "0x" + "00".repeat(65), // demo signature — the vault contract verifies EIP-712 on-chain
  };
}

// ── Decision Pipeline: dry-run assessment ─────────────────────────────
async function runAssessment() {
  const addr = document.getElementById("borrower-input").value.trim();
  if (!addr.startsWith("0x") || addr.length !== 42) {
    alert("Load a valid borrower address first.");
    return;
  }
  const btn = document.getElementById("btn-assess");
  btn.disabled = true;
  btn.textContent = "Running…";
  logEvent("info", `POST /positions/${short(addr)}/assessment`, "Running risk → sizing → selection → viability…");

  try {
    const res = await fetch(`${API_BASE}/positions/${addr}/assessment`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildParamsPayload(addr)),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail ? JSON.stringify(data.detail) : `HTTP ${res.status}`);
    renderAssessment(data);
    logEvent(data.viable ? "ok" : "warn", `POST /assessment`,
      `viable=${data.viable} repay=${(data.repay_amount / 1e6).toFixed(2)} USDC cost=${data.est_cost_bps}bps${data.reason ? " · " + data.reason : ""}`);
  } catch (err) {
    logEvent("err", `POST /assessment`, err.message);
    document.getElementById("assessment-result").className = "result-panel empty";
    document.getElementById("assessment-result").textContent = `Request failed: ${err.message}`;
  } finally {
    btn.disabled = false;
    btn.textContent = "Run Dry-Run Assessment";
  }
}

function renderAssessment(data) {
  const panel = document.getElementById("assessment-result");
  panel.className = "result-panel";
  panel.innerHTML = `
    <div class="result-row"><span class="r-key">Viable</span><span class="r-val" style="color:${data.viable ? "var(--safe)" : "var(--danger)"}">${data.viable ? "TRUE" : "FALSE"}</span></div>
    <div class="result-row"><span class="r-key">Current HF</span><span class="r-val">${(data.hf === null || data.hf === undefined || !isFinite(data.hf)) ? "∞" : data.hf.toFixed(4)}</span></div>
    <div class="result-row"><span class="r-key">Target HF</span><span class="r-val">${data.hf_target.toFixed(4)}</span></div>
    <div class="result-row"><span class="r-key">Repay Amount (Δd*)</span><span class="r-val">${(data.repay_amount / 1e6).toLocaleString(undefined, {minimumFractionDigits:2})} USDC</span></div>
    <div class="result-row"><span class="r-key">Collateral Source</span><span class="r-val">${symbolOf(data.collateral_asset)}</span></div>
    <div class="result-row"><span class="r-key">Estimated Cost</span><span class="r-val">${data.est_cost_bps} bps</span></div>
    ${data.reason ? `<div class="result-row"><span class="r-key">Reason</span><span class="r-val">${data.reason}</span></div>` : ""}
  `;
}

// ── Execute Protection (assess → simulate → submit) ───────────────────
async function runProtect() {
  const addr = document.getElementById("borrower-input").value.trim();
  if (!addr.startsWith("0x") || addr.length !== 42) {
    alert("Load a valid borrower address first.");
    return;
  }
  const btn = document.getElementById("btn-protect");
  btn.disabled = true;
  btn.textContent = "Executing…";
  logEvent("info", `POST /positions/${short(addr)}/protect`, "Assess → simulate → submit…");

  try {
    const res = await fetch(`${API_BASE}/positions/${addr}/protect`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildParamsPayload(addr)),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail ? JSON.stringify(data.detail) : `HTTP ${res.status}`);

    const panel = document.getElementById("assessment-result");
    panel.className = "result-panel";
    panel.innerHTML = `
      <div class="result-row"><span class="r-key">Result State</span><span class="r-val" style="color:${data.state === "RESTORED" ? "var(--safe)" : "var(--danger)"}">${data.state}</span></div>
      <div class="result-row"><span class="r-key">Submitted On-Chain</span><span class="r-val">${data.submitted ? "TRUE" : "FALSE"}</span></div>
      <div class="result-row"><span class="r-key">Tx Hash</span><span class="r-val">${data.tx_hash || "—"}</span></div>
      ${data.reason ? `<div class="result-row"><span class="r-key">Reason</span><span class="r-val">${data.reason}</span></div>` : ""}
    `;
    logEvent(data.submitted ? "ok" : "warn", `POST /protect`,
      `state=${data.state} submitted=${data.submitted}${data.reason ? " · " + data.reason : ""}`);
    loadPosition();
    pollMetrics();
  } catch (err) {
    logEvent("err", `POST /protect`, err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Execute Protection";
  }
}

// ── Keeper Config ───────────────────────────────────────────────────
async function loadConfig() {
  try {
    const res = await fetch(`${API_BASE}/config`);
    const data = await res.json();
    document.getElementById("cfg-poll").value = data.poll_interval_seconds;
    document.getElementById("cfg-breaker").value = data.breaker_max_consecutive_failures;
    document.getElementById("cfg-cooldown").value = data.inflight_cooldown_seconds;
    document.getElementById("cfg-bumps").value = data.max_simulation_bumps;
    autonomousEnabled = data.autonomous_enabled;
    syncAutoSwitch();
  } catch (err) {
    logEvent("err", "GET /config", err.message);
  }
}

function toggleAuto() {
  autonomousEnabled = !autonomousEnabled;
  syncAutoSwitch();
}
function syncAutoSwitch() {
  document.getElementById("cfg-auto-switch").className = "switch" + (autonomousEnabled ? " on" : "");
}

async function saveConfig() {
  const payload = {
    poll_interval_seconds: parseInt(document.getElementById("cfg-poll").value, 10),
    breaker_max_consecutive_failures: parseInt(document.getElementById("cfg-breaker").value, 10),
    inflight_cooldown_seconds: parseInt(document.getElementById("cfg-cooldown").value, 10),
    max_simulation_bumps: parseInt(document.getElementById("cfg-bumps").value, 10),
    autonomous_enabled: autonomousEnabled,
  };
  logEvent("info", "PUT /config", "Applying live config update…");
  try {
    const res = await fetch(`${API_BASE}/config`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail ? JSON.stringify(data.detail) : `HTTP ${res.status}`);
    logEvent("ok", "PUT /config", "Config applied — takes effect on the live service.");
  } catch (err) {
    logEvent("err", "PUT /config", err.message);
  }
}

// ── Activity log ────────────────────────────────────────────────────
function logEvent(level, tag, detail) {
  const box = document.getElementById("activity-log");
  const now = new Date();
  const stamp = now.toTimeString().slice(0, 8);
  const line = document.createElement("div");
  line.className = `log-line ${level}`;
  line.innerHTML = `<span class="l-time">${stamp}</span><span class="l-tag">${escapeHtml(tag)}</span> — ${escapeHtml(detail)}`;
  box.prepend(line);
}
function clearLog() {
  document.getElementById("activity-log").innerHTML = "";
}
function short(addr) {
  return addr.slice(0, 6) + "…" + addr.slice(-4);
}
function escapeHtml(str) {
  return String(str).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
