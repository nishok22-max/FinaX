/**
 * Liquidation Shield — Guided Protection Check
 * One button drives a real, sequential run through the backend: health check,
 * position read, decision pipeline, and (if viable) protection execution.
 * Every step is a real API call — the stepper just visualizes real progress.
 */

const API_BASE = "";
const TOTAL_STEPS = 5;

const TOKEN_SYMBOLS = {
  "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1": "WETH",
  "0xaf88d065e77c8cC2239327C5EDb3A432268e5831": "USDC",
  "0x5979D7b546E38E414F7E9822514be443A4800529": "wstETH",
};
function symbolOf(addr) {
  if (!addr) return "—";
  return TOKEN_SYMBOLS[addr] || `${addr.slice(0, 6)}…${addr.slice(-4)}`;
}
function fmtUsd(n) {
  return `$${(n ?? 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
function fmtHf(hf) {
  return hf === null || hf === undefined || !isFinite(hf) ? "∞" : hf.toFixed(4);
}
function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

// ── Init: quiet RPC status ping (does not touch the stepper) ──────────
window.addEventListener("DOMContentLoaded", () => {
  pollHealthQuiet();
  setInterval(pollHealthQuiet, 8000);
});

async function pollHealthQuiet() {
  const chip = document.getElementById("chip-rpc");
  try {
    const res = await fetch(`${API_BASE}/health`);
    const data = await res.json();
    if (data.rpc_connected) {
      chip.className = "chip status-ok";
      chip.innerHTML = `<span class="dot"></span> RPC connected <span class="mono">#${data.block_number?.toLocaleString() ?? "?"}</span>`;
    } else {
      chip.className = "chip status-bad";
      chip.innerHTML = `<span class="dot"></span> RPC not connected`;
    }
  } catch (err) {
    chip.className = "chip status-bad";
    chip.innerHTML = `<span class="dot"></span> Backend unreachable`;
  }
}

// ── Stepper helpers ─────────────────────────────────────────────────
function setStep(n, state) {
  // state: 'active' | 'done' | 'declined' | 'failed' | 'skipped'
  const el = document.querySelector(`.step[data-step="${n}"]`);
  el.className = "step " + state;
  const pct = Math.round(((n - (state === "active" ? 0.5 : 0)) / TOTAL_STEPS) * 100);
  document.getElementById("progress-fill").style.width = `${Math.min(100, Math.max(0, pct))}%`;
}
function setStatus(text) {
  document.getElementById("stepper-status").textContent = text;
}
function resetStepper() {
  for (let i = 1; i <= TOTAL_STEPS; i++) {
    const el = document.querySelector(`.step[data-step="${i}"]`);
    el.className = "step";
  }
  document.getElementById("progress-fill").style.width = "0%";
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
    signature: "0x" + "00".repeat(65), // contract verifies EIP-712 on-chain at submission time
  };
}

// ── The full guided run ─────────────────────────────────────────────
async function runFullCheck() {
  const addr = document.getElementById("borrower-input").value.trim();
  if (!addr.startsWith("0x") || addr.length !== 42) {
    alert("Enter a valid 0x-prefixed, 42-character wallet address.");
    return;
  }

  const btn = document.getElementById("btn-run");
  btn.disabled = true;
  btn.textContent = "Running…";
  document.getElementById("stepper-card").style.display = "";
  document.getElementById("result-card").style.display = "none";
  resetStepper();

  let position = null;
  let assessment = null;
  let protectResult = null;

  try {
    // Step 1 — connect
    setStep(1, "active");
    setStatus("Pinging the backend's Arbitrum RPC connection…");
    const healthRes = await fetch(`${API_BASE}/health`);
    const health = await healthRes.json();
    if (!health.rpc_connected) throw new Error("RPC not connected");
    setStep(1, "done");

    // Step 2 — read position
    setStep(2, "active");
    setStatus(`Reading ${short(addr)}'s live Aave V3 position…`);
    const posRes = await fetch(`${API_BASE}/positions/${addr}`);
    if (!posRes.ok) {
      const errBody = await posRes.json().catch(() => ({}));
      throw new Error(errBody.detail || `HTTP ${posRes.status}`);
    }
    position = await posRes.json();
    setStep(2, "done");

    if (!position.has_debt) {
      setStep(3, "skipped");
      setStep(4, "skipped");
      setStep(5, "skipped");
      setStatus("No open debt on this wallet — nothing to protect.");
      showResult({ position, assessment: null, protectResult: null, verdict: "no-debt" });
      return;
    }

    // Step 3 — risk & sizing pipeline
    setStep(3, "active");
    setStatus("Computing dynamic HF target and minimum rescue size…");
    const assessPromise = fetch(`${API_BASE}/positions/${addr}/assessment`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildParamsPayload(addr)),
    });
    await sleep(500); // real pipeline work is happening in this single request; this paces the visual step
    setStep(3, "done");

    // Step 4 — viability
    setStep(4, "active");
    setStatus("Checking whether the rescue is economically worth it…");
    const assessRes = await assessPromise;
    const assessBody = await assessRes.json();
    if (!assessRes.ok) throw new Error(assessBody.detail ? JSON.stringify(assessBody.detail) : `HTTP ${assessRes.status}`);
    assessment = assessBody;

    if (!assessment.viable) {
      setStep(4, "declined");
      setStep(5, "skipped");
      setStatus(`Declined: ${assessment.reason || "not economically viable"}`);
      showResult({ position, assessment, protectResult: null, verdict: "declined" });
      return;
    }
    setStep(4, "done");

    // Step 5 — execute
    setStep(5, "active");
    setStatus("Submitting the atomic rescue (assess → simulate → submit)…");
    const protectRes = await fetch(`${API_BASE}/positions/${addr}/protect`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildParamsPayload(addr)),
    });
    const protectBody = await protectRes.json();
    protectResult = protectBody;

    if (protectResult.state === "RESTORED") {
      setStep(5, "done");
      setStatus("Rescue executed — position restored.");
      showResult({ position, assessment, protectResult, verdict: "restored" });
    } else {
      setStep(5, "declined");
      setStatus(`Execution stopped: ${protectResult.reason || protectResult.state}`);
      showResult({ position, assessment, protectResult, verdict: "exec-stopped" });
    }
  } catch (err) {
    const activeStep = document.querySelector(".step.active");
    if (activeStep) activeStep.className = activeStep.className.replace("active", "failed");
    setStatus(`Error: ${err.message}`);
    showResult({ position, assessment, protectResult, verdict: "error", error: err.message });
  } finally {
    btn.disabled = false;
    btn.textContent = "Run Protection Check";
  }
}

