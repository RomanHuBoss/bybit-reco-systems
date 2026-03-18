const $ = (id) => document.getElementById(id);

let recoAbort = null;
let recoDebounce = null;
let statusPayload = null;
let countdownTimer = null;
let countdownVal = 10;
let currentRecId = null;   // rec_id currently shown in Details panel
let currentMeta  = null;   // {venue, symbol, bot_type} — used to find fresh rec_id on refresh

// ── sort state ────────────────────────────────────────────────────────────────
let sortCol = "confidence";  // default: sort by confidence descending
let sortDir = "desc";        // "asc" | "desc"
let lastItems = [];          // last fetched items — re-sorted on header click without refetch

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

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function fmtPrice(x) {
  if (x === null || x === undefined || x === "") return "—";
  const v = Number(x);
  if (Number.isNaN(v)) return String(x);
  const av = Math.abs(v);
  const frac = av >= 1000 ? 2 : av >= 1 ? 4 : 6;
  return v.toLocaleString("ru-RU", { maximumFractionDigits: frac });
}

function fmtPct(x, n = 2) {
  if (x === null || x === undefined || x === "") return "—";
  const v = Number(x);
  if (Number.isNaN(v)) return String(x);
  return `${v >= 0 ? "+" : ""}${v.toFixed(n)}%`;
}

function directionRu(dir) {
  if (dir === "long") return "Лонг";
  if (dir === "short") return "Шорт";
  return "Нейтральный";
}

function botTypeLabel(botType) {
  if (botType === "futures_grid") return "Фьючерсный grid";
  if (botType === "spot_grid") return "Спотовый grid";
  return botType || "—";
}

function venueLabel(venue) {
  if (venue === "linear") return "Фьючерсы";
  if (venue === "spot") return "Спот";
  return venue || "—";
}

function statusBadgeHtml(status) {
  let cls = "badge-inline badge-muted";
  if (status === "recommended") cls = "badge-inline badge-good";
  else if (status === "blocked") cls = "badge-inline badge-bad";
  else if (status === "no_trade") cls = "badge-inline badge-warn";
  return `<span class="${cls}">${escapeHtml(status || "—")}</span>`;
}

function shockBadgeHtml(shock) {
  const severity = (shock || {}).severity || "normal";
  const cls = severity === "lockdown" ? "shock-badge shock-lockdown" : severity === "guarded" ? "shock-badge shock-guarded" : "shock-badge shock-normal";
  const text = (shock || {}).title || "Нормальный режим";
  return `<span class="${cls}">${escapeHtml(text)}</span>`;
}

function copyButton(copyValue) {
  if (!copyValue && copyValue !== 0) return "";
  return `<button class="copy-chip" data-act="copy-field" data-copy="${escapeHtml(copyValue)}">копия</button>`;
}

function fieldBox(label, value, copyValue = null) {
  const effectiveCopy = copyValue === null ? value : copyValue;
  return `
    <div class="field-box">
      <div class="field-label">${escapeHtml(label)}</div>
      <div class="field-value-row">
        <div class="field-value">${escapeHtml(value ?? "—")}</div>
        ${copyButton(effectiveCopy)}
      </div>
    </div>
  `;
}

function buildLaunchSheetText(it) {
  const params = (it || {}).params || {};
  const plan = params.trade_plan || {};
  const ks = (((plan || {}).levels || {}).kill_switch) || {};
  const tpPerLeg = (((plan || {}).levels || {}).tp_per_leg) || {};
  const shock = ((it || {}).reasons || {}).market_shock || {};
  const lines = [];
  lines.push(`${it.symbol} | ${botTypeLabel(it.bot_type)} | ${directionRu(it.direction)}`);
  lines.push(`Площадка: ${venueLabel(it.venue)}`);
  lines.push(`Режим: ${directionRu(it.direction)}`);
  lines.push(`Диапазон: ${fmtPrice(params.price_range_lower)} — ${fmtPrice(params.price_range_upper)}`);
  lines.push(`Кол-во сеток: ${params.grid_levels ?? "—"}`);
  lines.push(`Шаг сетки: ${fmt(params.grid_spacing_pct, 4)}%`);
  lines.push(`Референс цены: ${fmtPrice(params.price_ref)}`);
  if (it.venue === "linear") lines.push(`Плечо: x${params.leverage ?? 1} | Маржа: ${params.margin_mode || "isolated"}`);
  if (it.direction === "neutral") {
    lines.push(`Нижняя стоп-цена: ${fmtPrice(ks.lower)}`);
    lines.push(`Верхняя стоп-цена: ${fmtPrice(ks.upper)}`);
  } else if (it.direction === "long") {
    lines.push(`Stop-loss: ${fmtPrice(ks.lower)}`);
    lines.push(`Take-profit: ${fmtPrice(ks.upper)}`);
  } else if (it.direction === "short") {
    lines.push(`Take-profit: ${fmtPrice(ks.lower)}`);
    lines.push(`Stop-loss: ${fmtPrice(ks.upper)}`);
  }
  lines.push(`Kill switch: ${fmtPrice(ks.lower)} — ${fmtPrice(ks.upper)}`);
  if (tpPerLeg.pct !== undefined && tpPerLeg.pct !== null) lines.push(`TP на одну ногу: ${fmt(tpPerLeg.pct, 4)}%`);
  lines.push(`Status: ${it.status}`);
  if (shock.title) lines.push(`Market guard: ${shock.title}`);
  if (shock.operator_note) lines.push(`Operator note: ${shock.operator_note}`);
  return lines.join("\n");
}

