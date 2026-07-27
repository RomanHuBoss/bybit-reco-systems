const $ = (id) => document.getElementById(id);

let recoAbort = null;
let recoDebounce = null;
let statusPayload = null;
let countdownTimer = null;
let countdownVal = 10;
let currentRecId = null;   // rec_id currently shown in Details panel
let detailsRequestSeq = 0;
let currentMeta  = null;   // {venue, symbol, bot_type} — used to find fresh rec_id on refresh
let refreshInFlight = null;
let lastHealthDiagnostics = null;

// ── sort state ────────────────────────────────────────────────────────────────
let sortCol = "plan_rr";  // по умолчанию: лучшие значения RR плана сверху
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
  const v = toFiniteNumber(x);
  if (v === null) return "-";
  return v.toFixed(n);
}

function timeAgo(ts) {
  if (!ts) return "—";
  const rawSec = Math.floor(Date.now() / 1000) - Number(ts);
  if (!Number.isFinite(rawSec)) return "некорректное время";
  if (rawSec < -300) return "некорректное время";
  const sec = Math.max(0, rawSec);
  if (sec < 5)  return "только что";
  if (sec < 60) return `${sec} с назад`;
  if (sec < 3600) return `${Math.floor(sec/60)} мин назад`;
  return `${Math.floor(sec/3600)} ч назад`;
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
  const v = toFiniteNumber(x);
  if (v === null) return "—";
  const av = Math.abs(v);
  const frac = av >= 1000 ? 2 : av >= 1 ? 4 : 6;
  return v.toLocaleString("ru-RU", { maximumFractionDigits: frac });
}

function fmtPct(x, n = 2) {
  const v = toFiniteNumber(x);
  if (v === null) return "—";
  return `${v >= 0 ? "+" : ""}${v.toFixed(n)}%`;
}

function toFiniteNumber(value) {
  if (value === null || value === undefined) return null;
  if (typeof value === "boolean") return null;
  if (typeof value === "string" && value.trim() === "") return null;
  const v = Number(value);
  return Number.isFinite(v) ? v : null;
}

function toStrictInteger(value) {
  const v = toFiniteNumber(value);
  return v !== null && Number.isInteger(v) ? v : null;
}

function resolveGridCountForDisplay(it) {
  const item = it && typeof it === "object" ? it : {};
  const params = item.params && typeof item.params === "object" ? item.params : {};
  const plan = params.trade_plan && typeof params.trade_plan === "object" ? params.trade_plan : {};
  const operatorSheet = params.operator_sheet && typeof params.operator_sheet === "object" ? params.operator_sheet : {};
  const paramsSizing = params.sizing && typeof params.sizing === "object" ? params.sizing : {};
  const paramsEconomics = params.economics && typeof params.economics === "object" ? params.economics : {};
  const planSizing = plan.sizing && typeof plan.sizing === "object" ? plan.sizing : {};
  const planEconomics = plan.economics && typeof plan.economics === "object" ? plan.economics : {};
  const operatorSizing = operatorSheet.sizing && typeof operatorSheet.sizing === "object" ? operatorSheet.sizing : {};
  const operatorEconomics = operatorSheet.economics && typeof operatorSheet.economics === "object" ? operatorSheet.economics : {};
  const candidates = [
    params.grid_count,
    plan.grid_count,
    params.grid_levels,
    operatorSheet.grid_count,
    operatorSheet.grid_levels,
    paramsSizing.grid_count,
    paramsEconomics.grid_count,
    planSizing.grid_count,
    planEconomics.grid_count,
    operatorSizing.grid_count,
    operatorEconomics.grid_count,
  ];
  const values = [];
  for (const raw of candidates) {
    if (raw === null || raw === undefined || (typeof raw === "string" && raw.trim() === "")) continue;
    const parsed = toStrictInteger(raw);
    if (parsed === null) return null;
    values.push(parsed);
  }
  if (!values.length) return null;
  const distinct = [...new Set(values)];
  return distinct.length === 1 ? distinct[0] : null;
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
  const v = toFiniteNumber(value);
  if (v === null) return "—";
  let out = v.toFixed(digits);
  if (!keepZeros) out = out.replace(/(\.\d*?[1-9])0+$/, "$1").replace(/\.0+$/, "");
  return out;
}

function quantizeByStep(value, step, mode = "nearest") {
  const v = toFiniteNumber(value);
  const tick = toFiniteNumber(step);
  if (v === null || tick === null || tick <= 0) return null;
  const decimals = countDecimalsFromStep(tick);
  const precision = Math.max(0, Number(decimals || 0));
  const unitsRaw = v / tick;
  const eps = Math.max(1e-12, Math.abs(unitsRaw) * 1e-12);
  let units;
  if (mode === "down") units = Math.floor(unitsRaw + eps);
  else if (mode === "up") units = Math.ceil(unitsRaw - eps);
  else units = Math.round(unitsRaw);
  const snapped = units * tick;
  return snapped.toFixed(precision);
}

function formatBybitPrice(value, meta = {}, mode = "nearest") {
  const v = toFiniteNumber(value);
  if (v === null) return "—";
  const tick = toFiniteNumber((meta || {}).tick_size);
  if (tick && tick > 0) {
    const snapped = quantizeByStep(v, tick, mode);
    if (snapped) return snapped;
  }
  return v.toFixed(inferPriceDecimals(v));
}

function formatPercentDot(value, digits = 4, withSign = false) {
  const v = toFiniteNumber(value);
  if (v === null) return "—";
  return `${withSign && v >= 0 ? "+" : ""}${formatDotNumber(v, digits)}%`;
}


function formatBps(value, digits = 2, withSign = false) {
  const v = toFiniteNumber(value);
  if (v === null) return "—";
  return `${withSign && v >= 0 ? "+" : ""}${formatDotNumber(v, digits)} б.п.`;
}

function formatUsdValue(value) {
  const v = toFiniteNumber(value);
  if (v === null) return "—";
  const av = Math.abs(v);
  if (av >= 1e9) return `${formatDotNumber(v / 1e9, 2)} млрд USDT`;
  if (av >= 1e6) return `${formatDotNumber(v / 1e6, 2)} млн USDT`;
  if (av >= 1e3) return `${formatDotNumber(v / 1e3, 1)} тыс. USDT`;
  return `${fmtPrice(v)} USDT`;
}

function formatProbability(value) {
  const v = toFiniteNumber(value);
  if (v === null) return "—";
  const pct = Math.abs(v) <= 1 ? v * 100 : v;
  return `${formatDotNumber(pct, 1)}%`;
}

function directionRu(dir) {
  const normalized = String(dir || "").trim().toLowerCase();
  if (normalized === "long") return "Покупка (рост)";
  if (normalized === "short") return "Продажа (снижение)";
  return "Нейтрально / направления нет";
}

function operatorStatusRu(status) {
  const value = String(status || "").trim().toLowerCase();
  const labels = {
    recommended: "Можно торговать",
    active: "Можно торговать",
    pending: "Ожидает проверки",
    blocked: "Заблокировано",
    no_trade: "Не торговать",
    suppressed: "Скрыто системой",
    expired: "Устарело",
    executed: "Запущено",
    ignored: "Отклонено оператором",
  };
  return labels[value] || (value ? "Неизвестный статус" : "Нет статуса");
}

function healthStatusRu(status) {
  const value = String(status || "").trim().toLowerCase();
  const labels = {
    ok: "Норма",
    ready: "Готово",
    enabled: "Включено",
    disabled: "Отключено",
    missing: "Нет данных",
    stale: "Устарело",
    error: "Ошибка",
    pending: "Ожидает проверки",
    warming_up: "Накопление данных",
    backlog: "Есть очередь",
    processing: "Обрабатывается",
    starting: "Запускается",
    healthy_not_actionable: "Работает, сделок нет",
    degraded: "Требует внимания",
    stalled: "Работа остановилась",
    unknown: "Неизвестно",
  };
  return labels[value] || humanizeOperatorText(value || "Нет данных");
}

function llmStatusRu(status) {
  const value = String(status || "").trim().toLowerCase();
  const labels = {
    ok: "Проверка завершена",
    pending: "Ожидает проверки",
    error: "Ошибка проверки",
    skipped: "Проверка пропущена",
    none: "Проверки не было",
    disabled: "Проверка отключена",
    enabled: "Проверка включена",
    warming_up: "Накопление данных",
    unknown: "Статус неизвестен",
  };
  return labels[value] || "Статус неизвестен";
}

function gateDecisionRu(value) {
  const key = String(value || "").trim().toLowerCase();
  const labels = {
    pass: "Условие выполнено",
    allow: "Разрешено",
    approved: "Разрешено",
    block: "Заблокировано",
    blocked: "Заблокировано",
    reject: "Отклонено",
    rejected: "Отклонено",
    hold: "Ожидание",
    pending: "Ожидает проверки",
    no_trade: "Не торговать",
    warning: "Есть предупреждение",
  };
  return labels[key] || humanizeOperatorText(value || "Нет решения");
}

function sampleRoleRu(value) {
  const key = String(value || "").trim().toLowerCase();
  const labels = {
    actionable_root: "Торговое наблюдение",
    shadow_no_trade: "Учебное наблюдение",
    shadow: "Учебное наблюдение",
    current_policy: "Текущий набор правил",
    archive: "Архивное наблюдение",
    legacy: "Архивное наблюдение",
  };
  return labels[key] || humanizeOperatorText(value || "Не указано");
}

function outcomeEligibilityCohortRu(value) {
  const key = String(value || "").trim().toLowerCase();
  const labels = {
    calibration_eligible: "Допущено к калибровке",
    policy_evaluation_candidate: "Кандидат точного набора правил; проверка не завершена",
    shadow_exploration: "Теневая исследовательская когорта; не калибровка",
    outcome_only: "Только аудит исхода",
    other_policy: "Другой идентификатор правил",
    excluded: "Исключено",
  };
  return labels[key] || humanizeOperatorText(value || "Не указано");
}

function outcomeEligibilityReasonRu(value) {
  const key = String(value || "").trim().toUpperCase();
  const labels = {
    ACTIVE_POLICY_UNSPECIFIED: "активный fingerprint не указан",
    ACTIVE_POLICY_FINGERPRINT_MISMATCH: "fingerprint не совпадает с активным",
    POLICY_CONTRACT_UNVERIFIED: "контракт policy не прошёл проверку",
    POLICY_EVALUATION_EXCLUDED: "не пройден допуск policy evaluation",
    SCORE_INVALID: "score отсутствует или некорректен",
    SCORE_POLICY_FLOOR_MISSING: "в контракте отсутствует порог score",
    SCORE_BELOW_POLICY_FLOOR: "score ниже действующего порога",
    MEAN_REVERSION_EVIDENCE_INVALID: "mean reversion evidence некорректен",
    MEAN_REVERSION_POLICY_FLOOR_MISSING: "в контракте отсутствует порог mean reversion",
    MEAN_REVERSION_BELOW_POLICY_FLOOR: "mean reversion ниже действующего порога",
    LABEL_DUE_MISSING: "не сохранён срок готовности label",
    LABEL_DUE_CONTRACT_MISMATCH: "срок label не совпадает с контрактом",
    LABEL_AVAILABILITY_MISSING: "не сохранён момент доступности label",
    LABEL_AVAILABILITY_PREMATURE: "label объявлен доступным раньше допустимого срока",
    LABEL_NOT_MATURED: "label ещё не созрел",
    MATERIALIZED_POLICY_ELIGIBILITY_MISMATCH: "расхождение materialized policy eligibility",
    MATERIALIZED_OUTCOME_ELIGIBILITY_MISMATCH: "расхождение materialized outcome eligibility",
  };
  return labels[key] || humanizeOperatorText(value || "Не указано");
}

function outcomeEligibilityReasonsText(row) {
  const blockers = Array.isArray(row?.eligibility?.eligibility_reason_codes)
    ? row.eligibility.eligibility_reason_codes
    : [];
  const decisions = Array.isArray(row?.eligibility?.decision_reason_codes)
    ? row.eligibility.decision_reason_codes
    : [];
  const parts = blockers.length
    ? [`Допуск: ${blockers.map(outcomeEligibilityReasonRu).join("; ")}`]
    : ["Все проверки точного набора правил выполнены"];
  if (decisions.length) {
    parts.push(`Решение: ${decisions.map(humanizeOperatorText).join("; ")}`);
  }
  return parts.join(". ");
}

function empiricalStatusRu(value) {
  const key = String(value || "").trim().toLowerCase();
  const labels = {
    positive: "Положительный результат подтверждён",
    negative: "Отрицательный результат подтверждён",
    uncertain: "Результат пока неопределён",
    insufficient: "Недостаточно данных",
    unavailable: "Нет данных",
  };
  return labels[key] || "Статус не определён";
}

function neutralSourceRu(value) {
  const key = String(value || "").trim().toLowerCase();
  if (key === "futures_neutral" || key === "neutralized_short") return "Нейтральное решение после проверок";
  if (key === "true_neutral") return "Изначально нейтральный сигнал";
  return humanizeOperatorText(value || "Не указано");
}

function marketStateRu(value, kind = "generic") {
  const key = String(value || "").trim().toLowerCase();
  const common = {
    low: "низкая", normal: "нормальная", medium: "средняя", high: "высокая",
    mixed: "смешанная", flat: "боковое движение", stable: "устойчивое состояние",
    bullish: "преобладает рост", bearish: "преобладает снижение",
    positive: "положительный", negative: "отрицательный", neutral: "нейтральный",
    risk_on: "риск допустим", risk_off: "повышенная осторожность",
    aggressive: "агрессивный", conservative: "консервативный", advisory: "рекомендательный",
    cached: "сохранённый ответ", fresh: "актуальные данные",
    guarded: "усиленный контроль", lockdown: "торговля заблокирована",
    normal_mode: "нормальный режим", unknown: "не определено",
  };
  if (Object.prototype.hasOwnProperty.call(common, key)) return common[key];
  if (kind === "trend" && key === "none") return "выраженного направления нет";
  return humanizeOperatorText(value || "не определено");
}

function timeframeRu(value) {
  const key = String(value ?? "").trim().toLowerCase();
  const labels = {
    "60": "1 мин", "300": "5 мин", "900": "15 мин", "1800": "30 мин",
    "3600": "1 ч", "14400": "4 ч", "86400": "1 д",
    "1m": "1 мин", "5m": "5 мин", "15m": "15 мин", "30m": "30 мин",
    "1h": "1 ч", "4h": "4 ч", "1d": "1 д",
  };
  return labels[key] || humanizeOperatorText(value || "—");
}

function timeframeListRu(value) {
  if (!Array.isArray(value)) return timeframeRu(value);
  return value.map(timeframeRu).join(", ");
}

function calibrationModeRu(value) {
  const key = String(value || "").trim().toLowerCase();
  const labels = {
    raw: "без калибровки", calibrated: "откалибровано", logreg: "вероятностная модель",
    platt: "устаревшая калибровка", legacy: "устаревшая калибровка",
    unfitted: "не обучено", fitted: "обучено", pending_refit: "ожидает пересчёта",
  };
  return labels[key] || humanizeOperatorText(value || "—");
}

function sentimentRegimeRu(value) {
  const key = String(value || "").trim().toLowerCase();
  const labels = {
    positive: "положительный", negative: "отрицательный", neutral: "нейтральный",
    bullish: "в пользу роста", bearish: "в пользу снижения", mixed: "смешанный",
    euphoria: "эйфория", panic: "паника",
  };
  return labels[key] || humanizeOperatorText(value || "не определён");
}

function humanizeOperatorText(value) {
  let text = String(value ?? "").trim();
  if (!text) return "—";
  const exact = {
    long: "Покупка (рост)", short: "Продажа (снижение)", neutral: "Нейтральная сетка",
    recommended: "Можно торговать", active: "Можно торговать", pending: "Ожидает проверки",
    blocked: "Заблокировано", no_trade: "Не торговать", suppressed: "Скрыто системой",
    executed: "Запущено", ignored: "Отклонено оператором", expired: "Устарело",
    true: "Да", false: "Нет", yes: "Да", no: "Нет", none: "Нет данных",
    pass: "Условие выполнено", fail: "Условие не выполнено", live: "Новый ответ", cache: "Сохранённый ответ",
    ok: "Норма", ready: "Готово", enabled: "Включено", disabled: "Отключено",
    low: "низкий", medium: "средний", high: "высокий", mixed: "смешанный",
    risk_on: "риск допустим", risk_off: "повышенная осторожность",
    aggressive: "агрессивный", conservative: "консервативный", advisory: "рекомендательный",
    cached: "сохранённый ответ", fresh: "актуальные данные",
    guarded: "усиленный контроль", lockdown: "торговля заблокирована",
    bullish: "преобладает рост", bearish: "преобладает снижение",
    handover: "передача управления", starting: "запуск", processing: "обработка", backlog: "обработка очереди",
  };
  const lower = text.toLowerCase();
  if (Object.prototype.hasOwnProperty.call(exact, lower)) return exact[lower];
  const replacements = [
    [/preflight blocked due stale ticker and funding rate unavailable/gi, "предзапусковая проверка заблокирована: текущая цена устарела, ставка платежа финансирования недоступна"],
    [/recommendation remains shadow no[ -]?trade/gi, "рекомендация остаётся учебной и не разрешена к торговле"],
    [/positive monetary expectancy (?:is )?not proven/gi, "положительная ожидаемая денежная эффективность не подтверждена"],
    [/repeated anti[ -]?persistence (?:is )?insufficient(?:ly expressed)?/gi, "повторяемое чередование направления цены выражено недостаточно"],
    [/bot[ -]?specific/gi, "для этого вида стратегии"],
    [/is not proven positive/gi, "не подтверждена как положительная"],
    [/not proven positive/gi, "не подтверждена как положительная"],
    [/not proven/gi, "не подтверждено"],
    [/under the current independent retained sample/gi, "по текущей независимой выборке"],
    [/current independent retained sample/gi, "текущая независимая выборка"],
    [/retained sample/gi, "сохранённая выборка"],
    [/configured/gi, "заданный"],
    [/unavailable/gi, "недоступно"],
    [/unproven/gi, "не подтверждено"],
    [/status\s*=/gi, "статус="],
    [/n_eff\s*=/gi, "эффективная выборка="],
    [/\bn\s*=/gi, "наблюдений="],
    [/ticker payload empty/gi, "биржа не вернула текущую цену"],
    [/funding too high/gi, "платёж финансирования слишком велик"],
    [/preflight blocked/gi, "предзапусковая проверка заблокирована"],
    [/mean_reversion_score/gi, "оценка возврата цены к среднему"],
    [/mean[ -]?reversion/gi, "возврат цены к среднему"],
    [/anti[ -]?persistence/gi, "чередование направления цены"],
    [/monetary expectancy/gi, "ожидаемая денежная эффективность"],
    [/empirical expectancy/gi, "доходность по наблюдениям"],
    [/expected shortfall/gi, "средний результат худших наблюдений"],
    [/funding/gi, "платёж финансирования"],
    [/preflight/gi, "предзапусковая проверка"],
    [/kill[ -]?switch/gi, "аварийная граница выхода"],
    [/take profit/gi, "цель прибыли"],
    [/stop loss/gi, "ограничение убытка"],
    [/spread/gi, "разница цен покупки и продажи"],
    [/slippage/gi, "проскальзывание"],
    [/grid/gi, "сетка"],
    [/reviewer/gi, "проверяющий модуль"],
    [/policy fingerprint/gi, "идентификатор набора правил"],
    [/gate/gi, "условие допуска"],
    [/policy/gi, "набор правил"],
    [/shadow/gi, "учебное наблюдение"],
    [/outcomes?/gi, "наблюдения"],
    [/confidence/gi, "уверенность"],
    [/launch[ -]?score/gi, "оценка допуска к запуску"],
    [/candidate floor/gi, "минимальный порог кандидата"],
    [/blocked/gi, "заблокировано"],
    [/pending/gi, "ожидает проверки"],
    [/no_trade/gi, "не торговать"],
    [/recommended/gi, "можно торговать"],
    [/active/gi, "можно торговать"],
    [/raw/gi, "исходный"],
    [/execution payload/gi, "набор параметров исполнения"],
    [/execution/gi, "исполнение"],
    [/runtime/gi, "рабочий контур"],
    [/payload/gi, "набор параметров"],
    [/snapshot/gi, "снимок данных"],
    [/leverage/gi, "кредитное плечо"],
    [/liquidation/gi, "ликвидация"],
    [/stress/gi, "стресс-проверка"],
    [/fills?/gi, "исполнения"],
    [/net[ -]?edge/gi, "чистое преимущество"],
    [/edge/gi, "преимущество"],
    [/range/gi, "диапазон"],
    [/direction/gi, "направление"],
    [/risk/gi, "риск"],
    [/margin/gi, "маржа"],
    [/perpetual futures/gi, "бессрочные фьючерсы"],
    [/futures/gi, "фьючерсы"],
    [/live edge/gi, "преимущество в реальной торговле"],
    [/model lineage/gi, "версия и происхождение модели"],
    [/feature[ -]?eligible/gi, "пригодные для расчёта признаков"],
    [/label horizon/gi, "горизонт наблюдения"],
    [/temporal floor/gi, "минимальная длительность наблюдения"],
    [/terminal holdout/gi, "заключительная проверочная выборка"],
    [/purged OOF/gi, "проверка вне обучения без пересечения временных окон"],
    [/OOF/gi, "проверка вне обучения"],
    [/legacy/gi, "устаревший"],
    [/raw/gi, "исходный"],
    [/calibrated/gi, "откалиброванный"],
    [/cross[ -]?margin/gi, "общая маржа"],
    [/linear USDT/gi, "линейный контракт с расчётом в USDT"],
    [/worst[ -]?case/gi, "худший сценарий"],
    [/reference[ -]?price/gi, "расчётная цена"],
    [/notional/gi, "номинальный объём"],
    [/quantity|qty/gi, "количество"],
    [/boolean/gi, "логический"],
    [/backend/gi, "сервер"],
    [/too high/gi, "слишком велик"],
    [/empty/gi, "пусто"],
    [/too many visits/gi, "превышен лимит запросов к Bybit"],
    [/rate[ -]?limit/gi, "лимит запросов"],
    [/instrument metadata absent/gi, "инструмент отсутствует в справочнике биржи"],
    [/deadlock/gi, "взаимоблокировка базы данных"],
    [/timeout/gi, "превышено время ожидания"],
    [/stale/gi, "устаревшие данные"],
    [/missing/gi, "нет данных"],
    [/disabled/gi, "отключено"],
    [/strong[ -]?trend/gi, "сильное направленное движение"],
    [/trendiness/gi, "выраженность направленного движения"],
    [/trend/gi, "направленное движение"],
    [/unfavorable/gi, "неблагоприятно"],
    [/not[ -]?actionable/gi, "запуск не разрешён"],
    [/insufficient/gi, "недостаточно данных"],
    [/unknown/gi, "неизвестно"],
    [/candidate/gi, "кандидат"],
    [/score/gi, "оценка"],
    [/threshold/gi, "порог"],
    [/freshness/gi, "актуальность"],
    [/model/gi, "модель"],
    [/calibration/gi, "калибровка"],
    [/feature/gi, "признак"],
    [/\bpositive\b/gi, "положительный"],
    [/\bnegative\b/gi, "отрицательный"],
    [/\bfailed\b/gi, "ошибка"],
    [/\bwarning\b/gi, "предупреждение"],
    [/\berror\b/gi, "ошибка"],
    [/no[ -]?trade/gi, "не торговать"],
    [/\brecommendation\b/gi, "рекомендация"],
    [/\bremains\b/gi, "остаётся"],
    [/\bdue to\b/gi, "из-за"],
    [/\bdue\b/gi, "из-за"],
    [/\bticker\b/gi, "текущая цена"],
    [/\brate\b/gi, "ставка"],
    [/\brepeated\b/gi, "повторное"],
    [/\band\b/gi, "и"],
    [/\bcurrent\b/gi, "текущий"],
    [/\bindependent\b/gi, "независимый"],
    [/\bsample\b/gi, "выборка"],
    [/\bbps\b/gi, "базисных пунктов"],
  ];
  for (const [pattern, replacement] of replacements) text = text.replace(pattern, replacement);
  return text.replace(/_/g, " ").replace(/\s+/g, " ").trim();
}


function decisionActionRu(value) {
  const code = String(value ?? "").trim();
  const labels = {
    OUTCOME_SKIP_INVALID_GRID_CONTRACT: "Исход не рассчитан: контракт сетки недостаточно наблюдаем",
    STALE_DATA_SKIP: "Расчёт пропущен: рыночные данные устарели",
    OUTCOME_WORKER_STALLED: "Контур расчёта исходов не продвигает очередь",
    COLLECT_ERROR: "Ошибка сбора рыночных данных",
    DB_PRUNE: "Плановая очистка устаревших технических данных",
    RECO_WARMUP_SKIP: "Рекомендации ожидают готовности рыночных данных",
    RECO_WARMUP_RECOVERED: "Рыночные данные готовы; расчёт рекомендаций возобновлён",
  };
  const label = labels[code];
  return label ? `${label} (${code})` : `${humanizeOperatorText(code)} (${code})`;
}

function outcomeObservabilityReasonRu(value) {
  const code = String(value ?? "").trim();
  const labels = {
    scheduled_for_label_horizon: "Исход зарегистрирован и ожидает созревания горизонта",
    label_horizon_not_mature: "Горизонт исхода ещё не созрел",
    outcome_not_matured: "Исход ещё не созрел",
    existing_outcome_materialized: "Состояние восстановлено по уже записанному исходу",
    missing_tradeable_entry_candle: "Нет подтверждённой минутной свечи для торгового входа",
    incomplete_horizon_candles: "Минутный ряд до конца горизонта неполон",
    missing_horizon_candle: "Отсутствует обязательная минутная свеча внутри горизонта",
    missing_funding_history: "Недоступна обязательная история funding",
    directional_trend_contract_invalid: "Повреждён контракт направленной trend-сделки",
    ambiguous_tp_sl_touch: "TP и SL затронуты одной минутной свечой; порядок не наблюдаем",
    intrabar_extreme_order_unobservable: "Не удалось однозначно определить порядок касаний цен внутри одной свечи",
    intrabar_replacement_fill_timing_unobservable: "Не удалось однозначно определить момент исполнения перевыставленной заявки внутри свечи",
    kill_switch_intrabar_order_unobservable: "Не удалось однозначно определить порядок срабатывания аварийной границы внутри свечи",
    insufficient_candle_volume_for_initial_inventory: "Объёма свечи недостаточно для подтверждения формирования начальной позиции",
    insufficient_candle_volume_for_terminal_liquidation: "Объёма свечи недостаточно для подтверждения полного закрытия позиции в конце горизонта",
  };
  const label = labels[code];
  return label ? `${label} (${code})` : `${humanizeOperatorText(code)} (${code})`;
}

