const $ = (id) => document.getElementById(id);

let recoAbort = null;
let recoDebounce = null;
let calibFitted = false;
let countdownTimer = null;
let countdownVal = 10;
let currentRecId = null;   // rec_id currently shown in Details panel
let currentMeta  = null;   // {venue, symbol, bot_type} — used to find fresh rec_id on refresh

// ── helpers ──────────────────────────────────────────────────────────────────

function fmt(x, n = 2) {
  if (x === null || x === undefined) return "-";
  const v = Number(x);
  if (Number.isNaN(v)) return String(x);
  return v.toFixed(n);
}

function timeAgo(ts) {
  if (!ts) return "—";
  const sec = Math.floor(Date.now() / 1000) - ts;
  if (sec < 5)  return "только что";
  if (sec < 60) return `${sec}с назад`;
  if (sec < 3600) return `${Math.floor(sec/60)}м назад`;
  return `${Math.floor(sec/3600)}ч назад`;
}

function pillStatus(status) {
  let cls = "pill";
  if (status === "recommended") cls += " good";
  else if (status === "blocked") cls += " bad";
  else cls += " warn";
  return `<span class="${cls}">${status}</span>`;
}

function confCell(conf) {
  const v = Number(conf);
  if (isNaN(v)) return "-";
  let cls = "conf-val";
  if (!calibFitted) cls += " conf-uncal";
  else if (v >= 0.75) cls += " conf-high";
  else if (v >= 0.60) cls += " conf-mid";
  else cls += " conf-low";
  const warn = !calibFitted ? " <span class='conf-warn-icon' title='Не откалибровано'>⚠</span>" : "";
  return `<span class="${cls}">${v.toFixed(2)}${warn}</span>`;
}

function dirConfCell(dirConf) {
  const v = Number(dirConf);
  if (isNaN(v)) return "-";
  let cls = "conf-val";
  if (v >= 0.75) cls += " conf-high";
  else if (v >= 0.55) cls += " conf-mid";
  else cls += " conf-low";
  return `<span class="${cls}">${v.toFixed(2)}</span>`;
}

function showModal(title, obj) {
  $("modalTitle").textContent = title;
  $("modalBody").textContent = typeof obj === "string" ? obj : JSON.stringify(obj, null, 2);
  $("modal").classList.remove("hidden");
}

function hideModal() {
  $("modal").classList.add("hidden");
}

// ── status & calibration ──────────────────────────────────────────────────────

async function loadStatus() {
  try {
    const res = await fetch("/api/v1/status");
    if (!res.ok) return;
    const s = await res.json();

    calibFitted = !!s.calibrator_fitted;
    const count = s.outcome_count || 0;
    const needed = s.calib_min_samples || 80;
    const pct = Math.min(100, Math.round(count / needed * 100));

    // calibration banner
    const banner = $("calibBanner");
    const header = $("confHeader");
    if (!calibFitted) {
      banner.classList.remove("hidden");
      $("calibProgress").textContent =
        `Накоплено ${count} / ${needed} исходов (${pct}%). Platt scaling включится автоматически.`;
      $("calibBarFill").style.width = pct + "%";
      header.textContent = "Увер ⚠";
      header.title = "Уверенность НЕ откалибрована — числа завышены";
    } else {
      banner.classList.add("hidden");
      header.textContent = "Увер ✓";
      header.title = "Уверенность откалибрована (Platt scaling)";
    }

    // collect error banner
    const errBanner = $("collectErrBanner");
    if (s.collect_errors_10m > 0) {
      errBanner.classList.remove("hidden");
      $("collectErrText").textContent =
        `${s.collect_errors_10m} ошибок сбора данных за последние 10 мин`;
    } else {
      errBanner.classList.add("hidden");
    }

    // sentiment badge
    const sent = s.sentiment || {};
    const ewma = sent.ewma_6h;
    const regime = sent.regime || "—";
    if (ewma !== null && ewma !== undefined) {
      const v = Number(ewma);
      let cls = "sentiment-badge";
      if (v >= 0.15) cls += " sent-pos";
      else if (v <= -0.15) cls += " sent-neg";
      else cls += " sent-neu";
      const flags = sent.flags || {};
      const flag = flags.panic ? " 🚨" : flags.euphoria ? " 🔥" : "";
      $("sentiment-badge").className = cls;
      $("sentiment-badge").textContent =
        `Сент: ${v >= 0 ? "+" : ""}${v.toFixed(2)} (${regime})${flag}`;
    }

    // last reco timestamp
    if (s.last_reco_ts) {
      $("lastUpdated").textContent = `Обновл: ${timeAgo(s.last_reco_ts)}`;
    }
  } catch (e) {
    // status endpoint may not be available yet
  }
}