function buildTechPayload(it) {
  const reasons = (it || {}).reasons || {};
  return {
    rec_id: it.rec_id,
    score: it.score,
    confidence: it.confidence,
    expected_rr: it.expected_rr,
    blocks: it.blocks || [],
    cost_model: reasons.cost_model || {},
    market_shock: reasons.market_shock || {},
    fast_veto: reasons.fast_veto || {},
    direction_agg: reasons.direction_agg || {},
    sentiment_agg: reasons.sentiment_agg || {},
    factors: {
      positive: reasons.top_positive_factors || [],
      negative: reasons.top_negative_factors || [],
    },
    params: it.params || {},
  };
}

function buildDetailsHtml(it) {
  const reasons = it.reasons || {};
  const params = it.params || {};
  const plan = params.trade_plan || {};
  const levels = plan.levels || {};
  const ks = levels.kill_switch || {};
  const tpPerLeg = levels.tp_per_leg || {};
  const shock = reasons.market_shock || {};
  const fastVeto = reasons.fast_veto || {};
  const sentAgg = reasons.sentiment_agg || {};
  const dirAgg = reasons.direction_agg || {};
  const funding = reasons.funding || {};
  const oi = reasons.open_interest || {};
  const volatility = plan.volatility || {};
  const blocks = it.blocks || [];
  const alertClass = (shock.severity || "normal") === "lockdown" ? "lock" : (shock.severity || "normal") === "guarded" ? "guard" : "";
  const dirConf = dirAgg.direction_confidence_calibrated ?? dirAgg.direction_confidence;
  const copyText = buildLaunchSheetText(it);
  const techPayload = JSON.stringify(buildTechPayload(it), null, 2);

  $("details").dataset.copyText = copyText;
  $("details").dataset.tech = techPayload;
  $("details").dataset.recId = it.rec_id;
  $("copyParamsBtn").classList.remove("hidden");

  const stopLossLabel = it.direction === "short" ? "Stop-loss (верх)" : it.direction === "long" ? "Stop-loss (низ)" : "Нижняя стоп-цена";
  const takeProfitLabel = it.direction === "short" ? "Take-profit (низ)" : it.direction === "long" ? "Take-profit (верх)" : "Верхняя стоп-цена";
  const upperField = fieldBox(takeProfitLabel, fmtPrice(ks.upper), fmtPrice(ks.upper));
  const lowerField = fieldBox(stopLossLabel, fmtPrice(ks.lower), fmtPrice(ks.lower));
  const reasonList = (shock.reasons || []).length ? `<ul class="reason-list">${(shock.reasons || []).slice(0, 4).map(r => `<li><code>${escapeHtml(r.code || "signal")}</code> — ${escapeHtml(r.msg || "")}</li>`).join("")}</ul>` : "";
  const blockCards = blocks.length ? `<div class="small-blocks">${blocks.map(b => `<div class="small-block"><code>${escapeHtml(b.code || "BLOCK")}</code><br>${escapeHtml(b.msg || "")}</div>`).join("")}</div>` : `<div class="helper-text">Активных блоков нет.</div>`;
  const fastVetoBlock = fastVeto.triggered ? `<div class="small-block"><code>${escapeHtml((fastVeto.blocks || [])[0]?.code || "FAST_VETO")}</code><br>${escapeHtml((fastVeto.blocks || [])[0]?.msg || "")}</div>` : "";

  return `
    <div class="operator-sheet">
      <div class="operator-hero">
        <div>
          <div class="operator-title">${escapeHtml(it.symbol)} · ${escapeHtml(botTypeLabel(it.bot_type))}</div>
          <div class="operator-subtitle">${escapeHtml(venueLabel(it.venue))} · ${escapeHtml(directionRu(it.direction))} · ${statusBadgeHtml(it.status)} · rec_id ${escapeHtml(it.rec_id)}</div>
        </div>
        <div class="operator-hero-metrics">
          <div class="metric-chip"><b>Score</b>${fmt(it.score)}</div>
          <div class="metric-chip"><b>Confidence</b>${fmt(it.confidence)}</div>
          <div class="metric-chip"><b>Dir conf</b>${fmt(dirConf)}</div>
          <div class="metric-chip"><b>Exp RR</b>${fmt(it.expected_rr)}</div>
        </div>
      </div>

      <div class="operator-card alert-card ${alertClass}">
        <h3>Market guard</h3>
        <div class="alert-line">${shockBadgeHtml(shock)}</div>
        <div>${escapeHtml(shock.operator_note || "Новые входы разрешены в обычном режиме.")}</div>
        <div class="alert-note">Breadth вниз: ${fmt(((shock.metrics || {}).breadth_down || 0) * 100, 1)}% · вверх: ${fmt(((shock.metrics || {}).breadth_up || 0) * 100, 1)}% · median 5m: ${fmtPct((((shock.metrics || {}).median_r5m || 0) * 100), 2)}</div>
        ${reasonList}
      </div>

      <div class="operator-card">
        <h3>Лист запуска Bybit</h3>
        <div class="operator-grid three">
          ${fieldBox("Режим", directionRu(it.direction), directionRu(it.direction))}
          ${fieldBox("Диапазон от", fmtPrice(params.price_range_lower), fmtPrice(params.price_range_lower))}
          ${fieldBox("Диапазон до", fmtPrice(params.price_range_upper), fmtPrice(params.price_range_upper))}
          ${fieldBox("Кол-во сеток", params.grid_levels ?? "—", params.grid_levels ?? "—")}
          ${fieldBox("Шаг сетки", `${fmt(params.grid_spacing_pct, 4)}%`, `${fmt(params.grid_spacing_pct, 4)}%`)}
          ${fieldBox("Референс цены", fmtPrice(params.price_ref), fmtPrice(params.price_ref))}
          ${it.venue === "linear" ? fieldBox("Плечо", `x${params.leverage ?? 1}`, `x${params.leverage ?? 1}`) : ""}
          ${it.venue === "linear" ? fieldBox("Маржа", params.margin_mode || "isolated", params.margin_mode || "isolated") : ""}
          ${lowerField}
          ${upperField}
          ${fieldBox("Kill switch", `${fmtPrice(ks.lower)} — ${fmtPrice(ks.upper)}`, `${fmtPrice(ks.lower)} — ${fmtPrice(ks.upper)}`)}
          ${fieldBox("TP на ногу", tpPerLeg.pct !== undefined && tpPerLeg.pct !== null ? `${fmt(tpPerLeg.pct, 4)}%` : "—", tpPerLeg.pct !== undefined && tpPerLeg.pct !== null ? `${fmt(tpPerLeg.pct, 4)}%` : "—")}
        </div>
        <div class="section-actions">
          <button class="ghost-chip" data-act="show-tech">Техподробности</button>
        </div>
        <div class="helper-text">Основной сценарий — ручной запуск на Bybit. JSON убран из основной панели; для копирования доступны отдельные поля и цельный лист запуска.</div>
      </div>

      <div class="operator-card">
        <h3>Контекст</h3>
        <div class="operator-grid">
          ${fieldBox("Сентимент 6h", fmt(sentAgg.ewma?.["6h"] ?? sentAgg.ewma?.["6h"], 2), fmt(sentAgg.ewma?.["6h"] ?? sentAgg.ewma?.["6h"], 2))}
          ${fieldBox("Regime", dirAgg.regime || "—", dirAgg.regime || "—")}
          ${fieldBox("Coherence", fmt(dirAgg.coherence), fmt(dirAgg.coherence))}
          ${fieldBox("ATR 1h", volatility.atr_pct_1h !== undefined && volatility.atr_pct_1h !== null ? fmtPct(volatility.atr_pct_1h * 100, 2) : "—")}
          ${fieldBox("Funding", funding.value !== undefined && funding.value !== null ? fmtPct(funding.value * 100, 4) : "—")}
          ${fieldBox("OI 4h", oi.oi_4h_chg_pct !== undefined && oi.oi_4h_chg_pct !== null ? fmtPct(oi.oi_4h_chg_pct, 2) : "—")}
        </div>
        ${fastVeto.triggered ? `<div class="small-blocks">${fastVetoBlock}</div>` : `<div class="helper-text">Fast-veto не сработал.</div>`}
      </div>

      <div class="operator-card">
        <h3>Блоки и предостережения</h3>
        ${blockCards}
      </div>
    </div>
  `;
}

