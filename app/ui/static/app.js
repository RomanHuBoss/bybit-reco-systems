const $ = (id) => document.getElementById(id);

let recoAbort = null;
let recoDebounce = null;
let statusPayload = null;
let countdownTimer = null;
let countdownVal = 10;
let currentRecId = null;   // rec_id currently shown in Details panel
let detailsRequestSeq = 0;
let currentMeta  = null;   // {venue, symbol, bot_type} — used to find fresh rec_id on refresh

// ── sort state ────────────────────────────────────────────────────────────────
let sortCol = "confidence";  // default: sort by confidence descending
let sortDir = "desc";        // "asc" | "desc"
let lastItems = [];          // last fetched items — re-sorted on header click without refetch
let uiScoreMetaById = new Map();
// Raw recommendation score is normalized roughly to [-1, 1]. Smaller deltas are
// not economically meaningful enough for a hard A/B/C split in the UI. The
// operator-facing value is a relative rank in the visible sample, not launch
// approval and not a probability.
const SCORE_UI_NEAR_TIE_DELTA = 0.025;

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

function toFiniteNumber(value) {
  const v = Number(value);
  return Number.isFinite(v) ? v : null;
}

function countDecimalsFromStep(step) {
  if (step === null || step === undefined || step === "") return null;
  const raw = String(step).trim().toLowerCase();
  if (!raw) return null;
  if (raw.includes("e-")) {
    const exp = Number(raw.split("e-")[1]);
    return Number.isFinite(exp) ? exp : null;
  }
  const normalized = raw.replace(/0+$/, "");
  const parts = normalized.split(".");
  return parts.length === 2 ? parts[1].length : 0;
}

function inferPriceDecimals(value) {
  const v = Math.abs(Number(value) || 0);
  if (v >= 10000) return 1;
  if (v >= 100) return 2;
  if (v >= 10) return 3;
  if (v >= 1) return 4;
  if (v >= 0.1) return 5;
  return 6;
}

function formatDotNumber(value, digits = 4, keepZeros = false) {
  if (value === null || value === undefined || value === "") return "—";
  const v = Number(value);
  if (!Number.isFinite(v)) return String(value);
  let out = v.toFixed(digits);
  if (!keepZeros) out = out.replace(/(\.\d*?[1-9])0+$/, "$1").replace(/\.0+$/, "");
  return out;
}

function quantizeByStep(value, step, mode = "nearest") {
  const v = toFiniteNumber(value);
  const tick = toFiniteNumber(step);
  if (v === null || tick === null || tick <= 0) return null;
  const decimals = countDecimalsFromStep(tick);
  const factor = 10 ** Math.max(0, Number(decimals || 0));
  const scaledValue = Math.round(v * factor);
  const scaledStep = Math.max(1, Math.round(tick * factor));
  let units;
  if (mode === "down") units = Math.floor((scaledValue + 1e-9) / scaledStep);
  else if (mode === "up") units = Math.ceil((scaledValue - 1e-9) / scaledStep);
  else units = Math.round(scaledValue / scaledStep);
  const snapped = (units * scaledStep) / factor;
  return snapped.toFixed(Math.max(0, Number(decimals || 0)));
}

function formatBybitPrice(value, meta = {}, mode = "nearest") {
  if (value === null || value === undefined || value === "") return "—";
  const v = Number(value);
  if (!Number.isFinite(v)) return String(value);
  const tick = toFiniteNumber((meta || {}).tick_size);
  if (tick && tick > 0) {
    const snapped = quantizeByStep(v, tick, mode);
    if (snapped) return snapped;
  }
  return v.toFixed(inferPriceDecimals(v));
}

function formatPercentDot(value, digits = 4, withSign = false) {
  if (value === null || value === undefined || value === "") return "—";
  const v = Number(value);
  if (!Number.isFinite(v)) return String(value);
  return `${withSign && v >= 0 ? "+" : ""}${formatDotNumber(v, digits)}%`;
}


function formatBps(value, digits = 2, withSign = false) {
  if (value === null || value === undefined || value === "") return "—";
  const v = Number(value);
  if (!Number.isFinite(v)) return String(value);
  return `${withSign && v >= 0 ? "+" : ""}${formatDotNumber(v, digits)} bps`;
}

function formatUsdValue(value) {
  if (value === null || value === undefined || value === "") return "—";
  const v = Number(value);
  if (!Number.isFinite(v)) return String(value);
  const av = Math.abs(v);
  if (av >= 1e9) return `$${formatDotNumber(v / 1e9, 2)}B`;
  if (av >= 1e6) return `$${formatDotNumber(v / 1e6, 2)}M`;
  if (av >= 1e3) return `$${formatDotNumber(v / 1e3, 1)}K`;
  return `$${fmtPrice(v)}`;
}

function formatProbability(value) {
  if (value === null || value === undefined || value === "") return "—";
  const v = Number(value);
  if (!Number.isFinite(v)) return String(value);
  const pct = Math.abs(v) <= 1 ? v * 100 : v;
  return `${formatDotNumber(pct, 1)}%`;
}

function directionRu(dir) {
  const normalized = String(dir || "").trim().toLowerCase();
  if (normalized === "long") return "Лонг";
  if (normalized === "short") return "Шорт";
  return "Нейтральный";
}

const SUPPORTED_GRID_BOT_TYPE = "futures_grid";
const SUPPORTED_GRID_VENUE = "linear";

function botTypeLabel(botType) {
  return botType === SUPPORTED_GRID_BOT_TYPE ? "Futures Grid" : "—";
}

function isLaunchableGridRecommendation(it) {
  if (!it || it.bot_type !== SUPPORTED_GRID_BOT_TYPE || it.venue !== SUPPORTED_GRID_VENUE) return false;
  if (!(it.status === "recommended" || it.status === "active")) return false;
  const params = it.params && typeof it.params === "object" ? it.params : {};
  if (!params.trade_plan || typeof params.trade_plan !== "object") return false;
  const riskDecision = params?.risk_report?.decision;
  if (riskDecision !== "recommended") return false;
  const llmStatus = String(it?.reasons?.llm_review?.status || "").toLowerCase();
  if (llmStatus === "pending" || llmStatus === "error") return false;
  const guard = it.bybit_operator_guard || {};
  const errors = Array.isArray(guard.errors) ? guard.errors : [];
  return guard.ok === true && guard.meta_checked === true && errors.length === 0;
}

function venueLabel(venue) {
  if (venue === "linear") return "Bybit Linear USDT Perpetual";
  return venue || "—";
}

function liquidityTierRu(tier) {
  if (tier === "deep") return "Глубокая";
  if (tier === "mid") return "Средняя";
  if (tier === "shallow") return "Тонкая";
  return tier || "—";
}

function marginModeRu(mode) {
  if (mode === "isolated") return "Изолированная (isolated)";
  if (mode === "cross") return "Кросс — не поддерживается";
  return mode || "—";
}

function splitLinearSymbol(symbol) {
  const s = String(symbol || "").toUpperCase();
  const quote = "USDT";
  if (s.endsWith(quote) && s.length > quote.length) {
    return { base: s.slice(0, -quote.length), quote };
  }
  return null;
}

function normalizeLinearUsdtPerpetualSymbol(symbol) {
  const raw = String(symbol || "").trim().toUpperCase();
  if (!raw) return "";
  // Backend normally provides DOGEUSDT/BTCUSDT. Keep the UI tolerant to
  // legacy display forms such as DOGE/USDT or DOGE-USDT, but never build the
  // obsolete locale-specific base/quote route for Bybit derivative charts.
  const compact = raw.replace(/[^A-Z0-9]/g, "");
  if (compact.endsWith("USDT") && compact.length > "USDT".length) return compact;
  return raw;
}

function futuresGridBotCreateUrl() {
  return "https://www.bybit.com/ru-RU/tradingbot/fgrid-create/";
}

function bybitChartUrl(venue, symbol) {
  const chartSymbol = venue === "linear"
    ? normalizeLinearUsdtPerpetualSymbol(symbol)
    : String(symbol || "").trim().toUpperCase();
  return `https://www.bybit.com/trade/usdt/${encodeURIComponent(chartSymbol)}`;
}

function iconSvg(kind) {
  if (kind === "chart") {
    return `
      <svg viewBox="0 0 20 20" aria-hidden="true" class="icon-svg icon-svg-chart">
        <path d="M3.5 16.5h13" />
        <path d="M5 13.2l3-3 2.5 2 4-5.2" />
        <path d="M12.8 7h3.1v3.1" />
      </svg>
    `;
  }
  return `
    <svg viewBox="0 0 20 20" aria-hidden="true" class="icon-svg icon-svg-bot">
      <rect x="5" y="6" width="10" height="8" rx="2" />
      <path d="M10 3.5v2" />
      <path d="M7 9.5h0.01" />
      <path d="M13 9.5h0.01" />
      <path d="M8 12h4" />
      <path d="M6 15.5l-1.2 1.5" />
      <path d="M14 15.5l1.2 1.5" />
    </svg>
  `;
}

function symbolLinksHtml(it, compact = false) {
  const chartUrl = bybitChartUrl(it.venue, it.symbol);
  const botLink = isLaunchableGridRecommendation(it)
    ? `<a class="icon-link" href="${escapeHtml(futuresGridBotCreateUrl())}" target="_blank" rel="noopener noreferrer" title="Открыть страницу создания Futures Grid на Bybit">${iconSvg("bot")}</a>`
    : "";
  const cls = compact ? "symbol-links compact" : "symbol-links";
  return `
    <span class="${cls}">
      <a class="icon-link" href="${escapeHtml(chartUrl)}" target="_blank" rel="noopener noreferrer" title="Открыть график Bybit">${iconSvg("chart")}</a>
      ${botLink}
    </span>
  `;
}

function statusBadgeHtml(status) {
  let cls = "badge-inline badge-muted";
  if (status === "recommended" || status === "active") cls = "badge-inline badge-good";
  else if (status === "blocked") cls = "badge-inline badge-bad";
  else if (status === "no_trade" || status === "pending") cls = "badge-inline badge-warn";
  return `<span class="${cls}">${escapeHtml(status || "—")}</span>`;
}

function shockBadgeHtml(shock) {
  const severity = (shock || {}).severity || "normal";
  const cls = severity === "lockdown" ? "shock-badge shock-lockdown" : severity === "guarded" ? "shock-badge shock-guarded" : "shock-badge shock-normal";
  const text = (shock || {}).title || "Нормальный режим";
  return `<span class="${cls}">${escapeHtml(text)}</span>`;
}

function btcRelationMetric(betaInfo, symbol) {
  const safeSymbol = String(symbol || "").toUpperCase();
  const corrRaw = safeSymbol === "BTCUSDT" ? 1.0 : Number(betaInfo?.correlation);
  if (!Number.isFinite(corrRaw)) {
    return {
      label: "BTC-завис.",
      value: "—",
      iconClass: "unknown",
      title: "Недостаточно данных для расчёта связи с BTC",
    };
  }
  const corr = Math.max(-1, Math.min(1, corrRaw));
  const absCorr = Math.abs(corr);
  let iconClass = "independent";
  let titlePrefix = "Независимый сигнал";
  if (absCorr >= 0.70) {
    iconClass = "strong";
    titlePrefix = safeSymbol === "BTCUSDT" ? "Базовый BTC-инструмент" : "Сильная корреляция с BTC";
  } else if (absCorr >= 0.35) {
    iconClass = "partial";
    titlePrefix = "Частичная корреляция с BTC";
  }
  return {
    label: "BTC-завис.",
    value: `r=${formatDotNumber(corr, 2, false)}`,
    iconClass,
    title: `${titlePrefix}; окно ${(Number(betaInfo?.window || 24))}h`,
  };
}

function btcMetricValueHtml(metric) {
  return `<span class="btc-metric-value"><span class="btc-state-icon ${escapeHtml(metric.iconClass || "unknown")}"></span><span>${escapeHtml(metric.value || "—")}</span></span>`;
}