// ── recommendations ───────────────────────────────────────────────────────────

async function loadRecommendations() {
  const venue = $("venue").value;
  const topN = Number($("topN").value || 50);
  const minConf = Number($("minConf").value || 0);

  const qs = new URLSearchParams();
  const showRecommended = $("showRecommended")?.checked ?? true;
  const showBlocked     = $("showBlocked")?.checked ?? false;
  const showNoTrade     = $("showNoTrade")?.checked ?? false;
  const showSuppressed  = $("showSuppressed")?.checked ?? false;

  if (venue) qs.set("venue", venue);
  qs.set("top_n", String(topN));
  qs.set("min_conf", String(minConf));
  qs.set("show_recommended", String(showRecommended));
  qs.set("show_blocked", String(showBlocked));
  qs.set("show_no_trade", String(showNoTrade));
  qs.set("show_suppressed", String(showSuppressed));
  qs.set("snapshot", "latest");

  if (recoAbort) { try { recoAbort.abort(); } catch (e) {} }
  recoAbort = new AbortController();

  let data;
  try {
    const res = await fetch(`/api/v1/recommendations?${qs.toString()}`, { signal: recoAbort.signal });
    data = await res.json();
  } catch (e) { return; }

  const regime = data.regime || {};
  $("regime").textContent =
    `Режим: ${regime.risk_state || "?"} | vol=${regime.vol_state || "?"} | trend=${regime.trend_state || "?"}`;

  const body = $("recoBody");
  body.innerHTML = "";

  const items = data.items || [];
  let hasRecommended = false;

  items.forEach((it, i) => {
    if (it.status === "recommended") hasRecommended = true;
    const dirAgg = (it.reasons || {}).direction_agg || {};
    const dirConf = dirAgg.direction_confidence_calibrated ?? dirAgg.direction_confidence;
    const tr = document.createElement("tr");
    if (it.status === "recommended") tr.classList.add("row-recommended");
    tr.innerHTML = `
      <td>${i + 1}</td>
      <td><span class="venue-pill venue-${it.venue}">${it.venue}</span></td>
      <td><b>${it.symbol}</b></td>
      <td><span class="bot-pill">${it.bot_type}</span></td>
      <td>${directionBadge(it.direction)}</td>
      <td>${dirConfCell(dirConf)}</td>
      <td>${fmt(it.score)}</td>
      <td>${confCell(it.confidence)}</td>
      <td>${fmt(it.expected_rr)}</td>
      <td>${pillStatus(it.status)}</td>
      <td>
        <button class="btn tiny" data-act="details" data-id="${it.rec_id}">Детали</button>
        <button class="btn tiny secondary" data-act="json" data-id="${it.rec_id}">JSON</button>
        ${it.status === "recommended" ? `
        <button class="btn tiny op-exec" data-act="execute" data-id="${it.rec_id}" title="Отметить исполненной">✓</button>
        <button class="btn tiny op-ignore" data-act="ignore" data-id="${it.rec_id}" title="Проигнорировать">✗</button>
        ` : `<span class="op-status-label op-${it.status}">${it.status}</span>`}
      </td>
    `;
    body.appendChild(tr);
  });

  const banner = $("noTrade");
  if (!hasRecommended) banner.classList.remove("hidden");
  else banner.classList.add("hidden");
}

function directionBadge(dir) {
  if (!dir || dir === "neutral") return `<span class="dir-badge dir-neu">neutral</span>`;
  if (dir === "long")  return `<span class="dir-badge dir-long">▲ long</span>`;
  if (dir === "short") return `<span class="dir-badge dir-short">▼ short</span>`;
  if (dir === "hedge") return `<span class="dir-badge dir-neu">⇅ hedge</span>`;
  return `<span class="dir-badge dir-neu">${dir}</span>`;
}

// ── details panel ─────────────────────────────────────────────────────────────