function pillStatus(status) {
  let cls = "pill";
  if (status === "recommended") cls += " good";
  else if (status === "blocked") cls += " bad";
  else cls += " warn";
  return `<span class="${cls}">${status}</span>`;
}

function getConfModel(item) {
  return ((item || {}).reasons || {}).confidence_model || {};
}

function confCell(item) {
  const v = Number((item || {}).confidence);
  if (isNaN(v)) return "-";

  const confModel = getConfModel(item);
  const fitted = !!confModel.fitted;
  const logregActive = !!confModel.logreg_active;

  let cls = "conf-val";
  if (!fitted) cls += " conf-uncal";
  else if (v >= 0.75) cls += " conf-high";
  else if (v >= 0.60) cls += " conf-mid";
  else cls += " conf-low";

  let marker = "";
  if (!fitted) {
    const src = confModel.source || "raw";
    const cap = confModel.heuristic_cap;
    const capText = (cap === null || cap === undefined) ? "" : `; cap≤${Number(cap).toFixed(2)}`;
    marker = ` <span class='conf-mode-tag conf-mode-raw' title='Не откалибровано: ${src}${capText}'>raw</span>`;
  } else if (logregActive) {
    marker = " <span class='conf-mode-tag conf-mode-cal' title='Откалибровано: LogReg + Platt'>cal</span>";
  } else {
    const nSamples = Number(confModel.n_samples || 0);
    marker = ` <span class='conf-mode-tag conf-mode-platt' title='Частично откалибровано: Platt only (n=${nSamples})'>platt</span>`;
  }

  return `<span class="${cls}">${v.toFixed(2)}${marker}</span>`;
}