function scoreUiZone(percentile) {
  const p = Math.max(0, Math.min(100, Number(percentile) || 0));
  if (p >= 80) return { grade: "A", label: "сильный" };
  if (p >= 60) return { grade: "B", label: "хороший" };
  if (p >= 40) return { grade: "C", label: "рабочий" };
  if (p >= 20) return { grade: "D", label: "осторожный" };
  return { grade: "E", label: "слабый" };
}

function computeUiScoreMetaMap(items) {
  const rows = (Array.isArray(items) ? items : [])
    .map((it) => ({ id: it?.rec_id, score: Number(it?.score) }))
    .filter((row) => row.id && Number.isFinite(row.score));
  const out = new Map();
  if (!rows.length) return out;

  rows.sort((a, b) => {
    if (b.score !== a.score) return b.score - a.score;
    return String(a.id).localeCompare(String(b.id));
  });

  const n = rows.length;
  let groupStart = 0;
  while (groupStart < n) {
    let groupEnd = groupStart;
    const anchorScore = rows[groupStart].score;
    while (
      groupEnd + 1 < n &&
      Math.abs(anchorScore - rows[groupEnd + 1].score) <= SCORE_UI_NEAR_TIE_DELTA
    ) {
      groupEnd += 1;
    }

    const avgRank = (groupStart + groupEnd) / 2;
    const percentile = n === 1 ? 100 : Math.round((1 - avgRank / (n - 1)) * 100);
    const zone = scoreUiZone(percentile);
    const groupSize = groupEnd - groupStart + 1;
    const groupSpread = Math.abs(rows[groupStart].score - rows[groupEnd].score);
    const tieNote = groupSize > 1
      ? `; near-tie группа=${groupSize}, Δraw=${formatDotNumber(groupSpread, 4)}, порог=${formatDotNumber(SCORE_UI_NEAR_TIE_DELTA, 4)}`
      : `; material gap > ${formatDotNumber(SCORE_UI_NEAR_TIE_DELTA, 4)}`;

    for (let i = groupStart; i <= groupEnd; i += 1) {
      out.set(rows[i].id, {
        percentile,
        grade: zone.grade,
        zoneLabel: zone.label,
        raw: rows[i].score,
        groupSize,
        groupSpread,
        tieThreshold: SCORE_UI_NEAR_TIE_DELTA,
        title: `Ранг в выборке: ${percentile}/100 — ${zone.label}; raw launch-score=${formatDotNumber(rows[i].score, 4)}${tieNote}; не является разрешением запуска`,
      });
    }
    groupStart = groupEnd + 1;
  }
  return out;
}

function ensureUiScoreMeta(item, poolItems = lastItems) {
  if (!item) return { percentile: 0, grade: "E", zoneLabel: "слабый", raw: null, title: "Ранг недоступен" };
  const existing = uiScoreMetaById.get(item.rec_id);
  if (existing) return existing;
  const basis = Array.isArray(poolItems) && poolItems.length ? poolItems : [item];
  const localMap = computeUiScoreMetaMap(basis.some((row) => row?.rec_id === item.rec_id) ? basis : [...basis, item]);
  return localMap.get(item.rec_id) || {
    percentile: 0,
    grade: "E",
    zoneLabel: "слабый",
    raw: Number.isFinite(Number(item.score)) ? Number(item.score) : null,
    title: `Ранг недоступен; raw launch-score=${formatDotNumber(item.score, 4)}`,
  };
}

function scoreUiCellHtml(meta) {
  const scoreMeta = meta || { percentile: 0, grade: "E", zoneLabel: "слабый", title: "Ранг недоступен" };
  return `<span class="score-ui-cell" title="${escapeHtml(scoreMeta.title || "")}"><span class="score-ui-num zone-${escapeHtml(String(scoreMeta.grade || "E").toLowerCase())}">${escapeHtml(String(scoreMeta.percentile ?? 0))}</span><span class="score-ui-grade grade-${escapeHtml(String(scoreMeta.grade || "E").toLowerCase())}">${escapeHtml(scoreMeta.grade || "E")}</span></span>`;
}

function scoreUiMetricHtml(meta) {
  const scoreMeta = meta || { percentile: 0, grade: "E", zoneLabel: "слабый", title: "Ранг недоступен" };
  return `<span class="score-ui-metric" title="${escapeHtml(scoreMeta.title || "")}"><span class="score-ui-metric-main">${escapeHtml(String(scoreMeta.percentile ?? 0))}/100</span><span class="score-ui-metric-sub grade-${escapeHtml(String(scoreMeta.grade || "E").toLowerCase())}">${escapeHtml(scoreMeta.grade || "E")} · ${escapeHtml(scoreMeta.zoneLabel || "")}</span></span>`;
}


function launchDecisionDiagnostics(it, scoreMeta) {
  const reasons = it?.reasons || {};
  const layers = reasons.decision_layers || {};
  const rawScore = toFiniteNumber(it?.score);
  const scoreThreshold = toFiniteNumber(layers.score_threshold);
  const confidence = toFiniteNumber(it?.confidence);
  const confidenceThreshold = toFiniteNumber(layers.confidence_threshold);
  const confidenceGateApplied = layers.confidence_gate_applied === true;
  const thesisStatus = String(layers.thesis_status || "").trim();
  const executionStatus = String(layers.execution_status || "").trim();
  const finalStatus = String(layers.final_status || it?.status || "").trim();

  const rows = [];
  rows.push({
    label: "Ранг в выборке",
    value: `${scoreMeta?.percentile ?? 0}/100 ${scoreMeta?.grade || "E"}`,
    note: "относительное место среди видимых монет; не разрешение запуска",
  });
  if (rawScore !== null) {
    const cmp = scoreThreshold !== null ? (rawScore >= scoreThreshold ? "≥" : "<") : "";
    const thr = scoreThreshold !== null ? ` ${cmp} порога ${formatDotNumber(scoreThreshold, 3)}` : "";
    rows.push({
      code: "launch_score",
      label: "Оценка запуска",
      value: `${formatDotNumber(rawScore, 3)}${thr}`,
      note: "внутренняя серверная оценка для решения о запуске",
    });
  }
  if (confidence !== null) {
    const showThreshold = confidenceGateApplied && confidenceThreshold !== null;
    const cmp = showThreshold ? (confidence >= confidenceThreshold ? "≥" : "<") : "";
    const thr = showThreshold ? ` ${cmp} порога ${formatDotNumber(confidenceThreshold, 3)}` : "";
    rows.push({
      code: "confidence_gate",
      label: "Порог уверенности",
      value: `${formatDotNumber(confidence, 3)}${thr}`,
      note: confidenceGateApplied ? "участвует в запуске" : "не включён для этого решения",
    });
  }
  rows.push({
    code: "decision_gates",
    label: "Проверки решения",
    value: [thesisStatus, executionStatus, finalStatus].filter(Boolean).join(" / ") || "—",
    note: "финальный статус задаётся обязательными проверками, а не UI-рангом",
  });
  return rows;
}

function launchDecisionDiagnosticsHtml(it, scoreMeta) {
  const rows = launchDecisionDiagnostics(it, scoreMeta);
  return `
    <div class="operator-card launch-decision-diagnostics-card">
      <h3>Ранг не равен разрешению запуска</h3>
      <div class="operator-grid two launch-diagnostics-grid">
        ${rows.map(row => `
          <div class="mini-metric">
            <div class="mini-metric-label">${escapeHtml(row.label)}</div>
            <div class="mini-metric-value">${escapeHtml(row.value)}</div>
            <div class="mini-metric-note">${escapeHtml(row.note)}</div>
          </div>
        `).join("")}
      </div>
    </div>
  `;
}

function noTradeDecisionMessage(it, scoreMeta) {
  const rows = launchDecisionDiagnostics(it, scoreMeta);
  const launchRow = rows.find(row => row.code === "launch_score");
  const confidenceRow = rows.find(row => row.code === "confidence_gate");
  const gateRow = rows.find(row => row.code === "decision_gates");
  const parts = [];
  if (launchRow) parts.push(launchRow.value);
  if (confidenceRow && !confidenceRow.value.includes("не включ")) parts.push(`уверенность ${confidenceRow.value}`);
  if (gateRow && gateRow.value !== "—") parts.push(`проверки: ${gateRow.value}`);
  const gateSummary = parts.length ? ` ${parts.join("; ")}.` : "";
  return `Запуск grid сейчас не рекомендован. Ранг ${scoreMeta?.percentile ?? 0}/100 (${scoreMeta?.grade || "E"} · ${scoreMeta?.zoneLabel || ""}) — это только относительное место в текущей выборке, не разрешение запуска.${gateSummary}`;
}

function copyButton(copyValue) {
  if (copyValue === null || copyValue === undefined || copyValue === "" || copyValue === "—") return "";
  return `<button class="copy-chip" data-act="copy-field" data-copy="${escapeHtml(copyValue)}">копия</button>`;
}

function fieldBox(label, value, copyValue = null, extraClass = "", helpText = "") {
  const safeValue = value ?? "—";
  const effectiveCopy = copyValue === null ? safeValue : copyValue;
  const inputValue = escapeHtml(String(safeValue));
  const inputClass = extraClass ? `field-input ${extraClass}` : "field-input";
  const help = helpText
    ? `<span class="field-help" title="${escapeHtml(helpText)}" aria-label="${escapeHtml(helpText)}">?</span>`
    : "";
  return `
    <div class="field-box">
      <div class="field-label"><span>${escapeHtml(label)}</span>${help}</div>
      <div class="field-value-row field-value-input-row">
        <input class="${inputClass}" type="text" readonly value="${inputValue}" data-copy-source="${escapeHtml(effectiveCopy)}">
        ${copyButton(effectiveCopy)}
      </div>
    </div>
  `;
}

function updateDetailsHeaderLinks(it) {
  const chart = $("detailsChartLink");
  const bot = $("detailsBotLink");
  if (!chart || !bot) return;
  chart.href = bybitChartUrl(it.venue, it.symbol);
  chart.innerHTML = iconSvg("chart");
  chart.classList.remove("hidden");

  if (isLaunchableGridRecommendation(it)) {
    bot.href = futuresGridBotCreateUrl();
    bot.innerHTML = iconSvg("bot");
    bot.title = "Открыть страницу создания Futures Grid на Bybit";
    bot.classList.remove("hidden");
  } else {
    bot.removeAttribute("href");
    bot.innerHTML = "";
    bot.title = "Создание grid-бота скрыто: рекомендация сейчас не исполнима";
    bot.classList.add("hidden");
  }
}

function clearDetailsHeaderLinks() {
  const chart = $("detailsChartLink");
  const bot = $("detailsBotLink");
  if (chart) chart.classList.add("hidden");
  if (bot) bot.classList.add("hidden");
}

function operatorExitLevels(direction, killLower, killUpper) {
  const dir = String(direction || "").trim().toLowerCase();
  if (dir === "short") {
    return {
      takeProfitValue: killLower,
      stopLossValue: killUpper,
      takeProfitLabel: "Take Profit",
      stopLossLabel: "Stop Loss",
      exitGeometry: "short: TP ниже диапазона, SL выше диапазона",
    };
  }
  if (dir === "long") {
    return {
      takeProfitValue: killUpper,
      stopLossValue: killLower,
      takeProfitLabel: "Take Profit",
      stopLossLabel: "Stop Loss",
      exitGeometry: "long: TP выше диапазона, SL ниже диапазона",
    };
  }
  return {
    takeProfitValue: "—",
    stopLossValue: `${killLower} / ${killUpper}`,
    takeProfitLabel: "Directional TP unavailable",
    stopLossLabel: "Stop Loss / Kill-switch",
    exitGeometry: "neutral: нет направленного TP; контроль выхода по нижнему/верхнему kill-switch",
  };
}