async function loadDetails(recId) {
  // Set currentRecId immediately so the Refresh button works even if fetch is slow
  currentRecId = recId;
  // currentMeta is set after successful parse so we have venue/symbol/bot_type
  const btn = $("refreshDetailsBtn");
  btn.classList.remove("hidden");
  btn.disabled = true;
  btn.textContent = "…";

  let it;
  try {
    const res = await fetch(`/api/v1/recommendations/${recId}`);
    if (!res.ok) {
      $("details").textContent = `Ошибка загрузки деталей (HTTP ${res.status}).`;
      btn.disabled = false;
      btn.textContent = "Обновить";
      return;
    }
    it = await res.json();
  } catch (e) {
    $("details").textContent = `Ошибка сети при загрузке деталей.`;
    btn.disabled = false;
    btn.textContent = "Обновить";
    return;
  }

  // Store meta so refresh can locate the fresh rec_id in the current table snapshot
  currentMeta = { venue: it.venue, symbol: it.symbol, bot_type: it.bot_type };
  // Normalise to the actual rec_id returned by the DB (arg may be stale)
  currentRecId = it.rec_id;

  btn.disabled = false;
  btn.textContent = "Обновить";

  const reasons = it.reasons || {};
  const blocks  = it.blocks  || [];
  const params  = it.params  || {};
  const dirAgg  = reasons.direction_agg  || {};
  const sentAgg = reasons.sentiment_agg || {};
  const confModel = reasons.confidence_model || {};

  const lines = [];
  lines.push(`rec_id: ${it.rec_id}`);
  lines.push(`venue/symbol: ${it.venue} ${it.symbol}`);
  lines.push(`bot: ${it.bot_type} | dir=${it.direction} | mode=${it.account_mode}/${it.margin_mode}`);
  lines.push(`score=${fmt(it.score)} conf=${fmt(it.confidence)} expRR=${fmt(it.expected_rr)} riskScore=${fmt(it.risk_score)}`);
  lines.push(`status=${it.status}`);
  if (!confModel.fitted) lines.push(`⚠ conf НЕ откалибрована (Platt не обучен)`);
  else lines.push(`✓ conf откалибрована (Platt a=${fmt(confModel.a,3)} b=${fmt(confModel.b,3)})`);

  lines.push("");
  // BTC beta block
  const beta = reasons.btc_beta || {};
  if (beta.correlation !== null && beta.correlation !== undefined) {
    const btcEmoji = beta.is_btc_driven ? "🔗" : beta.independent_signal ? "🆓" : "〰";
    const btcNote = beta.is_btc_driven
      ? "сигнал отражает BTC, не сам актив"
      : beta.independent_signal
        ? "независимый сигнал"
        : "частичная корреляция";
    lines.push(`BTC beta: r=${beta.correlation} β=${beta.beta} ${btcEmoji} ${btcNote}`);
    lines.push("");
  }

  lines.push("── Направление ──");
  lines.push(`direction=${dirAgg.direction || "—"} bias=${dirAgg.bias || "—"}`);
  lines.push(`dir_conf=${fmt(dirAgg.direction_confidence)} cal=${fmt(dirAgg.direction_confidence_calibrated)}`);
  lines.push(`regime=${dirAgg.regime || "—"} coherence=${fmt(dirAgg.coherence)}`);
  lines.push(`scores: tactical=${fmt((dirAgg.scores||{}).tactical)} struct=${fmt((dirAgg.scores||{}).structural)} all=${fmt((dirAgg.scores||{}).all)}`);
  if (dirAgg.structural_veto_applied) lines.push("⚠ Structural veto applied");

  lines.push("");
  lines.push("── Сентимент ──");
  const ewma = (sentAgg.ewma || {});
  const symSent = reasons.symbol_sentiment || {};
  lines.push(`Глобальный (EWMA): 1h=${fmt(ewma["1h"])} 6h=${fmt(ewma["6h"])} 1d=${fmt(ewma["1d"])} 7d=${fmt(ewma["7d"])}`);
  lines.push(`regime=${sentAgg.regime || "—"} strength=${fmt(sentAgg.strength)} flags: panic=${(sentAgg.flags||{}).panic} euphoria=${(sentAgg.flags||{}).euphoria}`);
  if (symSent.blended) {
    lines.push(`Per-symbol: ${fmt(symSent.value)} | Итого в скоринге (effective): ${fmt(symSent.effective)} = 0.5×global + 0.5×symbol`);
  } else {
    lines.push(`Per-symbol: нет данных — используется только глобальный (effective=${fmt(symSent.effective)})`);
  }

  lines.push("");
  lines.push("── Факторы ──");
  lines.push("Факторы +:");
  (reasons.top_positive_factors || []).forEach(f =>
    lines.push(`  + ${f.text} (${f.feature}=${fmt(f.value, 4)})`));
  lines.push("Факторы -:");
  (reasons.top_negative_factors || []).forEach(f =>
    lines.push(`  - ${f.text} (${f.feature}=${fmt(f.value, 4)})`));

  // Liquidity + futures meta
  const liq = reasons.liquidity || {};
  const fr  = reasons.funding   || {};
  const oi  = reasons.open_interest || {};

  lines.push("");
  lines.push("── Ликвидность ──");
  const tierEmoji = {"high":"🟢","medium":"🟡","low":"🟠","micro":"🔴","unknown":"⚪"}[liq.tier] || "⚪";
  lines.push(`${tierEmoji} Тир: ${liq.tier || "—"} | Оборот 24h: ${liq.turnover24h_usd ? "$" + Number(liq.turnover24h_usd).toLocaleString("ru") : "—"}`);

  if (fr.value !== undefined && fr.value !== null) {
    const frEmoji = {"bullish":"🟢","bearish":"🔴","neutral":"⚪","unknown":"⚪"}[fr.signal] || "⚪";
    lines.push("");
    lines.push("── Funding Rate ──");
    lines.push(`${frEmoji} ${(fr.value * 100).toFixed(4)}% / 8ч | ${fr.annualized_pct?.toFixed(0)}% годовых | сигнал: ${fr.signal}`);
    lines.push(`Carry cost: ${fr.carry_cost_bps_8h} bps / 8ч`);
  }

  if (oi.oi_now !== undefined && oi.oi_now !== null) {
    const oiEmoji = {"bullish":"🟢","bearish":"🔴","caution":"🟠","neutral":"⚪","pending":"⚪","unknown":"⚪"}[oi.signal] || "⚪";
    lines.push("");
    lines.push("── Open Interest ──");
    lines.push(`${oiEmoji} OI: ${Number(oi.oi_now).toLocaleString("ru")} | тренд: ${oi.trend} | сигнал: ${oi.signal}`);
    lines.push(`Δ4h: ${oi.oi_4h_chg_pct !== null ? oi.oi_4h_chg_pct + "%" : "—"} | Δ24h: ${oi.oi_24h_chg_pct !== null ? oi.oi_24h_chg_pct + "%" : "—"}`);
  }

  lines.push("");
  lines.push("── Стоимость исполнения (bps) ──");
  lines.push(JSON.stringify(reasons.cost_model || {}, null, 2));

  lines.push("");
  lines.push("── Риск-гейты ──");
  if (!blocks.length) lines.push("  OK");
  else blocks.forEach(b => lines.push(`  ✗ ${b.code}: ${b.msg}`));

  lines.push("");
  lines.push("── Параметры для Bybit (копируйте в UI) ──");
  lines.push("Grid — флет: режим Нейтральный; direction_bias = смещение. Диапазон: price_range_lower/price_range_upper.");
  lines.push(JSON.stringify(params, null, 2));

  // Store params for copy button (must be done before setting textContent, which doesn't affect dataset)
  $("details").dataset.params = JSON.stringify(params, null, 2);
  $("details").dataset.recId  = it.rec_id;
  $("copyParamsBtn").classList.remove("hidden");

  // Append refresh timestamp so the user can see the panel was actually updated
  const now = new Date();
  const hms = now.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  lines.push("");
  lines.push(`── обновлено ${hms} ──`);

  $("details").textContent = lines.join("\n");
}