function summariseCalibState(items) {
  const summary = { unfitted: 0, platt: 0, logreg: 0, total: 0 };
  (items || []).forEach((it) => {
    const confModel = getConfModel(it);
    summary.total += 1;
    if (!confModel.fitted) summary.unfitted += 1;
    else if (confModel.logreg_active) summary.logreg += 1;
    else summary.platt += 1;
  });
  return summary;
}

function buildBotCalibText(botType, info, totalOutcomeCount) {
  const total = Number(info?.outcomes_total || 0);
  const wins = Number(info?.wins || 0);
  const losses = Number(info?.losses || Math.max(0, total - wins));
  const effective = Number(info?.effective_samples || (2 * Math.min(wins, losses)) || 0);
  const needed = Number(info?.min_samples || statusPayload?.calib_min_samples || 80);
  const winRate = info?.win_rate;
  const winRateText = (winRate === null || winRate === undefined) ? "—" : Number(winRate).toFixed(2);
  const allText = Number(totalOutcomeCount || 0);

  if (info?.fitted) {
    if (info?.logreg_active) {
      return `${botType}: калибратор активен (LogReg + Platt, n=${Number(info.n_samples || 0)}; побед=${wins}, поражений=${losses}).`;
    }
    return `${botType}: включён Platt-only (n=${Number(info.n_samples || 0)} / ${Number(info.logreg_min_samples || 300)}; побед=${wins}, поражений=${losses}).`;
  }

  if ((info?.unfitted_reason || "") === "degenerate_win_rate") {
    const minority = Number(info?.minority_class_count || Math.min(wins, losses) || 0);
    const recent7d = Number(info?.outcomes_7d || 0) > 0
      ? ` За 7д: побед=${Number(info?.wins_7d || 0)}, поражений=${Number(info?.losses_7d || 0)}.`
      : "";
    return `${botType}: raw-only, калибровка отключена из-за вырожденных меток (minority=${minority}, effective=${effective} / ${needed}, win-rate=${winRateText}, entropy=${fmt(info?.class_entropy_bits, 3)}). Всего исходов в базе: ${allText}.${recent7d}`;
  }
  if ((info?.unfitted_reason || "") === "pending_refit") {
    return `${botType}: исходов уже достаточно (effective=${effective} / ${needed}, всего=${total}, побед=${wins}, поражений=${losses}, win-rate=${winRateText}). Модель ещё не обновлена в текущем цикле.`;
  }
  return `${botType}: effective для fit ${effective} / ${needed} (всего=${total}, побед=${wins}, поражений=${losses}). Всего исходов в базе: ${allText}.`;
}