function isTechnicalIdentifierField(key) {
  return new Set([
    "rec_id", "publication_root_rec_id", "outcome_root_rec_id", "model_version", "policy_fingerprint",
    "database_instance_id", "runtime_owner", "owner", "lock_key",
  ]).has(String(key || ""));
}

const SUPPORTED_GRID_BOT_TYPE = "futures_grid";
const DIRECTIONAL_TREND_BOT_TYPE = "directional_trend";
const TREND_EVALUATION_REJECTED_KIND = "trend_evaluation_rejected";
const SUPPORTED_GRID_VENUE = "linear";

function candidateKindOf(it) {
  if (!it || typeof it !== "object") return "strategy_recommendation";
  const params = it.params && typeof it.params === "object" ? it.params : {};
  const reasons = it.reasons && typeof it.reasons === "object" ? it.reasons : {};
  if (String(it.bot_type || "") === DIRECTIONAL_TREND_BOT_TYPE && !["long", "short"].includes(String(it.direction || "").trim().toLowerCase())) {
    return TREND_EVALUATION_REJECTED_KIND;
  }
  const explicit = String(it.candidate_kind || params.candidate_kind || reasons.candidate_kind || "").trim().toLowerCase();
  if (["strategy_recommendation", TREND_EVALUATION_REJECTED_KIND].includes(explicit)) return explicit;
  return "strategy_recommendation";
}

function strategyLabelForItem(it) {
  if (candidateKindOf(it) === TREND_EVALUATION_REJECTED_KIND) return "Проверка тренда · сигнал отклонён";
  return botTypeLabel(it?.bot_type);
}

function botTypeLabel(botType) {
  if (botType === SUPPORTED_GRID_BOT_TYPE) return "Фьючерсная сетка";
  if (botType === DIRECTIONAL_TREND_BOT_TYPE) return "Направленный тренд · одна позиция";
  return "—";
}

function strategyDirectionRu(botType, dir) {
  const strategy = String(botType || "").trim().toLowerCase();
  const normalized = String(dir || "").trim().toLowerCase();
  if (normalized === "long" || normalized === "short") return directionRu(normalized);
  if (strategy === SUPPORTED_GRID_BOT_TYPE) return "Нейтральная сетка";
  if (strategy === DIRECTIONAL_TREND_BOT_TYPE) return "Направление не подтверждено";
  return directionRu(normalized);
}

function strategyDirectionBadge(botType, dir) {
  const strategy = String(botType || "").trim().toLowerCase();
  const normalized = String(dir || "").trim().toLowerCase();
  if (normalized === "long") return `<span class="dir-badge dir-long">▲ ${escapeHtml(strategyDirectionRu(strategy, normalized))}</span>`;
  if (normalized === "short") return `<span class="dir-badge dir-short">▼ ${escapeHtml(strategyDirectionRu(strategy, normalized))}</span>`;
  const isGrid = strategy === SUPPORTED_GRID_BOT_TYPE;
  const prefix = isGrid ? "•" : "!";
  const cls = isGrid ? "dir-neu" : "dir-invalid";
  return `<span class="dir-badge ${cls}">${prefix} ${escapeHtml(strategyDirectionRu(strategy, normalized))}</span>`;
}

function isRejectedTrendEvaluation(it) {
  return candidateKindOf(it) === TREND_EVALUATION_REJECTED_KIND;
}

function isDirectionalTrendSinglePosition(it) {
  return String(it?.bot_type || "") === DIRECTIONAL_TREND_BOT_TYPE
    && !isRejectedTrendEvaluation(it)
    && ["long", "short"].includes(String(it?.direction || "").trim().toLowerCase());
}

function operatorEffectiveStatus(it) {
  return String(it?.effective_status || it?.status || "").trim().toLowerCase();
}

