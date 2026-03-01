const $ = (id) => document.getElementById(id);

let recoAbort = null;
let recoDebounce = null;


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
  const showRecommended = $("showRecommended")?.checked ?? true;
  const showBlocked = $("showBlocked")?.checked ?? false;
  const showNoTrade = $("showNoTrade")?.checked ?? false;
  const showSuppressed = $("showSuppressed")?.checked ?? false;

  if (venue) qs.set("venue", venue);
  qs.set("top_n", String(topN));
  qs.set("min_conf", String(minConf));
  qs.set("show_recommended", String(showRecommended));
  qs.set("show_blocked", String(showBlocked));
  qs.set("show_no_trade", String(showNoTrade));
  qs.set("show_suppressed", String(showSuppressed));
  qs.set("snapshot", "latest");

  if (recoAbort) { try { recoAbort.abort(); } catch(e) {} }
  recoAbort = new AbortController();
  const res = await fetch(`/api/v1/recommendations?${qs.toString()}`, { signal: recoAbort.signal });
  let data;
  try { data = await res.json(); } catch(e) { return; }

  const regime = data.regime || {};
  $("regime").textContent = `Режим: ${regime.risk_state || "?"} | vol=${regime.vol_state || "?"} | trend=${regime.trend_state || "?"}`;

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
        <button class="btn tiny" data-act="details" data-id="${it.rec_id}">Детали</button>
        <button class="btn tiny secondary" data-act="json" data-id="${it.rec_id}">JSON</button>
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
  lines.push("ПОЧЕМУ:");
  lines.push("Подсказка: детальный multi-horizon сентимент смотрите в JSON -> reasons.sentiment_agg. Консенсус направления: reasons.direction_agg.");
  lines.push(reasons.summary || "-");
  lines.push("");
  lines.push("Факторы +:");
  (reasons.top_positive_factors || []).forEach(f => lines.push(`  + ${f.text} (${f.feature}=${fmt(f.value,4)})`));
  lines.push("Факторы -:");
  (reasons.top_negative_factors || []).forEach(f => lines.push(`  - ${f.text} (${f.feature}=${fmt(f.value,4)})`));
  lines.push("");
  lines.push("Стоимость исполнения (bps):");
  lines.push(JSON.stringify(reasons.cost_model || {}, null, 2));
  lines.push("");
  lines.push("Риск-гейты / блокировки:");
  if (!blocks.length) lines.push("  OK");
  else blocks.forEach(b => lines.push(`  - ${b.code}: ${b.msg}`));
  lines.push("");
  lines.push("Параметры для Bybit (копируйте в UI):");
  lines.push("Подсказка: Grid — если в сильном флете, режим обычно Нейтральный; direction_bias показывает смещение. Диапазон: price_range_lower/price_range_upper.");
  lines.push(JSON.stringify(params, null, 2));

  $("details").textContent = lines.join("\n");
}


async function loadDecisions() {
  const res = await fetch("/api/v1/decisions?limit=200");
  let data;
  try { data = await res.json(); } catch(e) { return; }
  showModal("Журнал решений", data);
}

async function loadRisk() {
  const res = await fetch("/api/v1/risk/status");
  let data;
  try { data = await res.json(); } catch(e) { return; }
  showModal("Risk status", data);
}

document.addEventListener("click", async (e) => {
  const t = e.target;
  if (t && t.dataset && t.dataset.act) {
    const act = t.dataset.act;
    const id = t.dataset.id;
    if (act === "details") await loadDetails(id);
    if (act === "json") {
      const res = await fetch(`/api/v1/recommendations/${id}`);
      let data;
  try { data = await res.json(); } catch(e) { return; }
      showModal("Recommendation JSON", data);
    }
      }
});

$("refreshBtn").addEventListener("click", loadRecommendations);
$("decisionsBtn").addEventListener("click", loadDecisions);
$("riskBtn").addEventListener("click", loadRisk);
$("modalClose").addEventListener("click", (e) => { e.stopPropagation(); hideModal(); });
$("modal").addEventListener("click", (e) => { if (e.target.id === "modal") hideModal(); });

loadRecommendations();
setInterval(loadRecommendations, 5000);

['venue','topN','minConf'].forEach(id => { const el = $(id); if (el) el.addEventListener('input', () => { if (recoDebounce) clearTimeout(recoDebounce); recoDebounce = setTimeout(loadRecommendations, 200); }); });
