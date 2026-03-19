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

function directionRu(dir) {
  if (dir === "long") return "Лонг";
  if (dir === "short") return "Шорт";
  return "Нейтральный";
}

function botTypeLabel(botType) {
  if (botType === "futures_grid") return "futures grid";
  if (botType === "spot_grid") return "spot grid";
  return botType || "—";
}

function botTypePillHtml(botType, compact = false) {
  const label = botTypeLabel(botType);
  const cls = compact ? "bot-type-pill compact" : "bot-type-pill";
  return `<span class="${cls} ${escapeHtml(botType || "other")}">${escapeHtml(label)}</span>`;
}

function venueLabel(venue) {
  if (venue === "linear") return "Фьючерсы";
  if (venue === "spot") return "Спот";
  return venue || "—";
}

function liquidityTierRu(tier) {
  if (tier === "deep") return "Глубокая";
  if (tier === "mid") return "Средняя";
  if (tier === "shallow") return "Тонкая";
  return tier || "—";
}

function marginModeRu(mode) {
  if (mode === "isolated") return "Изолированная";
  if (mode === "cross") return "Кросс";
  if (mode === "cash") return "Cash";
  return mode || "—";
}

function splitSpotSymbol(symbol) {
  const s = String(symbol || "").toUpperCase();
  const quotes = ["USDT", "USDC", "BTC", "ETH", "EUR", "BRL", "TRY"];
  for (const quote of quotes) {
    if (s.endsWith(quote) && s.length > quote.length) {
      return { base: s.slice(0, -quote.length), quote };
    }
  }
  return null;
}

function bybitBotCreateUrl(botType) {
  return botType === "futures_grid"
    ? "https://www.bybit.com/ru-RU/tradingbot/fgrid-create/"
    : "https://www.bybit.com/ru-RU/tradingbot/create/";
}