// ── decisions / risk ──────────────────────────────────────────────────────────

async function loadHealth() {
  const res = await fetch("/api/v1/health/symbols");
  let data;
  try { data = await res.json(); } catch(e) { return; }

  const sum = data.summary || {};
  const lines = [];
  const total = (sum.ok || 0) + (sum.stale || 0) + (sum.missing || 0);
  // 🟢 only when every symbol is ok AND there is at least one symbol
  const okEmoji = total > 0 && sum.ok === total ? "🟢" : (sum.missing || 0) > 0 ? "🔴" : "🟠";
  lines.push(`${okEmoji} Символы: ${sum.ok} ok | ${sum.stale} stale | ${sum.missing} missing | ${sum.errors_10m} ошибок за 10 мин`);
  lines.push("");

  const symbols = data.symbols || [];
  const bad  = symbols.filter(sym => sym.status !== "ok");
  const good = symbols.filter(sym => sym.status === "ok");

  if (bad.length > 0) {
    lines.push("── Проблемные ──");
    bad.forEach(sym => {
      const emoji = sym.status === "missing" ? "🔴" : "🟠";
      const age  = sym.age_sec !== null ? `${Math.round(sym.age_sec / 60)}m ago` : "нет данных";
      const errs = sym.error_count_10m > 0 ? ` | ⚡${sym.error_count_10m} err/10m` : "";
      const dis  = sym.disabled ? " | 🚫DISABLED" : "";
      const skip = sym.stale_skips_1h > 0 ? ` | skip×${sym.stale_skips_1h}/h` : "";
      lines.push(`${emoji} ${sym.venue.padEnd(6)} ${sym.symbol.padEnd(14)} ${sym.status.padEnd(8)} ${age}${errs}${dis}${skip}`);
    });
    lines.push("");
  }

  lines.push("── Здоровые ──");
  good.forEach(sym => {
    const age = sym.age_sec !== null ? `${sym.age_sec}s ago` : "—";
    lines.push(`🟢 ${sym.venue.padEnd(6)} ${sym.symbol.padEnd(14)} ok      ${age}`);
  });

  showModal("Здоровье символов", lines.join("\n"));
}

