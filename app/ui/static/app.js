const $ = (id) => document.getElementById(id);

function fmt(x, n=2) {
  if (x === null || x === undefined) return "-";
  const v = Number(x);
  if (Number.isNaN(v)) return String(x);
  return v.toFixed(n);
}

function pillStatus(status) {
  let cls = "pill";
  if (status === "recommended") cls += " good";
  else if (status === "blocked") cls += " bad";
  else cls += " warn";
  return `<span class="${cls}">${status}</span>`;
}

function showModal(title, obj) {
  $("modalTitle").textContent = title;
  $("modalBody").textContent = JSON.stringify(obj, null, 2);
  $("modal").classList.remove("hidden");
}

function hideModal() {
  $("modal").classList.add("hidden");
}

async function loadRecommendations() {
  const venue = $("venue").value;
  const topN = Number($("topN").value || 20);
  const minConf = Number($("minConf").value || 0);

  const qs = new URLSearchParams();
  if (venue) qs.set("venue", venue);
  qs.set("top_n", String(topN));
  qs.set("min_conf", String(minConf));

  const res = await fetch(`/api/v1/recommendations?${qs.toString()}`);
  const data = await res.json();

  const regime = data.regime || {};
  $("regime").textContent = `Regime: ${regime.risk_state || "?"} | vol=${regime.vol_state || "?"} | trend=${regime.trend_state || "?"}`;

  const body = $("recoBody");
  body.innerHTML = "";

  const items = data.items || [];
  let hasRecommended = false;

  items.forEach((it, i) => {
    if (it.status === "recommended") hasRecommended = true;
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${i+1}</td>
      <td>${it.venue}</td>
      <td><b>${it.symbol}</b></td>
      <td>${it.bot_type}</td>
      <td>${it.direction}</td>
      <td>${fmt(it.score)}</td>
      <td>${fmt(it.confidence)}</td>
      <td>${fmt(it.expected_rr)}</td>
      <td>${pillStatus(it.status)}</td>
      <td>
        <button class="btn tiny" data-act="details" data-id="${it.rec_id}">Details</button>
        <button class="btn tiny secondary" data-act="dry" data-id="${it.rec_id}">Dry</button>
        <button class="btn tiny secondary" data-act="prod" data-id="${it.rec_id}">Prod</button>
      </td>
    `;
    body.appendChild(tr);
  });

  const banner = $("noTrade");
  if (!hasRecommended) banner.classList.remove("hidden");
  else banner.classList.add("hidden");
}

async function loadDetails(recId) {
  const res = await fetch(`/api/v1/recommendations/${recId}`);
  const it = await res.json();

  const reasons = it.reasons || {};
  const blocks = it.blocks || [];
  const params = it.params || {};

  const lines = [];
  lines.push(`rec_id: ${it.rec_id}`);
  lines.push(`venue/symbol: ${it.venue} ${it.symbol}`);
  lines.push(`bot: ${it.bot_type} | dir=${it.direction} | mode=${it.account_mode}/${it.margin_mode}`);
  lines.push(`score=${fmt(it.score)} conf=${fmt(it.confidence)} expRR=${fmt(it.expected_rr)} riskScore=${fmt(it.risk_score)}`);
  lines.push(`status=${it.status}`);
  lines.push("");
  lines.push("WHY:");
  lines.push(reasons.summary || "-");
  lines.push("");
  lines.push("Top + factors:");
  (reasons.top_positive_factors || []).forEach(f => lines.push(`  + ${f.text} (${f.feature}=${fmt(f.value,4)})`));
  lines.push("Top - factors:");
  (reasons.top_negative_factors || []).forEach(f => lines.push(`  - ${f.text} (${f.feature}=${fmt(f.value,4)})`));
  lines.push("");
  lines.push("Cost model:");
  lines.push(JSON.stringify(reasons.cost_model || {}, null, 2));
  lines.push("");
  lines.push("Risk checks / blocks:");
  if (!blocks.length) lines.push("  OK");
  else blocks.forEach(b => lines.push(`  - ${b.code}: ${b.msg}`));
  lines.push("");
  lines.push("Suggested params (editable via activate override):");
  lines.push(JSON.stringify(params, null, 2));

  $("details").textContent = lines.join("\n");
}

async function activate(recId, dryRun) {
  const body = {
    rec_id: recId,
    dry_run: dryRun,
    override_params: {},
    operator: "operator"
  };
  const res = await fetch("/api/v1/bots/activate", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)
  });
  const data = await res.json();
  if (!res.ok) {
    showModal("Activate failed", data);
    return;
  }
  showModal("Bot activated", data);
}

async function loadBots() {
  const res = await fetch("/api/v1/bots");
  const data = await res.json();
  showModal("Bots", data);
}

async function loadRisk() {
  const res = await fetch("/api/v1/risk/status");
  const data = await res.json();
  showModal("Risk status", data);
}

document.addEventListener("click", async (e) => {
  const t = e.target;
  if (t && t.dataset && t.dataset.act) {
    const act = t.dataset.act;
    const id = t.dataset.id;
    if (act === "details") await loadDetails(id);
    if (act === "dry") await activate(id, true);
    if (act === "prod") await activate(id, false);
  }
});

$("refreshBtn").addEventListener("click", loadRecommendations);
$("loadBotsBtn").addEventListener("click", loadBots);
$("riskBtn").addEventListener("click", loadRisk);
$("modalClose").addEventListener("click", hideModal);
$("modal").addEventListener("click", (e) => { if (e.target.id === "modal") hideModal(); });

loadRecommendations();
setInterval(loadRecommendations, 5000);