function isLaunchableGridRecommendation(it) {
  if (!it || it.bot_type !== SUPPORTED_GRID_BOT_TYPE || it.venue !== SUPPORTED_GRID_VENUE) return false;
  const status = operatorEffectiveStatus(it);
  if (!(status === "recommended" || status === "active")) return false;
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

function isLaunchableRecommendation(it) {
  if (!it || it.venue !== SUPPORTED_GRID_VENUE || isRejectedTrendEvaluation(it)) return false;
  if (!(it.bot_type === SUPPORTED_GRID_BOT_TYPE || it.bot_type === DIRECTIONAL_TREND_BOT_TYPE)) return false;
  const status = operatorEffectiveStatus(it);
  if (!(status === "recommended" || status === "active")) return false;
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
  if (venue === "linear") return "Bybit: бессрочный фьючерс USDT";
  return venue || "—";
}

function liquidityTierRu(tier) {
  if (tier === "deep") return "Глубокая";
  if (tier === "mid") return "Средняя";
  if (tier === "shallow") return "Тонкая";
  return tier || "—";
}

function marginModeRu(mode) {
  if (mode === "cross") return "Общая маржа сеточного бота Bybit";
  if (mode === "isolated") return "Изолированная маржа single-position";
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
    ? `<a class="icon-link" href="${escapeHtml(futuresGridBotCreateUrl())}" target="_blank" rel="noopener noreferrer" title="Открыть страницу создания фьючерсной сетки на Bybit">${iconSvg("bot")}</a>`
    : "";
  const cls = compact ? "symbol-links compact" : "symbol-links";
  return `
    <span class="${cls}">
      <a class="icon-link" href="${escapeHtml(chartUrl)}" target="_blank" rel="noopener noreferrer" title="Открыть график Bybit">${iconSvg("chart")}</a>
      ${botLink}
    </span>
  `;
}

function operatorStatusTone(status) {
  const value = String(status || "").trim().toLowerCase();
  if (value === "recommended" || value === "active") return "good";
  if (value === "blocked") return "bad";
  if (value === "no_trade" || value === "pending") return "warn";
  if (value === "executed") return "executed";
  return "muted";
}

function statusBadgeHtml(status) {
  const tone = operatorStatusTone(status);
  return `<span class="badge-inline badge-${tone}">${escapeHtml(operatorStatusRu(status))}</span>`;
}

function shockBadgeHtml(shock) {
  const severity = (shock || {}).severity || "normal";
  const cls = severity === "lockdown" ? "shock-badge shock-lockdown" : severity === "guarded" ? "shock-badge shock-guarded" : "shock-badge shock-normal";
  const text = humanizeOperatorText((shock || {}).title || "Нормальный режим");
  return `<span class="${cls}">${escapeHtml(text)}</span>`;
}

function btcRelationMetric(betaInfo, symbol) {
  const safeSymbol = String(symbol || "").toUpperCase();
  const corrRaw = safeSymbol === "BTCUSDT" ? 1.0 : toFiniteNumber(betaInfo?.correlation);
  if (corrRaw === null) {
    return {
      label: "Связь с BTC",
      value: "—",
      iconClass: "unknown",
      title: "Недостаточно данных для расчёта связи с BTC",
    };
  }
  const corr = Math.max(-1, Math.min(1, corrRaw));
  const absCorr = Math.abs(corr);
  let iconClass = "independent";
  let titlePrefix = "Сигнал слабо связан с BTC";
  if (absCorr >= 0.70) {
    iconClass = "strong";
    titlePrefix = safeSymbol === "BTCUSDT" ? "Базовый BTC-инструмент" : "Сильная корреляция с BTC";
  } else if (absCorr >= 0.35) {
    iconClass = "partial";
    titlePrefix = "Частичная корреляция с BTC";
  }
  return {
    label: "Связь с BTC",
    value: `r=${formatDotNumber(corr, 2, false)}`,
    iconClass,
    title: `${titlePrefix}; окно ${toFiniteNumber(betaInfo?.window) ?? 24} ч`,
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
    .map((it) => ({ id: it?.rec_id, score: toFiniteNumber(it?.score) }))
    .filter((row) => row.id && row.score !== null);
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
      ? `; почти равные оценки: группа=${groupSize}, разница исходных оценок=${formatDotNumber(groupSpread, 4)}, порог=${formatDotNumber(SCORE_UI_NEAR_TIE_DELTA, 4)}`
      : `; заметная разница > ${formatDotNumber(SCORE_UI_NEAR_TIE_DELTA, 4)}`;

    for (let i = groupStart; i <= groupEnd; i += 1) {
      out.set(rows[i].id, {
        percentile,
        grade: zone.grade,
        zoneLabel: zone.label,
        raw: rows[i].score,
        groupSize,
        groupSpread,
        tieThreshold: SCORE_UI_NEAR_TIE_DELTA,
        title: `Ранг в выборке: ${percentile}/100 — ${zone.label}; исходная оценка допуска=${formatDotNumber(rows[i].score, 4)}${tieNote}; не является разрешением запуска`,
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
    raw: toFiniteNumber(item.score),
    title: `Ранг недоступен; исходная оценка допуска=${formatDotNumber(item.score, 4)}`,
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
  if (isDirectionalTrendSinglePosition(it)) {
    return "Это отдельная single-position trend-ветка. Она формирует TP/SL-план без усреднения, но сейчас не прошла model-specific profitability gate или meta-router; реальный биржевой ордер сервис не отправляет.";
  }
  const rows = launchDecisionDiagnostics(it, scoreMeta);
  const launchRow = rows.find(row => row.code === "launch_score");
  const confidenceRow = rows.find(row => row.code === "confidence_gate");
  const gateRow = rows.find(row => row.code === "decision_gates");
  const parts = [];
  if (launchRow) parts.push(launchRow.value);
  if (confidenceRow && !confidenceRow.value.includes("не включ")) parts.push(`уверенность ${confidenceRow.value}`);
  if (gateRow && gateRow.value !== "—") parts.push(`проверки: ${gateRow.value}`);
  const gateSummary = parts.length ? ` ${parts.join("; ")}.` : "";
  return `Запуск сетки сейчас не рекомендован. Ранг ${scoreMeta?.percentile ?? 0}/100 (${scoreMeta?.grade || "E"} · ${scoreMeta?.zoneLabel || ""}) — это только относительное место в текущей выборке, не разрешение запуска.${gateSummary}`;
}

function operatorNextActionsHtml(it) {
  const actions = Array.isArray(decisionContext(it).operator_next_actions)
    ? decisionContext(it).operator_next_actions.slice(0, 5)
    : [];
  if (!actions.length) return "";
  return `
    <div class="operator-card operator-next-actions-card">
      <h3>Что делать дальше</h3>
      <div class="small-blocks">
        ${actions.map(a => `
          <div class="small-block ${a.severity === "danger" ? "small-block-critical" : ""}">
            <code>${escapeHtml(a.code || "ACTION")}</code><br>
            <b>${escapeHtml(a.title || "Действие")}</b><br>
            ${escapeHtml(a.detail || "")}
          </div>
        `).join("")}
      </div>
    </div>
  `;
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
    ? `<span class="field-help" tabindex="0" title="${escapeHtml(helpText)}" aria-label="${escapeHtml(helpText)}">?</span>`
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
    bot.title = "Открыть страницу создания фьючерсной сетки на Bybit";
    bot.classList.remove("hidden");
  } else {
    bot.removeAttribute("href");
    bot.innerHTML = "";
    bot.title = "Создание сеточного бота скрыто: рекомендация сейчас не разрешена к запуску";
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
      takeProfitLabel: "Цель прибыли",
      stopLossLabel: "Ограничение убытка",
      exitGeometry: "Продажа (снижение): цель прибыли ниже диапазона, ограничение убытка выше диапазона",
    };
  }
  if (dir === "long") {
    return {
      takeProfitValue: killUpper,
      stopLossValue: killLower,
      takeProfitLabel: "Цель прибыли",
      stopLossLabel: "Ограничение убытка",
      exitGeometry: "Покупка (рост): цель прибыли выше диапазона, ограничение убытка ниже диапазона",
    };
  }
  return {
    takeProfitValue: "—",
    stopLossValue: `${killLower} / ${killUpper}`,
    takeProfitLabel: "Направленная цель прибыли не применяется",
    stopLossLabel: "Ограничение убытка / аварийная граница выхода",
    exitGeometry: "Нейтральная сетка: направленная цель прибыли не применяется; выход контролируется нижней и верхней аварийными границами",
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

function directionalExitMathForDisplay(it) {
  const exitLevels = (it || {}).directional_exit_levels;
  if (!exitLevels || typeof exitLevels !== "object") return {};
  const expectedDir = String((it || {}).direction || "").trim().toLowerCase();
  const dir = String(exitLevels.direction || "").trim().toLowerCase();
  if (expectedDir === "long" || expectedDir === "short") {
    if (dir !== expectedDir) return {};
  }
  if (dir !== "long" && dir !== "short") return {};
  if (exitLevels.has_directional_take_profit !== true) return {};
  if (exitLevels.geometry_valid === false) return {};
  if (!directionalExitGeometryOk(dir, exitLevels.take_profit, exitLevels.stop_loss, exitLevels.reference_price)) return {};
  const mathPayload = exitLevels.trade_math;
  return mathPayload && typeof mathPayload === "object" ? mathPayload : {};
}

function operatorExitLevelsFromBackend(exitLevels, fallback, meta = {}, expectedDirection = null) {
  if (!exitLevels || typeof exitLevels !== "object") return fallback;
  const dir = String(exitLevels.direction || "").trim().toLowerCase();
  const expectedDir = String(expectedDirection || "").trim().toLowerCase();
  const lower = formatBybitPrice(exitLevels.kill_switch_lower, meta, "down");
  const upper = formatBybitPrice(exitLevels.kill_switch_upper, meta, "up");
  const hasDirectionalTp = exitLevels.has_directional_take_profit === true && (dir === "long" || dir === "short");
  if ((expectedDir === "long" || expectedDir === "short") && dir !== expectedDir) {
    return {
      takeProfitValue: "—",
      stopLossValue: `${lower} / ${upper}`,
      takeProfitLabel: "Направленная цель прибыли заблокирована",
      stopLossLabel: "Ограничение убытка / аварийная граница выхода",
      exitGeometry: `Направление защитных уровней не совпадает с рекомендацией; показаны только аварийные границы выхода · ${fallback.exitGeometry || ""}`.trim(),
    };
  }
  const backendGeometryOk = exitLevels.geometry_valid !== false && directionalExitGeometryOk(dir, exitLevels.take_profit, exitLevels.stop_loss, exitLevels.reference_price);
  if (hasDirectionalTp && !backendGeometryOk) {
    return {
      takeProfitValue: "—",
      stopLossValue: `${lower} / ${upper}`,
      takeProfitLabel: "Направленная цель прибыли заблокирована",
      stopLossLabel: "Ограничение убытка / аварийная граница выхода",
      exitGeometry: `Защитные уровни имеют неверную геометрию; показаны только аварийные границы выхода · ${fallback.exitGeometry || ""}`.trim(),
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
    takeProfitLabel: hasDirectionalTp ? "Цель прибыли" : "Направленная цель прибыли не применяется",
    stopLossLabel: hasDirectionalTp ? "Ограничение убытка" : "Ограничение убытка / аварийная граница выхода",
    exitGeometry: fallback.exitGeometry || "",
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
  const operatorSheetKillSwitch = operatorSheet.kill_switch || {};
  const meta = (it || {}).bybit_meta || {};
  const rangeLowerRaw = firstFiniteValue([range, params, operatorSheet], ["lower", "price_range_lower", "range_lower"]);
  const rangeUpperRaw = firstFiniteValue([range, params, operatorSheet], ["upper", "price_range_upper", "range_upper"]);
  const entryRefRaw = firstFiniteValue([plan, params, operatorSheet], ["reference_price", "price_ref"]);
  const killLowerRaw = firstFiniteValue([ks, operatorSheetKillSwitch], ["lower", "kill_switch_lower"]);
  const killUpperRaw = firstFiniteValue([ks, operatorSheetKillSwitch], ["upper", "kill_switch_upper"]);
  const leverageRaw = firstFiniteValue([params, operatorSheet], ["leverage"]);
  const marginModeRaw = params.margin_mode || operatorSheet.margin_mode || "cross";
  const rangeLower = formatBybitPrice(rangeLowerRaw, meta, "down");
  const rangeUpper = formatBybitPrice(rangeUpperRaw, meta, "up");
  const entryRef = formatBybitPrice(entryRefRaw, meta, "nearest");
  const killLower = formatBybitPrice(killLowerRaw, meta, "down");
  const killUpper = formatBybitPrice(killUpperRaw, meta, "up");
  const gridStepAbs = formatBybitPrice(gridStep.step_abs, meta, "nearest");
  const tpLegAbs = formatBybitPrice(tpPerLeg.abs, meta, "nearest");
  const stepPct = formatPercentDot(params.grid_spacing_pct ?? gridStep.step_pct, 4, false);
  const tpLegPct = formatPercentDot(tpPerLeg.pct, 4, false);
  const leverage = it.venue === "linear" ? String(leverageRaw ?? 1) : "—";
  const marginMode = it.venue === "linear" ? marginModeRu(marginModeRaw) : "—";
  const gridCount = resolveGridCountForDisplay(it);
  const exits = operatorExitLevels((it || {}).direction, killLower, killUpper);
  const rawBackendExits = (it || {}).directional_exit_levels;
  const dirNorm = String((it || {}).direction || "").trim().toLowerCase();
  const venueNorm = String((it || {}).venue || "").trim().toLowerCase();
  const requiresBackendExitPayload = venueNorm === "linear" && (dirNorm === "long" || dirNorm === "short");
  const backendExitPayloadAvailable = rawBackendExits && typeof rawBackendExits === "object";
  const canonicalExits = requiresBackendExitPayload && !backendExitPayloadAvailable
    ? {
        takeProfitValue: "—",
        stopLossValue: `${killLower} / ${killUpper}`,
        takeProfitLabel: "Направленная цель прибыли заблокирована",
        stopLossLabel: "Ограничение убытка / аварийная граница выхода",
        exitGeometry: `Нет полного набора защитных уровней; показаны только аварийные границы выхода · ${exits.exitGeometry || ""}`.trim(),
      }
    : operatorExitLevelsFromBackend(rawBackendExits, exits, meta, dirNorm);
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
    gridCount,
    ...canonicalExits,
  };
}

function firstFiniteValue(sources, keys) {
  const picked = firstFiniteField(sources, keys);
  return picked ? picked.value : null;
}

function firstFiniteField(sources, keys) {
  for (const source of sources) {
    if (!source || typeof source !== "object") continue;
    for (const key of keys) {
      const value = toFiniteNumber(source[key]);
      if (value !== null) return { key, value };
    }
  }
  return null;
}

function gridMaxNotionalPrice(referencePrice, rangeLower, rangeUpper) {
  const candidates = [referencePrice, rangeLower, rangeUpper]
    .map(toFiniteNumber)
    .filter(value => value !== null && value > 0);
  return candidates.length ? Math.max(...candidates) : null;
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

function operatorMetrics(it) {
  const reasonsMetrics = (it?.reasons || {}).operator_metrics;
  const paramsMetrics = (it?.params || {}).operator_metrics;
  const metrics = reasonsMetrics && typeof reasonsMetrics === "object"
    ? reasonsMetrics
    : (paramsMetrics && typeof paramsMetrics === "object" ? paramsMetrics : {});
  return {
    plan: metrics.plan_rr && typeof metrics.plan_rr === "object" ? metrics.plan_rr : {},
    empirical: metrics.empirical_expectancy && typeof metrics.empirical_expectancy === "object"
      ? metrics.empirical_expectancy
      : {},
  };
}

function planRrNumber(it) {
  const ctx = decisionContext(it);
  const metrics = operatorMetrics(it);
  return toFiniteNumber(it?.operator_summary?.plan_rr ?? ctx.plan_rr ?? metrics.plan.rr);
}

function empiricalMeanReturnNumber(it) {
  const ctx = decisionContext(it);
  const metrics = operatorMetrics(it);
  return toFiniteNumber(it?.operator_summary?.empirical_mean_return ?? ctx.empirical_mean_return ?? metrics.empirical.mean_return);
}

function formatReturnFraction(value, digits = 2, signed = true) {
  const v = toFiniteNumber(value);
  if (v === null) return "—";
  return formatPercentDot(v * 100.0, digits, signed);
}

function planRrCell(it) {
  const value = planRrNumber(it);
  if (value === null) return `<span class="metric-unavailable" title="RR плана недоступен: не хватает данных о прибыли и убытке у аварийной границы выхода">—</span>`;
  const ctx = decisionContext(it);
  const metrics = operatorMetrics(it);
  const reward = toFiniteNumber(ctx.plan_projected_net_reward_usdt ?? metrics.plan.projected_net_reward_usdt);
  const loss = toFiniteNumber(ctx.plan_kill_switch_loss_usdt ?? metrics.plan.kill_switch_loss_usdt);
  const title = `Ожидаемая прибыль: ${reward === null ? "—" : formatUsdValue(reward)}; убыток у аварийной границы: ${loss === null ? "—" : formatUsdValue(loss)}. Сценарная метрика плана, не статистическая вероятность.`;
  return `<span class="metric-primary" title="${escapeHtml(title)}">${escapeHtml(formatDotNumber(value, 2, false))}</span>`;
}

function empiricalExpectancyCell(it) {
  const ctx = decisionContext(it);
  const metrics = operatorMetrics(it);
  const empirical = metrics.empirical;
  const status = String(ctx.empirical_expectancy_status ?? empirical.status ?? "insufficient").toLowerCase();
  const mean = empiricalMeanReturnNumber(it);
  const samples = toFiniteNumber(ctx.empirical_return_samples ?? empirical.return_samples) ?? 0;
  const ci = empirical.confidence_interval && typeof empirical.confidence_interval === "object"
    ? empirical.confidence_interval
    : {};
  const lower = toFiniteNumber(ctx.empirical_confidence_interval_lower ?? ci.lower);
  const upper = toFiniteNumber(ctx.empirical_confidence_interval_upper ?? ci.upper);
  const level = toFiniteNumber(ctx.empirical_confidence_level ?? ci.level) ?? 0.95;
  if (mean === null) {
    return `<span class="metric-unavailable" title="Завершённые наблюдения текущего набора правил: ${escapeHtml(String(samples))}; статистики пока недостаточно">мало данных</span>`;
  }
  const ciText = lower === null || upper === null
    ? "доверительный интервал недоступен"
    : `${formatReturnFraction(lower)} … ${formatReturnFraction(upper)}`;
  const title = `Статус: ${empiricalStatusRu(status)}; наблюдений: ${samples}; ${(level * 100).toFixed(0)}% доверительный интервал: ${ciText}. Это оценка по наблюдениям текущего набора правил, а не доказательство преимущества в реальной торговле.`;
  const cls = status === "positive" ? "metric-positive" : status === "negative" ? "metric-negative" : "metric-uncertain";
  return `<span class="${cls}" title="${escapeHtml(title)}">${escapeHtml(formatReturnFraction(mean))}</span>`;
}

function riskBufferNumber(it) {
  return toFiniteNumber(decisionContext(it).liquidation_buffer_pct);
}

function riskBufferCell(it) {
  const value = riskBufferNumber(it);
  if (value === null) return `<span class="metric-unavailable" title="Запас капитала недоступен">—</span>`;
  const cls = value >= 20 ? "metric-positive" : value >= 10 ? "metric-uncertain" : "metric-negative";
  return `<span class="${cls}" title="Остаток выделенного капитала после худшего сценария у аварийной границы выхода; это не точная цена ликвидации">${escapeHtml(formatPercentDot(value, 2, false))}</span>`;
}

function priceStatusRu(status) {
  if (status === "inside_range") return "внутри диапазона";
  if (status === "outside_range") return "вне диапазона";
  if (status === "available") return "цена доступна";
  return "нет текущей цены";
}

function preflightStatusRu(status) {
  if (status === "ok") return "Проверка пройдена — запуск технически допустим";
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
  const recTimestampValid = ctx.recommendation_timestamp_valid !== false;
  const chainTimestampValid = ctx.publication_chain_timestamp_valid !== false;
  const ttlText = !recTimestampValid
    ? "Некорректная метка времени — запуск заблокирован"
    : ctx.is_expired === true
      ? `истекла ${formatDurationValue(Math.abs(expiresIn ?? 0))} назад`
      : expiresIn !== null && expiresIn !== undefined
        ? `осталось ${formatDurationValue(expiresIn)}`
        : "Срок действия не задан";
  const chainTtlText = !chainTimestampValid
    ? "Некорректная метка времени цепочки — запуск заблокирован"
    : ctx.is_publication_chain_expired === true
      ? `цепочка истекла ${formatDurationValue(Math.abs(chainExpiresIn ?? 0))} назад`
      : chainExpiresIn !== null && chainExpiresIn !== undefined
        ? `цепочка: осталось ${formatDurationValue(chainExpiresIn)}`
        : "Срок действия цепочки не задан";
  if (candidateKindOf(it) === TREND_EVALUATION_REJECTED_KIND) {
    return [
      { label: "Опорная цена анализа", value: ov.entryRef, mono: true, help: "Цена рыночного снимка, на котором проверялось наличие направленного тренда. Это не цена входа: позиция не сформирована." },
      { label: "Результат проверки", value: "LONG/SHORT не подтверждён", help: "Для отклонённой проверки тренда позиция, TP и SL не формируются." },
      { label: "Текущая цена", value: formatBybitPrice(currentPrice, meta, "nearest"), mono: true, help: "Текущая цена показана только как контекст анализа; она не превращает отклонённую оценку в торговый план." },
      { label: "Возраст проверки", value: recAge == null ? "—" : formatDurationValue(recAge), help: "Возраст диагностической проверки направления. У неё нет operator TTL торгового плана и нет outcome horizon." },
    ];
  }
  if (String(it?.bot_type || "") === "directional_trend") {
    const levels = it?.params?.trade_plan?.levels || {};
    const backendExit = it?.directional_exit_levels && typeof it.directional_exit_levels === "object"
      ? it.directional_exit_levels
      : {};
    const tp = toFiniteNumber(backendExit.take_profit ?? levels?.take_profit?.price ?? it?.params?.take_profit_price);
    const sl = toFiniteNumber(backendExit.stop_loss ?? levels?.stop_loss?.price ?? it?.params?.stop_loss_price);
    const px = toFiniteNumber(currentPrice);
    const direction = String(it?.direction || "long").toLowerCase();
    const tpDistance = px === null || tp === null || px <= 0 ? null : (direction === "short" ? (px - tp) : (tp - px)) / px * 100;
    const slDistance = px === null || sl === null || px <= 0 ? null : (direction === "short" ? (sl - px) : (px - sl)) / px * 100;
    const between = px !== null && tp !== null && sl !== null && px >= Math.min(tp, sl) && px <= Math.max(tp, sl);
    return [
      { label: "Цена входа", value: ov.entryRef, mono: true, help: "Расчётная опорная цена single-position trend-сделки; перед внешним исполнением должна быть сопоставлена с текущей ценой." },
      { label: "Текущая цена", value: formatBybitPrice(currentPrice, meta, "nearest"), mono: true, help: "Последняя доступная биржевая цена. Нужна для проверки, не были ли TP или SL уже достигнуты и не устарел ли вход." },
      { label: "Отклонение от входа", value: drift == null ? "—" : formatPercentDot(drift, 2, true), help: "Отклонение текущей цены от расчётного входа single-position плана." },
      { label: "Положение цены", value: px === null ? "нет текущей цены" : (between ? "между TP и SL" : "за пределами TP/SL"), help: "Для допуска текущая цена должна сохранять исходную геометрию entry–TP–SL; уже пройденная граница делает план устаревшим." },
      { label: "До take-profit", value: tpDistance === null ? "—" : formatPercentDot(tpDistance, 2, true), help: "Направленное расстояние от текущей цены до take-profit. Отрицательное значение означает, что цель уже пройдена." },
      { label: "До stop-loss", value: slDistance === null ? "—" : formatPercentDot(slDistance, 2, true), help: "Направленный запас от текущей цены до stop-loss. Отрицательное значение означает, что защитная граница уже пройдена." },
      { label: "Возраст цены", value: tickerAge == null ? "—" : formatDurationValue(tickerAge), help: "Возраст последнего биржевого снимка; stale-price блокирует формирование исполнимого single-order package." },
      { label: "Возраст текущей строки", value: `${recAge == null ? "—" : formatDurationValue(recAge)} · ${ttlText}`, help: "Возраст текущей публикации trend-плана и остаток operator TTL." },
      { label: "Возраст идеи с первого сигнала", value: `${chainAge == null ? "—" : formatDurationValue(chainAge)} · обновлений: ${chainUpdates ?? "—"} · ${chainTtlText}`, help: `Возраст trend-идеи с начала публикационной цепочки. Старт: ${formatTs(chainStartedTs)}.` },
    ];
  }
  return [
    {
      label: "Цена входа",
      value: ov.entryRef,
      mono: true,
      help: "Расчётная цена входа из рекомендации. Используется оператором при создании сеточного бота и не должна удаляться из панели.",
    },
    {
      label: "Текущая цена",
      value: formatBybitPrice(currentPrice, meta, "nearest"),
      mono: true,
      help: "Последняя доступная биржевая цена или середина между ценой покупки и продажи. Нужна, чтобы понять, не устарели ли уровни сетки.",
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
      help: `Сколько прошло с первичного сигнала этой публикационной цепочки. Старт цепочки: ${formatTs(chainStartedTs)}. Если это сильно больше возраста текущей строки, рекомендация может выглядеть свежей, хотя идея уже долго живёт.`,
    },
  ];
}

function buildRiskEconomicsFields(it) {
  if (candidateKindOf(it) === TREND_EVALUATION_REJECTED_KIND) {
    return [
      { label: "Тип записи", value: "Диагностическая проверка направления", help: "Это не рекомендация single-position trend и не сеточный бот." },
      { label: "Причина", value: "LONG/SHORT не подтверждён", help: "Без подтверждённого направления отсутствуют entry, TP, SL, first-touch outcome и обучающая метка." },
      { label: "Outcome", value: "не применяется", help: "Отклонённая проверка не создаёт outcome-root, не попадает в историю стратегии и не обучает trend-модель." },
      { label: "Действие", value: "ждать новый рыночный снимок", help: "Нельзя вручную достраивать TP/SL или интерпретировать neutral как трендовую позицию." },
    ];
  }
  const ctx = decisionContext(it);
  const metrics = operatorMetrics(it);
  const plan = metrics.plan;
  const empirical = metrics.empirical;
  const planRr = toFiniteNumber(ctx.plan_rr ?? plan.rr);
  const planReward = toFiniteNumber(ctx.plan_projected_net_reward_usdt ?? plan.projected_net_reward_usdt);
  const planLoss = toFiniteNumber(ctx.plan_kill_switch_loss_usdt ?? plan.kill_switch_loss_usdt);
  const empiricalMean = toFiniteNumber(ctx.empirical_mean_return ?? empirical.mean_return);
  const empiricalTail = toFiniteNumber(ctx.empirical_expected_shortfall ?? empirical.expected_shortfall);
  const empiricalRr = toFiniteNumber(ctx.empirical_rr ?? empirical.empirical_rr);
  const empiricalStatus = String(ctx.empirical_expectancy_status ?? empirical.status ?? "insufficient");
  const empiricalSamples = toFiniteNumber(ctx.empirical_return_samples ?? empirical.return_samples) ?? 0;
  const empiricalClusters = toFiniteNumber(ctx.empirical_temporal_cluster_count ?? empirical.temporal_cluster_count) ?? 0;
  const empiricalMinClusters = toFiniteNumber(ctx.empirical_minimum_temporal_clusters ?? empirical.minimum_temporal_clusters) ?? 0;
  const empiricalCi = empirical.confidence_interval && typeof empirical.confidence_interval === "object"
    ? empirical.confidence_interval
    : {};
  const empiricalLower = toFiniteNumber(ctx.empirical_confidence_interval_lower ?? empiricalCi.lower);
  const empiricalUpper = toFiniteNumber(ctx.empirical_confidence_interval_upper ?? empiricalCi.upper);
  const empiricalLevel = toFiniteNumber(ctx.empirical_confidence_level ?? empiricalCi.level) ?? 0.95;
  const empiricalValue = empiricalMean === null
    ? `недостаточно данных · наблюдений: ${empiricalSamples}`
    : `${formatReturnFraction(empiricalMean)} · ${(empiricalLevel * 100).toFixed(0)}% доверительный интервал ${empiricalLower === null || empiricalUpper === null ? "—" : `${formatReturnFraction(empiricalLower)} … ${formatReturnFraction(empiricalUpper)}`}`;
  const empiricalTailValue = empiricalTail === null
    ? "—"
    : `${formatReturnFraction(empiricalTail)}${empiricalRr === null ? "" : ` · RR ${formatDotNumber(empiricalRr, 2, false)}`}`;
  const router = it?.reasons?.strategy_router && typeof it.reasons.strategy_router === "object" ? it.reasons.strategy_router : {};
  const routerCandidate = router.candidate && typeof router.candidate === "object" ? router.candidate : {};
  const routerUtility = toFiniteNumber(routerCandidate.utility);
  const routerEdge = toFiniteNumber(router.utility_edge);
  const routerWinner = String(router.winner_bot_type || "");
  if (String(it?.bot_type || "") === "directional_trend") {
    const eventModel = it?.reasons?.trend_event_model && typeof it.reasons.trend_event_model === "object" ? it.reasons.trend_event_model : {};
    const tpFirst = toFiniteNumber(eventModel.tp_first_probability);
    const slFirst = toFiniteNumber(eventModel.sl_first_probability);
    const timeout = toFiniteNumber(eventModel.horizon_exit_probability);
    const eventEv = toFiniteNumber(eventModel.event_expected_net_return);
    const eventEvLower = toFiniteNumber(eventModel.event_expected_net_return_lower_bound);
    return [
      { label: "Выбор стратегии", value: router.status === "selected" ? `${botTypeLabel(routerWinner)} · utility ${routerUtility === null ? "—" : formatReturnFraction(routerUtility)}` : humanizeOperatorText(router.reason_code || router.status || "нет решения"), help: "Meta-router сравнивает grid и trend только по сопоставимой calibrated monetary utility." },
      { label: "Преимущество победителя", value: routerEdge === null ? "—" : formatReturnFraction(routerEdge), help: "Разница utility с ближайшим допустимым конкурентом; при малом преимуществе выбирается no_trade." },
      { label: "Предзапусковая проверка", value: preflightStatusRu(ctx.preflight_status), help: "Проверяет live-price, TP/SL, tickSize, qtyStep, notional, margin, funding и конфликт стратегии по символу." },
      { label: "Профиль риска", value: riskProfileRu(ctx.risk_profile), help: "Сводная оценка риска single-position сделки до stop-loss и лимитов капитала." },
      { label: "RR entry–TP/SL", value: planRr === null ? "—" : formatDotNumber(planRr, 2, false), help: "Отношение чистого payoff при TP к модулю чистого убытка при SL; не является вероятностью достижения TP." },
      { label: "Net TP / net SL", value: `${planReward === null ? "—" : formatUsdValue(planReward)} / ${planLoss === null ? "—" : formatUsdValue(planLoss)}`, help: "Денежные сценарии одной позиции после оценочных издержек; знак SL должен быть неблагоприятным." },
      { label: "P(TP_FIRST) / P(SL_FIRST)", value: `${tpFirst === null ? "—" : formatPercentDot(tpFirst * 100, 1, false)} / ${slFirst === null ? "—" : formatPercentDot(slFirst * 100, 1, false)}`, help: "Отдельная multiclass first-touch модель оценивает порядок достижения границ, а не просто знак цены через 12 часов." },
      { label: "P(HORIZON_EXIT)", value: timeout === null ? "—" : formatPercentDot(timeout * 100, 1, false), help: "Вероятность выхода по времени без однозначного достижения TP или SL." },
      { label: "First-touch EV", value: eventEv === null ? "—" : formatReturnFraction(eventEv), help: "Денежная ожидаемость по трём событиям с costs/funding." },
      { label: "Нижняя граница first-touch EV", value: eventEvLower === null ? "—" : formatReturnFraction(eventEvLower), help: "Консервативная EV после учёта uncertainty; должна быть положительной для допуска trend." },
      { label: "Доходность по наблюдениям", value: empiricalValue, help: `Отдельная trend-выборка текущей lineage. Статус: ${empiricalStatusRu(empiricalStatus)}; наблюдений: ${empiricalSamples}; временных групп: ${empiricalClusters}/${empiricalMinClusters}.` },
      { label: "Худший хвост trend-наблюдений", value: empiricalTailValue, help: "Expected shortfall и empirical RR только по single-position trend-outcomes." },
      { label: "Расходы на исполнение", value: ctx.execution_cost_bps == null ? "—" : formatBps(ctx.execution_cost_bps, 2, false), help: "Оценка round-trip комиссий, spread и slippage для одной позиции." },
      { label: "Платёж финансирования", value: ctx.funding_cost_bps == null ? "—" : formatBps(ctx.funding_cost_bps, 2, false), help: "Неблагоприятный funding за фактический горизонт single-position сделки." },
    ];
  }
  return [
    {
      label: "Выбор стратегии",
      value: router.status === "selected"
        ? `${botTypeLabel(routerWinner)} · utility ${routerUtility === null ? "—" : formatReturnFraction(routerUtility)}`
        : humanizeOperatorText(router.reason_code || router.status || "нет решения"),
      help: "Meta-router сравнивает только bot-specific calibrated модели на общем 12-часовом net-return basis. Raw score разных стратегий не сравнивается.",
    },
    {
      label: "Преимущество победителя",
      value: routerEdge === null ? "—" : formatReturnFraction(routerEdge),
      help: "Разница risk-adjusted monetary utility между лучшей и второй допустимой стратегией. При недостаточном преимуществе система выбирает no_trade.",
    },
    {
      label: "Предзапусковая проверка",
      value: preflightStatusRu(ctx.preflight_status),
      help: "Результат технической проверки перед запуском: данные инструмента Bybit, диапазон, размеры заявки, шаг цены, шаг количества, минимальная сумма и защитные уровни.",
    },
    {
      label: "Профиль риска",
      value: riskProfileRu(ctx.risk_profile),
      help: "Сводная оценка запаса капитала при движении цены к аварийной границе выхода. Это консервативный стресс-сценарий, а не точная биржевая цена ликвидации.",
    },
    {
      label: "RR плана",
      value: planRr === null ? "—" : formatDotNumber(planRr, 2, false),
      help: "Сценарное отношение ожидаемой чистой прибыли именно этого плана сетки к убытку при достижении худшей аварийной границы выхода. Это не вероятность прибыли и не статистика прошлых наблюдений.",
    },
    {
      label: "Прибыль плана / убыток у аварийной границы",
      value: `${planReward === null ? "—" : formatUsdValue(planReward)} / ${planLoss === null ? "—" : formatUsdValue(planLoss)}`,
      help: "Числитель RR плана — ожидаемая прибыль завершённых пар уровней после комиссий, разовых расходов на исполнение и неблагоприятного платежа финансирования. Знаменатель — ценовой убыток при худшей аварийной границе выхода. Резерв поддерживающей маржи в убыток не включается.",
    },
    {
      label: "Доходность по наблюдениям",
      value: empiricalValue,
      help: `Средний чистый результат завершённых наблюдений текущего неизменного набора правил с доверительным интервалом. Статус: ${empiricalStatusRu(empiricalStatus)}; наблюдений: ${empiricalSamples}; независимых временных групп: ${empiricalClusters}/${empiricalMinClusters}.`,
    },
    {
      label: "Худшие наблюдения и RR",
      value: empiricalTailValue,
      help: "Средний результат худших наблюдений — средний результат худшего хвоста завершённых наблюдений. RR по наблюдениям = положительная средняя доходность / модуль среднего результата худших наблюдений. При недостаточной или односторонней выборке RR не показывается.",
    },
    {
      label: "Запас капитала",
      value: ctx.liquidation_buffer_pct === null || ctx.liquidation_buffer_pct === undefined ? "—" : formatPercentDot(ctx.liquidation_buffer_pct, 2, false),
      help: "Остаток выделенного капитала после неблагоприятного движения к аварийной границе выхода, расходов на исполнение и резерва поддерживающей маржи. Возможная выгода от платежа финансирования и прибыль сетки не засчитываются заранее.",
    },
    {
      label: "Чистая прибыль одной пары уровней",
      value: ctx.net_profit_bps === null || ctx.net_profit_bps === undefined ? "—" : formatBps(ctx.net_profit_bps, 2, true),
      help: "Прибыль одной завершённой пары соседних уровней после комиссий двух исполнений. Разовые расходы на исполнение и платёж финансирования за весь горизонт учитываются отдельно в RR плана. 1 б.п. = 0,01%.",
    },
    {
      label: "Расходы на исполнение",
      value: ctx.execution_cost_bps === null || ctx.execution_cost_bps === undefined ? "—" : formatBps(ctx.execution_cost_bps, 2, false),
      help: "Оценка расходов на вход и выход: комиссия, разница цен покупки и продажи и возможное проскальзывание. 1 б.п. = 0,01%.",
    },
    {
      label: "Платёж финансирования",
      value: ctx.funding_cost_bps === null || ctx.funding_cost_bps === undefined ? "—" : formatBps(ctx.funding_cost_bps, 2, false),
      help: "Ожидаемый неблагоприятный платёж финансирования за горизонт удержания. Это периодический платёж между участниками, рассчитывающими на рост и снижение цены бессрочного фьючерса.",
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
  const qtyNumber = toFiniteNumber(qty);
  if (qtyNumber !== null) {
    const qtyText = formatDotNumber(qtyNumber, 8, false);
    parts.push(baseAsset ? `${qtyText} ${baseAsset}` : qtyText);
  }
  return parts.length ? parts.join(" · ") : "—";
}

function buildOperatorFieldSpecs(it, ov) {
  const params = (it || {}).params || {};
  const economics = params.economics || {};
  const sizing = params.sizing || {};
  const plan = params.trade_plan || {};
  const levels = plan.levels || {};
  const candidateKind = String((it || {}).candidate_kind || params.candidate_kind || (it || {})?.reasons?.candidate_kind || "").trim().toLowerCase();
  const rejectedTrendEvaluation = candidateKind === TREND_EVALUATION_REJECTED_KIND
    || (String((it || {}).bot_type || "") === DIRECTIONAL_TREND_BOT_TYPE
      && !["long", "short"].includes(String((it || {}).direction || "").trim().toLowerCase()));
  if (rejectedTrendEvaluation) {
    return [
      { label: "Тип записи", value: "Отклонённая предварительная проверка тренда", help: "Это диагностический результат анализа рынка, а не сформированная торговая стратегия." },
      { label: "Результат проверки", value: "LONG/SHORT не подтверждён", help: "Направление оказалось неоднозначным или недостаточно доказанным. Нейтральная trend-позиция не создаётся." },
      { label: "Торговая позиция", value: "Не сформирована", help: "Нет entry, количества, leverage и биржевого плана исполнения." },
      { label: "TP / SL", value: "Не рассчитываются", help: "Без подтверждённого LONG или SHORT корректная first-touch геометрия отсутствует." },
      { label: "Исход", value: "Не планируется", help: "Эта запись исключена из outcome scheduling и не создаёт независимое 12-часовое окно." },
      { label: "Обучение", value: "Исключено", help: "Отклонённая оценка не является размеченным примером directional_trend и не попадает в calibrator или first-touch модель." },
      { label: "Следующее действие", value: "Ждать новый рыночный снимок", help: "Повторная полноценная trend-рекомендация появится только после подтверждения LONG или SHORT." },
    ];
  }
  if (String((it || {}).bot_type || "") === "directional_trend") {
    const takeProfit = levels.take_profit || {};
    const stopLoss = levels.stop_loss || {};
    const targetNotional = toFiniteNumber(sizing.target_notional_usdt);
    const planRr = toFiniteNumber(economics.plan_rr);
    const horizon = toFiniteNumber(plan.label_horizon_hours ?? params.label_horizon_hours);
    const eventModel = (it?.reasons?.trend_event_model && typeof it.reasons.trend_event_model === "object")
      ? it.reasons.trend_event_model
      : ((params.trend_event_assessment && typeof params.trend_event_assessment === "object") ? params.trend_event_assessment : {});
    const tpFirst = toFiniteNumber(eventModel.tp_first_probability);
    const slFirst = toFiniteNumber(eventModel.sl_first_probability);
    const timeout = toFiniteNumber(eventModel.horizon_exit_probability);
    const eventEv = toFiniteNumber(eventModel.event_expected_net_return);
    const eventEvLower = toFiniteNumber(eventModel.event_expected_net_return_lower_bound);
    const eventReady = eventModel.ready === true;
    return [
      { label: "Стратегия", value: "Направленный тренд · одна позиция", help: "Отдельная single-position стратегия, а не направленная сетка. Система формирует проверяемый пакет для ручного или внешнего исполнения." },
      { label: "Направление", value: strategyDirectionRu((it || {}).bot_type, (it || {}).direction), help: "LONG следует за подтверждённым ростом, SHORT — за подтверждённым снижением." },
      { label: "Модель входа", value: "Одна позиция, без усреднения", help: "Позиция не увеличивается против движения и не использует grid-levels." },
      { label: "Расчётная цена входа", value: formatBybitPrice(plan.reference_price ?? params.price_ref, it?.bybit_meta || {}, "nearest"), mono: true },
      { label: "Цель прибыли", value: formatBybitPrice(takeProfit.price, it?.bybit_meta || {}, "nearest"), mono: true },
      { label: "Ограничение убытка", value: formatBybitPrice(stopLoss.price, it?.bybit_meta || {}, "nearest"), mono: true },
      { label: "Номинал позиции", value: targetNotional === null ? "—" : formatUsdValue(targetNotional), help: "Расчётный номинал одной позиции после Bybit qty-step preflight и runtime risk caps." },
      { label: "Расчётный RR", value: planRr === null ? "—" : formatDotNumber(planRr, 2, false), help: "Проектный reward/risk после оценочных издержек; не доказательство live edge." },
      { label: "P(TP раньше SL)", value: tpFirst === null ? "—" : formatPercentDot(tpFirst * 100.0, 1, false), help: "Вероятность того, что цель прибыли будет достигнута раньше ограничения убытка по отдельной first-touch модели." },
      { label: "P(SL раньше TP)", value: slFirst === null ? "—" : formatPercentDot(slFirst * 100.0, 1, false), help: "Вероятность первичного достижения stop-loss до take-profit." },
      { label: "P(выход по времени)", value: timeout === null ? "—" : formatPercentDot(timeout * 100.0, 1, false), help: "Вероятность, что за горизонт не будет однозначно достигнут ни TP, ни SL." },
      { label: "First-touch EV", value: eventEv === null ? "—" : formatPercentDot(eventEv * 100.0, 3, true), help: "Ожидаемая чистая доходность: P(TP_FIRST)×TP + P(SL_FIRST)×SL + P(HORIZON_EXIT)×ожидаемый выход по времени." },
      { label: "Нижняя граница first-touch EV", value: eventEvLower === null ? "—" : formatPercentDot(eventEvLower * 100.0, 3, true), help: "Консервативная граница EV с переносом вероятностной неопределённости от TP к SL. Для допуска должна быть положительной." },
      { label: "Готовность first-touch модели", value: eventReady ? "Подтверждена" : "Не подтверждена", help: "Требуются точные TP_FIRST/SL_FIRST/HORIZON_EXIT метки, purged chronological holdout и положительная консервативная EV." },
      { label: "Горизонт метки", value: horizon === null ? "—" : `${formatDotNumber(horizon, 0, false)} ч`, help: "Период независимого proxy-outcome для трендовой ветки." },
      { label: "Исполнение", value: "Ручное / внешний execution-layer", help: "Сервис не отправляет биржевой ордер. Кнопка подтверждения создаёт только внутренний audit-instance и фиксирует пакет entry/TP/SL." },
    ];
  }
  const range = levels.range || {};
  const rangeValue = `${ov.rangeLower} — ${ov.rangeUpper}`;
  const operatorSheet = params.operator_sheet || {};
  const operatorSizing = operatorSheet.sizing || {};
  const operatorEconomics = operatorSheet.economics || {};
  const marginRequired = firstFiniteValue(
    [sizing, economics, operatorSizing, operatorEconomics, params, operatorSheet],
    [
      "estimated_worst_case_margin_required_usdt",
      "worst_case_margin_required_usdt",
      "estimated_margin_required_usdt",
      "margin_required_usdt",
      "capital_required_usdt",
      "margin_usdt",
      "investment_usdt",
    ]
  );
  const leverageRaw = firstFiniteValue([params, plan, operatorSheet], ["leverage"]);
  const leverage = Math.max(1, Number(leverageRaw || 1));
  const positionNotionalKeys = [
    "estimated_worst_case_total_order_notional_usdt",
    "worst_case_total_order_notional_usdt",
    "estimated_max_position_notional_usdt",
    "max_position_notional_usdt",
    "estimated_total_order_notional_usdt",
    "total_order_notional_usdt",
    "position_notional_usdt",
    "notional_usdt",
  ];
  const worstCasePositionNotionalKeys = new Set([
    "estimated_worst_case_total_order_notional_usdt",
    "worst_case_total_order_notional_usdt",
    "estimated_max_position_notional_usdt",
    "max_position_notional_usdt",
  ]);
  const positionNotionalPick = firstFiniteField(
    [sizing, economics, operatorSizing, operatorEconomics, params, operatorSheet],
    positionNotionalKeys
  );
  const positionNotional = positionNotionalPick !== null
    ? positionNotionalPick.value
    : (marginRequired !== null && Number.isFinite(leverage) ? marginRequired * leverage : null);
  const symbolParts = splitLinearSymbol((it || {}).symbol);
  const referencePrice = firstFiniteValue([plan, params, operatorSheet], ["reference_price", "price_ref"]);
  const rangeLowerForQty = firstFiniteValue([range, params, operatorSheet], ["lower", "price_range_lower", "range_lower"]);
  const rangeUpperForQty = firstFiniteValue([range, params, operatorSheet], ["upper", "price_range_upper", "range_upper"]);
  const explicitPositionQty = firstFiniteValue(
    [sizing, economics, operatorSizing, operatorEconomics, params, operatorSheet],
    ["estimated_position_qty", "position_qty", "total_qty", "estimated_total_qty", "max_position_qty", "estimated_max_position_qty"]
  );
  const qtyPrice = positionNotionalPick && worstCasePositionNotionalKeys.has(positionNotionalPick.key)
    ? (gridMaxNotionalPrice(referencePrice, rangeLowerForQty, rangeUpperForQty) ?? referencePrice)
    : referencePrice;
  const positionQty = explicitPositionQty ?? (
    positionNotional !== null && qtyPrice !== null && qtyPrice > 0
      ? positionNotional / qtyPrice
      : null
  );
  const capitalValue = formatUsdValue(marginRequired);
  const positionValue = formatPositionSizeValue(positionNotional, positionQty, symbolParts?.base || "");
  const botLifetimeValue = formatBotLifetimeValue(params);
  const exitMath = directionalExitMathForDisplay(it);
  const tpDistancePct = toFiniteNumber(exitMath.take_profit_distance_pct);
  const slDistancePct = toFiniteNumber(exitMath.stop_loss_distance_pct);
  const distanceValue = tpDistancePct === null && slDistancePct === null
    ? "—"
    : `Цель ${tpDistancePct === null ? "—" : formatPercentDot(tpDistancePct, 2, false)} / ограничение ${slDistancePct === null ? "—" : formatPercentDot(slDistancePct, 2, false)}`;
  const fields = [
    { label: "Направление", value: strategyDirectionRu((it || {}).bot_type, (it || {}).direction), mono: false, help: "Покупка рассчитана на рост цены, продажа — на снижение. Нейтральная сетка работает внутри диапазона и не должна подменяться направленной целью прибыли." },
    { label: "Размер позиции", value: positionValue, copyValue: positionNotional !== null ? formatDotNumber(positionNotional, 4, false) : positionValue, mono: true, help: "Наибольший оценочный номинальный объём позиции в худшем сценарии. Количество рассчитывается по неблагоприятной цене диапазона, чтобы не занизить риск." },
    { label: "Время работы", value: botLifetimeValue, copyValue: botLifetimeValue, help: "Рекомендуемый горизонт удержания бота, а не срок действия самой рекомендации." },
    { label: "Требуемая маржа", value: capitalValue, copyValue: marginRequired !== null ? formatDotNumber(marginRequired, 4, false) : capitalValue, help: "Оценочная сумма USDT, которую нужно выделить под сетку с указанным плечом. Используется более консервативная оценка для худшего сценария." },
    { label: "Диапазон входа", value: rangeValue, mono: true, help: "Нижняя и верхняя границы основного диапазона сетки, которые оператор переносит в Bybit." },
    { label: "Цена входа", value: ov.entryRef, mono: true, help: "Расчётная цена входа из рекомендации. Используется при создании бота и остаётся обязательным полем основной панели." },
    { label: "Число интервалов сетки", value: ov.gridCount === null ? "—" : ov.gridCount, help: "Количество ценовых интервалов сетки. Противоречивое, дробное или логическое значение блокируется предзапусковой проверкой." },
    { label: "Кредитное плечо", value: ov.leverage, help: "Кредитное плечо линейного фьючерса с расчётом в USDT. Увеличивает и возможную прибыль, и риск потери капитала." },
    { label: ov.takeProfitLabel || "Цель прибыли", value: ov.takeProfitValue, mono: true, help: "Уровень фиксации прибыли. Для покупки он выше цены входа, для продажи — ниже." },
    { label: ov.stopLossLabel || "Ограничение убытка", value: ov.stopLossValue, mono: true, help: "Защитный уровень остановки убытка. Для покупки он ниже цены входа, для продажи — выше. У нейтральной сетки используются две аварийные границы." },
    { label: "Расстояние до цели / ограничения", value: distanceValue, mono: true, help: "Расстояния от расчётной цены входа до цели прибыли и ограничения убытка. Для продажи цель находится ниже, а ограничение — выше." },
  ];
  return fields.filter(f => f.value !== undefined && f.value !== null && f.value !== "");
}

function factorNameRu(name) {
  const mapping = {
    range_score: "Диапазонность",
    coherence: "Согласованность таймфреймов",
    regime_confidence: "Уверенность режима",
    effective_sentiment: "Новостной фон",
    direction_strength: "Сила направления",
    trend_strength: "Трендовость",
    atr_pct: "Средний диапазон колебаний цены",
    execution_cost_bps: "Издержки исполнения",
    spread_bps: "Разница цен покупки и продажи",
  };
  return mapping[name] || name || "factor";
}

function factorItemHtml(factor, tone = "positive") {
  if (!factor) return "";
  const cls = tone === "positive" ? "factor-item positive" : "factor-item negative";
  const msg = factor.msg || factor.reason || factorNameRu(factor.name);
  const weight = toFiniteNumber(factor.weight);
  const weightText = weight === null ? "—" : `${weight >= 0 ? "+" : ""}${formatDotNumber(weight, 2)}`;
  return `
    <div class="${cls}">
      <div class="factor-sign">${tone === "positive" ? "+" : "−"}</div>
      <div class="factor-body">
        <div class="factor-msg">${escapeHtml(humanizeOperatorText(msg))}</div>
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
    blocks: it.blocks || [],
    cost_model: reasons.cost_model || {},
    market_shock: reasons.market_shock || {},
    fast_veto: reasons.fast_veto || {},
    direction_agg: reasons.direction_agg || {},
    sentiment_agg: reasons.sentiment_agg || {},
    bybit_meta: it.bybit_meta || {},
    bybit_plan_validation: it.bybit_plan_validation || {},
    operator_decision_context: it.operator_decision_context || {},
    operator_summary: it.operator_summary || {},
    factors: {
      positive: reasons.top_positive_factors || [],
      negative: reasons.top_negative_factors || [],
    },
    params: it.params || {},
  };
}

function riskReportMessageItem(item, fallbackCode, critical = true) {
  if (item && typeof item === "object") {
    return { code: item.code || fallbackCode, msg: item.msg || item.message || item.reason || "", critical };
  }
  return { code: fallbackCode, msg: String(item || ""), critical };
}

function operatorBlockMessageRu(item) {
  const code = String(item?.code || "").trim().toUpperCase();
  const labels = {
    ACCOUNT_MODE_MISSING: "Не сохранён режим счёта. Исполнение блокируется, потому что сервис не должен угадывать account mode.",
    ACCOUNT_MODE_UNSUPPORTED: "Режим счёта не соответствует контракту выбранной стратегии.",
    MARGIN_MODE_MISSING: "Не сохранён режим маржи. Исполнение блокируется fail-closed.",
    MARGIN_MODE_UNSUPPORTED: "Режим маржи не соответствует контракту выбранной стратегии.",
    ACCOUNT_MODE_LEGACY_ALIAS: "В рекомендации сохранён устаревший алиас режима счёта. Нужна новая публикация с каноническим account mode.",
    MIN_LEVERAGE_PER_BOT_AT_EXECUTION: "Кредитное плечо ниже минимального значения, разрешённого текущим операторским профилем.",
    GRID_STEP_LEVELS_MISMATCH: "Число интервалов сетки не совпадает с числом сохранённых уровней. Такой grid-план нельзя запускать.",
    DIRECTIONAL_TREND_CONTRACT_MISSING: "План одной направленной позиции неполон или не помечен как directional_trend.",
    DIRECTIONAL_TREND_GEOMETRY_INVALID: "TP/SL-геометрия trend-позиции неполна или противоречит выбранному направлению.",
    DIRECTIONAL_TREND_DIRECTION_INVALID: "Для trend-позиции не определено направление LONG или SHORT.",
    DIRECTIONAL_TREND_LEVELS_MISSING: "Для trend-позиции отсутствуют корректные положительные цены входа, TP или SL.",
    SHADOW_EVIDENCE: "Прокси-исходы предназначены для исследования и не доказывают преимущество в реальной торговле.",
    DIRECTIONAL_TREND_POSITION_POLICY_INVALID: "Направленная позиция должна явно запрещать усреднение и наращивание объёма.",
    EXTERNAL_EXECUTION_PACKAGE_INVALID: "Внешний пакет исполнения не подтверждает рекомендательный режим и отсутствие отправленного биржевого ордера.",
    EXTERNAL_EXECUTION_PACKAGE_MISSING: "Для trend-рекомендации отсутствует обязательный пакет одной внешней сделки.",
    LEVERAGE_MISSING_FOR_EXECUTION: "Не указано положительное кредитное плечо для исполнения trend-позиции.",
    ORDER_QTY_MISSING: "Не указано положительное количество контракта для single-position сделки.",
    TARGET_NOTIONAL_MISSING: "Не указан положительный целевой номинал позиции.",
    BYBIT_META_UNAVAILABLE: "Метаданные инструмента Bybit недоступны; tick, lot, minNotional и тип контракта нельзя подтвердить.",
    DIRECTIONAL_TREND_LIVE_GEOMETRY_UNVERIFIABLE: "По свежей цене нельзя доказать, что entry, TP и SL остаются исполнимыми.",
    ORDER_QTY_BELOW_MIN: "Размер одной заявки меньше минимально допустимого на Bybit. Для автоматически рассчитанного плана система должна использовать минимальный исполнимый шаг и заново проверить полный риск сетки.",
    ORDER_QTY_OFF_STEP: "Размер заявки не кратен биржевому шагу количества. Ручной размер нужно уменьшить до допустимого шага; автоматически рассчитанный план нормализуется перед запуском.",
    MAX_POSITION_NOTIONAL_PER_BOT_AT_EXECUTION: "Минимально исполнимый размер этой сетки превышает текущий лимит позиции. Уменьшите число сеток/диапазон либо увеличьте лимит осознанно.",
    MAX_MARGIN_PER_BOT_AT_EXECUTION: "Минимально исполнимый размер этой сетки требует больше маржи, чем разрешено текущим профилем риска.",
    FUNDING_EXTREME_AT_EXECUTION: "Текущий funding ухудшает экономику сделки сверх разрешённого предела.",
    SYMBOL_STRATEGY_ALREADY_RUNNING: "По символу уже работает несовместимая стратегия; одновременный grid и trend запрещены.",
  };
  return labels[code] || humanizeOperatorText(item?.msg || item?.message || item?.reason || code);
}

function uniqueBlockerItems(items) {
  const source = Array.isArray(items) ? items : [];
  const seen = new Set();
  const genericCodes = new Set(["", "warn", "risk", "no_trade", "block", "bybit", "bybit_warn"]);
  const messageKey = (item) => String(item?.msg || "")
    .trim()
    .toLowerCase()
    .replace(/\s+/g, " ")
    .replace(/[.;,:]+$/g, "");
  // Persisted blocks and live validation carry concrete machine codes, while
  // risk_report often repeats the same prose under the generic RISK bucket.
  // Prefer the concrete invariant regardless of input order; otherwise the
  // operator sees one failure two or three times as in ORDER_QTY_* screenshots.
  const concreteMessages = new Set(source
    .filter((item) => !genericCodes.has(String(item?.code || "").trim().toLowerCase()))
    .map(messageKey)
    .filter(Boolean));
  return source.filter((item) => {
    const msgKey = messageKey(item);
    const codeKey = String(item?.code || "").trim().toLowerCase();
    const isGeneric = genericCodes.has(codeKey);
    if (isGeneric && msgKey && concreteMessages.has(msgKey)) return false;
    const key = !isGeneric
      ? `code:${codeKey}`
      : msgKey || `${codeKey}|${String(item?.critical ?? "")}`;
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function outcomeTrackingHtml(it) {
  const tracking = it?.outcome_tracking && typeof it.outcome_tracking === "object"
    ? it.outcome_tracking
    : {};
  const state = String(tracking.state || "unavailable").toLowerCase();
  const rawEventType = String(tracking.event_type || tracking.diagnostics?.event_type || "").trim();
  const eventType = canonicalOutcomeEventType(tracking.event_type, tracking.diagnostics || {});
  const hasObservedEventType = Boolean(rawEventType);
  const stateLabels = {
    waiting: "Ожидает созревания / обработки",
    labeled: "Исход сформирован",
    censored: "Наблюдение цензурировано",
    unavailable: "Состояние недоступно",
    not_applicable: "Не применяется к отклонённой оценке",
  };
  const resultHtml = state === "labeled"
    ? renderOutcomeResult(tracking.success, tracking.diagnostics, eventType, it?.bot_type)
    : `<span class="neutral-note">${escapeHtml(stateLabels[state] || healthStatusRu(state))}</span>`;
  const fields = [
    { label: "Состояние окна исхода", html: resultHtml },
    { label: "Тип события", value: hasObservedEventType ? outcomeEventTypeRu(eventType) : "—" },
    { label: "Расчётный результат", value: state === "labeled" ? renderOutcomeReturn(tracking.ret) : "—" },
    { label: "Интерпретация", value: (state === "labeled" || state === "censored")
        ? outcomeReasonText({
            bot_type: it?.bot_type,
            event_type: hasObservedEventType ? eventType : null,
            success: tracking.success,
            ret: tracking.ret,
            outcome_diagnostics: tracking.diagnostics || {},
          })
        : (state === "not_applicable"
          ? "Позиция не сформирована: outcome, TP/SL first-touch и обучающая метка для этой диагностической строки не создаются."
          : "Исход ещё не сформирован; текущая рекомендация не должна трактоваться как победа или поражение.") },
    { label: "Срок созревания", value: formatTs(tracking.label_due_ts) },
    { label: "Последняя попытка", value: formatTs(tracking.last_attempt_ts) },
    { label: "Причина состояния", value: humanizeOperatorText(tracking.reason || "—") },
    { label: "Корень независимого окна", value: tracking.outcome_root_rec_id || "—" },
  ];
  return `
    <div class="operator-card outcome-tracking-card">
      <h3>Состояние исхода и разметки</h3>
      <p class="helper-text">Для trend тип first-touch и денежная прибыльность отображаются раздельно. Для grid результат отражает экономику сетки и защитные границы.</p>
      <div class="operator-grid two decision-context-grid">
        ${fields.map(field => field.html !== undefined
          ? `<div class="field-box"><label>${escapeHtml(field.label)}</label><div class="field-static">${field.html}</div></div>`
          : fieldBox(field.label, field.value, null, "", "")
        ).join("")}
      </div>
    </div>
  `;
}

function buildDetailsHtml(it) {
  const reasons = it.reasons || {};
  const params = it.params || {};
  const llmReview = reasons.llm_review || null;
  const blocks = it.blocks || [];
  const bybitValidation = it.bybit_plan_validation || {};
  const riskReport = params.risk_report || {};
  const riskReportRejected = Array.isArray(riskReport.rejection_reasons) ? riskReport.rejection_reasons : [];
  const riskReportNoTradeReasons = Array.isArray(riskReport.no_trade_reasons) ? riskReport.no_trade_reasons : [];
  const riskReportWarnings = Array.isArray(riskReport.warnings) ? riskReport.warnings : [];
  const normalizedRiskRejected = riskReportRejected.map(msg => riskReportMessageItem(msg, "RISK", true));
  const normalizedNoTradeReasonItems = riskReportNoTradeReasons.map(msg => riskReportMessageItem(msg, "NO_TRADE", false));
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

  const launchable = isLaunchableRecommendation(it);
  const trendSingle = isDirectionalTrendSinglePosition(it);
  const rejectedTrendEvaluation = candidateKindOf(it) === TREND_EVALUATION_REJECTED_KIND;
  const scoreMeta = ensureUiScoreMeta(it);
  const status = operatorEffectiveStatus(it);
  const explicitHardBlocked = bybitErrors.length > 0 || blocks.length > 0 || riskReportRejected.length > 0 || status === "blocked";
  // risk_report.decision is intentionally conservative for pending async-LLM holds:
  // backend may store it as not_recommended until the reviewer finalizes the row.
  // Therefore only the persisted operator status may render the score/risk no_trade copy.
  const noTradeDecision = status === "no_trade";
  const pendingDecision = status === "pending";
  const decisionClass = launchable ? "go" : explicitHardBlocked ? "stop" : "wait";
  const hardBlockCodes = new Set([
    ...blocks.map(item => String(item?.code || "").trim().toUpperCase()),
    ...bybitErrors.map(item => String(item?.code || "").trim().toUpperCase()),
  ]);
  const trendCandidateStructurallyInvalid = trendSingle && [
    "DIRECTIONAL_TREND_GEOMETRY_INVALID",
    "DIRECTIONAL_TREND_DIRECTION_INVALID",
    "DIRECTIONAL_TREND_LEVELS_MISSING",
    "DIRECTIONAL_TREND_CONTRACT_MISSING",
  ].some(code => hardBlockCodes.has(code));
  const decisionTitle = rejectedTrendEvaluation
    ? "Проверка тренда отклонена"
    : trendSingle && launchable
      ? "Трендовая позиция выбрана meta-router"
      : launchable
      ? "Можно запускать после предпроверки"
      : explicitHardBlocked
        ? (trendCandidateStructurallyInvalid ? "Trend-кандидат отклонён" : "Не запускать")
        : noTradeDecision
          ? "Не запускать сейчас"
          : pendingDecision
            ? "Ждать проверку LLM"
            : "Ждать / перепроверить";
  const decisionText = rejectedTrendEvaluation
    ? "Предварительный анализ не подтвердил LONG или SHORT. Это не trend-позиция и не нейтральная сетка: entry, TP, SL, outcome и обучающая метка для этой строки отсутствуют."
    : trendSingle && launchable
    ? "Meta-router выбрал single-position trend по сопоставимой risk-adjusted monetary utility. Подтверждение создаёт только audit-instance; ордер размещается вручную или внешним execution-layer."
    : launchable
    ? "Проверьте цену, актуальность, риск и экономику; затем используйте блок параметров запуска для создания бота."
    : explicitHardBlocked
      ? (trendCandidateStructurallyInvalid
          ? "Это отдельный кандидат направленной стратегии: он отклонён до построения исполнимой позиции и не является нейтральной сеткой. Кандидат сеточной стратегии по этому символу, если он существует, отображается отдельной строкой."
          : trendSingle
            ? "Есть жёсткая причина, запрещающая создание single-position trend-плана. Фактическая причина показана сразу под этим решением."
            : "Есть жёсткая причина, запрещающая ручное создание сеточного бота. Фактическая причина показана сразу под этим решением.")
      : noTradeDecision
        ? (trendSingle
            ? "«Не торговать» означает: направленную позицию сейчас не открывать. Причина показана сразу под этим решением и отделена от относительного ранга в таблице."
            : "«Не торговать» означает: сетку сейчас не запускать. Это не техническая блокировка Bybit; причина показана сразу под этим решением и отделена от относительного ранга в таблице.")
        : pendingDecision
          ? "Рекомендация ожидает завершения проверки LLM. Это не отказ и не техническая блокировка Bybit; дождитесь статуса «Можно торговать» либо окончательного запрета."
          : "Рекомендация пока не готова к ручному запуску. Дождитесь новой публикации или новой предзапусковой проверки.";

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
    ? (normalizedNoTradeReasonItems.length
      ? normalizedNoTradeReasonItems
      : [{
          code: "NO_TRADE",
          msg: noTradeDecisionMessage(it, scoreMeta),
          critical: false,
        }])
    : [];
  const factorWarnings = riskReportWarnings.length
    ? []
    : (reasons.top_negative_factors || []).slice(0, 4).map(item => ({ code: String(item.code || item.feature || "WARN").trim().toUpperCase(), msg: item.msg || item.text || item.feature || "", critical: false }));
  const blockerItems = uniqueBlockerItems([
    ...blocks.map(b => ({ code: b.code || "BLOCK", msg: b.msg || "" , critical: true })),
    ...normalizedRiskRejected,
    ...bybitErrors.map(b => ({ code: b.code || "BYBIT", msg: b.msg || "", critical: true })),
    ...noTradeReasonItems,
    ...riskReportWarnings.slice(0, 4).map(msg => riskReportMessageItem(msg, "WARN", false)),
    ...factorWarnings,
    ...bybitWarnings.slice(0, 4).map(b => ({ code: b.code || "BYBIT_WARN", msg: b.msg || "", critical: false })),
  ]).slice(0, 8);
  const blockersTitle = explicitHardBlocked
    ? "Фактическая причина блокировки / предупреждения"
    : noTradeDecision
      ? "Почему запуск не рекомендован / предупреждения"
      : "Предупреждения";
  const blockersCardClass = explicitHardBlocked ? "launch-blockers-card" : "launch-warnings-card";
  const blockersHtml = blockerItems.length
    ? `
      <div class="operator-card ${blockersCardClass}">
        <h3>${escapeHtml(blockersTitle)}</h3>
        <div class="small-blocks">
          ${blockerItems.map(b => `<div class="small-block ${b.critical ? "small-block-critical" : ""}"><span class="diagnostic-code" title="Технический код для журнала">${escapeHtml(b.code)}</span><br>${escapeHtml(operatorBlockMessageRu(b))}</div>`).join("")}
        </div>
      </div>
    `
    : "";
  const nextActionsHtml = operatorNextActionsHtml(it);
  const outcomeTrackingCard = outcomeTrackingHtml(it);
  const historyButtonHtml = rejectedTrendEvaluation
    ? ""
    : `
          <button
            class="ghost-chip"
            data-act="show-recommendation-history"
            data-venue="${escapeHtml(it.venue || "linear")}"
            data-symbol="${escapeHtml(it.symbol || "")}"
            data-bot-type="${escapeHtml(it.bot_type || "futures_grid")}">
            История и динамика
          </button>`;

  return `
    <div class="operator-sheet compact-details-sheet operator-minimal-sheet">
      <div class="operator-card operator-decision-card ${decisionClass}">
        <div class="decision-title-row">
          <div>
            <h3>${escapeHtml(it.symbol)} · ${escapeHtml(decisionTitle)}</h3>
            <div class="operator-subtitle operator-subtitle-inline">${escapeHtml(strategyLabelForItem(it))}<span class="operator-sub-sep">·</span>${strategyDirectionBadge(it.bot_type, it.direction)}<span class="operator-sub-sep">·</span>${statusBadgeHtml(operatorEffectiveStatus(it))}</div>
          </div>
          <button class="ghost-chip" data-act="show-tech">Технические данные</button>
        </div>
        <div class="decision-text">${escapeHtml(decisionText)}</div>
      </div>

      ${blockersHtml}

      ${nextActionsHtml}

      ${launchDecisionDiagnosticsHtml(it, scoreMeta)}

      ${outcomeTrackingCard}

      <div class="operator-card price-freshness-card">
        <div class="operator-card-heading">
          <h3>Цена и актуальность</h3>
          ${historyButtonHtml}
        </div>
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
        <h3>${rejectedTrendEvaluation ? "Результат предварительной проверки тренда" : trendSingle ? "Параметры single-position trend-плана" : "Параметры запуска фьючерсной сетки Bybit"}</h3>
        <div class="operator-grid two minimal-launch-grid">
          ${operatorFields.map(field => fieldBox(field.label, field.value, field.copyValue ?? field.value, field.mono ? "field-input-mono" : "", field.help || "")).join("")}
        </div>
      </div>

      <div class="operator-card llm-operator-card">
        <h3>Проверка LLM</h3>
        <div class="operator-grid three minimal-llm-grid">
          ${fieldBox("Рекомендация LLM", llmRecommendation, null, "", "LLM — языковая модель, которая дополнительно проверяет идею. Это не самостоятельное разрешение запуска без серверных и предзапусковых риск-проверок.")}
          ${fieldBox("Уверенность LLM", llmProbability, null, "", "Внутренняя уверенность LLM в собственном выводе. Это не вероятность прибыли и не замена RR, проверкам риска и статистике наблюдений.")}
          ${fieldBox("Сравнение с алгоритмом", llmAgreement, null, "", "Показывает, совпадает ли вывод LLM с направлением и исполнением алгоритма.")}
        </div>
        ${llmSummary ? `<div class="llm-summary-box compact-llm-summary">${escapeHtml(humanizeOperatorText(llmSummary))}</div>` : `<div class="helper-text">LLM-проверка отсутствует для этой рекомендации.</div>`}
      </div>

    </div>
  `;
}

function pillStatus(status) {
  let cls = "pill";
  if (status === "recommended" || status === "active") cls += " good";
  else if (status === "blocked") cls += " bad";
  else cls += " warn";
  return `<span class="${cls}">${escapeHtml(operatorStatusRu(status))}</span>`;
}

function getConfModel(item) {
  return ((item || {}).reasons || {}).confidence_model || {};
}

function confCell(item) {
  const v = toFiniteNumber((item || {}).confidence);
  if (v === null) return "-";

  const confModel = getConfModel(item);
  const fitted = !!confModel.fitted;
  const logregActive = !!confModel.logreg_active;

  let cls = "conf-val";
  if (!fitted || !logregActive) cls += " conf-uncal";
  else if (v >= 0.75) cls += " conf-high";
  else if (v >= 0.60) cls += " conf-mid";
  else cls += " conf-low";

  let marker = "";
  if (!fitted) {
    const src = confModel.source || "raw";
    const cap = toFiniteNumber(confModel.heuristic_cap);
    const capText = cap === null ? "" : `; cap≤${cap.toFixed(2)}`;
    marker = ` <span class='conf-mode-tag conf-mode-raw' title='Уверенность не откалибрована; источник: ${calibrationModeRu(src)}${capText}'>без калибровки</span>`;
  } else if (logregActive) {
    marker = " <span class='conf-mode-tag conf-mode-cal' title='Уверенность откалибрована на отдельной проверочной выборке'>откалибровано</span>";
  } else {
    const nSamples = toFiniteNumber(confModel.n_samples) ?? 0;
    marker = ` <span class='conf-mode-tag conf-mode-platt' title='Устаревшая калибровка не соответствует текущим правилам проверки; наблюдений: ${nSamples}'>устаревшая</span>`;
  }

  return `<span class="${cls}">${v.toFixed(2)}${marker}</span>`;
}

function summariseCalibState(items) {
  const summary = { unfitted: 0, legacy: 0, logreg: 0, total: 0 };
  (items || []).forEach((it) => {
    const confModel = getConfModel(it);
    summary.total += 1;
    if (!confModel.fitted) summary.unfitted += 1;
    else if (confModel.logreg_active) summary.logreg += 1;
    else summary.legacy += 1;
  });
  return summary;
}

function buildBotCalibText(botType, info, totalOutcomeCount) {
  const product = botTypeLabel(botType) || "Фьючерсная сетка";
  const archiveTotal = Number(info?.historical_outcomes_total ?? totalOutcomeCount ?? 0);
  const currentModelTotal = Number(info?.current_model_outcomes_total || 0);
  const policyEligibleTotal = Number(info?.policy_eligible_outcomes_total ?? info?.outcomes_total ?? 0);
  const wins = Number(info?.wins || 0);
  const losses = Number(info?.losses || Math.max(0, policyEligibleTotal - wins));
  const effective = Number(info?.effective_samples || (2 * Math.min(wins, losses)) || 0);
  const monetaryNeeded = Number(info?.monetary_min_samples || info?.min_samples || 80);
  const probabilityNeeded = Number(info?.probability_min_samples || info?.logreg_min_samples || 300);
  const temporalClusters = Number(info?.temporal_cluster_count || 0);
  const minimumTemporalClusters = Number(info?.minimum_temporal_clusters || 0);
  const matured = Number(info?.policy_matured_total || 0);
  const labeled = Number(info?.policy_labeled_total || 0);
  const censored = Number(info?.policy_censored_total || 0);
  const unresolved = Number(info?.policy_unresolved_total || 0);
  const invalidLabeled = Number(info?.policy_invalid_labeled_total || 0);
  const terminalSelectedStatus = String(info?.terminal_selected_policy_expectancy_status || "not_evaluated");
  const terminalSelectedSamples = Number(info?.terminal_selected_policy_samples || 0);
  const terminalSelectedRequired = Number(info?.terminal_selected_policy_required_samples || monetaryNeeded);
  const terminalSelectedStatusRu = ({
    positive: "положительная",
    negative: "отрицательная",
    uncertain: "неопределённая",
    insufficient: "недостаточно данных",
    not_evaluated: "ещё не выполнена",
  })[terminalSelectedStatus] || terminalSelectedStatus;
  const readiness = `Для денежной оценки: ${policyEligibleTotal}/${monetaryNeeded}; для вероятностной калибровки: ${policyEligibleTotal}/${probabilityNeeded}.`;
  const lineage = `Архив: ${archiveTotal}; текущая версия модели: ${currentModelTotal}; завершено: ${matured}; размечено: ${labeled}.`;
  const clusters = minimumTemporalClusters > 0 ? ` Независимые временные группы: ${temporalClusters}/${minimumTemporalClusters}.` : "";
  const terminalMoney = ` Денежная проверка выбранной политики на итоговом периоде: ${terminalSelectedStatusRu} (${terminalSelectedSamples}/${terminalSelectedRequired} строк).`;

  if (censored > 0 || unresolved > 0 || invalidLabeled > 0) {
    return `${product}: калибровка заблокирована неполными наблюдениями (незавершённых: ${unresolved}, ограниченно наблюдаемых: ${censored}, некорректных: ${invalidLabeled}). ${readiness} ${lineage}${clusters}${terminalMoney}`;
  }
  if (info?.fitted && info?.logreg_active) {
    return `${product}: вероятностная калибровка активна. Наблюдений: ${policyEligibleTotal}; успешных: ${wins}; неуспешных: ${losses}; эффективная выборка: ${effective}. ${lineage}${clusters}${terminalMoney}`;
  }
  if (info?.fitted) {
    return `${product}: сохранённая старая калибровка не соответствует текущим правилам проверки и не используется. ${readiness} ${lineage}${clusters}${terminalMoney}`;
  }
  if ((info?.unfitted_reason || "") === "degenerate_win_rate") {
    return `${product}: в выборке пока недостаточно результатов одного из классов, поэтому вероятностная калибровка невозможна. Успешных: ${wins}; неуспешных: ${losses}; эффективная выборка: ${effective}. ${readiness} ${lineage}${clusters}${terminalMoney}`;
  }
  if ((info?.unfitted_reason || "") === "pending_refit") {
    return `${product}: данных уже достаточно, но пересчёт калибратора ещё не завершён. ${readiness} ${lineage}${clusters}${terminalMoney}`;
  }
  return `${product}: калибратор ещё не обучен. ${readiness} ${lineage}${clusters}${terminalMoney}`;
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
        ? `Калибровка готова частично (${fittedBots.length}/${botCalibs.length}); общая модель используется только для диагностики и не подставляется вместо модели конкретного вида стратегии.`
        : "Калибровка продукта ещё не готова.";
      banner.classList.remove("hidden");
      const archiveCount = Number(statusPayload?.historical_outcome_count ?? statusPayload?.outcome_count ?? 0);
      const currentModelCount = Number(statusPayload?.current_model_outcome_count || 0);
      const eligibleCount = Number(statusPayload?.calibration_eligible_outcome_count || 0);
      const monetaryNeeded = Number(statusPayload?.calib_min_samples || 80);
      const probabilityNeeded = Number(statusPayload?.calib_logreg_min_samples || 300);
      const fullNeeded = statusPayload?.calibration_gate_contract?.require_conf_gate === false ? monetaryNeeded : probabilityNeeded;
      const pct = fullNeeded > 0 ? Math.min(100, Math.round(eligibleCount / fullNeeded * 100)) : 0;
      const readiness = botCalibs.length > 0
        ? `Готово: ${fittedBots.length}/${botCalibs.length}${logregBots.length ? ` (вероятностная модель: ${logregBots.length})` : ""}. `
        : "";
      const temporalDays = Number(statusPayload?.calibration_gate_contract?.minimum_temporal_span_days || 10);
      $("calibProgress").textContent = `${readiness}Исторический архив: ${archiveCount}; наблюдения текущей версии модели: ${currentModelCount}; пригодные для обучения: ${eligibleCount}. Для денежной оценки требуется ${eligibleCount}/${monetaryNeeded}. Для вероятностной калибровки требуется ${eligibleCount}/${probabilityNeeded}, а также отдельная проверка вне обучения без пересечения временных окон. Итоговая отложенная выборка содержит не менее ${monetaryNeeded} строк и 5 целых временных когорт; выбранная порогом политика обязана иметь положительные денежные lower bounds и на всей OOF-истории, и отдельно на итоговой отложенной выборке. При обязательной проверке уверенности 80 наблюдений недостаточно. При 12-часовом горизонте требуется не менее ${temporalDays} суток работы неизменного набора правил. Смена идентификатора набора правил начинает новую выборку наблюдений; старые наблюдения сохраняются только в архиве.`;
      $("calibBarFill").style.width = `${pct}%`;
    }
    return;
  }

  if (summary.unfitted === 0 && summary.legacy === 0) {
    header.textContent = "Увер ✓";
    header.title = `Все строки откалиброваны: вероятностная калибровка (${summary.logreg}/${summary.total}).`;
    banner.classList.add("hidden");
    return;
  }
  if (summary.unfitted === 0) {
    header.textContent = summary.logreg > 0 ? "Увер ~" : "Увер ~";
    header.title = `Обнаружена устаревшая калибровка, не соответствующая текущим правилам проверки (текущая вероятностная модель: ${summary.logreg}; устаревших состояний: ${summary.legacy}).`;
    banner.classList.remove("hidden");
    return;
  }

  header.textContent = summary.logreg > 0 ? "Увер ?" : "Увер ⚠";
  header.title = `Есть строки с неподтверждённой уверенностью: без калибровки — ${summary.unfitted}; устаревших — ${summary.legacy}; с текущей вероятностной моделью — ${summary.logreg}.`;

  const botTypes = [...new Set((items || []).map(it => it.bot_type).filter(Boolean))];
  const botCalibs = statusPayload?.bot_calibrators || {};
  const totalOutcomeCount = Number(statusPayload?.outcome_count || 0);
  const messages = botTypes.slice(0, 3).map((botType) => buildBotCalibText(botType, botCalibs[botType], totalOutcomeCount));
  if (botTypes.length > 3) messages.push(`И ещё ${botTypes.length - 3} внутренних сегмента.`);

  const primaryBot = botTypes.length === 1 ? botTypes[0] : null;
  const primaryInfo = primaryBot ? botCalibs[primaryBot] : null;
  const effective = Number(primaryInfo?.effective_samples || 0);
  const monetaryNeeded = Number(primaryInfo?.monetary_min_samples || primaryInfo?.min_samples || statusPayload?.calib_min_samples || 80);
  const probabilityNeeded = Number(primaryInfo?.probability_min_samples || primaryInfo?.logreg_min_samples || statusPayload?.calib_logreg_min_samples || 300);
  const fullNeeded = statusPayload?.calibration_gate_contract?.require_conf_gate === false ? monetaryNeeded : probabilityNeeded;
  const eligibleForProbability = Number(primaryInfo?.policy_eligible_outcomes_total || 0);
  const pct = fullNeeded > 0 ? Math.min(100, Math.round(eligibleForProbability / fullNeeded * 100)) : 0;

  banner.classList.remove("hidden");
  if (primaryBot) {
    const strategyName = botTypeLabel(primaryBot);
    const title = primaryInfo?.fitted
      ? (primaryInfo?.logreg_active
        ? `${strategyName}: калибратор активен`
        : `${strategyName}: старая калибровка отклонена`)
      : `${strategyName}: калибратор не обучен`;
    document.querySelector(".calib-title").innerHTML = `${title} — уверенность <b>${primaryInfo?.fitted ? "частично/полностью откалибрована" : "не откалибрована"}</b>`;
  } else {
    document.querySelector(".calib-title").innerHTML = `Калибровка по текущему набору <b>смешанная</b>`;
  }
  const temporalDays = Number(statusPayload?.calibration_gate_contract?.minimum_temporal_span_days || 10);
  $("calibProgress").textContent = `${messages.join(" ")} Для полной готовности недостаточно только 80 наблюдений: требуется не менее ${probabilityNeeded} наблюдений текущего набора правил, проверка на данных вне обучения, итоговая отложенная выборка не менее ${monetaryNeeded} строк из 5 целых временных когорт и положительное денежное ожидание именно у выбранной порогом уверенности подвыборки. При 12-часовом горизонте наблюдения необходимы как минимум ${temporalDays} суток неизменных правил. После изменения правил статистика собирается заново. Исходная уверенность алгоритма остаётся только технической диагностикой и не разрешает торговлю.`;
  $("calibBarFill").style.width = `${pct}%`;
}

function dirConfCell(dirConf) {
  const v = toFiniteNumber(dirConf);
  if (v === null) return "-";
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

function renderDirectionBadge(dir, botType = "") {
  if (botType) return strategyDirectionBadge(botType, dir);
  const value = String(dir || "neutral").toLowerCase();
  const cls = value === "long" ? "dir-long" : value === "short" ? "dir-short" : "dir-neu";
  return `<span class="dir-badge ${cls}">${escapeHtml(directionRu(value))}</span>`;
}

function outcomeEventTypeRu(value) {
  const eventType = String(value || "").trim().toUpperCase();
  const labels = {
    TP_FIRST: "TP раньше SL",
    SL_FIRST: "SL раньше TP",
    HORIZON_EXIT: "Выход по времени",
    AMBIGUOUS: "Порядок TP/SL не наблюдаем",
    GRID_OUTCOME: "Итог сеточной стратегии",
    LEGACY_BINARY: "Legacy binary outcome",
  };
  return labels[eventType] || (eventType || "Событие не указано");
}

function canonicalOutcomeEventType(eventType, diagnostics) {
  const explicit = String(eventType || "").trim().toUpperCase();
  if (explicit) return explicit;
  const details = diagnostics && typeof diagnostics === "object" ? diagnostics : {};
  return String(details.event_type || "").trim().toUpperCase() || "LEGACY_BINARY";
}

function renderOutcomeResult(success, diagnostics, eventType = null, botType = null) {
  const value = toStrictInteger(success);
  const details = diagnostics && typeof diagnostics === "object" ? diagnostics : {};
  const canonicalEvent = String(eventType || details.event_type || "LEGACY_BINARY").trim().toUpperCase();
  const eventLabel = ({TP_FIRST:"TP раньше SL",SL_FIRST:"SL раньше TP",HORIZON_EXIT:"Выход по времени",AMBIGUOUS:"Порядок TP/SL не наблюдаем",GRID_OUTCOME:"Итог сеточной стратегии",LEGACY_BINARY:"Legacy binary outcome"})[canonicalEvent] || canonicalEvent;
  if (canonicalEvent === "AMBIGUOUS") {
    return '<span class="outcome-result outcome-result-unknown">Цензурировано · TP/SL в одной свече</span>';
  }
  if (value !== 0 && value !== 1) {
    return `<span class="outcome-result outcome-result-unknown">Неизвестно · ${escapeHtml(eventLabel)}</span>`;
  }
  const stopped = details.stopped === true || details.terminal_reason === "kill_switch_breached";
  const ok = value === 1;
  let label;
  if (String(botType || details.bot_type || "") === "directional_trend" || ["TP_FIRST", "SL_FIRST", "HORIZON_EXIT"].includes(canonicalEvent)) {
    label = `${eventLabel} · ${ok ? "прибыльно" : "неприбыльно"}`;
  } else {
    label = ok ? "Успех сетки" : (stopped ? "Неуспех сетки · kill-switch" : "Неуспех сетки");
  }
  return `<span class="outcome-result ${ok ? "outcome-result-win" : "outcome-result-loss"}">${escapeHtml(label)}</span>`;
}

function renderOutcomeReturn(value) {
  const ret = toFiniteNumber(value);
  return ret === null ? "—" : fmtPct(ret * 100, 2);
}

function outcomeReasonText(row) {
  const item = row && typeof row === "object" ? row : {};
  const details = item.outcome_diagnostics && typeof item.outcome_diagnostics === "object"
    ? item.outcome_diagnostics
    : {};
  const success = toStrictInteger(item.success);
  const ret = toFiniteNumber(item.ret);
  const eventType = String(item.event_type || details.event_type || "LEGACY_BINARY").trim().toUpperCase();
  const botType = String(item.bot_type || details.bot_type || "");
  const stopped = details.stopped === true || details.terminal_reason === "kill_switch_breached";

  if (botType === "directional_trend" || ["TP_FIRST", "SL_FIRST", "HORIZON_EXIT", "AMBIGUOUS"].includes(eventType)) {
    if (eventType === "TP_FIRST") {
      return `Take-profit был однозначно достигнут раньше stop-loss. Экономический результат после издержек: ${renderOutcomeReturn(ret)}; TP_FIRST и прибыльность показываются раздельно.`;
    }
    if (eventType === "SL_FIRST") {
      return `Stop-loss был однозначно достигнут раньше take-profit. Экономический результат после издержек: ${renderOutcomeReturn(ret)}.`;
    }
    if (eventType === "HORIZON_EXIT") {
      return `За горизонт ни TP, ни SL не были однозначно достигнуты; позиция оценена по выходу во времени. Результат ${renderOutcomeReturn(ret)} не считается достижением TP.`;
    }
    if (eventType === "AMBIGUOUS") {
      return "TP и SL попали в одну минутную свечу, поэтому порядок first-touch не наблюдаем. Наблюдение должно быть цензурировано и не использоваться как успех или поражение.";
    }
    return `Trend-outcome имеет legacy-семантику; net результат ${renderOutcomeReturn(ret)}. Для обучения first-touch такая строка не должна считаться современной точной меткой.`;
  }

  if (stopped) {
    const side = details.kill_switch_breach_side === "upper"
      ? "верхний"
      : details.kill_switch_breach_side === "lower"
        ? "нижний"
        : "защитный";
    const boundary = toFiniteNumber(details.kill_switch_boundary_price);
    const observed = toFiniteNumber(details.kill_switch_observed_extreme);
    const boundaryText = boundary === null ? "" : ` на границе ${String(boundary)}`;
    const observedText = observed === null ? "" : `; наблюдавшийся экстремум ${String(observed)}`;
    const pnlText = ret === null ? "" : `; итоговый net proxy P&L ${renderOutcomeReturn(ret)}`;
    return `Сработал ${side} kill-switch${boundaryText}${observedText}${pnlText}. Срабатывание защиты означает неуспешный grid-outcome независимо от знака proxy P&L.`;
  }
  if (success === 1) {
    return ret === null
      ? "Защитные границы сетки не нарушены; числовой proxy P&L недоступен."
      : `Защитные границы сетки не нарушены; net proxy P&L ${renderOutcomeReturn(ret)}.`;
  }
  if (success === 0) {
    if (ret !== null && ret > 0) {
      return "Положительный net proxy P&L, но терминальная причина отсутствует в legacy-архиве; сохранённый grid-outcome остаётся неуспешным.";
    }
    return ret === null
      ? "Неуспешный grid-outcome; числовой proxy P&L или терминальная диагностика недоступны."
      : `Net proxy P&L ${renderOutcomeReturn(ret)} не прошёл критерий успешного grid-outcome.`;
  }
  return "Исход или терминальная диагностика имеют недопустимый формат.";
}

function renderNeutralSourceTag(source) {
  if (!source) return "—";
  const cls = source === "futures_neutral"
    ? "neutral-note neutral-note-neutralized"
    : source === "true_neutral"
      ? "neutral-note neutral-note-true"
      : "neutral-note";
  return `<span class="${cls}">${escapeHtml(neutralSourceRu(source))}</span>`;
}

function renderLlmStatusBadge(status) {
  const value = String(status || "none").toLowerCase();
  let cls = "llm-badge llm-badge-neutral";
  if (value === "ok") cls = "llm-badge llm-badge-ok";
  else if (value === "pending") cls = "llm-badge llm-badge-pending";
  else if (value === "error") cls = "llm-badge llm-badge-error";
  else if (value === "skipped") cls = "llm-badge llm-badge-skipped";
  else if (value === "none") cls = "llm-badge llm-badge-none";
  return `<span class="${cls}">${escapeHtml(llmStatusRu(value))}</span>`;
}

function renderAgreementBadge(agree) {
  if (agree === true) return '<span class="llm-badge llm-badge-agree">совпадает</span>';
  if (agree === false) return '<span class="llm-badge llm-badge-disagree">расходится</span>';
  return '<span class="llm-badge llm-badge-neutral">н/д</span>';
}

function renderLlmFlagList(flags) {
  const items = Array.isArray(flags) ? flags.filter(Boolean) : [];
  if (!items.length) return '<div class="helper-text">Признаки риска не указаны.</div>';
  return `<div class="tag-list">${items.map(flag => `<span class="tag-chip">${escapeHtml(humanizeOperatorText(flag))}</span>`).join("")}</div>`;
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
        <h3>Проверка LLM</h3>
        <div class="helper-text">По этой рекомендации проверка LLM не запускалась или её данные недоступны.</div>
      </div>
    `;
  }

  const status = llm.status || "unknown";
  const confidence = llm.confidence === null || llm.confidence === undefined ? "—" : formatDotNumber(llm.confidence, 2);
  const mode = llm.mode || "—";
  const gateDecision = llm.gate_decision || "—";
  const regimeView = llm.regime_view || "—";
  const summary = humanizeOperatorText(llm.summary || llm.error || "—");
  const source = llm.source || (llm.cached ? "cache" : "live");
  const freshness = llm.cache_age_sec === null || llm.cache_age_sec === undefined ? "—" : formatAgeHuman(llm.cache_age_sec);
  const reviewTs = llm.review_ts ? formatTs(llm.review_ts) : "—";
  const inheritedFrom = llm.inherited_from_rec_id || "—";
  const errorLine = llm.error ? `<div class="helper-text llm-error-text">Ошибка проверки LLM: ${escapeHtml(humanizeOperatorText(llm.error))}</div>` : "";

  return `
    <div class="operator-card llm-review-card">
      <h3>Проверка LLM</h3>
      <div class="operator-grid">
        ${fieldBox("Статус", llmStatusRu(status), null)}
        ${fieldBox("Сервис / модель", formatReviewerModel(llm), null)}
        ${fieldBox("Источник", humanizeOperatorText(source), null, "", "Новый ответ LLM или ранее сохранённый ответ, повторно использованный для той же неизменной рекомендации.")}
        ${fieldBox("Режим", humanizeOperatorText(mode), null)}
        ${fieldBox("Результат проверки допуска", gateDecisionRu(gateDecision), null, "", "Показывает, разрешила ли проверка LLM продолжить рассмотрение идеи. LLM не может отменить жёсткую риск-блокировку.")}
        ${fieldBox("Время проверки", reviewTs, null)}
        ${fieldBox("Возраст проверки", freshness, null)}
        ${fieldBox("Наследовано от рекомендации", inheritedFrom, null)}
        ${fieldBox("Направление алгоритма", directionRu(engineDirection || "neutral"), null)}
        ${fieldBox("Вывод LLM", directionRu(llm.thesis_direction || "neutral"), null)}
        ${fieldBox("Решение LLM", directionRu(llm.execution_direction || "neutral"), null)}
        ${fieldBox("Совпадение с алгоритмом", llm.agree_with_engine === true ? "Да" : llm.agree_with_engine === false ? "Нет" : "Н/Д", null, "", "Сравнение направления после обязательных проверок с направлением, предложенным LLM.")}
        ${fieldBox("Уверенность LLM", confidence, null, "", "Внутренняя оценка уверенности LLM. Она не является вероятностью прибыли и не заменяет RR, риск и доходность по наблюдениям.")}
        ${fieldBox("Оценка состояния рынка LLM", marketStateRu(regimeView), null, "", "Краткое описание того, как LLM оценивает текущее состояние рынка. Это вспомогательное мнение, а не разрешение на вход.")}
      </div>
      <div class="llm-review-row">
        <div class="llm-review-badges">
          ${renderLlmStatusBadge(status)}
          ${renderAgreementBadge(llm.agree_with_engine)}
          ${renderDirectionBadge(llm.execution_direction || "neutral")}
        </div>
        <div class="helper-text">Если проверка LLM включена, запуск ожидает её завершения; при превышении времени ожидания торговля запрещается.</div>
      </div>
      <div class="llm-summary-box">${escapeHtml(summary)}</div>
      ${errorLine}
      <div class="modal-section-title" style="margin-top:10px">Признаки риска</div>
      ${renderLlmFlagList(llm.risk_flags)}
    </div>
  `;
}

function renderHealthStatus(status) {
  const value = String(status || "missing").toLowerCase();
  const cls = value === "ok" || value === "ready"
    ? "health-status-ok"
    : ["stale", "disabled", "pending", "backlog", "processing", "starting", "warming_up", "healthy_not_actionable"].includes(value)
      ? "health-status-stale"
      : "health-status-missing";
  return `<span class="health-status ${cls}">${escapeHtml(healthStatusRu(value))}</span>`;
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

function renderModalDisclosure(title, html, { open = false, note = "" } = {}) {
  return `
    <details class="modal-disclosure"${open ? " open" : ""}>
      <summary>${escapeHtml(title || "Дополнительные данные")}</summary>
      ${note ? `<p class="modal-note modal-disclosure-note">${escapeHtml(note)}</p>` : ""}
      <div class="modal-disclosure-body">${html || ""}</div>
    </details>
  `;
}

function renderSampleSizeBadge(sample) {
  const source = sample && typeof sample === "object"
    ? (sample.sample_observability || sample)
    : { rows: Number(sample || 0) };
  const rows = Math.max(0, Number(source.rows ?? sample?.total ?? sample ?? 0) || 0);
  const timestamps = Math.max(0, Number(source.unique_timestamps || 0) || 0);
  const clusters = Math.max(0, Number(source.temporal_clusters || 0) || 0);
  const nonOverlapping = Math.max(0, Number(source.max_non_overlapping_windows || 0) || 0);
  const symbols = Math.max(0, Number(source.unique_symbols || 0) || 0);
  const title = `Строки БД: ${rows}; уникальные времена старта: ${timestamps}; связанные временные группы: ${clusters}; максимум неперекрывающихся окон: ${nonOverlapping}; символы: ${symbols}. Строки по коррелированным инструментам и перекрывающимся горизонтам не считаются независимыми испытаниями.`;
  return `<span class="sample-structure-badge" title="${escapeHtml(title)}">строк: ${escapeHtml(String(rows))} · временных групп: ${escapeHtml(String(clusters))} · неперекрывающихся окон: ${escapeHtml(String(nonOverlapping))}</span>`;
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
      title: `LLM и алгоритм: ${directionRu(engineDir)}`,
      body: `${better} выглядят сильнее: ${(agree.winRate * 100).toFixed(1)}% против ${(disagree.winRate * 100).toFixed(1)}% успешных при числе наблюдений ${agree.total}/${disagree.total}. Стоит проверить, не смешаны ли разные подтипы внутри ${directionRu(engineDir).toLowerCase()}.`,
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
        title: "Нейтральные сигналы нужно разделять по происхождению",
        body: `Изначально нейтральный сигнал и нейтральное решение после проверок ведут себя по-разному: ${(tn.winRate * 100).toFixed(1)}% против ${(sn.winRate * 100).toFixed(1)}% успешных при числе наблюдений ${tn.total}/${sn.total}. Их нельзя держать в одной строке.`,
      });
    }
  }

  const duplicates = Number(summary?.deduped_duplicates || 0);
  const rawTotal = Number(summary?.raw_total || 0);
  if (duplicates > 0 && rawTotal > 0) {
    insights.push({
      kind: "neutral",
      title: "Повторы публикаций отфильтрованы",
      body: `${duplicates} из ${rawTotal} исходных строк исключены из доли успешных как подтверждения уже открытой цепочки публикаций. Для оператора это правильно: иначе окно завышало бы уверенность.`,
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
  if (columns.length === 2) tableClasses.push("modal-table-two-column");
  if (columns.length >= 10) tableClasses.push("modal-table-many-columns");
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

function journalActionTone(action) {
  const normalized = String(action || "").trim().toUpperCase();
  if (/(ERROR|FAILED|FAILURE|BLOCK|DENIED|REJECT|CORRUPT)/.test(normalized)) return "bad";
  if (/(SKIP|STALE|PENDING|EXPIRED|NO_TRADE|WAIT|DISABLED)/.test(normalized)) return "warn";
  if (/(EXECUTED|APPROVED|STARTED|CREATED|RECOVERED|PUBLISHED|PUBLISH|STOPPED)/.test(normalized)) return "good";
  return "neutral";
}

function journalActionLabel(action) {
  const code = String(action || "").trim().toUpperCase();
  const labels = {
    PUBLISH: "Публикация",
    SENTIMENT_COLLECT: "Сбор сентимента",
    COLLECT_ERROR: "Ошибка сбора данных",
    OUTCOME_LABELLED: "Исход рассчитан",
    OUTCOME_LABELED: "Исход рассчитан",
    OUTCOME_SKIP_INVALID_GRID_CONTRACT: "Исход пропущен: контракт сетки",
    EXECUTION_PREFLIGHT_BLOCKED: "Preflight заблокирован",
    SYMBOL_DISABLED: "Символ отключён",
    DB_PRUNE: "Очистка технических данных",
    STALE_DATA_SKIP: "Пропуск устаревших данных",
    RECO_WARMUP_SKIP: "Рекомендации ожидают готовности данных",
    RECO_WARMUP_RECOVERED: "Рыночные данные готовы",
  };
  return labels[code] || humanizeOperatorText(code || "—");
}

function journalStatusLabel(status) {
  const code = String(status || "").trim().toLowerCase();
  const labels = {
    recommended: "Можно торговать",
    active: "Можно торговать",
    pending: "Ожидает проверки",
    blocked: "Заблокировано",
    no_trade: "Не торговать",
    suppressed: "Скрыто системой",
    expired: "Устарело",
    executed: "Запущено",
    ignored: "Отклонено оператором",
  };
  return labels[code] || humanizeOperatorText(code || "—");
}

function journalFieldLabel(path) {
  const labels = {
    error: "Ошибка",
    message: "Сообщение",
    reason: "Причина",
    code: "Код",
    count: "Количество",
    status: "Статус",
    symbol: "Символ",
    venue: "Контур",
    bot_type: "Стратегия",
    direction: "Направление",
    model_version: "Версия модели",
    policy_fingerprint: "Идентификатор правил",
    retry_at: "Повтор после",
    retry_after_sec: "Повтор через, сек.",
    duration_ms: "Длительность, мс",
  };
  const segments = String(path || "").split(".").filter(Boolean);
  return segments.map(segment => labels[segment] || humanizeOperatorText(segment)).join(" › ") || "Деталь";
}

function journalPrimitiveText(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "да" : "нет";
  if (typeof value === "number") return Number.isFinite(value) ? String(value) : "некорректное число";
  return humanizeOperatorText(String(value));
}

function flattenJournalDetails(value, prefix = "", output = [], depth = 0) {
  if (output.length >= 160) return output;
  if (value === null || value === undefined || typeof value !== "object") {
    output.push({ label: journalFieldLabel(prefix), value: journalPrimitiveText(value) });
    return output;
  }
  if (Array.isArray(value)) {
    if (!value.length) {
      output.push({ label: journalFieldLabel(prefix), value: "пустой список" });
      return output;
    }
    value.forEach((item, index) => flattenJournalDetails(item, `${prefix}${prefix ? "." : ""}${index + 1}`, output, depth + 1));
    return output;
  }
  const entries = Object.entries(value);
  if (!entries.length) {
    output.push({ label: journalFieldLabel(prefix), value: "детали отсутствуют" });
    return output;
  }
  for (const [key, item] of entries) {
    const nextPrefix = `${prefix}${prefix ? "." : ""}${key}`;
    if (item && typeof item === "object" && depth < 5) {
      flattenJournalDetails(item, nextPrefix, output, depth + 1);
    } else {
      output.push({ label: journalFieldLabel(nextPrefix), value: journalPrimitiveText(item) });
    }
    if (output.length >= 160) break;
  }
  return output;
}

function journalPreview(entries) {
  if (!entries.length) return "Дополнительные сведения не сохранены.";
  const priority = entries.filter(item => /(Ошибка|Причина|Сообщение|Код|Статус)/i.test(item.label));
  const selected = (priority.length ? priority : entries).slice(0, 2);
  return selected.map(item => `${item.label}: ${item.value}`).join(" · ");
}

function shortAuditId(value) {
  const text = String(value || "").trim();
  if (!text) return "—";
  return text.length <= 34 ? text : `${text.slice(0, 18)}…${text.slice(-10)}`;
}

function renderDecisionJournal(rows) {
  const items = Array.isArray(rows) ? rows : [];
  if (!items.length) return `<div class="decision-journal-empty">Журнал решений пуст.</div>`;
  return `<div class="decision-journal-list">${items.map(row => {
    const details = flattenJournalDetails(row?.details || {});
    const tone = journalActionTone(row?.action);
    const linked = Boolean(row?.rec_id);
    const strategy = row?.bot_type ? botTypeLabel(row.bot_type) : "Общесистемное событие";
    const context = [row?.symbol || "Все символы", strategy].filter(Boolean).join(" · ");
    const detailRows = details.map(item => `
      <div class="decision-journal-detail-row">
        <div class="decision-journal-detail-label">${escapeHtml(item.label)}</div>
        <div class="decision-journal-detail-value">${escapeHtml(item.value)}</div>
      </div>`).join("");
    return `
      <article class="decision-journal-card decision-journal-${tone}">
        <div class="decision-journal-head">
          <div class="decision-journal-time">${escapeHtml(formatTs(row?.ts))}</div>
          <div><span class="decision-journal-action decision-journal-action-${tone}">${escapeHtml(journalActionLabel(row?.action))}</span></div>
          <div class="decision-journal-context">
            <strong>${escapeHtml(context)}</strong>
            <span>${row?.direction ? renderDirectionBadge(row.direction, row.bot_type) : "Направление не задано"}</span>
          </div>
          <div class="decision-journal-status">${row?.recommendation_status ? pillStatus(journalStatusLabel(row.recommendation_status)) : (linked ? "Статус не сохранён" : "Системное")}</div>
        </div>
        <div class="decision-journal-preview">${escapeHtml(journalPreview(details))}</div>
        <div class="decision-journal-meta">
          <span>Оператор: <b>${escapeHtml(row?.operator || "система")}</b></span>
          <span>Rec ID: <code title="${escapeHtml(row?.rec_id || "")}">${escapeHtml(shortAuditId(row?.rec_id))}</code></span>
          ${row?.model_version ? `<span>Модель: <code title="${escapeHtml(row.model_version)}">${escapeHtml(shortAuditId(row.model_version))}</code></span>` : ""}
        </div>
        <details class="decision-journal-details">
          <summary>Все детали (${details.length})</summary>
          <div class="decision-journal-detail-grid">${detailRows}</div>
        </details>
      </article>`;
  }).join("")}</div>`;
}

function timelineDirectionValue(direction) {
  if (direction === "long") return 1;
  if (direction === "short") return -1;
  return 0;
}

function timelineDirectionClass(direction) {
  if (direction === "long") return "timeline-long";
  if (direction === "short") return "timeline-short";
  return "timeline-neutral";
}

function timelinePointClass(item) {
  const status = String(item?.stored_status || item?.status || "unknown").toLowerCase();
  if (status === "recommended" || status === "active") return "timeline-point-actionable";
  if (status === "blocked" || status === "expired") return "timeline-point-blocked";
  if (status === "pending") return "timeline-point-pending";
  return "timeline-point-muted";
}

function buildRecommendationTimelineSvg(items) {
  const rows = Array.isArray(items)
    ? items.filter(row => row?.timestamp_valid !== false && toFiniteNumber(row?.ts) !== null)
    : [];
  if (!rows.length) return `<div class="timeline-empty">История рекомендаций для пары отсутствует.</div>`;

  const width = 920;
  const height = 300;
  const left = 78;
  const right = 24;
  const top = 24;
  const bottom = 58;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;
  const timestamps = rows.map(row => Number(row.ts));
  const minTs = Math.min(...timestamps);
  const maxTs = Math.max(...timestamps);
  const span = Math.max(1, maxTs - minTs);
  const xFor = ts => left + ((Number(ts) - minTs) / span) * plotWidth;
  const yFor = direction => {
    const value = timelineDirectionValue(direction);
    return top + ((1 - value) / 2) * plotHeight;
  };

  const horizontalLines = [
    { direction: "long", label: "ПОКУПКА" },
    { direction: "neutral", label: "НЕЙТРАЛЬНО" },
    { direction: "short", label: "ПРОДАЖА" },
  ].map(row => {
    const y = yFor(row.direction);
    return `
      <line class="timeline-grid-line" x1="${left}" y1="${y}" x2="${width - right}" y2="${y}"></line>
      <text class="timeline-axis-label" x="${left - 12}" y="${y + 4}" text-anchor="end">${row.label}</text>
    `;
  }).join("");

  const tickCount = Math.min(5, Math.max(2, rows.length));
  const ticks = Array.from({ length: tickCount }, (_unused, index) => {
    const ratio = tickCount === 1 ? 0 : index / (tickCount - 1);
    const ts = Math.round(minTs + span * ratio);
    const x = left + plotWidth * ratio;
    const label = new Date(ts * 1000).toLocaleString("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
    return `
      <line class="timeline-grid-line timeline-grid-line-vertical" x1="${x}" y1="${top}" x2="${x}" y2="${height - bottom}"></line>
      <text class="timeline-time-label" x="${x}" y="${height - 25}" text-anchor="middle">${escapeHtml(label)}</text>
    `;
  }).join("");

  let path = "";
  rows.forEach((row, index) => {
    const x = xFor(row.ts);
    const y = yFor(row.direction);
    if (index === 0) {
      path = `M ${x.toFixed(2)} ${y.toFixed(2)}`;
      return;
    }
    const prevY = yFor(rows[index - 1].direction);
    path += ` H ${x.toFixed(2)} V ${y.toFixed(2)}`;
    if (Math.abs(prevY - y) < 0.01) path += ` L ${x.toFixed(2)} ${y.toFixed(2)}`;
  });

  const points = rows.map((row, index) => {
    const x = xFor(row.ts);
    const y = yFor(row.direction);
    const radius = row.publication_kind === "root" ? 6 : 4.5;
    const title = [
      formatTs(row.ts),
      directionRu(row.direction),
      `статус: ${operatorStatusRu(row.stored_status || row.status || "unknown")}`,
      `LLM: ${llmStatusRu(row.llm_status || "none")}`,
      row.publication_kind === "root" ? "новая цепочка" : "обновление цепочки",
    ].join(" · ");
    return `
      <g class="timeline-point ${timelinePointClass(row)} ${timelineDirectionClass(row.direction)}">
        <circle cx="${x.toFixed(2)}" cy="${y.toFixed(2)}" r="${radius}"></circle>
        <title>${escapeHtml(title)}</title>
        ${row.direction_changed ? `<line class="timeline-change-marker" x1="${x.toFixed(2)}" y1="${top}" x2="${x.toFixed(2)}" y2="${height - bottom}"></line>` : ""}
        ${index === rows.length - 1 ? `<text class="timeline-latest-label" x="${Math.max(left + 30, x - 6).toFixed(2)}" y="${Math.max(top + 12, y - 14).toFixed(2)}" text-anchor="end">последняя</text>` : ""}
      </g>
    `;
  }).join("");

  return `
    <div class="recommendation-timeline" role="img" aria-label="Динамика направления рекомендаций по времени">
      <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="xMidYMid meet">
        ${horizontalLines}
        ${ticks}
        <path class="timeline-direction-path" d="${path}"></path>
        ${points}
      </svg>
    </div>
    <div class="timeline-legend">
      <span><i class="timeline-legend-dot timeline-point-actionable"></i> можно торговать</span>
      <span><i class="timeline-legend-dot timeline-point-pending"></i> ожидает проверки</span>
      <span><i class="timeline-legend-dot timeline-point-blocked"></i> заблокировано или устарело</span>
      <span>крупная точка — новая цепочка публикаций; вертикальная отметка — смена направления</span>
    </div>
  `;
}

function buildStrategyPriceTimelineSvg(items) {
  const rows = Array.isArray(items)
    ? items.filter(row => row?.timestamp_valid !== false && toFiniteNumber(row?.ts) !== null && row?.price_geometry)
    : [];
  if (!rows.length) return `<div class="timeline-empty">Ценовая геометрия истории отсутствует.</div>`;
  const botType = String(rows[rows.length - 1]?.bot_type || rows[0]?.bot_type || "futures_grid");
  const keys = botType === "directional_trend"
    ? [
        { key: "take_profit", label: "TP", cls: "strategy-line-tp" },
        { key: "reference_price", label: "Вход", cls: "strategy-line-entry" },
        { key: "stop_loss", label: "SL", cls: "strategy-line-sl" },
      ]
    : [
        { key: "kill_upper", label: "Kill ↑", cls: "strategy-line-kill" },
        { key: "range_upper", label: "Диапазон ↑", cls: "strategy-line-range" },
        { key: "reference_price", label: "Опорная цена", cls: "strategy-line-entry" },
        { key: "range_lower", label: "Диапазон ↓", cls: "strategy-line-range" },
        { key: "kill_lower", label: "Kill ↓", cls: "strategy-line-kill" },
      ];
  const points = [];
  rows.forEach(row => keys.forEach(series => {
    const value = toFiniteNumber(row?.price_geometry?.[series.key]);
    if (value !== null) points.push(value);
  }));
  if (!points.length) return `<div class="timeline-empty">В публикациях отсутствуют числовые уровни цены.</div>`;

  const width = 920, height = 330, left = 88, right = 32, top = 24, bottom = 64;
  const plotWidth = width - left - right, plotHeight = height - top - bottom;
  const timestamps = rows.map(row => Number(row.ts));
  const minTs = Math.min(...timestamps), maxTs = Math.max(...timestamps), span = Math.max(1, maxTs - minTs);
  let minPrice = Math.min(...points), maxPrice = Math.max(...points);
  const pad = Math.max((maxPrice - minPrice) * 0.08, Math.abs(maxPrice || 1) * 0.002, 1e-9);
  minPrice -= pad; maxPrice += pad;
  const priceSpan = Math.max(1e-12, maxPrice - minPrice);
  const xFor = ts => left + ((Number(ts) - minTs) / span) * plotWidth;
  const yFor = value => top + ((maxPrice - Number(value)) / priceSpan) * plotHeight;

  const horizontalTicks = Array.from({ length: 5 }, (_unused, index) => {
    const ratio = index / 4;
    const value = maxPrice - priceSpan * ratio;
    const y = top + plotHeight * ratio;
    return `<line class="timeline-grid-line" x1="${left}" y1="${y}" x2="${width-right}" y2="${y}"></line>
      <text class="timeline-axis-label" x="${left-10}" y="${y+4}" text-anchor="end">${escapeHtml(formatDotNumber(value, 6, false))}</text>`;
  }).join("");

  const paths = keys.map(series => {
    const sampled = rows.map(row => ({ ts: Number(row.ts), value: toFiniteNumber(row?.price_geometry?.[series.key]) }));
    const valid = sampled.filter(point => point.value !== null);
    if (!valid.length) return "";
    // A missing persisted level is an evidentiary gap.  Break the SVG path
    // instead of drawing a synthetic line across publications that do not carry
    // comparable geometry.
    let d = "";
    let segmentOpen = false;
    sampled.forEach(point => {
      if (point.value === null) {
        segmentOpen = false;
        return;
      }
      d += `${segmentOpen ? " L" : " M"} ${xFor(point.ts).toFixed(2)} ${yFor(point.value).toFixed(2)}`;
      segmentOpen = true;
    });
    const circles = valid.map(point => `<circle class="${series.cls}" cx="${xFor(point.ts).toFixed(2)}" cy="${yFor(point.value).toFixed(2)}" r="3"><title>${escapeHtml(`${series.label}: ${formatDotNumber(point.value, 8, false)} · ${formatTs(point.ts)}`)}</title></circle>`).join("");
    return `<path class="strategy-price-path ${series.cls}" d="${d.trim()}"></path>${circles}`;
  }).join("");

  const timeTickCount = Math.min(5, Math.max(2, rows.length));
  const timeTicks = Array.from({ length: timeTickCount }, (_unused, index) => {
    const ratio = timeTickCount === 1 ? 0 : index / (timeTickCount - 1);
    const ts = Math.round(minTs + span * ratio), x = left + plotWidth * ratio;
    return `<line class="timeline-grid-line timeline-grid-line-vertical" x1="${x}" y1="${top}" x2="${x}" y2="${height-bottom}"></line>
      <text class="timeline-time-label" x="${x}" y="${height-24}" text-anchor="middle">${escapeHtml(new Date(ts*1000).toLocaleString("ru-RU", {day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"}))}</text>`;
  }).join("");
  const legend = keys.map(series => `<span><i class="timeline-legend-line ${series.cls}"></i>${escapeHtml(series.label)}</span>`).join("");
  return `<div class="recommendation-timeline" role="img" aria-label="Динамика ценовой геометрии стратегии">
    <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="xMidYMid meet">${horizontalTicks}${timeTicks}${paths}</svg>
  </div><div class="timeline-legend">${legend}</div>`;
}

function sortRecommendationHistoryRowsNewestFirst(items) {
  if (!Array.isArray(items)) return [];
  return [...items].sort((left, right) => {
    const leftTs = toFiniteNumber(left?.ts);
    const rightTs = toFiniteNumber(right?.ts);
    if (leftTs === null && rightTs !== null) return 1;
    if (rightTs === null && leftTs !== null) return -1;
    if (leftTs !== null && rightTs !== null && leftTs !== rightTs) return rightTs - leftTs;

    const leftSequence = toFiniteNumber(left?.sequence);
    const rightSequence = toFiniteNumber(right?.sequence);
    if (leftSequence === null && rightSequence !== null) return 1;
    if (rightSequence === null && leftSequence !== null) return -1;
    if (leftSequence !== null && rightSequence !== null && leftSequence !== rightSequence) {
      return rightSequence - leftSequence;
    }

    return String(right?.rec_id || "").localeCompare(String(left?.rec_id || ""));
  });
}

function buildRecommendationHistoryHtml(data) {
  const items = Array.isArray(data?.items) ? data.items : [];
  const tableItems = sortRecommendationHistoryRowsNewestFirst(items);
  const latest = items.length ? items[items.length - 1] : null;
  const summary = [
    { label: "Стратегия", value: latest ? strategyLabelForItem(latest) : botTypeLabel(data?.bot_type || "—") },
    { label: "Публикаций", value: `${data?.returned ?? 0}${data?.truncated ? ` из ${data?.items_total ?? "?"}` : ""}` },
    { label: "Операторских цепочек", value: data?.publication_root_count ?? 0 },
    { label: "Независимых окон исхода", value: data?.outcome_root_count ?? 0 },
    { label: "Смен направления", value: data?.direction_change_count ?? 0 },
    { label: "Последняя рекомендация", html: latest ? `${strategyDirectionBadge(latest.bot_type || data?.bot_type, latest.direction)} ${statusBadgeHtml(data?.latest_effective_status || latest.stored_status)}` : "—" },
    { label: "Возраст последней", value: latest ? (latest.timestamp_valid === false ? "Некорректная метка времени" : formatAgeHuman(latest.age_sec)) : "—" },
    { label: "Первая в окне", value: formatTs(data?.first_ts) },
  ];

  const table = buildModalTable([
    { label: "Время", render: row => escapeHtml(formatTs(row.ts)) },
    { label: "Возраст", render: row => escapeHtml(row.timestamp_valid === false ? "Некорректная метка времени" : formatAgeHuman(row.age_sec)) },
    { label: "Направление", render: row => strategyDirectionBadge(row.bot_type || data?.bot_type, row.direction) },
    { label: "Ценовой план", className: "wrap", render: row => {
        const g = row.price_geometry || {};
        if (g.kind === "directional_trend") return escapeHtml(`Вход ${formatDotNumber(g.reference_price, 8, false)} · TP ${formatDotNumber(g.take_profit, 8, false)} · SL ${formatDotNumber(g.stop_loss, 8, false)}`);
        return escapeHtml(`Опора ${formatDotNumber(g.reference_price, 8, false)} · диапазон ${formatDotNumber(g.range_lower, 8, false)}—${formatDotNumber(g.range_upper, 8, false)} · kill ${formatDotNumber(g.kill_lower, 8, false)}—${formatDotNumber(g.kill_upper, 8, false)}`);
      }
    },
    { label: "Исход", render: row => {
        const tracking = row.outcome_tracking || {};
        if (String(tracking.state || "") !== "labeled") return `<span class="neutral-note">${escapeHtml(healthStatusRu(tracking.state || "waiting"))}</span>`;
        return renderOutcomeResult(tracking.success, tracking.diagnostics, tracking.event_type, row.bot_type);
      }
    },
    { label: "Статус в БД", render: row => pillStatus(row.stored_status || row.status) },
    { label: "Проверка LLM", render: row => renderLlmStatusBadge(row.llm_status || "none") },
    { label: "Публикация", render: row => escapeHtml(row.publication_kind === "root" ? "новая операторская идея" : "обновление цепочки") },
    { label: "Разметка", render: row => escapeHtml(row.outcome_kind === "root" ? "новое окно исхода" : "общее окно исхода") },
    { label: "RR плана", render: row => {
        const value = toFiniteNumber(row.plan_rr);
        return escapeHtml(value === null ? "—" : formatDotNumber(value, 2, false));
      }
    },
    { label: "Доходность по наблюдениям", render: row => {
        const mean = toFiniteNumber(row.empirical_mean_return);
        const status = String(row.empirical_expectancy_status || "insufficient");
        if (mean === null) return escapeHtml(`недостаточно данных (${empiricalStatusRu(status)})`);
        return escapeHtml(`${formatReturnFraction(mean)} (${empiricalStatusRu(status)})`);
      }
    },
    { label: "Изменение", className: "wrap", render: row => {
        const marks = [];
        if (row.direction_changed) marks.push("смена направления");
        if (row.status_changed) marks.push("смена статуса");
        if (row.publication_root_changed) marks.push("новая операторская цепочка");
        if (row.outcome_root_changed) marks.push("новое независимое окно исхода");
        return escapeHtml(marks.join(", ") || "—");
      }
    },
  ], tableItems, { emptyText: "Публикаций по этой паре пока нет.", compact: true, maxHeight: 420 });

  return `
    <p class="modal-note">График показывает каждую сохранённую публикацию для ${escapeHtml(data?.symbol || "пары")}. Исторический статус и состояние LLM берутся из базы данных; текущие проверки Bybit и предзапусковые условия достоверно пересчитываются только для последней строки и не приписываются задним числом старым точкам.</p>
    ${renderModalSummaryCards(summary)}
    <div class="modal-section">
      <div class="modal-section-title">Динамика направления и моменты публикаций</div>
      ${buildRecommendationTimelineSvg(items)}
    </div>
    <div class="modal-section">
      <div class="modal-section-title">Динамика ценовой геометрии стратегии</div>
      ${buildStrategyPriceTimelineSvg(items)}
    </div>
    <div class="modal-section">
      <div class="modal-section-title">Журнал публикаций</div>
      ${table}
    </div>
  `;
}

async function fetchRecommendationHistory(meta, limit = 500) {
  const venue = String(meta?.venue || "linear");
  const symbol = String(meta?.symbol || "").trim().toUpperCase();
  const botType = String(meta?.bot_type || "futures_grid");
  if (!symbol) return null;
  const qs = new URLSearchParams({ venue, symbol, bot_type: botType, limit: String(limit) });
  const response = await fetch(`/api/v1/recommendations/history?${qs.toString()}`);
  if (!response.ok) return null;
  return response.json();
}

async function loadRecommendationHistory(meta = currentMeta) {
  let data;
  try {
    data = await fetchRecommendationHistory(meta, 500);
  } catch (e) {
    showModal("История рекомендации", "Ошибка сети при загрузке истории.");
    return;
  }
  if (!data) {
    showModal("История рекомендации", "Не удалось загрузить историю по выбранной паре.");
    return;
  }
  try {
    showModalHtml(`История ${data.symbol || meta?.symbol || "рекомендации"}`, buildRecommendationHistoryHtml(data));
  } catch (e) {
    showModalHtml("Ошибка отображения истории", `
      <div class="modal-section">
        <div class="modal-section-title">История получена, но не может быть безопасно отображена</div>
        <p class="modal-note">Данные не подменялись вымышленной линией или пустым графиком. Техническая причина:</p>
        <pre class="json-box">${escapeHtml(String(e?.message || e || "history_render_error"))}</pre>
      </div>
    `);
  }
}

function localizeObjectForDisplay(value, key = "") {
  if (Array.isArray(value)) return value.map(item => localizeObjectForDisplay(item, key));
  if (value && typeof value === "object") {
    const keyLabels = {
      ts: "Время", action: "Действие", operator: "Оператор", status: "Статус",
      rec_id: "Идентификатор рекомендации", publication_root_rec_id: "Корневой идентификатор операторской публикации",
      outcome_root_rec_id: "Корневой идентификатор независимого окна исхода",
      entry_ts: "Время начала наблюдения", entry_price: "Начальная цена",
      label_available_ts: "Время готовности результата", event_ts: "Время события",
      candle_high: "Максимум свечи", candle_low: "Минимум свечи", candle_volume: "Объём свечи",
      effective_status: "Итоговый статус", symbol: "Символ", direction: "Направление",
      message: "Сообщение", msg: "Сообщение", reason: "Причина", code: "Технический код",
      details: "Сведения", severity: "Важность", count: "Количество", total: "Всего",
      state: "Состояние", enabled: "Включено", ready: "Готово", error: "Ошибка",
      risk_state: "Состояние риска", decision: "Решение", limits: "Ограничения",
      venue: "Рынок", bot_type: "Вид стратегии", margin_mode: "Режим маржи",
      risk_profile: "Профиль риска", preflight_status: "Предзапусковая проверка",
      funding_rate: "Ставка платежа финансирования", funding_interval: "Период платежа финансирования",
      spread_bps: "Разница цен покупки и продажи", slippage_bps: "Проскальзывание",
      execution_cost_bps: "Расходы на исполнение", plan_rr: "RR плана",
      empirical_expectancy_status: "Статус доходности по наблюдениям",
      policy_fingerprint: "Идентификатор набора правил", sample_role: "Роль наблюдения",
      llm_status: "Статус LLM", gate_decision: "Результат проверки допуска",
      raw_direction: "Исходное направление", execution_direction: "Направление после проверок",
      confidence: "Уверенность", calibrated_confidence: "Откалиброванная уверенность",
      timeframe: "Временной интервал", tf_sec: "Временной интервал",
    };
    const out = {};
    for (const [childKey, childValue] of Object.entries(value)) {
      const label = keyLabels[childKey] || humanizeOperatorText(childKey);
      out[label] = localizeObjectForDisplay(childValue, childKey);
    }
    return out;
  }
  if (typeof value === "string") {
    if (isTechnicalIdentifierField(key)) return value;
    if (key.includes("status")) return operatorStatusRu(value);
    if (key === "direction" || key === "raw_direction" || key === "execution_direction") return directionRu(value);
    if (key === "action") return decisionActionRu(value);
    if (key === "reason") return outcomeObservabilityReasonRu(value);
    if (key === "llm_status") return llmStatusRu(value);
    if (key === "sample_role") return sampleRoleRu(value);
    if (key === "gate_decision") return gateDecisionRu(value);
    if (key === "timeframe" || key === "tf_sec") return timeframeRu(value);
    if (key === "bot_type") return botTypeLabel(value);
    if (key === "margin_mode") return marginModeRu(value);
    return humanizeOperatorText(value);
  }
  return value;
}

function configureModalLayout({ wide = false } = {}) {
  const card = document.querySelector("#modal .modal-card");
  if (!card) return;
  card.classList.toggle("modal-card-wide", Boolean(wide));
}

function showModal(title, obj) {
  configureModalLayout();
  const body = $("modalBody");
  $("modalTitle").textContent = title;
  body.classList.remove("modal-html");
  body.classList.add("pre");
  const displayValue = typeof obj === "string" ? humanizeOperatorText(obj) : localizeObjectForDisplay(obj);
  body.textContent = typeof displayValue === "string" ? displayValue : JSON.stringify(displayValue, null, 2);
  $("modal").classList.remove("hidden");
}

function showRawTechnicalModal(title, obj) {
  configureModalLayout();
  const body = $("modalBody");
  $("modalTitle").textContent = title;
  body.classList.remove("modal-html");
  body.classList.add("pre");
  body.textContent = typeof obj === "string" ? obj : JSON.stringify(obj, null, 2);
  $("modal").classList.remove("hidden");
}

function showModalHtml(title, html, { wide = false } = {}) {
  configureModalLayout({ wide });
  const body = $("modalBody");
  $("modalTitle").textContent = title;
  body.classList.remove("pre");
  body.classList.add("modal-html");
  body.innerHTML = html;
  $("modal").classList.remove("hidden");
}

function closeAllDialogs() {
  document.querySelectorAll(".modal").forEach(dialog => dialog.classList.add("hidden"));
}

function hideModal() {
  closeAllDialogs();
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
      shockEl.textContent = `Защита: ${humanizeOperatorText(shock.title || "Нормальный режим")}`;
      shockEl.title = humanizeOperatorText(shock.operator_note || "");
    }

    // sentiment badge
    const sent = s.sentiment || {};
    const ewma = sent.ewma_6h;
    const regime = sentimentRegimeRu(sent.regime || "—");
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
        `Новостной фон* ${v >= 0 ? "+" : ""}${v.toFixed(2)} (${regime})${flag}`;
      $("sentiment-badge").title = "Оценочный новостной фон: новостные ленты, Reddit и рыночный контекст. Это не полный смысловой анализ текстов новостей.";
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

const RECO_FILTER_STORAGE_KEY = "operator.recommendationStatusFilters.v1";
const RECO_FILTER_IDS = ["showRecommended", "showPending", "showBlocked", "showNoTrade", "showSuppressed"];

function getRecommendationFilterState() {
  const state = {};
  RECO_FILTER_IDS.forEach((id) => {
    const el = $(id);
    if (el) state[id] = !!el.checked;
  });
  return state;
}

function applyRecommendationFilterState(state) {
  if (!state || typeof state !== "object") return;
  RECO_FILTER_IDS.forEach((id) => {
    const value = state[id];
    const el = $(id);
    if (el && typeof value === "boolean") el.checked = value;
  });
}

function restoreRecommendationFilterState() {
  try {
    const raw = window.localStorage.getItem(RECO_FILTER_STORAGE_KEY);
    if (!raw) return;
    applyRecommendationFilterState(JSON.parse(raw));
  } catch (e) {
    // Keep the safe HTML defaults when localStorage is unavailable or corrupted.
  }
}

function persistRecommendationFilterState() {
  try {
    window.localStorage.setItem(RECO_FILTER_STORAGE_KEY, JSON.stringify(getRecommendationFilterState()));
  } catch (e) {
    // Non-critical UI preference; recommendations are still fetched from current controls.
  }
}

function shouldAutoExpandDiagnostics(data, items, filters) {
  const storedCounts = (data && data.status_counts) || {};
  const effectiveCounts = (data && data.effective_status_counts) || {};
  const counts = {
    pending: Math.max(Number(storedCounts.pending || 0), Number(effectiveCounts.pending || 0)),
    blocked: Math.max(Number(storedCounts.blocked || 0), Number(effectiveCounts.blocked || 0)),
    no_trade: Math.max(Number(storedCounts.no_trade || 0), Number(effectiveCounts.no_trade || 0)),
  };
  const onlyActionableFilter = filters.showRecommended === true
    && filters.showPending !== true
    && filters.showBlocked !== true
    && filters.showNoTrade !== true
    && filters.showSuppressed !== true;
  const nonActionableCount = Number(counts.pending || 0) + Number(counts.blocked || 0) + Number(counts.no_trade || 0);
  return onlyActionableFilter && Array.isArray(items) && items.length === 0 && nonActionableCount > 0;
}

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
  const activeFilters = { showRecommended, showPending, showBlocked, showNoTrade, showSuppressed };

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
    `Рынок: риск — ${marketStateRu(regime.risk_state, "risk")} | колебания — ${marketStateRu(regime.vol_state, "vol")} | направление — ${marketStateRu(regime.trend_state, "trend")}`;

  const body = $("recoBody");
  body.innerHTML = "";

  const items = data.items || [];
  if (shouldAutoExpandDiagnostics(data, items, activeFilters)) {
    const storedCounts = data.status_counts || {};
    const effectiveCounts = data.effective_status_counts || {};
    const counts = {
      pending: Math.max(Number(storedCounts.pending || 0), Number(effectiveCounts.pending || 0)),
      blocked: Math.max(Number(storedCounts.blocked || 0), Number(effectiveCounts.blocked || 0)),
      no_trade: Math.max(Number(storedCounts.no_trade || 0), Number(effectiveCounts.no_trade || 0)),
    };
    if ((Number(counts.pending || 0) > 0) && $("showPending")) $("showPending").checked = true;
    if ((Number(counts.blocked || 0) > 0) && $("showBlocked")) $("showBlocked").checked = true;
    if ((Number(counts.no_trade || 0) > 0) && $("showNoTrade")) $("showNoTrade").checked = true;
    await loadRecommendations();
    return;
  }
  lastItems = items;
  uiScoreMetaById = computeUiScoreMetaMap(items);
  renderRecoTable(items);
  updateCalibrationUi(items);

  const banner = $("noTrade");
  const hasActionable = items.some(it => { const s = operatorEffectiveStatus(it); return s === "recommended" || s === "active"; });
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
      av = da.direction_confidence_feature ?? da.direction_confidence_calibrated ?? da.direction_confidence ?? -1;
      bv = db.direction_confidence_feature ?? db.direction_confidence_calibrated ?? db.direction_confidence ?? -1;
    } else if (sortCol === "score") {
      av = ensureUiScoreMeta(a, items).percentile ?? -1;
      bv = ensureUiScoreMeta(b, items).percentile ?? -1;
    } else if (sortCol === "plan_rr") {
      av = planRrNumber(a) ?? -1;
      bv = planRrNumber(b) ?? -1;
    } else if (sortCol === "empirical_expectancy") {
      av = empiricalMeanReturnNumber(a) ?? -1e9;
      bv = empiricalMeanReturnNumber(b) ?? -1e9;
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

function operatorDecisionPresentation(it) {
  const summary = it?.operator_summary && typeof it.operator_summary === "object" ? it.operator_summary : {};
  const decision = String(summary.decision || "").trim().toLowerCase();
  const effectiveStatus = String(summary.effective_status || operatorEffectiveStatus(it) || "").trim().toLowerCase();

  if (effectiveStatus === "executed" || decision === "executed") {
    return { label: "ЗАПУЩЕНО", className: "decision-executed", effectiveStatus };
  }
  if (effectiveStatus === "recommended" || effectiveStatus === "active" || decision === "enter_allowed") {
    return { label: "ВХОДИТЬ", className: "decision-enter", effectiveStatus };
  }
  if (effectiveStatus === "blocked") {
    return { label: "ЗАБЛОКИРОВАНО", className: "decision-blocked", effectiveStatus };
  }
  if (effectiveStatus === "no_trade") {
    return { label: "НЕ ТОРГОВАТЬ", className: "decision-no-trade", effectiveStatus };
  }
  if (effectiveStatus === "pending" || decision === "wait") {
    return { label: "ЖДАТЬ", className: "decision-pending", effectiveStatus };
  }
  if (effectiveStatus === "ignored") {
    return { label: "ОТКЛОНЕНО", className: "decision-muted", effectiveStatus };
  }
  if (effectiveStatus === "expired") {
    return { label: "УСТАРЕЛО", className: "decision-muted", effectiveStatus };
  }
  if (effectiveStatus === "suppressed") {
    return { label: "СКРЫТО", className: "decision-muted", effectiveStatus };
  }
  return { label: "НЕИЗВЕСТНО", className: "decision-muted", effectiveStatus: effectiveStatus || "unknown" };
}

function operatorDecisionCell(it) {
  const summary = it?.operator_summary && typeof it.operator_summary === "object" ? it.operator_summary : {};
  const reason = String(summary.primary_reason || "Решение требует проверки");
  const presentation = operatorDecisionPresentation(it);
  const ariaLabel = `${presentation.label}: ${reason}`;
  return `<span class="decision ${presentation.className}" tabindex="0" title="${escapeHtml(reason)}" aria-label="${escapeHtml(ariaLabel)}">${presentation.label}</span>`;
}

function renderRecoTable(items) {
  const sorted = applySort(items);
  updateSortHeaders();
  const body = $("recoBody");
  body.innerHTML = "";
  let hasActionable = false;
  sorted.forEach((it, i) => {
    { const s = operatorEffectiveStatus(it); if (s === "recommended" || s === "active") hasActionable = true; }
    const tr = document.createElement("tr");
    { const s = operatorEffectiveStatus(it); if (s === "recommended" || s === "active") tr.classList.add("row-recommended"); }
    tr.innerHTML = `
      <td>
        <div class="symbol-cell">
          <b>${escapeHtml(it.symbol || "—")}</b>
          <span class="helper-text">${escapeHtml(strategyLabelForItem(it))}</span>
          ${symbolLinksHtml(it)}
        </div>
      </td>
      <td>${strategyDirectionBadge(it.bot_type, it.direction)}</td>
      <td>${planRrCell(it)}</td>
      <td>${empiricalExpectancyCell(it)}</td>
      <td data-cell="status">${operatorDecisionCell(it)}</td>
      <td data-cell="details" class="details-action-cell"><button class="btn tiny symbol-details" data-act="details" data-id="${escapeHtml(it.rec_id)}">Детали</button></td>
    `;
    body.appendChild(tr);
  });
  const banner = $("noTrade");
  if (!hasActionable) {
    const shock = (statusPayload || {}).market_shock || {};
    if (shock && shock.state && shock.state !== "normal") {
      banner.innerHTML = `НЕТ РАЗРЕШЁННЫХ СДЕЛОК: <b>${escapeHtml(humanizeOperatorText(shock.title || "Защитный режим"))}</b>. ${escapeHtml(humanizeOperatorText(shock.operator_note || "Новые входы заблокированы."))}`;
    } else {
      banner.innerHTML = 'НЕТ РАЗРЕШЁННЫХ СДЕЛОК: по текущим фильтрам нет рекомендаций со статусом <b>«Можно торговать»</b>. Поэтому UI показывает строки <b>«Ожидает проверки»</b>, <b>«Заблокировано»</b> и <b>«Не торговать»</b>. Заблокировано означает жёсткий запрет по риску, данным Bybit или предзапусковой проверке. Не торговать означает, что идея не прошла обязательные условия качества и экономики; это не обязательно техническая ошибка.';
    }
    banner.classList.remove("hidden");
  } else {
    banner.classList.add("hidden");
  }
}
function directionBadge(dir) {
  const normalized = String(dir || "").trim().toLowerCase();
  if (normalized === "long") return `<span class="dir-badge dir-long">▲ ${escapeHtml(directionRu(normalized))}</span>`;
  if (normalized === "short") return `<span class="dir-badge dir-short">▼ ${escapeHtml(directionRu(normalized))}</span>`;
  return `<span class="dir-badge dir-neu">• ${escapeHtml(directionRu(normalized))}</span>`;
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
        $("details").textContent = `Ошибка загрузки деталей (код ответа сервера ${res.status}).`;
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

async function refreshCurrentDetails() {
  // Recommendation identity is immutable. Refresh the exact selected audit row;
  // newer rows for the same pair belong in the history timeline and must not
  // silently replace this card (possibly with a different direction/status).
  if (currentRecId) await loadDetails(currentRecId);
}

// ── decisions / risk ──────────────────────────────────────────────────────────

async function loadHealth() {
  showModalHtml("Здоровье системы", `
    <div class="modal-section">
      <div class="modal-section-title">Загрузка диагностики</div>
      <p class="modal-note">Получаем актуальное состояние фоновых контуров и базы данных…</p>
    </div>
  `, { wide: true });
  const [healthRes, statusRes, decisionsRes] = await Promise.all([
    fetch("/api/v1/health/symbols"),
    fetch("/api/v1/status"),
    fetch("/api/v1/decisions?limit=200"),
  ]);
  let data;
  let systemStatus;
  let recentDecisions = [];
  try {
    data = await healthRes.json();
    systemStatus = await statusRes.json();
    if (decisionsRes.ok) recentDecisions = await decisionsRes.json();
  } catch (e) {
    showModal("Ошибка загрузки здоровья", { error: "Сервер вернул некорректный диагностический ответ" });
    return;
  }
  if (!healthRes.ok || !statusRes.ok) {
    showModal("Ошибка загрузки здоровья", { health: data, status: systemStatus });
    return;
  }
  lastHealthDiagnostics = {
    generated_at: new Date().toISOString(),
    browser_location: window.location.href,
    health: data,
    status: systemStatus,
    recent_decisions: recentDecisions,
  };

  const sum = data.summary || {};
  const llm = data.llm_reviewer || {};
  const warmup = data.warmup || {};
  const runtime = data.runtime || {};
  const collector = data.collector || {};
  const backfill = data.backfill || {};
  const operatorReadiness = systemStatus.operator_readiness || {};
  const recommendationReadiness = systemStatus.recommendation_readiness || {};
  const outcomeWorker = systemStatus.outcome_worker || {};
  const databaseSchema = systemStatus.database_schema || {};
  const databaseContinuity = systemStatus.database_continuity || {};
  const outcomeIntegrity = databaseContinuity.outcome_semantic_integrity || {};
  const runtimeProvenance = systemStatus.runtime_provenance || {};
  const fundingRepair = systemStatus.funding_settlement_repair || {};
  const tradeJournal = systemStatus.market_trade_journal || {};
  const botCalibrator = systemStatus.bot_calibrators?.futures_grid || {};
  const trendCalibrator = systemStatus.bot_calibrators?.directional_trend || {};
  const trendFirstTouch = systemStatus.trend_first_touch_model || {};
  const outcomeByBot = outcomeWorker.by_bot_type || {};
  const gridOutcomeQueue = outcomeByBot.futures_grid || {};
  const trendOutcomeQueue = outcomeByBot.directional_trend || {};
  const backgroundThreads = Object.entries(systemStatus.background_threads || {}).map(([name, info]) => ({
    name,
    ...(info || {}),
  }));
  const readinessStateLabels = {
    ready: "Работает, есть торговые кандидаты",
    healthy_not_actionable: "Работает, торговых кандидатов нет",
    starting: "Запускается / накапливает данные",
    degraded: "Есть эксплуатационная проблема",
  };
  const readinessState = String(operatorReadiness.state || "unknown");
  const recCounts = recommendationReadiness.status_counts || {};
  const warmupRatio = Number(warmup.ready_ratio || 0);
  const warmupMinRatio = Number(warmup.min_ready_ratio || 0);
  const warmupReadySymbols = Number(warmup.ready_symbols || 0);
  const warmupSymbolsTotal = Number(warmup.symbols_total || 0);
  const problemSymbols = Number(sum.stale || 0) + Number(sum.missing || 0) + Number(sum.disabled || 0);
  const symbols = [...(data.symbols || [])].sort((a, b) => {
    const rank = { disabled: 0, missing: 1, stale: 2, ok: 3 };
    const ra = rank[a.status] ?? 9;
    const rb = rank[b.status] ?? 9;
    if (ra !== rb) return ra - rb;
    if (Boolean(b.disabled) !== Boolean(a.disabled)) return Number(b.disabled) - Number(a.disabled);
    if (Number(b.error_count_10m || 0) !== Number(a.error_count_10m || 0)) return Number(b.error_count_10m || 0) - Number(a.error_count_10m || 0);
    return String(a.symbol || "").localeCompare(String(b.symbol || ""), "ru");
  });

  const operatorRows = [
    ...(operatorReadiness.explanations || []).map(row => ({ kind: "Состояние", count: null, ...row })),
    ...(recommendationReadiness.no_trade_reason_counts || []).map(row => ({ kind: "Не торговать", ...row })),
    ...(recommendationReadiness.blocked_reason_counts || []).map(row => ({ kind: "Блокировка", ...row })),
  ];

  const readinessRows = [
    { name: "Готовность рыночных данных", value: `${warmupReadySymbols}/${warmupSymbolsTotal} (${warmupSymbolsTotal > 0 ? `${(warmupRatio * 100).toFixed(1)}%` : "—"}; минимум ${(warmupMinRatio * 100).toFixed(1)}%)` },
    { name: "Символы: норма / stale / missing / disabled", value: `${Number(sum.ok || 0)} / ${Number(sum.stale || 0)} / ${Number(sum.missing || 0)} / ${Number(sum.disabled || 0)}` },
    { name: "Ошибки за 10 минут", value: Number(sum.errors_10m || 0) },
    { name: "Ожидают созревания горизонта", value: `${outcomeWorker.scheduled_waiting_total ?? 0}; созревших ${outcomeWorker.matured_pending_total ?? 0}; worker=${healthStatusRu(outcomeWorker.state || "unknown")}` },
    { name: "Grid · ожидают горизонта", value: `${gridOutcomeQueue.scheduled_waiting_total ?? 0}; ближайший срок ${formatTs(gridOutcomeQueue.next_due_ts)}` },
    { name: "Trend · ожидают горизонта", value: `${trendOutcomeQueue.scheduled_waiting_total ?? 0}; ближайший срок ${formatTs(trendOutcomeQueue.next_due_ts)}` },
    { name: "Funding settlement recovery", value: `ожидают ${fundingRepair.pending ?? 0}; восстановлено ${fundingRepair.resolved ?? 0}; следующая попытка ${formatTs(fundingRepair.next_due_ts)}` },
    { name: "Intrabar trade journal", value: tradeJournal.enabled === false ? "Отключён" : `строк ${tradeJournal.trade_rows_total ?? 0}; инструментов ${Object.keys(tradeJournal.symbols || {}).length}; разрывов ${tradeJournal.closed_gap_spans_total ?? 0}; поток ${tradeJournal.stream_enabled === false ? "REST fallback" : "WebSocket + REST fallback"}` },
    { name: "Семантическая целостность исходов", value: outcomeIntegrity.ok === true ? "Норма" : (outcomeIntegrity.ok === false ? "Нарушена" : "—") },
    { name: "Схема / материализация БД", value: databaseSchema.migration_applied && Number(databaseSchema.materialization_pending || 0) === 0 ? "Норма" : `Требует внимания; pending=${databaseSchema.materialization_pending ?? "—"}` },
    { name: "Grid evidence", value: `${botCalibrator.fitted ? "калибратор обучен" : "калибратор не обучен"}; exact-policy n=${botCalibrator.policy_eligible_outcomes_total ?? 0}; expectancy=${empiricalStatusRu(botCalibrator.expectancy_status || "insufficient")}` },
    { name: "Trend evidence", value: `${trendCalibrator.fitted ? "binary-калибратор обучен" : "binary-калибратор не обучен"}; exact-policy n=${trendCalibrator.policy_eligible_outcomes_total ?? 0}; first-touch n=${trendFirstTouch.n_samples ?? 0}` },
  ];

  const advancedOutcomeRows = [
    { name: "Идентификатор БД", value: databaseContinuity.database_instance_id || "—" },
    { name: "Ближайшее созревание outcome", value: formatTs(outcomeWorker.next_due_ts) },
    { name: "До ближайшего созревания", value: formatAgeHuman(outcomeWorker.next_due_in_sec) },
    { name: "Возраст старейшего просроченного", value: formatAgeHuman(outcomeWorker.oldest_due_age_sec) },
    { name: "Рекомендаций / исходов в БД", value: `${databaseContinuity.recommendations_total ?? "—"} / ${databaseContinuity.outcomes_total ?? "—"}` },
    { name: "Strategy recommendations / rejected trend evaluations", value: `${databaseContinuity.recommendations_by_candidate_kind?.strategy_recommendation ?? 0} / ${databaseContinuity.recommendations_by_candidate_kind?.trend_evaluation_rejected ?? 0}` },
    { name: "Grid / trend исходов", value: `${databaseContinuity.outcomes_by_bot_type?.futures_grid ?? 0} / ${databaseContinuity.outcomes_by_bot_type?.directional_trend ?? 0}` },
    { name: "Orphan / missing observability", value: `${outcomeIntegrity.orphan_outcome_total ?? "—"} / ${outcomeIntegrity.missing_observability_total ?? "—"}` },
    { name: "Identity mismatch / invalid events", value: `${outcomeIntegrity.recommendation_identity_mismatch_total ?? "—"} / ${outcomeIntegrity.invalid_event_type_total ?? "—"}` },
    { name: "Outcomes у rejected trend", value: outcomeIntegrity.rejected_trend_outcome_total ?? "—" },
    { name: "Grid temporal clusters", value: `${botCalibrator.temporal_cluster_count ?? 0}/${botCalibrator.minimum_temporal_clusters ?? 0}` },
    { name: "Trend first-touch classes", value: `TP ${trendFirstTouch.class_counts?.TP_FIRST ?? 0}; SL ${trendFirstTouch.class_counts?.SL_FIRST ?? 0}; horizon ${trendFirstTouch.class_counts?.HORIZON_EXIT ?? 0}` },
    { name: "Trend terminal holdout / log-loss", value: `${trendFirstTouch.holdout_status || "—"} / ${trendFirstTouch.holdout_log_loss ?? "—"}` },
    { name: "Funding repair / next due", value: `${fundingRepair.pending ?? 0} pending; ${formatTs(fundingRepair.next_due_ts)}` },
    { name: "Trade journal transport", value: tradeJournal.transport || tradeJournal.primary_source || "—" },
    { name: "Trade journal retention / poll limit", value: `${tradeJournal.retention_hours ?? "—"}ч / ${tradeJournal.poll_limit ?? "—"}` },
    { name: "Trade journal evidence boundary", value: tradeJournal.evidence_boundary || "—" },
  ];

  const runtimeRows = [
    { name: "PID / владелец", value: `${runtime.pid ?? "—"} / ${runtimeProvenance.runtime_owner || runtime.runtime_owner || "—"}` },
    { name: "Память Python (потоки / RSS / peak RSS)", value: `${runtime.thread_count ?? "—"} / ${runtime.rss_mb == null ? "—" : `${Number(runtime.rss_mb).toFixed(1)} МБ`} / ${runtime.peak_rss_mb == null ? "—" : `${Number(runtime.peak_rss_mb).toFixed(1)} МБ`}` },
    { name: "Цикл сборщика текущего процесса", value: runtimeProvenance.collector_cycle_current_process ? "Да" : "Нет" },
    { name: "Collector lock текущего процесса", value: runtimeProvenance.collector_lock_owned_by_current_process ? "Да" : "Нет" },
    { name: "Публикация текущего процесса", value: runtimeProvenance.publication_current_process ? "Да" : "Нет" },
    { name: "Свежий хвост / max buffer", value: `${collector.recent_tail_bars ?? 360} / ${collector.max_buffered_ohlcv_rows ?? "—"}` },
    { name: "Backfill chunk / budget", value: `${backfill.chunk_bars ?? "—"} / ${backfill.budget_per_tf ?? "—"}` },
    { name: "Строк восстановления / Оставшихся заданий восстановления", value: `${backfill.gap_backfill_rows ?? 0} / ${backfill.gap_backfill_jobs_remaining ?? 0}` },
    { name: "LLM", value: `${llm.enabled ? "включён" : "выключен"}; ${[llm.provider, llm.model].filter(Boolean).join(" / ") || "—"}; mode=${llm.mode || "—"}` },
    { name: "LLM cadence / timeout", value: `${llm.cadence_sec ?? "—"} / ${llm.pending_timeout_sec ?? "—"} сек` },
  ];

  const html = `
    ${renderModalSummaryCards([
      { label: "Состояние", value: readinessStateLabels[readinessState] || healthStatusRu(readinessState) },
      { label: "Версия", value: systemStatus.app_version || "—" },
      { label: "Торговые кандидаты", value: Number(recommendationReadiness.actionable_count || 0) },
      { label: "Не торговать / blocked", value: `${Number(recCounts.no_trade || 0)} / ${Number(recCounts.blocked || 0)}` },
      { label: "Готовые инструменты", value: `${warmupReadySymbols}/${warmupSymbolsTotal}` },
      { label: "Проблемные инструменты", value: problemSymbols },
      { label: "Outcome waiting grid / trend", value: `${gridOutcomeQueue.scheduled_waiting_total ?? 0} / ${trendOutcomeQueue.scheduled_waiting_total ?? 0}` },
      { label: "Модели grid / trend", value: `${botCalibrator.fitted ? "готова" : "нет"} / ${trendFirstTouch.fitted ? "готова" : "нет"}` },
    ])}
    <p class="modal-note"><b>${escapeHtml(readinessStateLabels[readinessState] || healthStatusRu(readinessState))}.</b> Исправный runtime не означает наличие разрешённой сделки. Экран ниже разделяет операторские причины, готовность данных и доказательность моделей, не повторяя одни и те же числа в нескольких таблицах.</p>
    <div class="diagnostic-actions">
      <button class="btn secondary" data-act="copy-health-diagnostics">Скопировать диагностику</button>
      <button class="btn secondary" data-act="download-health-diagnostics">Скачать диагностику JSON</button>
    </div>
    <div class="modal-section">
      <div class="modal-section-title">Операторский статус</div>
      ${buildModalTable([
        { label: "Тип", render: row => `<span class="wrap">${escapeHtml(row.kind || "—")}</span>` },
        { label: "Код", render: row => `<span class="wrap">${escapeHtml(row.code || "—")}</span>` },
        { label: "Количество", render: row => row.count === null || row.count === undefined ? "—" : escapeHtml(String(Number(row.count || 0))) },
        { label: "Пояснение", className: "wrap", render: row => `<span class="wrap">${escapeHtml(humanizeOperatorText(row.message || row.code || "—"))}</span>` },
      ], operatorRows, { emptyText: "Эксплуатационные проблемы и структурированные причины запрета отсутствуют." })}
    </div>
    <div class="modal-section">
      <div class="modal-section-title">Готовность данных и доказательность</div>
      ${buildModalTable([
        { label: "Контур", render: row => `<span class="wrap">${escapeHtml(row.name)}</span>` },
        { label: "Состояние", className: "wrap", render: row => `<span class="wrap">${escapeHtml(String(row.value ?? "—"))}</span>` },
      ], readinessRows, { emptyText: "Диагностика готовности недоступна." })}
    </div>
    <div class="modal-section">
      <div class="modal-section-title">Фоновые контуры</div>
      ${buildModalTable([
        { label: "Контур", render: row => `<span class="wrap">${escapeHtml(humanizeOperatorText(row.name || "—"))}</span>` },
        { label: "Состояние", render: row => renderHealthStatus(row.state || "unknown") },
        { label: "Последнее изменение", render: row => escapeHtml(formatTs(row.updated_ts || row.last_heartbeat_ts || row.started_ts)) },
        { label: "Ошибка", className: "wrap", render: row => `<span class="wrap">${escapeHtml(humanizeOperatorText(row.error || "—"))}</span>` },
      ], backgroundThreads, { emptyText: "Состояние фоновых контуров недоступно." })}
    </div>
    ${renderModalDisclosure("Расширенная диагностика БД, outcome, runtime и LLM", `
      ${buildModalTable([
        { label: "Параметр", render: row => `<span class="wrap">${escapeHtml(row.name)}</span>` },
        { label: "Значение", className: "wrap", render: row => `<span class="wrap">${escapeHtml(String(row.value ?? "—"))}</span>` },
      ], [...advancedOutcomeRows, ...runtimeRows], { emptyText: "Расширенная диагностика недоступна." })}
    `, { note: "Технические поля сохранены для аудита, но скрыты по умолчанию, чтобы не дублировать основную сводку." })}
    <div class="modal-section">
      <div class="modal-section-title">Журнал здоровья символов</div>
      ${buildModalTable([
        { label: "Символ", render: row => `<span class="wrap">${escapeHtml(row.symbol || "—")}</span>` },
        { label: "Статус", render: row => renderHealthStatus(row.status) },
        { label: "Возраст свечи", render: row => escapeHtml(formatAgeHuman(row.age_sec)) },
        { label: "Последняя свеча", render: row => escapeHtml(formatTs(row.last_candle_ts)) },
        { label: "Последний тикер", render: row => escapeHtml(formatTs(row.last_ticker_ts)) },
        { label: "Ошибки/10м", render: row => escapeHtml(String(Number(row.error_count_10m || 0))) },
        { label: "Stale-пропуски/ч", render: row => escapeHtml(String(Number(row.stale_skips_1h || 0))) },
        { label: "Отключён", render: row => row.disabled ? '<span class="neutral-note neutral-note-neutralized">Да</span>' : 'Нет' },
      ], symbols, { emptyText: "Данные по инструментам пока отсутствуют.", maxHeight: 520 })}
    </div>
  `;

  showModalHtml("Здоровье системы", html, { wide: true });
}

async function loadOutcomes() {
  showModalHtml("Результаты наблюдений", `
    <div class="modal-section">
      <div class="modal-section-title">Загрузка статистики</div>
      <p class="modal-note">Считаем текущую policy-когорту и загружаем краткую сводку исторического архива…</p>
    </div>
  `, { wide: true });
  const [currentRes, archiveRes] = await Promise.all([
    fetch("/api/v1/outcomes/stats?scope=current_policy"),
    fetch("/api/v1/outcomes/stats?scope=archive&detail=summary"),
  ]);
  let data;
  let archiveData;
  try {
    data = await currentRes.json();
    archiveData = await archiveRes.json();
  } catch (e) {
    showModalHtml("Ошибка загрузки исходов", `
      <div class="modal-section">
        <div class="modal-section-title">Ответ API невозможно прочитать</div>
        <p class="modal-note">Сервис вернул повреждённый или не-JSON ответ. Данные не подменялись пустой статистикой.</p>
        <pre class="json-box">${escapeHtml(String(e?.message || e || "unknown_json_error"))}</pre>
      </div>
    `, { wide: true });
    return;
  }
  if (!currentRes.ok || !archiveRes.ok) {
    showModal("Ошибка загрузки исходов", {
      current_policy: data,
      archive: archiveData,
    });
    return;
  }

  const s = data.summary || {};
  const archiveSummary = archiveData?.summary || {};
  const archiveRoots = archiveData?.cohorts?.all_roots || archiveSummary;
  const currentScope = data?.scope || {};
  const actionableSummary = data.cohorts?.actionable || {};
  const shadowSummary = data.cohorts?.shadow_no_trade || {};
  const allRootsSummary = data.cohorts?.all_roots || s;
  const eligibilitySummary = data.eligibility_summary || {};
  const eligibilityCohorts = data.eligibility_cohorts || {};
  const eligibilityCohortRows = Object.entries(eligibilityCohorts)
    .map(([cohort, stats]) => ({ cohort, ...(stats || {}) }))
    .filter(row => Number(row.total || 0) > 0);
  const eligibilityReasonCounts = data.eligibility_reason_counts || [];
  const decisionReasonCounts = data.decision_reason_counts || [];
  const eligibilityCodeRows = [
    ...eligibilityReasonCounts.map(row => ({ kind: "Исключение из exact-policy", ...row })),
    ...decisionReasonCounts.map(row => ({ kind: "Причина решения", ...row })),
  ];
  const llmSummary = data.llm_summary || {};
  const neutralBreakdown = (data.neutral_breakdown || []).filter(row => String(row?.neutral_source || "") !== "directional");
  const llmByEngine = data.llm_engine_alignment || [];
  const llmMatrix = data.llm_engine_matrix || [];
  const byBotCohort = data.by_bot_cohort || [];
  const sampleObservability = data.sample_observability || {};
  const archiveEventTypeByBotRows = Object.entries(archiveData?.event_type_counts_by_bot || {}).flatMap(([bot_type, counts]) =>
    Object.entries(counts || {}).map(([event_type, count]) => ({ bot_type, event_type, count }))
  );
  const archiveByBot = archiveData?.by_bot || [];
  const archiveEventsByBot = new Map();
  for (const row of archiveEventTypeByBotRows) {
    const key = String(row.bot_type || "unknown");
    if (!archiveEventsByBot.has(key)) archiveEventsByBot.set(key, []);
    archiveEventsByBot.get(key).push(`${outcomeEventTypeRu(row.event_type)}: ${Number(row.count || 0)}`);
  }
  const archiveStrategyRows = archiveByBot.map(row => ({
    ...row,
    event_summary: (archiveEventsByBot.get(String(row.bot_type || "unknown")) || []).join("; ") || "—",
  }));
  const bySymbol = (data.by_symbol || []).slice(0, 30);
  const recent = (data.recent || []).slice(0, 80);
  const archiveRecent = (archiveData?.recent || []).slice(0, 20);
  const insights = buildOutcomeDiagnostics(llmByEngine, neutralBreakdown, s);

  const total = Number(allRootsSummary.total || 0);
  const archiveTotal = Number(archiveRoots.total || 0);
  const actionableTotal = Number(actionableSummary.total || 0);
  const calibrationEligibleTotal = Number(eligibilitySummary.calibration_eligible_total || 0);
  const exactPolicyRetentionDays = Number(eligibilitySummary.exact_policy_retention_days || 0);
  const llmReviewed = Number(llmSummary.ok_total || 0);
  const actionableAvg = actionableTotal > 0 && actionableSummary.avg_ret !== null && actionableSummary.avg_ret !== undefined
    ? `${Number(actionableSummary.avg_ret).toFixed(2)}%`
    : "—";
  const actionableWinRate = actionableTotal > 0 && actionableSummary.win_rate !== null && actionableSummary.win_rate !== undefined
    ? `${(Number(actionableSummary.win_rate) * 100).toFixed(1)}%`
    : "—";
  const evidenceWarning = actionableTotal === 0
    ? "В текущем наборе правил нет ни одного торгового outcome. Стратегические строки ниже — shadow/no-trade наблюдения для исследования; это учебные наблюдения, а не результаты разрешённых прогнозов или исполненных сделок, а не подтверждение реального исполнения сделок."
    : `Торговых outcomes текущего набора правил: ${actionableTotal}; shadow/no-trade: ${Number(shadowSummary.total || 0)}.`;

  const html = `
    ${renderModalSummaryCards([
      { label: "Торговые outcomes", value: actionableTotal },
      { label: "Средний net торговых", value: actionableAvg },
      { label: "Все корневые строки", value: total },
      { label: "Уникальные времена старта", value: Number(sampleObservability.unique_timestamps || 0) },
      { label: "Связанные временные группы", value: Number(sampleObservability.temporal_clusters || 0) },
      { label: "Неперекрывающиеся окна", value: Number(sampleObservability.max_non_overlapping_windows || 0) },
      { label: "Текущие правила · доля успешных", value: actionableWinRate },
      { label: "Средний net · все", value: `${Number(allRootsSummary.avg_ret || 0).toFixed(2)}%` },
      { label: "Допущено к калибровке", value: calibrationEligibleTotal },
      { label: "Shadow no-trade", value: Number(shadowSummary.total || 0) },
      { label: "Архив / LLM reviewed", value: `${archiveTotal} / ${llmReviewed}` },
    ])}
    <p class="modal-note"><b>${escapeHtml(evidenceWarning)}</b> ${escapeHtml(currentScope.label || "Выборка текущего набора правил")} содержит корневые исходы с одним policy fingerprint. Исторический архив в основные показатели не входит. Идентификатор: <span class="wrap">${escapeHtml(String(currentScope.policy_fingerprint || "—").slice(0, 16))}</span>; exact-policy evidence хранится ${exactPolicyRetentionDays || "—"} дней.</p>
    <div class="modal-section">
      <div class="modal-section-title">Когорты допуска (не пересекаются)</div>
      <p class="modal-note">Shadow/no-trade — учебные наблюдения, не калибровка; calibration-eligible и policy-evaluation учитываются отдельно.</p>
      ${buildModalTable([
        { label: "Когорта", className: "wrap", render: row => `<span class="wrap">${escapeHtml(outcomeEligibilityCohortRu(row.cohort))}</span>` },
        { label: "Всего", render: row => escapeHtml(String(row.total || 0)) },
        { label: "Доля", render: row => escapeHtml(formatShare(row.total, total)) },
        { label: "Доля успешных по контракту", render: row => row.win_rate === null || row.win_rate === undefined ? "—" : escapeHtml(`${(Number(row.win_rate) * 100).toFixed(1)}%`) },
        { label: "Средний net", render: row => escapeHtml(fmtPct(row.avg_ret, 2)) },
      ], eligibilityCohortRows, { emptyText: "Когорты допуска пока не рассчитаны." })}
    </div>
    <div class="modal-section">
      <div class="modal-section-title">Стратегии</div>
      ${buildModalTable([
        { label: "Стратегия", render: row => `<span class="wrap">${escapeHtml(botTypeLabel(row.bot_type))}</span>` },
        { label: "Когорта допуска", className: "wrap", render: row => `<span class="wrap">${escapeHtml(outcomeEligibilityCohortRu(row.eligibility_cohort))}</span>` },
        { label: "Исходное направление алгоритма", render: row => renderDirectionBadge(row.raw_direction, row.bot_type) },
        { label: "Исполнимое направление", render: row => renderDirectionBadge(row.execution_direction, row.bot_type) },
        { label: "Строк", render: row => escapeHtml(String(row.total || 0)) },
        { label: "Доля успешных по контракту", render: row => row.win_rate == null ? "—" : escapeHtml(`${(Number(row.win_rate) * 100).toFixed(1)}%`) },
        { label: "Средний net результат", render: row => escapeHtml(fmtPct(row.avg_ret, 2)) },
        { label: "Временная структура", className: "wrap", render: row => renderSampleSizeBadge(row) },
      ], byBotCohort, { emptyText: "Исходы по стратегиям пока не накоплены." })}
      <p class="modal-note">Это единственная основная strategy-агрегация, и каждая строка относится только к одной mutually exclusive когорте допуска. Количество строк не называется количеством независимых испытаний: коррелированные символы и перекрывающиеся горизонты отдельно отражены в колонке временной структуры. Тип нейтрального сигнала доступен в расширенной диагностике, но не дублируется отдельной основной таблицей: прежние таблицы по исполнимому направлению, преобразованию направления, нейтральным подтипам и сырому тезису удалены как повторные срезы тех же строк. «Успех по контракту» и знак net P&amp;L — разные метрики. Для grid срабатывание kill-switch означает неуспех даже при положительном терминальном proxy P&amp;L; для trend TP/SL/HORIZON_EXIT и денежный результат также показываются раздельно.</p>
    </div>
    <div class="modal-section">
      <div class="modal-section-title">На что смотреть в первую очередь</div>
      ${renderOutcomeInsightCards(insights)}
    </div>
    <div class="modal-section">
      <div class="modal-section-title">LLM и решение алгоритма</div>
      ${buildModalTable([
        { label: "Направление алгоритма", render: row => renderDirectionBadge(row.engine_execution_direction) },
        { label: "Статус LLM", render: row => renderLlmStatusBadge(row.llm_status) },
        { label: "Совпадение", render: row => renderAgreementBadge(row.llm_alignment === "agree" ? true : row.llm_alignment === "disagree" ? false : null) },
        { label: "Условие допуска", render: row => `<span class="neutral-note">${escapeHtml(gateDecisionRu(row.llm_gate_decision || "pass"))}</span>` },
        { label: "Всего", render: row => escapeHtml(String(row.total)) },
        { label: "Доля успешных по контракту", render: row => escapeHtml(`${(Number(row.win_rate || 0) * 100).toFixed(1)}%`) },
        { label: "Средний net", render: row => escapeHtml(fmtPct(row.avg_ret, 2)) },
      ], llmByEngine, { emptyText: "В завершённых наблюдениях пока нет результатов проверки LLM." })}
    </div>
    ${renderModalDisclosure("Причины допуска и детальная LLM-матрица", `
      ${buildModalTable([
        { label: "Тип", render: row => `<span class="wrap">${escapeHtml(row.kind || "—")}</span>` },
        { label: "Код", className: "wrap", render: row => `<span class="wrap">${escapeHtml(row.kind === "Исключение из exact-policy" ? outcomeEligibilityReasonRu(row.code) : humanizeOperatorText(row.code || "—"))}</span>` },
        { label: "Наблюдений", render: row => escapeHtml(String(row.count || 0)) },
      ], eligibilityCodeRows, { emptyText: "Причины исключения и решения не сохранены." })}
      <div class="modal-section-title modal-subsection-title">Решение алгоритма → решение LLM</div>
      ${buildModalTable([
        { label: "Алгоритм", render: row => renderDirectionBadge(row.engine_execution_direction) },
        { label: "LLM", render: row => renderDirectionBadge(row.llm_execution_direction) },
        { label: "Совпадение", render: row => renderAgreementBadge(row.llm_alignment === "agree" ? true : row.llm_alignment === "disagree" ? false : null) },
        { label: "Статус", render: row => renderLlmStatusBadge(row.llm_status) },
        { label: "Всего", render: row => escapeHtml(String(row.total)) },
        { label: "Средний net", render: row => escapeHtml(fmtPct(row.avg_ret, 2)) },
      ], llmMatrix, { emptyText: "Сопоставление решений алгоритма и LLM пока не накоплено." })}
    `, { note: "Эти срезы нужны для расследования причин, но не должны занимать основной экран." })}
    ${renderModalDisclosure("По символам (топ 30)", `
      ${buildModalTable([
        { label: "Символ", render: row => `<span class="wrap">${escapeHtml(row.symbol || "—")}</span>` },
        { label: "Стратегия", render: row => `<span class="wrap">${escapeHtml(botTypeLabel(row.bot_type))}</span>` },
        { label: "Исходное направление алгоритма", render: row => renderDirectionBadge(row.raw_direction, row.bot_type) },
        { label: "Исполнимое направление", render: row => renderDirectionBadge(row.execution_direction, row.bot_type) },
        { label: "Всего", render: row => escapeHtml(String(row.total)) },
        { label: "Доля успешных по контракту", render: row => escapeHtml(`${(Number(row.win_rate || 0) * 100).toFixed(1)}%`) },
        { label: "Средний net", render: row => escapeHtml(fmtPct(row.avg_ret, 2)) },
        { label: "Временная структура", className: "wrap", render: row => renderSampleSizeBadge(row) },
      ], bySymbol, { emptyText: "Данные по инструментам пока отсутствуют." })}
    `)}
    ${renderModalDisclosure("Журнал текущего набора правил (последние 80)", `
      ${buildModalTable([
        { label: "Время", render: row => escapeHtml(formatTs(row.ts)) },
        { label: "Символ", render: row => `<span class="wrap">${escapeHtml(row.symbol || "—")}</span>` },
        { label: "Стратегия", render: row => `<span class="wrap">${escapeHtml(botTypeLabel(row.bot_type))}</span>` },
        { label: "Событие", render: row => `<span class="wrap">${escapeHtml(outcomeEventTypeRu(row.event_type))}</span>` },
        { label: "Исходное направление алгоритма", render: row => renderDirectionBadge(row.raw_direction, row.bot_type) },
        { label: "Исполнимое направление", render: row => renderDirectionBadge(row.execution_direction, row.bot_type) },
        { label: "Когорта", className: "wrap", render: row => `<span class="wrap">${escapeHtml(outcomeEligibilityCohortRu(row.eligibility?.cohort))}</span>` },
        { label: "Исход по контракту", render: row => renderOutcomeResult(row.success, row.outcome_diagnostics, row.event_type, row.bot_type) },
        { label: "Net proxy P&L", render: row => escapeHtml(renderOutcomeReturn(row.ret)) },
        { label: "Причина", className: "wrap", render: row => `<span class="wrap">${escapeHtml(outcomeReasonText(row))}</span>` },
        { label: "LLM", render: row => renderLlmStatusBadge(row.llm_review?.status || "none") },
        { label: "Rec ID", className: "wrap", render: row => `<span class="wrap">${escapeHtml(row.rec_id || "—")}</span>` },
      ], recent, { emptyText: "В текущем наборе правил завершённых наблюдений пока нет.", compact: true, maxHeight: 560 })}
    `)}
    ${renderModalDisclosure("Исторический архив (не входит в основную статистику)", `
      ${buildModalTable([
        { label: "Стратегия", render: row => `<span class="wrap">${escapeHtml(botTypeLabel(row.bot_type))}</span>` },
        { label: "Всего", render: row => escapeHtml(String(row.total || 0)) },
        { label: "Доля успешных по контракту", render: row => row.win_rate == null ? "—" : escapeHtml(`${(Number(row.win_rate) * 100).toFixed(1)}%`) },
        { label: "Средний net", render: row => escapeHtml(fmtPct(row.avg_ret, 2)) },
        { label: "События", className: "wrap", render: row => `<span class="wrap">${escapeHtml(row.event_summary || "—")}</span>` },
      ], archiveStrategyRows, { emptyText: "Архивные исходы по стратегиям отсутствуют." })}
      <div class="modal-section-title modal-subsection-title">Последние 20 архивных исходов</div>
      ${buildModalTable([
        { label: "Время", render: row => escapeHtml(formatTs(row.ts)) },
        { label: "Символ", render: row => `<span class="wrap">${escapeHtml(row.symbol || "—")}</span>` },
        { label: "Стратегия", render: row => `<span class="wrap">${escapeHtml(botTypeLabel(row.bot_type))}</span>` },
        { label: "Событие", render: row => `<span class="wrap">${escapeHtml(outcomeEventTypeRu(row.event_type))}</span>` },
        { label: "Исход по контракту", render: row => renderOutcomeResult(row.success, row.outcome_diagnostics, row.event_type, row.bot_type) },
        { label: "Net proxy P&L", render: row => escapeHtml(renderOutcomeReturn(row.ret)) },
        { label: "Причина", className: "wrap", render: row => `<span class="wrap">${escapeHtml(outcomeReasonText(row))}</span>` },
        { label: "Rec ID", className: "wrap", render: row => `<span class="wrap">${escapeHtml(row.rec_id || "—")}</span>` },
      ], archiveRecent, { emptyText: "Исторический архив пуст.", compact: true, maxHeight: 420 })}
    `)}
  `;

  showModalHtml("Результаты наблюдений", html, { wide: true });
}

async function loadDecisions() {
  const res = await fetch("/api/v1/decisions?limit=200");
  let data;
  try { data = await res.json(); } catch (e) { return; }
  if (!res.ok) { showModal("Ошибка загрузки журнала", data); return; }
  const rows = Array.isArray(data) ? data : [];
  const linked = rows.filter(row => Boolean(row?.rec_id)).length;
  const system = rows.length - linked;
  const alerts = rows.filter(row => journalActionTone(row?.action) === "bad").length;
  const html = `
    ${renderModalSummaryCards([
      { label: "Записей", value: rows.length },
      { label: "Связано с рекомендациями", value: linked },
      { label: "Общесистемных", value: system },
      { label: "Ошибок и блокировок", value: alerts },
    ])}
    <p class="modal-note">Каждая запись показана отдельной карточкой. Основной контекст виден сразу, а полный структурированный payload раскрывается по запросу и не сжимает остальные поля в одну узкую строку.</p>
    ${renderDecisionJournal(rows)}
  `;
  showModalHtml("Журнал решений", html, { wide: true });
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
  $("refreshCountdown").textContent = `↻ ${countdownVal}с`;
  countdownTimer = setInterval(() => {
    countdownVal--;
    if (countdownVal <= 0) {
      $("refreshCountdown").textContent = "↻ …";
    } else {
      $("refreshCountdown").textContent = `↻ ${countdownVal}с`;
    }
  }, 1000);
}

// ── main refresh ──────────────────────────────────────────────────────────────

function refreshAll() {
  if (refreshInFlight) return refreshInFlight;
  refreshInFlight = (async () => {
    await loadStatus();
    await loadRecommendations();
    await refreshCurrentDetails();
    startCountdown();
  })();
  return refreshInFlight.finally(() => {
    refreshInFlight = null;
  });
}

// ── events ────────────────────────────────────────────────────────────────────

document.addEventListener("click", async (e) => {
  const t = e.target;
  if (!t || !t.dataset) return;
  const act = t.dataset.act;
  const id  = t.dataset.id;

  if (act === "details") await loadDetails(id);

  if (act === "show-recommendation-history") {
    await loadRecommendationHistory({
      venue: t.dataset.venue || currentMeta?.venue || "linear",
      symbol: t.dataset.symbol || currentMeta?.symbol || "",
      bot_type: t.dataset.botType || currentMeta?.bot_type || "futures_grid",
    });
    return;
  }

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

  if (act === "copy-health-diagnostics") {
    if (!lastHealthDiagnostics) return;
    const txt = JSON.stringify(lastHealthDiagnostics, null, 2);
    try {
      await navigator.clipboard.writeText(txt);
      const old = t.textContent;
      t.textContent = "Диагностика скопирована";
      setTimeout(() => { t.textContent = old; }, 1500);
    } catch (err) {
      showRawTechnicalModal("Диагностика системы", lastHealthDiagnostics);
    }
    return;
  }

  if (act === "download-health-diagnostics") {
    if (!lastHealthDiagnostics) return;
    const blob = new Blob([JSON.stringify(lastHealthDiagnostics, null, 2)], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    link.href = url;
    link.download = `bybit-recommender-diagnostics-${stamp}.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    return;
  }

  if (act === "show-tech") {
    const tech = $("details").dataset.tech;
    if (tech) showRawTechnicalModal("Технические данные", tech);
    return;
  }

  if (act === "json") {
    let data;
    try {
      const res = await fetch(`/api/v1/recommendations/${id}`);
      data = await res.json();
    } catch (e) { return; }
    showRawTechnicalModal("Технические данные рекомендации", data);
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
              `<span class="op-status-label ${statusClass}">${escapeHtml(operatorStatusRu(action))}</span>`;
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
    sortDir = ["plan_rr", "empirical_expectancy", "risk_buffer"].includes(col) ? "desc" : "asc";
  }
  if (lastItems.length) renderRecoTable(lastItems);
});

$("refreshDetailsBtn").addEventListener("click", () => {
  refreshCurrentDetails();
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

RECO_FILTER_IDS.forEach(id => {
  const el = $(id);
  if (el) el.addEventListener("change", () => {
    persistRecommendationFilterState();
    refreshAll();
  });
});

// Keyboard: Escape closes every open dialog; R refreshes outside text controls.
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    e.preventDefault();
    closeAllDialogs();
    return;
  }
  const active = document.activeElement;
  const activeTag = String(active?.tagName || "").toUpperCase();
  const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(activeTag) || Boolean(active?.isContentEditable);
  if (e.key.toLowerCase() === "r" && !e.ctrlKey && !e.metaKey && !typing) {
    refreshAll();
  }
});

// ── boot ──────────────────────────────────────────────────────────────────────

restoreRecommendationFilterState();
refreshAll();
setInterval(refreshAll, 10000);

const adminApiKeyEl = $("adminApiKey");
if (adminApiKeyEl) {
  adminApiKeyEl.value = "";
}