async function loadOutcomes() {
  const res = await fetch("/api/v1/outcomes/stats");
  let data;
  try { data = await res.json(); } catch(e) { return; }

  const s = data.summary || {};
  const lines = [];
  lines.push(`Всего исходов: ${s.total || 0} | Win-rate: ${s.win_rate !== null && s.win_rate !== undefined ? (s.win_rate*100).toFixed(1)+"%" : "нет данных"}`);
  lines.push("");

  if ((data.by_bot || []).length > 0) {
    lines.push("── По типу бота ──");
    lines.push(["Бот", "Напр", "Всего", "Побед", "WR%", "Avg ret%"].join(" | "));
    (data.by_bot || []).forEach(r => {
      lines.push([
        r.bot_type.padEnd(20),
        (r.direction||"—").padEnd(7),
        String(r.total).padStart(5),
        String(r.wins).padStart(5),
        (r.win_rate*100).toFixed(1).padStart(5)+"%",
        (r.avg_ret >= 0 ? "+" : "") + r.avg_ret.toFixed(2)+"%",
      ].join(" | "));
    });
    lines.push("");
  }

  if ((data.by_symbol || []).length > 0) {
    lines.push("── По символу (топ 30) ──");
    lines.push(["Символ", "Бот", "Всего", "WR%", "Avg ret%"].join(" | "));
    (data.by_symbol || []).slice(0, 30).forEach(r => {
      lines.push([
        r.symbol.padEnd(12),
        r.bot_type.padEnd(20),
        String(r.total).padStart(5),
        (r.win_rate*100).toFixed(1).padStart(5)+"%",
        (r.avg_ret >= 0 ? "+" : "") + r.avg_ret.toFixed(2)+"%",
      ].join(" | "));
    });
  }

  if (s.total === 0) {
    lines.push("Исходов пока нет. Данные появятся через ~15 мин после первых рекомендаций.");
  }

  showModal("Экран исходов (win-rate)", lines.join("\n"));
}

async function loadDecisions() {
  const res = await fetch("/api/v1/decisions?limit=200");
  let data;
  try { data = await res.json(); } catch (e) { return; }
  showModal("Журнал решений", data);
}

async function loadRisk() {
  const res = await fetch("/api/v1/risk/status");
  let data;
  try { data = await res.json(); } catch (e) { return; }
  showModal("Статус рисков", data);
}

// ── countdown ─────────────────────────────────────────────────────────────────

function startCountdown() {
  if (countdownTimer) clearInterval(countdownTimer);
  countdownVal = 10;
  $("refreshCountdown").textContent = `↻ ${countdownVal}s`;
  countdownTimer = setInterval(() => {
    countdownVal--;
    if (countdownVal <= 0) {
      $("refreshCountdown").textContent = "↻ …";
    } else {
      $("refreshCountdown").textContent = `↻ ${countdownVal}s`;
    }
  }, 1000);
}

// ── main refresh ──────────────────────────────────────────────────────────────

async function refreshAll() {
  await loadStatus();
  await loadRecommendations();
  startCountdown();
}

// ── events ────────────────────────────────────────────────────────────────────

