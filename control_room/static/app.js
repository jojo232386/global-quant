const $ = (id) => document.getElementById(id);
const text = (id, value) => { $(id).textContent = value ?? "—"; };
const shortSha = (value) => value ? String(value).slice(0, 10) : "—";
const numeric = (value) => value === null || value === undefined ? "—" : String(value);
const percent = (value) => value === null || value === undefined ? "—" : `${(Number(value) * 100).toFixed(2)}%`;

function setPill(element, verdict) {
  const value = String(verdict || "UNKNOWN").toUpperCase();
  element.textContent = value;
  element.className = "pill " + (
    ["HEALTHY", "MATCH", "VERIFIED", "DISARMED", "PASS"].includes(value) ? "good" :
    ["FAIL", "BROKEN", "UNHEALTHY", "MISMATCH", "ARMED"].includes(value) ? "bad" :
    ["BLOCKED_NO_CURRENT_PASS", "BLOCKED_UNVERIFIED_ARTIFACTS", "REJECT"].includes(value) ? "blocked" : "neutral"
  );
}

function renderChecks(checks) {
  const root = $("healthChecks");
  root.replaceChildren();
  (checks || []).forEach((check) => {
    const row = document.createElement("div");
    row.className = "check-row";
    const dot = document.createElement("span");
    dot.className = `check-dot ${check.passed ? "pass" : ""}`;
    const name = document.createElement("strong");
    name.textContent = check.name;
    const detail = document.createElement("span");
    detail.className = "check-detail";
    detail.textContent = check.detail === null ? "unknown" : `${check.detail}${check.unit ? ` ${check.unit}` : ""}`;
    row.append(dot, name, detail);
    root.append(row);
  });
}

function renderRisk(reconcile, audit, gate) {
  setPill($("reconcilePill"), reconcile.verdict);
  text("riskTrades", numeric(reconcile.open_trades));
  text("riskOrders", numeric(reconcile.open_orders));
  text("partialOrders", numeric(reconcile.partial_orders));
  text("unknownOutcomes", (reconcile.unknown_outcomes || []).length);
  const conditions = [
    ["REST 与 SQLite 身份一致", reconcile.matches_database === true],
    ["开放订单为零", reconcile.open_orders === 0],
    ["部分成交为零", reconcile.partial_orders === 0],
    ["未知结果为零", (reconcile.unknown_outcomes || []).length === 0],
    ["审计哈希链完整", audit.verdict === "VERIFIED"],
    ["入场闸门保持关闭", gate.state === "DISARMED"],
  ];
  const root = $("riskMatrix");
  root.replaceChildren();
  conditions.forEach(([label, passed]) => {
    const item = document.createElement("div");
    item.className = "risk-condition";
    const name = document.createElement("span");
    name.textContent = label;
    const state = document.createElement("strong");
    state.className = passed ? "pass" : "";
    state.textContent = passed ? "PASS" : "STOP";
    item.append(name, state);
    root.append(item);
  });
}

function renderResearch(research) {
  const counts = research.counts || {};
  text("passCount", research.unverified_pass_count || 0);
  text("rejectCount", counts.REJECT || 0);
  text("inconclusiveCount", counts.INCONCLUSIVE || 0);
  const currentPass = Number(research.current_pass_count || 0);
  text("currentPassCount", currentPass);
  text("promotionVerdict", currentPass ? "SHADOW ONLY" : "BLOCKED");
  setPill($("researchPill"), research.promotion_verdict);
  if (research.promotion_verdict === "BLOCKED_UNVERIFIED_ARTIFACTS") {
    $("researchPill").textContent = "无可验证 PASS";
  } else if (research.promotion_verdict === "BLOCKED_NO_CURRENT_PASS") {
    $("researchPill").textContent = "无当前 PASS";
  } else if (research.promotion_verdict === "RESEARCH_PASS_SHADOW_ONLY") {
    $("researchPill").textContent = "仅可进入影子验证";
  }
  const root = $("researchRows");
  root.replaceChildren();
  (research.studies || []).forEach((study) => {
    const row = document.createElement("tr");
    const fields = [
      study.study_id,
      study.verdict,
      study.evidence_generation === "VERIFIED_CURATED_V1" ? "V1 已验证" :
        study.evidence_generation === "COST_MODEL_MATCH_ONLY" ? "仅成本哈希匹配" : "未验证产物",
      percent(study.total_return),
      numeric(study.sharpe),
      numeric(study.trade_count),
    ];
    fields.forEach((value, index) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      if (index === 1) cell.className = `table-verdict ${String(value).toLowerCase()}`;
      row.append(cell);
    });
    root.append(row);
  });
}