function directionalExitGeometryOk(direction, takeProfit, stopLoss, referencePrice = null) {
  const dir = String(direction || "").trim().toLowerCase();
  const tp = toFiniteNumber(takeProfit);
  const sl = toFiniteNumber(stopLoss);
  const ref = toFiniteNumber(referencePrice);
  if (dir !== "long" && dir !== "short") return true;
  if (tp === null || sl === null || ref === null || tp <= 0 || sl <= 0 || ref <= 0) return false;
  if (dir === "long") return tp > ref && sl < ref;
  return tp < ref && sl > ref;
}

function operatorExitLevelsFromBackend(exitLevels, fallback, meta = {}) {
  if (!exitLevels || typeof exitLevels !== "object") return fallback;
  const dir = String(exitLevels.direction || "").trim().toLowerCase();
  const lower = formatBybitPrice(exitLevels.kill_switch_lower, meta, "down");
  const upper = formatBybitPrice(exitLevels.kill_switch_upper, meta, "up");
  const hasDirectionalTp = exitLevels.has_directional_take_profit === true && (dir === "long" || dir === "short");
  const backendGeometryOk = exitLevels.geometry_valid !== false && directionalExitGeometryOk(dir, exitLevels.take_profit, exitLevels.stop_loss, exitLevels.reference_price);
  if (hasDirectionalTp && !backendGeometryOk) {
    return {
      takeProfitValue: "—",
      stopLossValue: `${lower} / ${upper}`,
      takeProfitLabel: "Directional TP blocked",
      stopLossLabel: "Stop Loss / Kill-switch",
      exitGeometry: `backend directional TP/SL invalid; rendering kill-switch only · ${fallback.exitGeometry || ""}`.trim(),
    };
  }
  const takeProfitValue = hasDirectionalTp
    ? formatBybitPrice(exitLevels.take_profit, meta, dir === "short" ? "down" : "up")
    : "—";
  const stopLossValue = hasDirectionalTp
    ? formatBybitPrice(exitLevels.stop_loss, meta, dir === "short" ? "up" : "down")
    : `${lower} / ${upper}`;
  return {
    takeProfitValue,
    stopLossValue,
    takeProfitLabel: exitLevels.take_profit_label || fallback.takeProfitLabel || (hasDirectionalTp ? "Take Profit" : "Directional TP unavailable"),
    stopLossLabel: exitLevels.stop_loss_label || fallback.stopLossLabel || "Stop Loss",
    exitGeometry: exitLevels.geometry || fallback.exitGeometry || "",
  };
}

function buildOperatorValues(it) {
  const params = (it || {}).params || {};
  const plan = params.trade_plan || {};
  const levels = plan.levels || {};
  const range = levels.range || {};
  const ks = levels.kill_switch || {};
  const tpPerLeg = levels.tp_per_leg || {};
  const gridStep = levels.grid_step || {};
  const operatorSheet = params.operator_sheet || {};
  const meta = (it || {}).bybit_meta || {};
  const rangeLowerRaw = firstFiniteValue([range, params, operatorSheet], ["lower", "price_range_lower", "range_lower"]);
  const rangeUpperRaw = firstFiniteValue([range, params, operatorSheet], ["upper", "price_range_upper", "range_upper"]);
  const entryRefRaw = firstFiniteValue([plan, params, operatorSheet], ["reference_price", "price_ref"]);
  const leverageRaw = firstFiniteValue([params, operatorSheet], ["leverage"]);
  const marginModeRaw = params.margin_mode || operatorSheet.margin_mode || "isolated";
  const rangeLower = formatBybitPrice(rangeLowerRaw, meta, "down");
  const rangeUpper = formatBybitPrice(rangeUpperRaw, meta, "up");
  const entryRef = formatBybitPrice(entryRefRaw, meta, "nearest");
  const killLower = formatBybitPrice(ks.lower, meta, "down");
  const killUpper = formatBybitPrice(ks.upper, meta, "up");
  const gridStepAbs = formatBybitPrice(gridStep.step_abs, meta, "nearest");
  const tpLegAbs = formatBybitPrice(tpPerLeg.abs, meta, "nearest");
  const stepPct = formatPercentDot(params.grid_spacing_pct ?? gridStep.step_pct, 4, false);
  const tpLegPct = formatPercentDot(tpPerLeg.pct, 4, false);
  const leverage = it.venue === "linear" ? String(leverageRaw ?? 1) : "—";
  const marginMode = it.venue === "linear" ? marginModeRu(marginModeRaw) : "—";
  const exits = operatorExitLevels((it || {}).direction, killLower, killUpper);
  const canonicalExits = operatorExitLevelsFromBackend((it || {}).directional_exit_levels, exits, meta);
  return {
    rangeLower,
    rangeUpper,
    entryRef,
    killLower,
    killUpper,
    gridStepAbs,
    tpLegAbs,
    stepPct,
    tpLegPct,
    leverage,
    marginMode,
    ...canonicalExits,
  };
}

function firstFiniteValue(sources, keys) {
  for (const source of sources) {
    if (!source || typeof source !== "object") continue;
    for (const key of keys) {
      const value = toFiniteNumber(source[key]);
      if (value !== null) return value;
    }
  }
  return null;
}


function formatHoursValue(value) {
  const v = toFiniteNumber(value);
  if (v === null || v <= 0) return null;
  if (v < 1) return `${formatDotNumber(v * 60, 0)} мин`;
  if (v < 24) return `${formatDotNumber(v, v % 1 === 0 ? 0 : 1)} ч`;
  const days = v / 24;
  return `${formatDotNumber(days, days % 1 === 0 ? 0 : 1)} д`;
}

function formatDurationValue(seconds) {
  const s = toFiniteNumber(seconds);
  if (s === null) return "—";
  const abs = Math.abs(s);
  const sign = s < 0 ? "−" : "";
  if (abs < 60) return `${sign}${formatDotNumber(abs, 0)} с`;
  if (abs < 3600) return `${sign}${formatDotNumber(abs / 60, 0)} мин`;
  if (abs < 86400) return `${sign}${formatDotNumber(abs / 3600, abs % 3600 === 0 ? 0 : 1)} ч`;
  return `${sign}${formatDotNumber(abs / 86400, abs % 86400 === 0 ? 0 : 1)} д`;
}

function decisionContext(it) {
  const ctx = it?.operator_decision_context;
  return ctx && typeof ctx === "object" ? ctx : {};
}

function priceStatusRu(status) {
  if (status === "inside_range") return "внутри диапазона";
  if (status === "outside_range") return "вне диапазона";
  if (status === "available") return "цена доступна";
  return "нет текущей цены";
}

function preflightStatusRu(status) {
  if (status === "ok") return "OK — запуск технически допустим";
  if (status === "blocked") return "Блокировка";
  if (status === "warning") return "Есть предупреждения";
  if (status === "not_checked") return "не проверено";
  return "н/д";
}

function riskProfileRu(profile) {
  if (profile === "low") return "низкий";
  if (profile === "moderate") return "умеренный";
  if (profile === "high") return "повышенный";
  if (profile === "critical") return "критический";
  return "не оценён";
}

function buildPriceFreshnessFields(it, ov) {
  const ctx = decisionContext(it);
  const meta = it?.bybit_meta || {};
  const currentPrice = ctx.current_price ?? null;
  const drift = ctx.price_drift_from_entry_pct;
  const tickerAge = ctx.ticker_age_sec;
  const recAge = ctx.recommendation_row_age_sec ?? ctx.recommendation_age_sec;
  const chainAge = ctx.publication_chain_age_sec;
  const chainUpdates = ctx.publication_chain_update_count;
  const chainStartedTs = ctx.publication_chain_started_ts;
  const chainExpiresIn = ctx.publication_chain_expires_in_sec;
  const expiresIn = ctx.expires_in_sec;
  const ttlText = ctx.is_expired === true
    ? `истекла ${formatDurationValue(Math.abs(expiresIn ?? 0))} назад`
    : expiresIn !== null && expiresIn !== undefined
      ? `осталось ${formatDurationValue(expiresIn)}`
      : "TTL не задан";
  const chainTtlText = ctx.is_publication_chain_expired === true
    ? `цепочка истекла ${formatDurationValue(Math.abs(chainExpiresIn ?? 0))} назад`
    : chainExpiresIn !== null && chainExpiresIn !== undefined
      ? `цепочка: осталось ${formatDurationValue(chainExpiresIn)}`
      : "TTL цепочки не задан";
  return [
    {
      label: "Цена входа",
      value: ov.entryRef,
      mono: true,
      help: "Расчётная цена входа из рекомендации. Используется оператором при создании grid-бота и не должна удаляться из панели.",
    },
    {
      label: "Текущая цена",
      value: formatBybitPrice(currentPrice, meta, "nearest"),
      mono: true,
      help: "Последняя доступная биржевая цена или середина bid/ask. Нужна, чтобы понять, не устарели ли уровни сетки.",
    },
    {
      label: "Отклонение от входа",
      value: drift === null || drift === undefined ? "—" : formatPercentDot(drift, 2, true),
      help: "Насколько текущая цена ушла от расчётной цены входа. Большое отклонение означает, что рекомендацию нужно пересчитать.",
    },
    {
      label: "Положение цены",
      value: priceStatusRu(ctx.price_status),
      help: "Показывает, находится ли текущая цена внутри рекомендованного диапазона запуска.",
    },
    {
      label: "До нижней границы",
      value: ctx.distance_to_lower_pct === null || ctx.distance_to_lower_pct === undefined ? "—" : formatPercentDot(ctx.distance_to_lower_pct, 2, true),
      help: "Запас от текущей цены до нижней границы основного диапазона сетки.",
    },
    {
      label: "До верхней границы",
      value: ctx.distance_to_upper_pct === null || ctx.distance_to_upper_pct === undefined ? "—" : formatPercentDot(ctx.distance_to_upper_pct, 2, true),
      help: "Запас от текущей цены до верхней границы основного диапазона сетки.",
    },
    {
      label: "Возраст цены",
      value: tickerAge === null || tickerAge === undefined ? "—" : formatDurationValue(tickerAge),
      help: "Сколько времени прошло с последнего биржевого снимка цены. Старый снимок нельзя считать надёжным основанием для запуска.",
    },
    {
      label: "Возраст текущей строки",
      value: `${recAge === null || recAge === undefined ? "—" : formatDurationValue(recAge)} · ${ttlText}`,
      help: "Возраст именно текущей записи рекомендации. Она могла заменить более раннюю рекомендацию той же публикационной цепочки.",
    },
    {
      label: "Возраст идеи с первого сигнала",
      value: `${chainAge === null || chainAge === undefined ? "—" : formatDurationValue(chainAge)} · обновлений: ${chainUpdates ?? "—"} · ${chainTtlText}`,
      help: `Сколько прошло с первого root-сигнала этой публикационной цепочки. Старт цепочки: ${formatTs(chainStartedTs)}. Если это сильно больше возраста текущей строки, рекомендация может выглядеть свежей, хотя идея уже долго живёт.`,
    },
  ];
}