document.addEventListener("click", async (e) => {
  const t = e.target;
  if (!t || !t.dataset) return;
  const act = t.dataset.act;
  const id  = t.dataset.id;

  if (act === "details") await loadDetails(id);

  if (act === "json") {
    let data;
    try {
      const res = await fetch(`/api/v1/recommendations/${id}`);
      data = await res.json();
    } catch (e) { return; }
    showModal("Recommendation JSON", data);
  }

  if (act === "execute" || act === "ignore") {
    const action = act === "execute" ? "executed" : "ignored";
    try {
      const res = await fetch(`/api/v1/recommendations/${id}/action`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, operator: "ui" }),
      });
      const data = await res.json();
      if (data.ok) {
        // Update row in-place — avoids the flicker caused by a full table rebuild.
        // The row will naturally disappear on the next scheduled auto-refresh.
        const row = t.closest("tr");
        if (row) {
          row.classList.remove("row-recommended");
          row.style.opacity = "0.45";

          // Update the status cell (column index 9, 0-based)
          const cells = row.querySelectorAll("td");
          if (cells.length >= 10) {
            const statusClass = action === "executed" ? "op-executed" : "op-ignored";
            cells[9].innerHTML =
              `<span class="op-status-label ${statusClass}">${action}</span>`;
          }

          // Remove execute/ignore buttons but keep Детали and JSON
          const actionTd = t.closest("td");
          if (actionTd) {
            actionTd.querySelectorAll(".op-exec, .op-ignore").forEach(b => b.remove());
          }
        }
        // Do NOT call refreshAll() here — that clears and rebuilds the entire table,
        // which makes the row disappear and potentially reappear if the reco thread
        // already produced a new cycle for the same symbol.
      }
    } catch (e) { /* ignore network errors */ }
  }
});

$("refreshBtn").addEventListener("click", refreshAll);
$("decisionsBtn").addEventListener("click", loadDecisions);
$("riskBtn").addEventListener("click", loadRisk);
$("outcomesBtn").addEventListener("click", loadOutcomes);
$("healthBtn").addEventListener("click", loadHealth);

$("refreshDetailsBtn").addEventListener("click", () => {
  if (!currentMeta) return;
  // Every recommender cycle produces new rec_ids for the same (venue, symbol, bot_type).
  // Look for the freshest rec_id in the current table DOM before falling back to the
  // stored one — otherwise we always re-fetch the stale record from a previous cycle.
  let freshId = null;
  if (currentMeta) {
    const rows = $("recoBody").querySelectorAll("tr");
    for (const row of rows) {
      const detailsBtn = row.querySelector('button[data-act="details"]');
      if (!detailsBtn) continue;
      const rid = detailsBtn.dataset.id || "";
      // rec_id format: R-{ts}-{venue}-{symbol}-{bot_type}-{hex}
      const parts = rid.split("-");
      // parts: ["R", ts, venue, symbol, bot_type_part1, ..., hex]
      // Reconstruct venue/symbol/bot_type from the known values stored in currentMeta
      if (
        rid.includes(`-${currentMeta.venue}-`) &&
        rid.includes(`-${currentMeta.symbol}-`) &&
        rid.includes(`-${currentMeta.bot_type}-`)
      ) {
        freshId = rid;
        break;
      }
    }
  }
  loadDetails(freshId || currentRecId);
});

$("copyParamsBtn").addEventListener("click", () => {
  const params = $("details").dataset.params;
  if (!params) return;
  navigator.clipboard.writeText(params).then(() => {
    $("copyParamsBtn").textContent = "✓ Скопировано";
    setTimeout(() => { $("copyParamsBtn").textContent = "Скопировать параметры"; }, 2000);
  });
});

$("modalClose").addEventListener("click", (e) => { e.stopPropagation(); hideModal(); });
$("modal").addEventListener("click", (e) => { if (e.target.id === "modal") hideModal(); });
$("collectErrJournal").addEventListener("click", (e) => { e.preventDefault(); loadDecisions(); });

["venue", "topN", "minConf"].forEach(id => {
  const el = $(id);
  if (el) el.addEventListener("input", () => {
    if (recoDebounce) clearTimeout(recoDebounce);
    recoDebounce = setTimeout(refreshAll, 300);
  });
});

["showRecommended", "showBlocked", "showNoTrade", "showSuppressed"].forEach(id => {
  const el = $(id);
  if (el) el.addEventListener("change", refreshAll);
});

// Keyboard: R = refresh
document.addEventListener("keydown", (e) => {
  if (e.key === "r" && !e.ctrlKey && !e.metaKey && document.activeElement.tagName !== "INPUT") {
    refreshAll();
  }
});

// ── boot ──────────────────────────────────────────────────────────────────────

refreshAll();
setInterval(refreshAll, 10000);