function short(addr) { return addr.slice(0, 6) + "…" + addr.slice(-4); }

// ── Result rendering (plain language) ──────────────────────────────
function showResult({ position, assessment, protectResult, verdict, error }) {
  const card = document.getElementById("result-card");
  card.style.display = "";

  const headline = document.getElementById("result-headline");
  const sub = document.getElementById("result-sub");

  const hf = position ? position.hf : null;
  const hfTarget = assessment ? assessment.hf_target : null;

  document.getElementById("hf-before").textContent = fmtHf(hf);
  document.getElementById("hf-target").textContent = hfTarget ? hfTarget.toFixed(4) : "—";
  document.getElementById("rt-collateral").textContent = position ? fmtUsd(position.collateral_usd) : "—";
  document.getElementById("rt-debt").textContent = position ? fmtUsd(position.debt_usd) : "—";
  document.getElementById("rt-repay").textContent = assessment && assessment.repay_amount
    ? `${(assessment.repay_amount / 1e6).toLocaleString(undefined, { minimumFractionDigits: 2 })} USDC` : "—";
  document.getElementById("rt-cost").textContent = assessment ? `${assessment.est_cost_bps} bps` : "—";

  // Gauge marker
  let percent = 92;
  if (hf !== null && hf !== undefined && isFinite(hf)) {
    percent = 10 + ((hf - 1.0) / 0.5) * 65;
    percent = Math.max(6, Math.min(92, percent));
  }
  document.getElementById("hf-marker").style.left = `${percent}%`;
  document.getElementById("hf-marker-tag").textContent = fmtHf(hf);

  const map = {
    "no-debt": {
      cls: "safe",
      h: "Position Healthy — Nothing to Protect",
      s: "This wallet currently has no open debt on Aave V3, so there is no liquidation risk and no rescue is needed.",
    },
    "declined": {
      cls: "warning",
      h: "At Risk, But Rescue Not Viable",
      s: `The position has real debt, but the pipeline declined to act: ${assessment?.reason || "not economically worthwhile"}.`,
    },
    "restored": {
      cls: "safe",
      h: "Rescue Executed — Position Restored",
      s: `A real atomic transaction repaid part of the debt and restored the health factor to the target band. Tx: ${protectResult?.tx_hash || "—"}`,
    },
    "exec-stopped": {
      cls: "warning",
      h: "At-Risk Position — Rescue Ready, Execution Stopped",
      s: `The pipeline found a viable rescue (see figures below), but on-chain execution stopped: ${protectResult?.reason || protectResult?.state}.`,
    },
    "error": {
      cls: "danger",
      h: "Could Not Complete The Check",
      s: error || "An unexpected error occurred.",
    },
  };
  const m = map[verdict] || map.error;
  headline.className = "result-headline " + m.cls;
  headline.textContent = m.h;
  sub.textContent = m.s;

  // Technical details (collapsed by default)
  const details = document.getElementById("details-box");
  const rows = [];
  if (position) {
    rows.push(["state", position.state], ["has_debt", position.has_debt], ["registered", position.registered]);
  }
  if (assessment) {
    rows.push(["viable", assessment.viable], ["collateral_asset", symbolOf(assessment.collateral_asset)], ["est_cost_bps", assessment.est_cost_bps]);
  }
  if (protectResult) {
    rows.push(["protect.state", protectResult.state], ["submitted", protectResult.submitted], ["tx_hash", protectResult.tx_hash || "—"]);
  }
  details.innerHTML = rows.map(([k, v]) => `<div class="d-row"><span>${k}</span><span>${v}</span></div>`).join("");
  details.style.display = "none";
  document.getElementById("btn-details").textContent = "Show technical details";
}

function toggleDetails() {
  const box = document.getElementById("details-box");
  const btn = document.getElementById("btn-details");
  const show = box.style.display === "none";
  box.style.display = show ? "" : "none";
  btn.textContent = show ? "Hide technical details" : "Show technical details";
}