function buildRiskEconomicsFields(it) {
  const ctx = decisionContext(it);
  return [
    {
      label: "Предпроверка запуска",
      value: preflightStatusRu(ctx.preflight_status),
      help: "Результат технической проверки перед запуском: Bybit-метаданные, диапазон, размеры, tick/qty/min-notional и защитные уровни.",
    },
    {
      label: "Профиль риска",
      value: riskProfileRu(ctx.risk_profile),
      help: "Сводная оценка риска по запасу до ликвидации. Это ориентир для оператора, а не гарантия биржевой ликвидационной цены.",
    },
    {
      label: "Запас до ликвидации",
      value: ctx.liquidation_buffer_pct === null || ctx.liquidation_buffer_pct === undefined ? "—" : formatPercentDot(ctx.liquidation_buffer_pct, 2, false),
      help: "Оценочный процентный запас до ликвидации с учётом стороны и плеча. Точная цена ликвидации зависит от Bybit risk tier, mark price и маржи аккаунта.",
    },
    {
      label: "Расчётная ликвидация",
      value: ctx.estimated_liquidation_price === null || ctx.estimated_liquidation_price === undefined ? "—" : formatBybitPrice(ctx.estimated_liquidation_price, it?.bybit_meta || {}, "nearest"),
      mono: true,
      help: "Приблизительная цена ликвидации для isolated linear USDT. Используется как защитная оценка, не как точная биржевая величина.",
    },
    {
      label: "Чистая прибыль/сетка",
      value: ctx.net_profit_bps === null || ctx.net_profit_bps === undefined ? "—" : formatBps(ctx.net_profit_bps, 2, true),
      help: "Ожидаемая прибыль одной сетки после комиссий, спреда, проскальзывания и неблагоприятного funding. bps = базисные пункты: 1 bps = 0,01%.",
    },
    {
      label: "Издержки исполнения",
      value: ctx.execution_cost_bps === null || ctx.execution_cost_bps === undefined ? "—" : formatBps(ctx.execution_cost_bps, 2, false),
      help: "Оценка расходов на вход/выход: комиссия, спред и возможное проскальзывание. bps = 0,01%.",
    },
    {
      label: "Funding-риск",
      value: ctx.funding_cost_bps === null || ctx.funding_cost_bps === undefined ? "—" : formatBps(ctx.funding_cost_bps, 2, false),
      help: "Ожидаемый неблагоприятный funding за горизонт удержания. Funding — периодические платежи между long и short на perpetual futures.",
    },
  ];
}

function formatBotLifetimeValue(params = {}) {
  const plan = params.trade_plan || {};
  const horizon = plan.expected_horizon || {};
  const minHours = firstFiniteValue(
    [horizon, plan, params],
    ["min_hours", "min_holding_hours", "bot_lifetime_min_hours", "runtime_min_hours"]
  );
  const maxHours = firstFiniteValue(
    [horizon, plan, params],
    ["max_hours", "max_holding_hours", "bot_lifetime_hours", "bot_lifetime_max_hours", "runtime_hours", "runtime_max_hours", "label_horizon_hours"]
  );
  const minText = formatHoursValue(minHours);
  const maxText = formatHoursValue(maxHours);
  if (minText && maxText && minText !== maxText) return `${minText} — ${maxText}`;
  if (maxText) return `до ${maxText}`;
  if (minText) return `от ${minText}`;
  return "—";
}

function formatPositionSizeValue(notional, qty, baseAsset = "") {
  const parts = [];
  if (notional !== null && notional !== undefined) parts.push(formatUsdValue(notional));
  if (qty !== null && qty !== undefined && Number.isFinite(Number(qty))) {
    const qtyText = formatDotNumber(qty, 8, false);
    parts.push(baseAsset ? `${qtyText} ${baseAsset}` : qtyText);
  }
  return parts.length ? parts.join(" · ") : "—";
}

function buildOperatorFieldSpecs(it, ov) {
  const params = (it || {}).params || {};
  const economics = params.economics || {};
  const sizing = params.sizing || {};
  const rangeValue = `${ov.rangeLower} — ${ov.rangeUpper}`;
  const marginRequired = firstFiniteValue(
    [sizing, economics, params],
    ["estimated_margin_required_usdt", "margin_required_usdt", "capital_required_usdt", "margin_usdt", "investment_usdt"]
  );
  const leverage = Math.max(1, Number(params.leverage || 1));
  const positionNotional = firstFiniteValue(
    [sizing, economics, params],
    [
      "estimated_max_position_notional_usdt",
      "max_position_notional_usdt",
      "estimated_total_order_notional_usdt",
      "total_order_notional_usdt",
      "position_notional_usdt",
      "notional_usdt",
    ]
  ) ?? (marginRequired !== null && Number.isFinite(leverage) ? marginRequired * leverage : null);
  const symbolParts = splitLinearSymbol((it || {}).symbol);
  const plan = params.trade_plan || {};
  const referencePrice = firstFiniteValue([plan, params], ["reference_price", "price_ref"]);
  const explicitPositionQty = firstFiniteValue(
    [sizing, economics, params],
    ["estimated_position_qty", "position_qty", "total_qty", "estimated_total_qty", "max_position_qty"]
  );
  const positionQty = explicitPositionQty ?? (
    positionNotional !== null && referencePrice !== null && referencePrice > 0
      ? positionNotional / referencePrice
      : null
  );
  const capitalValue = formatUsdValue(marginRequired);
  const positionValue = formatPositionSizeValue(positionNotional, positionQty, symbolParts?.base || "");
  const botLifetimeValue = formatBotLifetimeValue(params);
  const exitMath = ((it || {}).directional_exit_levels || {}).trade_math || {};
  const tpDistancePct = toFiniteNumber(exitMath.take_profit_distance_pct);
  const slDistancePct = toFiniteNumber(exitMath.stop_loss_distance_pct);
  const rrValue = toFiniteNumber(exitMath.risk_reward);
  const distanceValue = tpDistancePct === null && slDistancePct === null
    ? "—"
    : `TP ${tpDistancePct === null ? "—" : formatPercentDot(tpDistancePct, 2, false)} / SL ${slDistancePct === null ? "—" : formatPercentDot(slDistancePct, 2, false)}`;
  const riskRewardValue = rrValue === null ? "—" : formatDotNumber(rrValue, 3, false);
  const fields = [
    { label: "Сторона", value: directionRu((it || {}).direction), mono: false, help: "Направление идеи: лонг зарабатывает на росте, шорт — на снижении. Нейтральная grid-логика не должна подменяться направленным TP/SL." },
    { label: "Размер позиции", value: positionValue, copyValue: positionNotional !== null ? formatDotNumber(positionNotional, 4, false) : positionValue, mono: true, help: "Оценочная максимальная экспозиция бота. Это не маржа: при плече экспозиция больше внесённой маржи." },
    { label: "Время работы", value: botLifetimeValue, copyValue: botLifetimeValue, help: "Рекомендуемый горизонт удержания бота, а не срок действия самой рекомендации." },
    { label: "Маржа", value: capitalValue, copyValue: marginRequired !== null ? formatDotNumber(marginRequired, 4, false) : capitalValue, help: "Оценочная сумма USDT, которую нужно выделить под бота с указанным плечом." },
    { label: "Диапазон входа", value: rangeValue, mono: true, help: "Нижняя и верхняя границы основного диапазона сетки, которые оператор переносит в Bybit." },
    { label: "Цена входа", value: ov.entryRef, mono: true, help: "Расчётная цена входа из рекомендации. Используется при создании бота и остаётся обязательным полем основной панели." },
    { label: "Кол-во сеток", value: params.grid_count ?? plan.grid_count ?? params.grid_levels ?? "—", help: "Количество ценовых интервалов сетки. Должно соответствовать ограничениям Bybit Futures Grid." },
    { label: "Плечо", value: ov.leverage, help: "Кредитное плечо linear USDT futures. Увеличивает и прибыль, и риск ликвидации." },
    { label: ov.takeProfitLabel || "Take Profit", value: ov.takeProfitValue, mono: true, help: "Take Profit — уровень фиксации прибыли. Для лонга он выше входа, для шорта ниже входа." },
    { label: ov.stopLossLabel || "Stop Loss", value: ov.stopLossValue, mono: true, help: "Stop Loss / kill-switch — защитный уровень остановки убытка. Для лонга ниже входа, для шорта выше входа." },
    { label: "TP/SL дистанция", value: distanceValue, mono: true, help: "Направленные расстояния от расчётного входа до TP и SL. Для short TP считается вниз, SL — вверх; знак не инвертируется форматированием." },
    { label: "Risk/Reward TP/SL", value: riskRewardValue, mono: true, help: "Отношение потенциальной прибыли к потенциальному убытку по тем же направленным TP/SL уровням, которые валидирует backend." },
  ];
  return fields.filter(f => f.value !== undefined && f.value !== null && f.value !== "");
}

function factorNameRu(name) {
  const mapping = {
    range_score: "Диапазонность",
    coherence: "Согласованность таймфреймов",
    regime_confidence: "Уверенность режима",
    effective_sentiment: "Сентимент",
    direction_strength: "Сила направления",
    trend_strength: "Трендовость",
    atr_pct: "Волатильность ATR",
    execution_cost_bps: "Издержки исполнения",
    spread_bps: "Спред",
  };
  return mapping[name] || name || "factor";
}

function factorItemHtml(factor, tone = "positive") {
  if (!factor) return "";
  const cls = tone === "positive" ? "factor-item positive" : "factor-item negative";
  const msg = factor.msg || factor.reason || factorNameRu(factor.name);
  const weight = Number(factor.weight);
  const weightText = Number.isFinite(weight) ? `${weight >= 0 ? "+" : ""}${formatDotNumber(weight, 2)}` : "—";
  return `
    <div class="${cls}">
      <div class="factor-sign">${tone === "positive" ? "+" : "−"}</div>
      <div class="factor-body">
        <div class="factor-msg">${escapeHtml(msg)}</div>
        <div class="factor-meta">${escapeHtml(factorNameRu(factor.name))} · вес ${escapeHtml(weightText)}</div>
      </div>
    </div>
  `;
}

function factorGroupHtml(title, factors, tone = "positive") {
  const list = Array.isArray(factors) ? factors.slice(0, 4) : [];
  const body = list.length ? list.map(item => factorItemHtml(item, tone)).join("") : `<div class="helper-text">Нет выраженных факторов.</div>`;
  return `<div class="factor-group"><h4>${escapeHtml(title)}</h4>${body}</div>`;
}