function updateCalibrationUi(items) {
  const header = $("confHeader");
  const banner = $("calibBanner");
  if (!header || !banner) return;

  const summary = summariseCalibState(items || []);
  if (summary.total === 0) {
    const botCalibs = Object.entries(statusPayload?.bot_calibrators || {})
      ;
    const fittedBots = botCalibs.filter(([, info]) => !!info?.fitted);
    const logregBots = botCalibs.filter(([, info]) => !!info?.logreg_active);

    if (botCalibs.length > 0 && fittedBots.length === botCalibs.length) {
      header.textContent = "Увер ✓";
      header.title = `Все поддерживаемые bot_type имеют bot-specific калибровку (${fittedBots.length}/${botCalibs.length}).`;
      banner.classList.add("hidden");
    } else {
      header.textContent = fittedBots.length > 0 ? "Увер ~" : "Увер ?";
      header.title = fittedBots.length > 0
        ? `Калибровка готова только для части bot_type (${fittedBots.length}/${botCalibs.length}); глобальная модель считается диагностической и не используется как fallback.`
        : "Bot-specific калибровка ещё не готова.";
      banner.classList.remove("hidden");
      const count = Number(statusPayload?.outcome_count || 0);
      const needed = Number(statusPayload?.calib_min_samples || 80);
      const pct = needed > 0 ? Math.min(100, Math.round(count / needed * 100)) : 0;
      const readiness = botCalibs.length > 0
        ? `Готово bot_type: ${fittedBots.length}/${botCalibs.length}${logregBots.length ? ` (LogReg: ${logregBots.length})` : ""}. `
        : "";
      $("calibProgress").textContent = `${readiness}Всего исходов: ${count}. Глобальная калибровка отображается только как диагностика; inference опирается на bot-specific модели.`;
      $("calibBarFill").style.width = `${pct}%`;
    }
    return;
  }

  if (summary.unfitted === 0 && summary.platt === 0) {
    header.textContent = "Увер ✓";
    header.title = `Все строки откалиброваны: LogReg + Platt (${summary.logreg}/${summary.total}).`;
    banner.classList.add("hidden");
    return;
  }
  if (summary.unfitted === 0) {
    header.textContent = summary.logreg > 0 ? "Увер ~" : "Увер ~";
    header.title = `Все строки имеют калибровку, но часть работает в режиме Platt only (LogReg: ${summary.logreg}, Platt: ${summary.platt}).`;
    banner.classList.add("hidden");
    return;
  }

  header.textContent = (summary.logreg > 0 || summary.platt > 0) ? "Увер ?" : "Увер ⚠";
  header.title = `Есть неоткалиброванные строки: raw=${summary.unfitted}, Platt=${summary.platt}, LogReg=${summary.logreg}.`;

  const botTypes = [...new Set((items || []).map(it => it.bot_type).filter(Boolean))];
  const botCalibs = statusPayload?.bot_calibrators || {};
  const totalOutcomeCount = Number(statusPayload?.outcome_count || 0);
  const messages = botTypes.slice(0, 3).map((botType) => buildBotCalibText(botType, botCalibs[botType], totalOutcomeCount));
  if (botTypes.length > 3) messages.push(`И ещё ${botTypes.length - 3} bot_type.`);

  const primaryBot = botTypes.length === 1 ? botTypes[0] : null;
  const primaryInfo = primaryBot ? botCalibs[primaryBot] : null;
  const effective = Number(primaryInfo?.effective_samples || 0);
  const needed = Number(primaryInfo?.min_samples || statusPayload?.calib_min_samples || 80);
  const pct = needed > 0 ? Math.min(100, Math.round(effective / needed * 100)) : 0;

  banner.classList.remove("hidden");
  if (primaryBot) {
    const title = primaryInfo?.fitted
      ? (primaryInfo?.logreg_active
        ? `Текущий bot_type ${primaryBot}: калибратор активен`
        : `Текущий bot_type ${primaryBot}: работает Platt-only`) 
      : `Текущий bot_type ${primaryBot}: калибратор не обучен`;
    document.querySelector(".calib-title").innerHTML = `${title} — уверенность <b>${primaryInfo?.fitted ? "частично/полностью откалибрована" : "не откалибрована"}</b>`;
  } else {
    document.querySelector(".calib-title").innerHTML = `Калибровка по текущему набору <b>смешанная</b>`;
  }
  $("calibProgress").textContent = messages.join(" ");
  $("calibBarFill").style.width = `${pct}%`;
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

function getAdminApiKey() {
  const el = $("adminApiKey");
  return el ? (el.value || "").trim() : "";
}

function authHeaders(base = {}) {
  const headers = { ...base };
  const apiKey = getAdminApiKey();
  if (apiKey) headers["X-API-Key"] = apiKey;
  return headers;
}

// ── status & calibration ──────────────────────────────────────────────────────

async function loadStatus() {
  try {
    const res = await fetch("/api/v1/status");
    if (!res.ok) return;
    const s = await res.json();
    statusPayload = s;
    updateCalibrationUi(lastItems);

    // collect error banner
    const errBanner = $("collectErrBanner");
    if (s.collect_errors_10m > 0) {
      errBanner.classList.remove("hidden");
      $("collectErrText").textContent =
        `${s.collect_errors_10m} ошибок сбора данных за последние 10 мин`;
    } else {
      errBanner.classList.add("hidden");
    }

    const shock = s.market_shock || {};
    const shockEl = $("shock-badge");
    if (shockEl) {
      const severity = shock.severity || "normal";
      shockEl.className = severity === "lockdown" ? "shock-badge shock-lockdown" : severity === "guarded" ? "shock-badge shock-guarded" : "shock-badge shock-normal";
      shockEl.textContent = `Guard: ${shock.title || "Нормальный режим"}`;
      shockEl.title = shock.operator_note || "";
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
  lastItems = items;
  renderRecoTable(items);
  updateCalibrationUi(items);

  const banner = $("noTrade");
  const hasRecommended = items.some(it => it.status === "recommended");
  if (!hasRecommended) banner.classList.remove("hidden");
  else banner.classList.add("hidden");
}

function applySort(items) {
  if (!sortCol) return [...items];
  return [...items].sort((a, b) => {
    let av, bv;
    if (sortCol === "dir_conf") {
      const da = (a.reasons || {}).direction_agg || {};
      const db = (b.reasons || {}).direction_agg || {};
      av = da.direction_confidence_calibrated ?? da.direction_confidence ?? -1;
      bv = db.direction_confidence_calibrated ?? db.direction_confidence ?? -1;
    } else {
      av = a[sortCol] ?? "";
      bv = b[sortCol] ?? "";
    }
    if (typeof av === "string") av = av.toLowerCase();
    if (typeof bv === "string") bv = bv.toLowerCase();
    if (av < bv) return sortDir === "asc" ? -1 :  1;
    if (av > bv) return sortDir === "asc" ?  1 : -1;
    return 0;
  });
}

function updateSortHeaders() {
  document.querySelectorAll(".table th[data-sort]").forEach(th => {
    th.classList.remove("sort-asc", "sort-desc");
    if (th.dataset.sort === sortCol) {
      th.classList.add(sortDir === "asc" ? "sort-asc" : "sort-desc");
    }
  });
}

function renderRecoTable(items) {
  const sorted = applySort(items);
  updateSortHeaders();
  const body = $("recoBody");
  body.innerHTML = "";
  let hasRecommended = false;
  sorted.forEach((it, i) => {
    if (it.status === "recommended") hasRecommended = true;
    const dirAgg = (it.reasons || {}).direction_agg || {};
    const dirConf = dirAgg.direction_confidence_calibrated ?? dirAgg.direction_confidence;
    const tr = document.createElement("tr");
    if (it.status === "recommended") tr.classList.add("row-recommended");
    tr.innerHTML = `
      <td>${i + 1}</td>
      <td><span class="venue-pill venue-${it.venue}">${it.venue}</span></td>
      <td><b>${it.symbol}</b></td>
      <td><span class="bot-pill">${botTypeLabel(it.bot_type)}</span></td>
      <td>${directionBadge(it.direction)}</td>
      <td>${dirConfCell(dirConf)}</td>
      <td>${fmt(it.score)}</td>
      <td>${confCell(it)}</td>
      <td>${fmt(it.expected_rr)}</td>
      <td>${pillStatus(it.status)}</td>
      <td><button class="btn tiny" data-act="details" data-id="${it.rec_id}">Карточка</button></td>
    `;
    body.appendChild(tr);
  });
  const banner = $("noTrade");
  if (!hasRecommended) {
    const shock = (statusPayload || {}).market_shock || {};
    if (shock && shock.state && shock.state !== "normal") {
      banner.innerHTML = `NO-TRADE: <b>${escapeHtml(shock.title || "Guard")}</b>. ${escapeHtml(shock.operator_note || "Новые входы заблокированы.")}`;
    } else {
      banner.innerHTML = 'NO-TRADE: нет рекомендаций со статусом <b>recommended</b> по текущим фильтрам/гейтам.';
    }
    banner.classList.remove("hidden");
  } else {
    banner.classList.add("hidden");
  }
}
function directionBadge(dir) {
  if (!dir || dir === "neutral") return `<span class="dir-badge dir-neu">neutral</span>`;
  if (dir === "long")  return `<span class="dir-badge dir-long">▲ long</span>`;
  if (dir === "short") return `<span class="dir-badge dir-short">▼ short</span>`;
  return `<span class="dir-badge dir-neu">${dir}</span>`;
}

// ── details panel ─────────────────────────────────────────────────────────────

async function loadDetails(recId) {
  currentRecId = recId;
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

  currentMeta = { venue: it.venue, symbol: it.symbol, bot_type: it.bot_type };
  currentRecId = it.rec_id;
  btn.disabled = false;
  btn.textContent = "Обновить";

  const now = new Date();
  const hms = now.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  $("details").innerHTML = `${buildDetailsHtml(it)}<div class="helper-text" style="margin-top:8px">обновлено ${hms}</div>`;
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
  const totalWins = Number(s.wins || 0);
  const totalLosses = Math.max(0, Number(s.total || 0) - totalWins);
  const lines = [];
  lines.push(`Всего исходов: ${s.total || 0} | Побед: ${totalWins} | Поражений: ${totalLosses} | Win-rate: ${s.win_rate !== null && s.win_rate !== undefined ? (s.win_rate*100).toFixed(1)+"%" : "нет данных"}`);
  lines.push("Примечание: это proxy-исходы outcome labeling, а не журнал фактически исполненных сделок.");
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

  if (act === "copy-field") {
    const txt = t.dataset.copy || "";
    if (!txt) return;
    navigator.clipboard.writeText(txt).then(() => {
      const old = t.textContent;
      t.textContent = "✓";
      setTimeout(() => { t.textContent = old; }, 1200);
    });
    return;
  }

  if (act === "show-tech") {
    const tech = $("details").dataset.tech;
    if (tech) showModal("Техподробности", tech);
    return;
  }

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
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ action, operator: "ui" }),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        showModal("Ошибка операторского действия", data.detail || data.error || data);
        return;
      }
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

// ── column sort ───────────────────────────────────────────────────────────────
document.querySelector("#recoTable thead").addEventListener("click", (e) => {
  const th = e.target.closest("th[data-sort]");
  if (!th) return;
  const col = th.dataset.sort;
  if (sortCol === col) {
    sortDir = sortDir === "desc" ? "asc" : "desc";
  } else {
    sortCol = col;
    // numeric columns: default desc (highest first); text columns: default asc
    sortDir = ["score", "confidence", "dir_conf", "expected_rr"].includes(col) ? "desc" : "asc";
  }
  if (lastItems.length) renderRecoTable(lastItems);
});

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
  const copyText = $("details").dataset.copyText;
  if (!copyText) return;
  navigator.clipboard.writeText(copyText).then(() => {
    $("copyParamsBtn").textContent = "✓ Скопировано";
    setTimeout(() => { $("copyParamsBtn").textContent = "Скопировать лист"; }, 2000);
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

const adminApiKeyEl = $("adminApiKey");
if (adminApiKeyEl) {
  adminApiKeyEl.value = "";
}