function bybitChartUrl(venue, symbol) {
  if (venue === "spot") {
    const parts = splitSpotSymbol(symbol);
    if (parts) return `https://www.bybit.com/ru-RU/trade/spot/${encodeURIComponent(parts.base)}/${encodeURIComponent(parts.quote)}`;
  }
  return `https://www.bybit.com/trade/usdt/${encodeURIComponent(symbol || "")}`;
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
  const botUrl = bybitBotCreateUrl(it.bot_type);
  const cls = compact ? "symbol-links compact" : "symbol-links";
  return `
    <span class="${cls}">
      <a class="icon-link" href="${escapeHtml(chartUrl)}" target="_blank" rel="noopener noreferrer" title="Открыть график Bybit">${iconSvg("chart")}</a>
      <a class="icon-link" href="${escapeHtml(botUrl)}" target="_blank" rel="noopener noreferrer" title="Открыть страницу создания бота">${iconSvg("bot")}</a>
    </span>
  `;
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

function copyButton(copyValue) {
  if (copyValue === null || copyValue === undefined || copyValue === "" || copyValue === "—") return "";
  return `<button class="copy-chip" data-act="copy-field" data-copy="${escapeHtml(copyValue)}">копия</button>`;
}

function fieldBox(label, value, copyValue = null, extraClass = "") {
  const safeValue = value ?? "—";
  const effectiveCopy = copyValue === null ? safeValue : copyValue;
  const inputValue = escapeHtml(String(safeValue));
  const inputClass = extraClass ? `field-input ${extraClass}` : "field-input";
  return `
    <div class="field-box">
      <div class="field-label">${escapeHtml(label)}</div>
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
  bot.href = bybitBotCreateUrl(it.bot_type);
  chart.innerHTML = iconSvg("chart");
  bot.innerHTML = iconSvg("bot");
  chart.classList.remove("hidden");
  bot.classList.remove("hidden");
}

function clearDetailsHeaderLinks() {
  const chart = $("detailsChartLink");
  const bot = $("detailsBotLink");
  if (chart) chart.classList.add("hidden");
  if (bot) bot.classList.add("hidden");
}

function buildOperatorValues(it) {
  const params = (it || {}).params || {};
  const plan = params.trade_plan || {};
  const levels = plan.levels || {};
  const ks = levels.kill_switch || {};
  const tpPerLeg = levels.tp_per_leg || {};
  const gridStep = levels.grid_step || {};
  const meta = (it || {}).bybit_meta || {};
  const rangeLower = formatBybitPrice(params.price_range_lower, meta, "down");
  const rangeUpper = formatBybitPrice(params.price_range_upper, meta, "up");
  const entryRef = formatBybitPrice(params.price_ref, meta, "nearest");
  const killLower = formatBybitPrice(ks.lower, meta, "down");
  const killUpper = formatBybitPrice(ks.upper, meta, "up");
  const gridStepAbs = formatBybitPrice(gridStep.step_abs, meta, "nearest");
  const tpLegAbs = formatBybitPrice(tpPerLeg.abs, meta, "nearest");
  const stepPct = formatPercentDot(params.grid_spacing_pct, 4, false);
  const tpLegPct = formatPercentDot(tpPerLeg.pct, 4, false);
  const leverage = it.venue === "linear" ? String(params.leverage ?? 1) : "—";
  const marginMode = it.venue === "linear" ? marginModeRu(params.margin_mode || "isolated") : "—";
  const isSpotBot = it.bot_type === "spot_grid";
  const isNeutralFutures = it.venue === "linear" && it.direction === "neutral";
  const stopLossLabel = isSpotBot ? "Стоп-лосс" : it.direction === "short" ? "Стоп-лосс (верх)" : it.direction === "long" ? "Стоп-лосс (низ)" : "Нижняя стоп-цена";
  const takeProfitLabel = isSpotBot ? "Тейк-профит" : it.direction === "short" ? "Тейк-профит (низ)" : it.direction === "long" ? "Тейк-профит (верх)" : "Верхняя стоп-цена";
  const stopLossValue = (isSpotBot || isNeutralFutures || it.direction === "long") ? killLower : killUpper;
  const takeProfitValue = (isSpotBot || isNeutralFutures || it.direction === "long") ? killUpper : killLower;
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
    stopLossLabel,
    stopLossValue,
    takeProfitLabel,
    takeProfitValue,
  };
}

function buildOperatorFieldSpecs(it, ov) {
  const params = (it || {}).params || {};
  const fields = [
    { label: "Диапазон от", value: ov.rangeLower, mono: true },
    { label: "Диапазон до", value: ov.rangeUpper, mono: true },
    { label: "Кол-во сеток", value: params.grid_levels ?? "—" },
    { label: "Интервал, цена", value: ov.gridStepAbs, mono: true },
    { label: "Интервал, %", value: ov.stepPct },
    { label: "Цена входа", value: ov.entryRef, mono: true },
  ];
  if (it.venue === "linear") {
    fields.push({ label: "Плечо", value: ov.leverage });
    fields.push({ label: "Режим маржи", value: ov.marginMode });
  }
  fields.push({ label: "Прибыль/сетка, %", value: ov.tpLegPct });
  if (ov.tpLegAbs !== "—") fields.push({ label: "Прибыль/сетка, цена", value: ov.tpLegAbs, mono: true });

  if (it.bot_type === "spot_grid") {
    fields.push({ label: "Стоп-лосс", value: ov.stopLossValue, mono: true });
    fields.push({ label: "Тейк-профит", value: ov.takeProfitValue, mono: true });
  } else if (it.direction === "neutral") {
    fields.push({ label: "Нижняя стоп-цена", value: ov.killLower, mono: true });
    fields.push({ label: "Верхняя стоп-цена", value: ov.killUpper, mono: true });
  } else {
    fields.push({ label: "Стоп-лосс", value: ov.stopLossValue, mono: true });
    fields.push({ label: "Тейк-профит", value: ov.takeProfitValue, mono: true });
  }
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
    score: it.score,
    confidence: it.confidence,
    expected_rr: it.expected_rr,
    blocks: it.blocks || [],
    cost_model: reasons.cost_model || {},
    market_shock: reasons.market_shock || {},
    fast_veto: reasons.fast_veto || {},
    direction_agg: reasons.direction_agg || {},
    sentiment_agg: reasons.sentiment_agg || {},
    bybit_meta: it.bybit_meta || {},
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
  const shock = reasons.market_shock || {};
  const fastVeto = reasons.fast_veto || {};
  const sentAgg = reasons.sentiment_agg || {};
  const dirAgg = reasons.direction_agg || {};
  const funding = reasons.funding || {};
  const oi = reasons.open_interest || {};
  const liquidity = reasons.liquidity || {};
  const costModel = reasons.cost_model || {};
  const symbolSent = reasons.symbol_sentiment || {};
  const volatility = plan.volatility || {};
  const btcBeta = reasons.btc_beta || {};
  const btcMetric = btcRelationMetric(btcBeta, it.symbol);
  const blocks = it.blocks || [];
  const ov = buildOperatorValues(it);
  const operatorFields = buildOperatorFieldSpecs(it, ov);
  const alertClass = (shock.severity || "normal") === "lockdown" ? "lock" : (shock.severity || "normal") === "guarded" ? "guard" : "";
  const dirConf = dirAgg.direction_confidence_calibrated ?? dirAgg.direction_confidence;
  const techPayload = JSON.stringify(buildTechPayload(it), null, 2);

  $("details").dataset.tech = techPayload;
  $("details").dataset.recId = it.rec_id;
  updateDetailsHeaderLinks(it);

  const reasonList = (shock.reasons || []).length ? `<ul class="reason-list">${(shock.reasons || []).slice(0, 4).map(r => `<li><code>${escapeHtml(r.code || "signal")}</code> — ${escapeHtml(r.msg || "")}</li>`).join("")}</ul>` : "";
  const blockCards = blocks.length ? `<div class="small-blocks">${blocks.map(b => `<div class="small-block"><code>${escapeHtml(b.code || "BLOCK")}</code><br>${escapeHtml(b.msg || "")}</div>`).join("")}</div>` : `<div class="helper-text">Активных блоков нет.</div>`;
  const fastVetoBlock = fastVeto.triggered ? `<div class="small-blocks"><div class="small-block"><code>${escapeHtml((fastVeto.blocks || [])[0]?.code || "FAST_VETO")}</code><br>${escapeHtml((fastVeto.blocks || [])[0]?.msg || "")}</div></div>` : `<div class="helper-text">Fast-veto не сработал.</div>`;

  return `
    <div class="operator-sheet">
      <div class="operator-hero compact-hero">
        <div>
          <div class="operator-title-row">
            <div class="operator-title">${escapeHtml(it.symbol)}</div>
          </div>
          <div class="operator-subtitle operator-subtitle-inline">${botTypePillHtml(it.bot_type, true)}<span class="operator-sub-sep">·</span>${directionBadge(it.direction)}<span class="operator-sub-sep">·</span>${statusBadgeHtml(it.status)}</div>
        </div>
        <div class="operator-hero-metrics">
          <div class="metric-chip"><b>Скор</b>${fmt(it.score)}</div>
          <div class="metric-chip"><b>Увер.</b>${fmt(it.confidence)}</div>
          <div class="metric-chip"><b>Ож. RR</b>${fmt(it.expected_rr)}</div>
          <div class="metric-chip metric-chip-wide" title="${escapeHtml(btcMetric.title || "")}"><b>${escapeHtml(btcMetric.label)}</b>${btcMetricValueHtml(btcMetric)}</div>
        </div>
      </div>

      <div class="operator-card primary-launch-card">
        <h3>Поля для Bybit</h3>
        <div class="helper-text" style="margin-bottom:10px">Только значения, которые реально нужны для ручного заполнения формы Bybit. Все ценовые уровни приведены к виду с точкой и без разделителей тысяч.</div>
        <div class="operator-grid three">
          ${operatorFields.map(field => fieldBox(field.label, field.value, field.value, field.mono ? "field-input-mono" : "")).join("")}
        </div>
      </div>

      <div class="operator-card">
        <h3>Факторы решения</h3>
        <div class="factors-grid">
          ${factorGroupHtml("Плюсы сигнала", reasons.top_positive_factors || [], "positive")}
          ${factorGroupHtml("Минусы и риски", reasons.top_negative_factors || [], "negative")}
        </div>
      </div>

      <div class="operator-card alert-card ${alertClass}">
        <h3>Защита и фон</h3>
        <div class="alert-line">${shockBadgeHtml(shock)}</div>
        <div>${escapeHtml(shock.operator_note || "Новые входы разрешены в обычном режиме.")}</div>
        <div class="alert-note">Сентимент в этой сборке остаётся эвристическим: это operator-grade фон, а не полноценный semantic news-анализ статей.</div>
        <div class="alert-note">Breadth вниз: ${fmt(((shock.metrics || {}).breadth_down || 0) * 100, 1)}% · вверх: ${fmt(((shock.metrics || {}).breadth_up || 0) * 100, 1)}% · median 5m: ${fmtPct((((shock.metrics || {}).median_r5m || 0) * 100), 2)}</div>
        ${reasonList}
      </div>

      <div class="operator-card">
        <h3>Контекст сигнала</h3>
        <div class="operator-grid">
          ${fieldBox("Уверенность направления", formatDotNumber(dirConf, 4), formatDotNumber(dirConf, 4))}
          ${fieldBox("Режим сигнала", dirAgg.regime || "—", dirAgg.regime || "—")}
          ${fieldBox("Согласованность", formatDotNumber(dirAgg.coherence, 4), formatDotNumber(dirAgg.coherence, 4))}
          ${fieldBox("Сентимент глобальный", formatDotNumber(symbolSent.global, 4), formatDotNumber(symbolSent.global, 4))}
          ${fieldBox("Сентимент по символу", formatDotNumber(symbolSent.value, 4), formatDotNumber(symbolSent.value, 4))}
          ${fieldBox("Сентимент итоговый", formatDotNumber(symbolSent.effective, 4), formatDotNumber(symbolSent.effective, 4))}
          ${fieldBox("ATR 1ч", volatility.atr_pct_1h !== undefined && volatility.atr_pct_1h !== null ? formatPercentDot(volatility.atr_pct_1h * 100, 2, false) : "—")}
          ${fieldBox("Фандинг", funding.value !== undefined && funding.value !== null ? formatPercentDot(funding.value * 100, 4, true) : "—")}
          ${fieldBox("OI 4ч", oi.oi_4h_chg_pct !== undefined && oi.oi_4h_chg_pct !== null ? formatPercentDot(oi.oi_4h_chg_pct, 2, true) : "—")}
        </div>
        ${fastVetoBlock}
      </div>

      <div class="operator-card">
        <h3>Исполнение и ликвидность</h3>
        <div class="operator-grid">
          ${fieldBox("Ликвидность", liquidityTierRu(liquidity.tier || "—"))}
          ${fieldBox("Оборот 24ч", formatUsdValue(liquidity.turnover24h_usd))}
          ${fieldBox("Спред", formatBps(costModel.spread_bps, 2, false))}
          ${fieldBox("Издержки всего", formatBps(costModel.total_cost_bps, 2, false))}
          ${fieldBox("Комиссия taker", formatBps(costModel.taker_fee_bps, 2, false))}
          ${fieldBox("Ожид. funding", formatBps(costModel.expected_funding_bps, 2, true))}
        </div>
        <div class="section-actions">
          <button class="ghost-chip" data-act="show-tech">Техподробности</button>
        </div>
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
      <td>${botTypePillHtml(it.bot_type)}</td>
      <td>
        <div class="symbol-cell">
          <b>${it.symbol}</b>
          ${symbolLinksHtml(it)}
        </div>
      </td>
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
  if (!dir || dir === "neutral") return `<span class="dir-badge dir-neu">• neutral</span>`;
  if (dir === "long")  return `<span class="dir-badge dir-long">▲ long</span>`;
  if (dir === "short") return `<span class="dir-badge dir-short">▼ short</span>`;
  return `<span class="dir-badge dir-neu">• ${escapeHtml(String(dir))}</span>`;
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
      clearDetailsHeaderLinks();
      $("details").textContent = `Ошибка загрузки деталей (HTTP ${res.status}).`;
      btn.disabled = false;
      btn.textContent = "Обновить";
      return;
    }
    it = await res.json();
  } catch (e) {
    clearDetailsHeaderLinks();
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