function renderEvidence(evidence, blockers) {
  const timeline = $("evidenceList");
  timeline.replaceChildren();
  (evidence || []).forEach((item) => {
    const row = document.createElement("div");
    row.className = "timeline-item";
    const title = document.createElement("strong");
    title.textContent = `${item.verdict} · ${shortSha(item.candidate_sha)}`;
    const meta = document.createElement("span");
    meta.textContent = item.name;
    row.append(title, meta);
    timeline.append(row);
  });
  if (!evidence?.length) {
    const empty = document.createElement("p");
    empty.className = "panel-note";
    empty.textContent = "尚无可读取的证据包。";
    timeline.append(empty);
  }
  text("blockerCount", blockers?.length || 0);
  const list = $("blockerList");
  list.replaceChildren();
  (blockers || []).forEach((blocker) => {
    const item = document.createElement("li");
    item.textContent = blocker;
    list.append(item);
  });
}

function render(snapshot) {
  const gate = snapshot.gate || {};
  const runtime = snapshot.runtime || {};
  const health = runtime.health || {};
  const reconcile = runtime.reconciliation || {};
  const audit = snapshot.audit || {};
  const config = snapshot.config || {};
  const repo = snapshot.repo || {};
  const safeWaiting = gate.state === "DISARMED"
    && gate.identity_verdict === "VERIFIED"
    && health.verdict === "HEALTHY"
    && reconcile.verdict === "MATCH"
    && audit.verdict === "VERIFIED"
    && repo.clean === true
    && config.credential_free === true;

  text("updatedAt", `更新于 ${snapshot.captured_at_utc || "—"}`);
  text("systemHeadline", safeWaiting ? "安全待命" : gate.state === "ARMED" ? "授权运行中" : "需要检查");
  text("systemSummary", safeWaiting ? "运行时健康，交易账本一致，最终入场闸门保持关闭。" : "至少一项运行、风险或 Gate 证据未达到安全待命条件。");
  setPill($("gatePill"), gate.state);
  text("candidateSha", shortSha(gate.candidate_sha || repo.sha));
  text("runId", gate.run_id);
  text("environment", gate.environment);
  text("healthVerdict", health.verdict);
  const heartbeat = (health.checks || []).find((item) => item.name === "Heartbeat");
  text("heartbeat", heartbeat?.detail === null || heartbeat?.detail === undefined ? "heartbeat unknown" : `heartbeat ${heartbeat.detail}s`);
  text("openTrades", numeric(reconcile.open_trades));
  text("openOrders", numeric(reconcile.open_orders));
  text("auditVerdict", audit.verdict);
  text("auditRecords", audit.records === null || audit.records === undefined ? "记录未知" : `${audit.records} 条记录`);

  text("branch", repo.branch);
  text("repoClean", repo.clean ? "CLEAN" : "DIRTY");
  $("repoClean").style.color = repo.clean ? "var(--teal)" : "var(--red)";
  text("tradingMode", config.trading_mode);
  text("marginMode", config.margin_mode);
  text("pairs", (config.pairs || []).join(", "));
  text("stakeAmount", `${numeric(config.stake_amount)} ${config.stake_currency || ""}`.trim());
  text("credentialState", config.credential_free ? "配置无交易所凭据" : "STOP：发现凭据字段");

  renderChecks(health.checks);
  renderRisk(reconcile, audit, gate);
  renderResearch(snapshot.research || {});
  renderEvidence(snapshot.evidence || [], snapshot.blockers || []);

  const scoreParts = [
    health.verdict === "HEALTHY",
    reconcile.verdict === "MATCH",
    audit.verdict === "VERIFIED",
    snapshot.research?.promotion_verdict === "ELIGIBLE",
  ];
  const score = scoreParts.filter(Boolean).length;
  text("scoreValue", score);
  $("scoreRing").style.setProperty("--score-angle", `${score * 90}deg`);
  text("scoreTitle", score === 4 ? "具备晋级证据" : `${score}/4 项成立`);
  text("scoreCopy", scoreParts[3] ? "执行与策略证据均成立" : "执行底座可用，策略资格仍阻断");
}

async function loadSnapshot() {
  $("refreshButton").disabled = true;
  try {
    const response = await fetch("/api/snapshot", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
    $("errorBanner").classList.add("hidden");
  } catch (error) {
    $("errorBanner").textContent = `无法读取控制室状态：${error.message}`;
    $("errorBanner").classList.remove("hidden");
  } finally {
    $("refreshButton").disabled = false;
  }
}

document.querySelectorAll(".nav-item").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((item) => item.classList.remove("active"));
    document.querySelectorAll(".page").forEach((page) => page.classList.remove("active"));
    button.classList.add("active");
    $(button.dataset.target).classList.add("active");
  });
});

$("refreshButton").addEventListener("click", loadSnapshot);
loadSnapshot();
setInterval(loadSnapshot, 15000);