function buildTechPayload(it) {
  const reasons = (it || {}).reasons || {};
  return {
    rec_id: it.rec_id,
    score_raw: it.score,
    score_ui: ensureUiScoreMeta(it),
    confidence: it.confidence,
    expected_rr: it.expected_rr,
    blocks: it.blocks || [],
    cost_model: reasons.cost_model || {},
    market_shock: reasons.market_shock || {},
    fast_veto: reasons.fast_veto || {},
    direction_agg: reasons.direction_agg || {},
    sentiment_agg: reasons.sentiment_agg || {},
    bybit_meta: it.bybit_meta || {},
    bybit_plan_validation: it.bybit_plan_validation || {},
    operator_decision_context: it.operator_decision_context || {},
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
  const llmReview = reasons.llm_review || null;
  const blocks = it.blocks || [];
  const bybitValidation = it.bybit_plan_validation || {};
  const riskReport = params.risk_report || {};
  const riskReportRejected = Array.isArray(riskReport.rejection_reasons) ? riskReport.rejection_reasons : [];
  const riskReportWarnings = Array.isArray(riskReport.warnings) ? riskReport.warnings : [];
  const bybitErrors = Array.isArray(bybitValidation.errors) ? bybitValidation.errors : [];
  const bybitWarnings = Array.isArray(bybitValidation.warnings) ? bybitValidation.warnings : [];
  const ov = buildOperatorValues(it);
  const operatorFields = buildOperatorFieldSpecs(it, ov);
  const priceFreshnessFields = buildPriceFreshnessFields(it, ov);
  const riskEconomicsFields = buildRiskEconomicsFields(it);
  const techPayload = JSON.stringify(buildTechPayload(it), null, 2);

  $("details").dataset.tech = techPayload;
  $("details").dataset.recId = it.rec_id;
  updateDetailsHeaderLinks(it);

  const launchable = isLaunchableGridRecommendation(it);
  const scoreMeta = ensureUiScoreMeta(it);
  const status = String(it.status || "").trim().toLowerCase();
  const explicitHardBlocked = bybitErrors.length > 0 || blocks.length > 0 || riskReportRejected.length > 0 || status === "blocked";
  // risk_report.decision is intentionally conservative for pending async-LLM holds:
  // backend may store it as not_recommended until the reviewer finalizes the row.
  // Therefore only the persisted operator status may render the score/risk no_trade copy.
  const noTradeDecision = status === "no_trade";
  const pendingDecision = status === "pending";
  const decisionClass = launchable ? "go" : explicitHardBlocked ? "stop" : "wait";
  const decisionTitle = launchable
    ? "Можно запускать после предпроверки"
    : explicitHardBlocked
      ? "Не запускать"
      : noTradeDecision
        ? "Не запускать сейчас"
        : pendingDecision
          ? "Ждать LLM-проверку"
          : "Ждать / перепроверить";
  const decisionText = launchable
    ? "Проверьте цену, актуальность, риск и экономику; затем используйте блок параметров запуска для создания бота."
    : explicitHardBlocked
      ? "Есть жёсткий блокер, запрещающий ручное создание grid-бота. Причина показана ниже."
      : noTradeDecision
        ? "no_trade означает: grid сейчас не запускать. Это не технический блокер Bybit; решение принято обязательными launch-проверками, а не относительным рангом в таблице."
        : pendingDecision
          ? "Рекомендация удержана до завершения LLM-проверки. Это не no_trade и не Bybit/preflight-блокер; дождитесь финального статуса recommended/active либо отказа."
          : "Рекомендация пока не готова к ручному запуску. Дождитесь новой публикации или live-предпроверки.";

  const llmDirection = llmReview?.execution_direction || llmReview?.thesis_direction || "neutral";
  const llmRecommendation = llmReview ? directionRu(llmDirection) : "нет данных";
  const llmProbability = llmReview ? formatProbability(llmReview.confidence) : "—";
  const llmAgreement = llmReview?.agree_with_engine === true
    ? "совпадает"
    : llmReview?.agree_with_engine === false
      ? "расходится"
      : "н/д";
  const llmSummary = llmReview?.summary || llmReview?.error || "";

  const noTradeReasonItems = noTradeDecision && !explicitHardBlocked
    ? [{
        code: "NO_TRADE",
        msg: noTradeDecisionMessage(it, scoreMeta),
        critical: false,
      }]
    : [];
  const factorWarnings = riskReportWarnings.length
    ? []
    : (reasons.top_negative_factors || []).slice(0, 4).map(item => ({ code: "WARN", msg: item.msg || item.text || item.feature || "", critical: false }));
  const blockerItems = [
    ...blocks.map(b => ({ code: b.code || "BLOCK", msg: b.msg || "" , critical: true })),
    ...riskReportRejected.map(msg => ({ code: "RISK", msg, critical: true })),
    ...bybitErrors.map(b => ({ code: b.code || "BYBIT", msg: b.msg || "", critical: true })),
    ...noTradeReasonItems,
    ...riskReportWarnings.slice(0, 4).map(msg => ({ code: "WARN", msg, critical: false })),
    ...factorWarnings,
    ...bybitWarnings.slice(0, 4).map(b => ({ code: b.code || "BYBIT_WARN", msg: b.msg || "", critical: false })),
  ].slice(0, 8);
  const blockersTitle = explicitHardBlocked
    ? "Блокеры / предупреждения"
    : noTradeDecision
      ? "Почему запуск не рекомендован / предупреждения"
      : "Предупреждения";
  const blockersCardClass = explicitHardBlocked ? "launch-blockers-card" : "launch-warnings-card";
  const blockersHtml = blockerItems.length
    ? `
      <div class="operator-card ${blockersCardClass}">
        <h3>${escapeHtml(blockersTitle)}</h3>
        <div class="small-blocks">
          ${blockerItems.map(b => `<div class="small-block ${b.critical ? "small-block-critical" : ""}"><code>${escapeHtml(b.code)}</code><br>${escapeHtml(b.msg)}</div>`).join("")}
        </div>
      </div>
    `
    : "";

  return `
    <div class="operator-sheet compact-details-sheet operator-minimal-sheet">
      <div class="operator-card operator-decision-card ${decisionClass}">
        <div class="decision-title-row">
          <div>
            <h3>${escapeHtml(it.symbol)} · ${escapeHtml(decisionTitle)}</h3>
            <div class="operator-subtitle operator-subtitle-inline">${directionBadge(it.direction)}<span class="operator-sub-sep">·</span>${statusBadgeHtml(it.status)}</div>
          </div>
          <button class="ghost-chip" data-act="show-tech">Техподробности</button>
        </div>
        <div class="decision-text">${escapeHtml(decisionText)}</div>
      </div>

      ${launchDecisionDiagnosticsHtml(it, scoreMeta)}

      <div class="operator-card price-freshness-card">
        <h3>Цена и актуальность</h3>
        <div class="operator-grid two decision-context-grid">
          ${priceFreshnessFields.map(field => fieldBox(field.label, field.value, field.copyValue ?? field.value, field.mono ? "field-input-mono" : "", field.help || "")).join("")}
        </div>
      </div>

      <div class="operator-card risk-economics-card">
        <h3>Риск и экономика запуска</h3>
        <div class="operator-grid two decision-context-grid">
          ${riskEconomicsFields.map(field => fieldBox(field.label, field.value, field.copyValue ?? null, field.mono ? "field-input-mono" : "", field.help || "")).join("")}
        </div>
      </div>

      <div class="operator-card primary-launch-card">
        <h3>Параметры запуска Bybit Futures Grid</h3>
        <div class="operator-grid two minimal-launch-grid">
          ${operatorFields.map(field => fieldBox(field.label, field.value, field.copyValue ?? field.value, field.mono ? "field-input-mono" : "", field.help || "")).join("")}
        </div>
      </div>

      <div class="operator-card llm-operator-card">
        <h3>LLM-рекомендация</h3>
        <div class="operator-grid three minimal-llm-grid">
          ${fieldBox("Рекомендация LLM", llmRecommendation, null, "", "LLM — языковая модель, которая дополнительно проверяет идею. Это не самостоятельное разрешение запуска без серверных и предпусковых риск-проверок.")}
          ${fieldBox("Вероятность LLM", llmProbability, null, "", "Уверенность LLM в собственном выводе. Это не биржевая вероятность прибыли и не замена риск-проверкам.")}
          ${fieldBox("Сравнение с алгоритмом", llmAgreement, null, "", "Показывает, совпадает ли вывод LLM с направлением и исполнением алгоритма.")}
        </div>
        ${llmSummary ? `<div class="llm-summary-box compact-llm-summary">${escapeHtml(llmSummary)}</div>` : `<div class="helper-text">LLM-проверка отсутствует для этой рекомендации.</div>`}
      </div>

      ${blockersHtml}
    </div>
  `;
}

function pillStatus(status) {
  let cls = "pill";
  if (status === "recommended" || status === "active") cls += " good";
  else if (status === "blocked") cls += " bad";
  else cls += " warn";
  return `<span class="${cls}">${escapeHtml(status || "—")}</span>`;
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
      const fitRows = Number(info?.n_samples || 0);
      const dropped = Number(info?.rows_dropped_for_fit || Math.max(0, total - fitRows) || 0);
      const droppedText = dropped > 0 ? `; не вошло в feature-fit=${dropped}` : "";
      return `${botType}: калибратор активен (LogReg + Platt, fit_rows=${fitRows}/${total}; побед=${wins}, поражений=${losses}${droppedText}).`;
    }
    return `${botType}: включён Platt-only (fit_rows=${Number(info.n_samples || 0)} / ${total}; побед=${wins}, поражений=${losses}).`;
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
      header.title = `Калибровка продукта готова (${fittedBots.length}/${botCalibs.length}).`;
      banner.classList.add("hidden");
    } else {
      header.textContent = fittedBots.length > 0 ? "Увер ~" : "Увер ?";
      header.title = fittedBots.length > 0
        ? `Калибровка готова частично (${fittedBots.length}/${botCalibs.length}); глобальная модель считается диагностической и не используется как fallback.`
        : "Калибровка продукта ещё не готова.";
      banner.classList.remove("hidden");
      const count = Number(statusPayload?.outcome_count || 0);
      const needed = Number(statusPayload?.calib_min_samples || 80);
      const pct = needed > 0 ? Math.min(100, Math.round(count / needed * 100)) : 0;
      const readiness = botCalibs.length > 0
        ? `Готово: ${fittedBots.length}/${botCalibs.length}${logregBots.length ? ` (LogReg: ${logregBots.length})` : ""}. `
        : "";
      $("calibProgress").textContent = `${readiness}Всего исходов: ${count}. Глобальная калибровка отображается только как диагностика; inference опирается на продуктовую модель.`;
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
  if (botTypes.length > 3) messages.push(`И ещё ${botTypes.length - 3} внутренних сегмента.`);

  const primaryBot = botTypes.length === 1 ? botTypes[0] : null;
  const primaryInfo = primaryBot ? botCalibs[primaryBot] : null;
  const effective = Number(primaryInfo?.effective_samples || 0);
  const needed = Number(primaryInfo?.min_samples || statusPayload?.calib_min_samples || 80);
  const pct = needed > 0 ? Math.min(100, Math.round(effective / needed * 100)) : 0;

  banner.classList.remove("hidden");
  if (primaryBot) {
    const title = primaryInfo?.fitted
      ? (primaryInfo?.logreg_active
        ? `Futures Grid: калибратор активен`
        : `Futures Grid: работает Platt-only`) 
      : `Futures Grid: калибратор не обучен`;
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

function formatTs(ts) {
  if (!ts) return "—";
  const d = new Date(Number(ts) * 1000);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("ru-RU", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function formatAgeHuman(sec) {
  if (sec === null || sec === undefined || sec === "") return "—";
  const s = Math.max(0, Number(sec));
  if (!Number.isFinite(s)) return "—";
  if (s < 60) return `${Math.round(s)}с`;
  if (s < 3600) return `${Math.round(s / 60)}м`;
  if (s < 86400) return `${(s / 3600).toFixed(1)}ч`;
  return `${(s / 86400).toFixed(1)}д`;
}

function renderDirectionBadge(dir) {
  const value = String(dir || "neutral").toLowerCase();
  const cls = value === "long" ? "dir-long" : value === "short" ? "dir-short" : "dir-neu";
  return `<span class="dir-badge ${cls}">${escapeHtml(directionRu(value))}</span>`;
}

function renderOutcomeResult(success) {
  const ok = Number(success) === 1;
  return `<span class="outcome-result ${ok ? "outcome-result-win" : "outcome-result-loss"}">${ok ? "Win" : "Loss"}</span>`;
}

function renderNeutralSourceTag(source) {
  if (!source) return "—";
  if (source === "futures_neutral") {
    return `<span class="neutral-note neutral-note-neutralized">futures-neutral</span>`;
  }
  if (source === "true_neutral") {
    return `<span class="neutral-note neutral-note-true">true neutral</span>`;
  }
  return `<span class="neutral-note">${escapeHtml(source)}</span>`;
}

function renderLlmStatusBadge(status) {
  const value = String(status || "none").toLowerCase();
  let cls = "llm-badge llm-badge-neutral";
  if (value === "ok") cls = "llm-badge llm-badge-ok";
  else if (value === "pending") cls = "llm-badge llm-badge-pending";
  else if (value === "error") cls = "llm-badge llm-badge-error";
  else if (value === "skipped") cls = "llm-badge llm-badge-skipped";
  else if (value === "none") cls = "llm-badge llm-badge-none";
  return `<span class="${cls}">${escapeHtml(value)}</span>`;
}

function renderAgreementBadge(agree) {
  if (agree === true) return '<span class="llm-badge llm-badge-agree">совпадает</span>';
  if (agree === false) return '<span class="llm-badge llm-badge-disagree">расходится</span>';
  return '<span class="llm-badge llm-badge-neutral">н/д</span>';
}

function renderLlmFlagList(flags) {
  const items = Array.isArray(flags) ? flags.filter(Boolean) : [];
  if (!items.length) return '<div class="helper-text">Риск-флаги не указаны.</div>';
  return `<div class="tag-list">${items.map(flag => `<span class="tag-chip">${escapeHtml(flag)}</span>`).join("")}</div>`;
}

function formatReviewerModel(llm) {
  if (!llm || typeof llm !== "object") return "—";
  const provider = String(llm.provider || "").trim();
  const model = String(llm.model || "").trim();
  if (provider && model) return `${provider}/${model}`;
  return provider || model || "—";
}

function buildLlmReviewCardHtml(llm, engineDirection) {
  if (!llm || typeof llm !== "object") {
    return `
      <div class="operator-card">
        <h3>LLM reviewer</h3>
        <div class="helper-text">По этой рекомендации reviewer не запускался или данные ревью недоступны.</div>
      </div>
    `;
  }

  const status = llm.status || "unknown";
  const confidence = llm.confidence === null || llm.confidence === undefined ? "—" : formatDotNumber(llm.confidence, 2);
  const mode = llm.mode || "—";
  const gateDecision = llm.gate_decision || "—";
  const regimeView = llm.regime_view || "—";
  const summary = llm.summary || llm.error || "—";
  const source = llm.source || (llm.cached ? "cache" : "live");
  const freshness = llm.cache_age_sec === null || llm.cache_age_sec === undefined ? "—" : formatAgeHuman(llm.cache_age_sec);
  const reviewTs = llm.review_ts ? formatTs(llm.review_ts) : "—";
  const inheritedFrom = llm.inherited_from_rec_id || "—";
  const errorLine = llm.error ? `<div class="helper-text llm-error-text">Ошибка reviewer: ${escapeHtml(String(llm.error))}</div>` : "";

  return `
    <div class="operator-card llm-review-card">
      <h3>LLM reviewer</h3>
      <div class="operator-grid">
        ${fieldBox("Статус", status, null)}
        ${fieldBox("Провайдер / модель", formatReviewerModel(llm), null)}
        ${fieldBox("Источник", source, null)}
        ${fieldBox("Режим", mode, null)}
        ${fieldBox("Gate decision", gateDecision, null)}
        ${fieldBox("Время review", reviewTs, null)}
        ${fieldBox("Свежесть review", freshness, null)}
        ${fieldBox("Наследовано от rec_id", inheritedFrom, null)}
        ${fieldBox("Engine direction", directionRu(engineDirection || "neutral"), null)}
        ${fieldBox("LLM thesis", directionRu(llm.thesis_direction || "neutral"), null)}
        ${fieldBox("LLM execution", directionRu(llm.execution_direction || "neutral"), null)}
        ${fieldBox("Совпадение с движком", llm.agree_with_engine === true ? "Да" : llm.agree_with_engine === false ? "Нет" : "Н/Д", null)}
        ${fieldBox("Уверенность LLM", confidence, null)}
        ${fieldBox("Regime view", regimeView, null)}
      </div>
      <div class="llm-review-row">
        <div class="llm-review-badges">
          ${renderLlmStatusBadge(status)}
          ${renderAgreementBadge(llm.agree_with_engine)}
          ${renderDirectionBadge(llm.execution_direction || "neutral")}
        </div>
        <div class="helper-text">Если LLM-reviewer включён, запуск удерживается в pending до OK-вердикта; при таймауте идея переводится в no_trade fail-closed.</div>
      </div>
      <div class="llm-summary-box">${escapeHtml(summary)}</div>
      ${errorLine}
      <div class="modal-section-title" style="margin-top:10px">Risk flags</div>
      ${renderLlmFlagList(llm.risk_flags)}
    </div>
  `;
}

function renderHealthStatus(status) {
  const value = String(status || "missing").toLowerCase();
  const cls = value === "ok"
    ? "health-status-ok"
    : value === "stale"
      ? "health-status-stale"
      : value === "disabled"
        ? "health-status-stale"
        : "health-status-missing";
  return `<span class="health-status ${cls}">${escapeHtml(value)}</span>`;
}

function renderModalSummaryCards(items = []) {
  if (!items.length) return "";
  return `<div class="modal-summary-grid">${items.map(item => `
    <div class="modal-summary-card">
      <div class="modal-summary-label">${escapeHtml(item.label || "")}</div>
      <div class="modal-summary-value">${item.html !== undefined ? item.html : escapeHtml(item.value ?? "—")}</div>
    </div>
  `).join("")}</div>`;
}

function renderSampleSizeBadge(total) {
  const n = Math.max(0, Number(total || 0));
  let cls = "sample-badge sample-badge-low";
  let label = "мало";
  if (n >= 30) {
    cls = "sample-badge sample-badge-high";
    label = "устойчиво";
  } else if (n >= 10) {
    cls = "sample-badge sample-badge-mid";
    label = "умеренно";
  }
  return `<span class="${cls}">n=${escapeHtml(String(n))} · ${escapeHtml(label)}</span>`;
}

function formatShare(part, total, digits = 1) {
  const p = Number(part || 0);
  const t = Number(total || 0);
  if (!(t > 0)) return "—";
  return `${((p / t) * 100).toFixed(digits)}%`;
}

function compareRowsByOutcome(rows = []) {
  const total = rows.reduce((acc, row) => acc + Number(row?.total || 0), 0);
  const wins = rows.reduce((acc, row) => acc + Number(row?.wins || 0), 0);
  const ret = rows.reduce((acc, row) => acc + (Number(row?.avg_ret || 0) * Number(row?.total || 0)), 0);
  return {
    total,
    wins,
    winRate: total > 0 ? wins / total : null,
    avgRet: total > 0 ? ret / total : null,
  };
}

function renderOutcomeInsightCards(items = []) {
  if (!items.length) return '<div class="helper-text">Явных содержательных перекосов по текущим агрегатам не обнаружено.</div>';
  return `<div class="outcome-insight-grid">${items.map(item => `
    <div class="outcome-insight-card ${escapeHtml(item.kind || "neutral")}">
      <div class="outcome-insight-title">${escapeHtml(item.title || "Наблюдение")}</div>
      <div class="outcome-insight-body">${escapeHtml(item.body || "")}</div>
    </div>
  `).join("")}</div>`;
}

function buildOutcomeDiagnostics(llmByEngine = [], neutralBreakdown = [], summary = {}) {
  const insights = [];
  const comparableRows = (Array.isArray(llmByEngine) ? llmByEngine : []).filter(row => {
    const status = String(row?.llm_status || "").toLowerCase();
    return status === "ok";
  });

  const byEngine = new Map();
  for (const row of comparableRows) {
    const key = String(row?.engine_execution_direction || "neutral");
    if (!byEngine.has(key)) byEngine.set(key, []);
    byEngine.get(key).push(row);
  }

  for (const [engineDir, rows] of byEngine.entries()) {
    const agree = compareRowsByOutcome(rows.filter(row => row?.llm_alignment === "agree"));
    const disagree = compareRowsByOutcome(rows.filter(row => row?.llm_alignment === "disagree"));
    if (!(agree.total > 0 && disagree.total > 0) || Math.min(agree.total, disagree.total) < 3) continue;
    const delta = (disagree.winRate ?? 0) - (agree.winRate ?? 0);
    if (Math.abs(delta) < 0.12) continue;
    const better = delta > 0 ? "расхождения" : "совпадения";
    insights.push({
      kind: delta > 0 ? "warn" : "good",
      title: `LLM vs algo: ${directionRu(engineDir)}`,
      body: `${better} выглядят сильнее: ${(agree.winRate * 100).toFixed(1)}% vs ${(disagree.winRate * 100).toFixed(1)}% WR при n=${agree.total}/${disagree.total}. Стоит проверить, не смешаны ли разные подтипы внутри ${directionRu(engineDir).toLowerCase()}.`,
    });
  }

  const trueNeutral = (Array.isArray(neutralBreakdown) ? neutralBreakdown : []).filter(row => row?.neutral_source === "true_neutral");
  const neutralized = (Array.isArray(neutralBreakdown) ? neutralBreakdown : []).filter(row => row?.neutral_source === "futures_neutral");
  const tn = compareRowsByOutcome(trueNeutral);
  const sn = compareRowsByOutcome(neutralized);
  if (tn.total > 0 && sn.total > 0) {
    const delta = (sn.winRate ?? 0) - (tn.winRate ?? 0);
    if (Math.abs(delta) >= 0.12) {
      insights.push({
        kind: delta < 0 ? "warn" : "neutral",
        title: "Neutral нужно делить на классы",
        body: `Истинный neutral и neutralized-short ведут себя по-разному: ${(tn.winRate * 100).toFixed(1)}% vs ${(sn.winRate * 100).toFixed(1)}% WR при n=${tn.total}/${sn.total}. Их нельзя держать в одной строке.`,
      });
    }
  }

  const duplicates = Number(summary?.deduped_duplicates || 0);
  const rawTotal = Number(summary?.raw_total || 0);
  if (duplicates > 0 && rawTotal > 0) {
    insights.push({
      kind: "neutral",
      title: "Повторы публикаций отфильтрованы",
      body: `${duplicates} из ${rawTotal} сырьевых строк исключены из win-rate как подтверждения уже открытой publication-chain. Для оператора это правильно: иначе окно завышало бы уверенность.`,
    });
  }

  return insights.slice(0, 6);
}

function buildModalTable(columns, rows, { emptyText = "Нет данных", rowClass, compact = false, maxHeight } = {}) {
  const head = columns.map(col => `<th>${escapeHtml(col.label || "")}</th>`).join("");
  const body = (rows && rows.length)
    ? rows.map((row, idx) => {
        const cls = rowClass ? rowClass(row, idx) : "";
        const cells = columns.map(col => {
          const content = col.render ? col.render(row, idx) : escapeHtml(row?.[col.key] ?? "—");
          const tdCls = col.className ? ` class="${escapeHtml(col.className)}"` : "";
          return `<td${tdCls}>${content}</td>`;
        }).join("");
        return `<tr${cls ? ` class="${escapeHtml(cls)}"` : ""}>${cells}</tr>`;
      }).join("")
    : `<tr><td colspan="${columns.length}" class="modal-table-empty">${escapeHtml(emptyText)}</td></tr>`;

  const tableClasses = ["table", "modal-table"];
  if (compact || columns.length >= 10) tableClasses.push("modal-table-compact");
  const wrapStyle = Number.isFinite(Number(maxHeight)) && Number(maxHeight) > 0
    ? ` style="max-height:${Number(maxHeight)}px"`
    : "";

  return `
    <div class="modal-table-wrap"${wrapStyle}>
      <table class="${tableClasses.join(" ")}">
        <thead><tr>${head}</tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>
  `;
}

function showModal(title, obj) {
  const body = $("modalBody");
  $("modalTitle").textContent = title;
  body.classList.remove("modal-html");
  body.classList.add("pre");
  body.textContent = typeof obj === "string" ? obj : JSON.stringify(obj, null, 2);
  $("modal").classList.remove("hidden");
}

function showModalHtml(title, html) {
  const body = $("modalBody");
  $("modalTitle").textContent = title;
  body.classList.remove("pre");
  body.classList.add("modal-html");
  body.innerHTML = html;
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
        `Сент.* ${v >= 0 ? "+" : ""}${v.toFixed(2)} (${regime})${flag}`;
      $("sentiment-badge").title = "Эвристический сентимент: RSS/Reddit/market context. Это не полноценный semantic news-анализ статей.";
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
  const venue = "linear";
  const topN = Number($("topN").value || 50);
  const minConf = Number($("minConf").value || 0);

  const qs = new URLSearchParams();
  const showRecommended = $("showRecommended")?.checked ?? true;
  const showPending    = $("showPending")?.checked ?? false;
  const showBlocked     = $("showBlocked")?.checked ?? false;
  const showNoTrade     = $("showNoTrade")?.checked ?? false;
  const showSuppressed  = $("showSuppressed")?.checked ?? false;

  if (venue) qs.set("venue", venue);
  qs.set("top_n", String(topN));
  qs.set("min_conf", String(minConf));
  qs.set("show_recommended", String(showRecommended));
  qs.set("show_pending", String(showPending));
  qs.set("show_blocked", String(showBlocked));
  qs.set("show_no_trade", String(showNoTrade));
  qs.set("show_suppressed", String(showSuppressed));
  qs.set("snapshot", "latest_operator");

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
  uiScoreMetaById = computeUiScoreMetaMap(items);
  renderRecoTable(items);
  updateCalibrationUi(items);

  const banner = $("noTrade");
  const hasActionable = items.some(it => it.status === "recommended" || it.status === "active");
  if (!hasActionable) banner.classList.remove("hidden");
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
    } else if (sortCol === "score") {
      av = ensureUiScoreMeta(a, items).percentile ?? -1;
      bv = ensureUiScoreMeta(b, items).percentile ?? -1;
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
  let hasActionable = false;
  sorted.forEach((it, i) => {
    if (it.status === "recommended" || it.status === "active") hasActionable = true;
    const dirAgg = (it.reasons || {}).direction_agg || {};
    const dirConf = dirAgg.direction_confidence_calibrated ?? dirAgg.direction_confidence;
    const scoreUi = ensureUiScoreMeta(it, items);
    const tr = document.createElement("tr");
    if (it.status === "recommended" || it.status === "active") tr.classList.add("row-recommended");
    tr.innerHTML = `
      <td>${i + 1}</td>
      <td>
        <div class="symbol-cell">
          <b>${escapeHtml(it.symbol || "—")}</b>
          ${symbolLinksHtml(it)}
        </div>
      </td>
      <td>${directionBadge(it.direction)}</td>
      <td>${dirConfCell(dirConf)}</td>
      <td>${scoreUiCellHtml(scoreUi)}</td>
      <td>${confCell(it)}</td>
      <td>${fmt(it.expected_rr)}</td>
      <td data-cell="status">${pillStatus(it.status)}</td>
      <td><button class="btn tiny" data-act="details" data-id="${escapeHtml(it.rec_id)}">Карточка</button></td>
    `;
    body.appendChild(tr);
  });
  const banner = $("noTrade");
  if (!hasActionable) {
    const shock = (statusPayload || {}).market_shock || {};
    if (shock && shock.state && shock.state !== "normal") {
      banner.innerHTML = `NO-TRADE: <b>${escapeHtml(shock.title || "Guard")}</b>. ${escapeHtml(shock.operator_note || "Новые входы заблокированы.")}`;
    } else {
      banner.innerHTML = 'NO-TRADE: нет актуальных рекомендаций со статусом <b>recommended</b> или <b>active</b> по текущим фильтрам/гейтам.';
    }
    banner.classList.remove("hidden");
  } else {
    banner.classList.add("hidden");
  }
}
function directionBadge(dir) {
  if (!dir || dir === "neutral") return `<span class="dir-badge dir-neu">• neutral</span>`;
  if (dir === "long")  return `<span class="dir-badge dir-long">▲ long</span>`;
  if (dir === "short") return `<span class="dir-badge dir-short">▼ short</span>`;
  return `<span class="dir-badge dir-neu">• ${escapeHtml(String(dir))}</span>`;
}

// ── details panel ─────────────────────────────────────────────────────────────

async function loadDetails(recId) {
  currentRecId = recId;
  const reqSeq = ++detailsRequestSeq;
  const btn = $("refreshDetailsBtn");
  btn.classList.remove("hidden");
  btn.disabled = true;
  btn.textContent = "…";

  let it;
  try {
    const res = await fetch(`/api/v1/recommendations/${recId}`);
    if (!res.ok) {
      if (reqSeq !== detailsRequestSeq) return;
      clearDetailsHeaderLinks();
      if (res.status === 404) {
        currentRecId = null;
        currentMeta = null;
        btn.classList.add("hidden");
        $("details").textContent = "Карточка больше не существует в текущей БД. Выберите рекомендацию заново.";
      } else {
        $("details").textContent = `Ошибка загрузки деталей (HTTP ${res.status}).`;
        btn.disabled = false;
        btn.textContent = "Обновить";
      }
      return;
    }
    it = await res.json();
  } catch (e) {
    if (reqSeq !== detailsRequestSeq) return;
    clearDetailsHeaderLinks();
    $("details").textContent = `Ошибка сети при загрузке деталей.`;
    btn.classList.remove("hidden");
    btn.disabled = false;
    btn.textContent = "Обновить";
    return;
  }

  if (reqSeq !== detailsRequestSeq) return;
  currentMeta = { venue: it.venue, symbol: it.symbol, bot_type: it.bot_type };
  it.ui_score_meta = ensureUiScoreMeta(it, lastItems);
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
  try { data = await res.json(); } catch (e) { return; }

  const sum = data.summary || {};
  const llm = data.llm_reviewer || {};
  const warmup = data.warmup || {};
  const llmTfText = Array.isArray(llm.tf_secs) && llm.tf_secs.length
    ? llm.tf_secs.map(tf => tf >= 3600 ? `${Math.round(tf / 3600)}h` : `${Math.round(tf / 60)}m`).join(", ")
    : "—";
  const warmupRatio = Number(warmup.ready_ratio || 0);
  const warmupMinRatio = Number(warmup.min_ready_ratio || 0);
  const warmupReadySymbols = Number(warmup.ready_symbols || 0);
  const warmupSymbolsTotal = Number(warmup.symbols_total || 0);
  const symbols = [...(data.symbols || [])].sort((a, b) => {
    const rank = { disabled: 0, missing: 1, stale: 2, ok: 3 };
    const ra = rank[a.status] ?? 9;
    const rb = rank[b.status] ?? 9;
    if (ra !== rb) return ra - rb;
    if (Boolean(b.disabled) !== Boolean(a.disabled)) return Number(b.disabled) - Number(a.disabled);
    if (Number(b.error_count_10m || 0) !== Number(a.error_count_10m || 0)) return Number(b.error_count_10m || 0) - Number(a.error_count_10m || 0);
    return String(a.symbol || "").localeCompare(String(b.symbol || ""), "ru");
  });

  const html = `
    ${renderModalSummaryCards([
      { label: "OK", value: Number(sum.ok || 0) },
      { label: "Stale", value: Number(sum.stale || 0) },
      { label: "Missing", value: Number(sum.missing || 0) },
      { label: "Disabled", value: Number(sum.disabled || 0) },
      { label: "Ready symbols", value: `${warmupReadySymbols}/${warmupSymbolsTotal}` },
      { label: "Ready ratio", value: warmupSymbolsTotal > 0 ? warmupRatio.toFixed(4) : "—" },
      { label: "Min ready ratio", value: warmupMinRatio > 0 ? warmupMinRatio.toFixed(4) : "—" },
      { label: "Ошибки / 10 мин", value: Number(sum.errors_10m || 0) },
      { label: "LLM reviewer", html: renderLlmStatusBadge(llm.enabled ? (llm.mode || "enabled") : "disabled") },
      { label: "Модель", value: llm.model || "—" },
    ])}
    <p class="modal-note">OK в этом окне означает свежие ticker + 1m. Готовность recommender считается отдельно по warmup/readiness и требует достаточной multi-timeframe history.</p>
    <div class="modal-section">
      <div class="modal-section-title">Warm-up / readiness</div>
      ${buildModalTable([
        { label: "Параметр", render: row => escapeHtml(row.name) },
        { label: "Значение", render: row => row.html !== undefined ? row.html : `<span class="wrap">${escapeHtml(row.value ?? "—")}</span>` },
      ], [
        { name: "Ready", html: renderLlmStatusBadge(warmup.ready ? "ok" : "warming_up") },
        { name: "Ready symbols", value: `${warmupReadySymbols}/${warmupSymbolsTotal}` },
        { name: "Ready ratio", value: warmupSymbolsTotal > 0 ? warmupRatio.toFixed(4) : "—" },
        { name: "Min ready ratio", value: warmupMinRatio > 0 ? warmupMinRatio.toFixed(4) : "—" },
        { name: "Min ready symbols", value: warmup.min_ready_symbols ?? "—" },
        { name: "Required TFs", value: Array.isArray(warmup.required_tfs) ? warmup.required_tfs.join(", ") : "—" },
        { name: "Min rows / TF", value: warmup.min_rows_per_tf ?? "—" },
        { name: "Derived on read", value: warmup.derived_on_read ? "yes" : "no" },
      ], { emptyText: "Warm-up status недоступен." })}
    </div>
    <div class="modal-section">
      <div class="modal-section-title">Конфигурация LLM reviewer</div>
      ${buildModalTable([
        { label: "Параметр", render: row => escapeHtml(row.name) },
        { label: "Значение", render: row => row.html !== undefined ? row.html : `<span class="wrap">${escapeHtml(row.value ?? "—")}</span>` },
      ], [
        { name: "Enabled", html: renderLlmStatusBadge(llm.enabled ? "ok" : "disabled") },
        { name: "Mode", value: llm.mode || "—" },
        { name: "Provider / model", value: [llm.provider, llm.model].filter(Boolean).join(" / ") || "—" },
        { name: "Таймфреймы", value: llmTfText },
        { name: "Свечей на ТФ", value: llm.candles_per_tf ?? "—" },
        { name: "Max кандидатов", value: llm.max_candidates ?? "—" },
        { name: "Мин. уверенность", value: llm.min_confidence ?? "—" },
        { name: "Каденс по символу", value: llm.cadence_sec == null ? "—" : `${llm.cadence_sec} сек` },
        { name: "Таймаут pending", value: llm.pending_timeout_sec == null ? "—" : `${llm.pending_timeout_sec} сек` },
      ], { emptyText: "Конфигурация reviewer недоступна." })}
    </div>
    <div class="modal-section">
      <div class="modal-section-title">Журнал здоровья символов</div>
      ${buildModalTable([
        { label: "Символ", render: row => `<span class="wrap">${escapeHtml(row.symbol || "—")}</span>` },
        { label: "Статус", render: row => renderHealthStatus(row.status) },
        { label: "Возраст свечи", render: row => escapeHtml(formatAgeHuman(row.age_sec)) },
        { label: "Последняя свеча", render: row => escapeHtml(formatTs(row.last_candle_ts)) },
        { label: "Последний тикер", render: row => escapeHtml(formatTs(row.last_ticker_ts)) },
        { label: "Ошибки/10м", render: row => escapeHtml(String(Number(row.error_count_10m || 0))) },
        { label: "Stale skip/1ч", render: row => escapeHtml(String(Number(row.stale_skips_1h || 0))) },
        { label: "Disabled", render: row => row.disabled ? '<span class="neutral-note neutral-note-neutralized">yes</span>' : '—' },
      ], symbols, { emptyText: "Нет данных по символам." })}
    </div>
  `;

  showModalHtml("Здоровье символов", html);
}

async function loadOutcomes() {
  const res = await fetch("/api/v1/outcomes/stats");
  let data;
  try { data = await res.json(); } catch (e) { return; }

  const s = data.summary || {};
  const llmSummary = data.llm_summary || {};
  const byExecution = data.by_execution_direction || [];
  const byRaw = data.by_raw_direction || [];
  const directionPairs = data.direction_pairs || [];
  const neutralBreakdown = (data.neutral_breakdown || []).filter(row => String(row?.neutral_source || "") !== "directional");
  const llmAlignment = data.llm_alignment || [];
  const llmByEngine = data.llm_engine_alignment || [];
  const llmMatrix = data.llm_engine_matrix || [];
  const byBot = data.by_bot || [];
  const bySymbol = (data.by_symbol || []).slice(0, 30);
  const recent = (data.recent || []).slice(0, 80);
  const insights = buildOutcomeDiagnostics(llmByEngine, neutralBreakdown, s);

  const total = Number(s.total || 0);
  const llmReviewed = Number(llmSummary.ok_total || 0);
  const llmDisagree = Number(llmSummary.disagree_total || 0);
  const llmDisagreeShare = llmReviewed > 0 ? `${((llmDisagree / llmReviewed) * 100).toFixed(1)}%` : "—";
  const llmErrorShare = llmReviewed > 0
    ? `${((Number(llmSummary.error_total || 0) / llmReviewed) * 100).toFixed(1)}%`
    : (Number(llmSummary.error_total || 0) > 0 ? "есть" : "0%");

  const html = `
    ${renderModalSummaryCards([
      { label: "Корневых исходов", value: total },
      { label: "Win-rate", value: s.win_rate !== null && s.win_rate !== undefined ? `${(Number(s.win_rate) * 100).toFixed(1)}%` : "—" },
      { label: "Avg ret", value: `${Number(s.avg_ret || 0).toFixed(2)}%` },
      { label: "Avg |ret|", value: `${Number(s.avg_abs_ret || 0).toFixed(2)}%` },
      { label: "Повторов убрано", value: Number(s.deduped_duplicates || 0) },
      { label: "Истинный neutral", value: Number(s.true_neutral_total || 0) },
      { label: "Short → neutral", value: Number(s.futures_neutral_total || 0) },
      { label: "LLM reviewed", value: llmReviewed },
      { label: "LLM disagree", value: `${llmDisagree} · ${llmDisagreeShare}` },
      { label: "LLM errors", value: `${Number(llmSummary.error_total || 0)} · ${llmErrorShare}` },
    ])}
    <p class="modal-note">Это окно должно помогать оператору принимать решения, поэтому вверху вынесено только главное: что реально исполнялось, что было нейтральным по-настоящему, где LLM совпадал или спорил с алгоритмом, и какие подтипы статистически ведут себя по-разному. Proxy-исходы считаются только по корневым публикациям одной идеи, чтобы repeated active-подтверждения не раздували win-rate.</p>
    <div class="modal-section">
      <div class="modal-section-title">На что стоит смотреть в первую очередь</div>
      ${renderOutcomeInsightCards(insights)}
    </div>
    <div class="modal-section">
      <div class="modal-section-title">1. Что реально торговалось</div>
      ${buildModalTable([
        { label: "Исполнимое направление", render: row => renderDirectionBadge(row.execution_direction) },
        { label: "Всего", render: row => escapeHtml(String(row.total)) },
        { label: "Доля", render: row => escapeHtml(formatShare(row.total, total)) },
        { label: "Побед", render: row => escapeHtml(String(row.wins)) },
        { label: "WR", render: row => escapeHtml(`${(Number(row.win_rate || 0) * 100).toFixed(1)}%`) },
        { label: "Avg ret", render: row => escapeHtml(fmtPct(row.avg_ret, 2)) },
        { label: "Надёжность", render: row => renderSampleSizeBadge(row.total) },
      ], byExecution, { emptyText: "Исходов пока нет." })}
    </div>
    <div class="modal-section">
      <div class="modal-section-title">2. Что хотел алгоритм и во что это превратилось</div>
      ${buildModalTable([
        { label: "Algo raw", render: row => renderDirectionBadge(row.raw_direction) },
        { label: "Algo exec", render: row => renderDirectionBadge(row.execution_direction) },
        { label: "Neutral class", render: row => renderNeutralSourceTag(row.neutral_source) },
        { label: "Всего", render: row => escapeHtml(String(row.total)) },
        { label: "WR", render: row => escapeHtml(`${(Number(row.win_rate || 0) * 100).toFixed(1)}%`) },
        { label: "Avg ret", render: row => escapeHtml(fmtPct(row.avg_ret, 2)) },
        { label: "Надёжность", render: row => renderSampleSizeBadge(row.total) },
      ], directionPairs, { emptyText: "Исходов пока нет." })}
    </div>
    <div class="modal-section">
      <div class="modal-section-title">3. Neutral нужно читать раздельно</div>
      ${buildModalTable([
        { label: "Класс", render: row => renderNeutralSourceTag(row.neutral_source) },
        { label: "Raw", render: row => renderDirectionBadge(row.raw_direction) },
        { label: "Exec", render: row => renderDirectionBadge(row.execution_direction) },
        { label: "Всего", render: row => escapeHtml(String(row.total)) },
        { label: "Побед", render: row => escapeHtml(String(row.wins)) },
        { label: "WR", render: row => escapeHtml(`${(Number(row.win_rate || 0) * 100).toFixed(1)}%`) },
        { label: "Avg ret", render: row => escapeHtml(fmtPct(row.avg_ret, 2)) },
        { label: "Надёжность", render: row => renderSampleSizeBadge(row.total) },
      ], neutralBreakdown, { emptyText: "Подклассы neutral пока не накопились." })}
    </div>
    <div class="modal-section">
      <div class="modal-section-title">4. LLM против исполнимого направления алгоритма</div>
      ${buildModalTable([
        { label: "Algo exec", render: row => renderDirectionBadge(row.engine_execution_direction) },
        { label: "Статус", render: row => renderLlmStatusBadge(row.llm_status) },
        { label: "Совпадение", render: row => renderAgreementBadge(row.llm_alignment === "agree" ? true : row.llm_alignment === "disagree" ? false : null) },
        { label: "Gate", render: row => `<span class="neutral-note">${escapeHtml(row.llm_gate_decision || "pass")}</span>` },
        { label: "Всего", render: row => escapeHtml(String(row.total)) },
        { label: "Доля внутри algo exec", render: row => escapeHtml(formatShare(row.total, (llmByEngine || []).filter(x => x.engine_execution_direction === row.engine_execution_direction).reduce((acc, x) => acc + Number(x.total || 0), 0))) },
        { label: "WR", render: row => escapeHtml(`${(Number(row.win_rate || 0) * 100).toFixed(1)}%`) },
        { label: "Avg ret", render: row => escapeHtml(fmtPct(row.avg_ret, 2)) },
        { label: "Надёжность", render: row => renderSampleSizeBadge(row.total) },
      ], llmByEngine, { emptyText: "LLM reviewer ещё не оставил следов в созревших исходах." })}
    </div>
    <div class="modal-section">
      <div class="modal-section-title">5. LLM: детальная матрица algo exec → llm exec</div>
      ${buildModalTable([
        { label: "Algo exec", render: row => renderDirectionBadge(row.engine_execution_direction) },
        { label: "LLM exec", render: row => renderDirectionBadge(row.llm_execution_direction) },
        { label: "Совпадение", render: row => renderAgreementBadge(row.llm_alignment === "agree" ? true : row.llm_alignment === "disagree" ? false : null) },
        { label: "Статус", render: row => renderLlmStatusBadge(row.llm_status) },
        { label: "Gate", render: row => `<span class="neutral-note">${escapeHtml(row.llm_gate_decision || "pass")}</span>` },
        { label: "Neutral class", render: row => renderNeutralSourceTag(row.neutral_source) },
        { label: "Всего", render: row => escapeHtml(String(row.total)) },
        { label: "WR", render: row => escapeHtml(`${(Number(row.win_rate || 0) * 100).toFixed(1)}%`) },
        { label: "Avg ret", render: row => escapeHtml(fmtPct(row.avg_ret, 2)) },
        { label: "Надёжность", render: row => renderSampleSizeBadge(row.total) },
      ], llmMatrix, { emptyText: "Детальная матрица LLM пока пуста." })}
    </div>
    <div class="modal-section">
      <div class="modal-section-title">6. Сырой тезис алгоритма</div>
      ${buildModalTable([
        { label: "Algo raw", render: row => renderDirectionBadge(row.raw_direction) },
        { label: "Всего", render: row => escapeHtml(String(row.total)) },
        { label: "Доля", render: row => escapeHtml(formatShare(row.total, total)) },
        { label: "WR", render: row => escapeHtml(`${(Number(row.win_rate || 0) * 100).toFixed(1)}%`) },
        { label: "Avg ret", render: row => escapeHtml(fmtPct(row.avg_ret, 2)) },
        { label: "Надёжность", render: row => renderSampleSizeBadge(row.total) },
      ], byRaw, { emptyText: "Нет сводки по raw direction." })}
    </div>
    <div class="modal-section">
      <div class="modal-section-title">7. По символу (топ 30)</div>
      ${buildModalTable([
        { label: "Символ", render: row => `<span class="wrap">${escapeHtml(row.symbol || "—")}</span>` },
        { label: "Raw direction", render: row => renderDirectionBadge(row.raw_direction) },
        { label: "Execution direction", render: row => renderDirectionBadge(row.execution_direction) },
        { label: "Всего", render: row => escapeHtml(String(row.total)) },
        { label: "WR", render: row => escapeHtml(`${(Number(row.win_rate || 0) * 100).toFixed(1)}%`) },
        { label: "Avg ret", render: row => escapeHtml(fmtPct(row.avg_ret, 2)) },
        { label: "Надёжность", render: row => renderSampleSizeBadge(row.total) },
      ], bySymbol, { emptyText: "Нет данных по символам." })}
    </div>
    <div class="modal-section">
      <div class="modal-section-title">8. Журнал исходов (последние 80)</div>
      ${buildModalTable([
        { label: "Время", render: row => escapeHtml(formatTs(row.ts)) },
        { label: "Символ", render: row => `<span class="wrap">${escapeHtml(row.symbol || "—")}</span>` },
        { label: "Algo raw", render: row => renderDirectionBadge(row.raw_direction) },
        { label: "Algo exec", render: row => renderDirectionBadge(row.execution_direction) },
        { label: "LLM status", render: row => renderLlmStatusBadge(row.llm_review?.status || "none") },
        { label: "LLM thesis", render: row => renderDirectionBadge(row.llm_review?.thesis_direction || "neutral") },
        { label: "LLM exec", render: row => renderDirectionBadge(row.llm_review?.execution_direction || "neutral") },
        { label: "Совпадение", render: row => renderAgreementBadge(row.llm_review?.agree_with_engine) },
        { label: "LLM conf", render: row => row.llm_review?.confidence === null || row.llm_review?.confidence === undefined ? '—' : escapeHtml(formatDotNumber(row.llm_review.confidence, 2)) },
        { label: "Neutral class", render: row => renderNeutralSourceTag(row.neutral_source) },
        { label: "Исход", render: row => renderOutcomeResult(row.success) },
        { label: "Ret", render: row => escapeHtml(fmtPct(Number(row.ret || 0) * 100, 2)) },
        { label: "Горизонт", render: row => escapeHtml(formatAgeHuman(row.horizon_sec)) },
        { label: "LLM summary", className: "wrap", render: row => `<span class="wrap">${escapeHtml(row.llm_review?.summary || row.llm_review?.error || "—")}</span>` },
        { label: "rec_id", className: "wrap", render: row => `<span class="wrap">${escapeHtml(row.rec_id || "—")}</span>` },
      ], recent, { emptyText: "Исходов пока нет. Данные появятся после созревания label horizon.", compact: true, maxHeight: 420 })}
    </div>
  `;

  showModalHtml("Экран исходов / Журнал исходов", html);
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
  if (currentRecId) {
    await loadDetails(currentRecId);
  }
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

          // Update the status cell without depending on the visible column count.
          const statusCell = row.querySelector('[data-cell="status"]');
          if (statusCell) {
            const statusClass = action === "executed" ? "op-executed" : "op-ignored";
            statusCell.innerHTML =
              `<span class="op-status-label ${statusClass}">${escapeHtml(action)}</span>`;
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
  if (!currentRecId) return;
  loadDetails(currentRecId);
});

$("modalClose").addEventListener("click", (e) => { e.stopPropagation(); hideModal(); });
$("modal").addEventListener("click", (e) => { if (e.target.id === "modal") hideModal(); });
$("collectErrJournal").addEventListener("click", (e) => { e.preventDefault(); loadDecisions(); });

["topN", "minConf"].forEach(id => {
  const el = $(id);
  if (el) el.addEventListener("input", () => {
    if (recoDebounce) clearTimeout(recoDebounce);
    recoDebounce = setTimeout(refreshAll, 300);
  });
});

["showRecommended", "showPending", "showBlocked", "showNoTrade", "showSuppressed"].forEach(id => {
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
