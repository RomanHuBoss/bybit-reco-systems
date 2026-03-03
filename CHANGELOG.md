## V3.8 — UI fixes, calibrator periodic re-fit, direction range fix, bug sweep

### 1. Периодическая рекалибровка (calibration.py + recommender.py) — критично

**Проблема**: калибраторы обучались ровно один раз при первом запуске и далее не
обновлялись, даже когда накапливались новые исходы. Модель деградировала со временем.

**Решение**:
- В `PlattScaler` добавлено поле `saved_ts` — unix-метка последнего обучения.
- `save_platt_to_db` сохраняет `ts` в JSON; `load_platt_from_db` читает его обратно.
- Все три загрузчика (`_load_or_fit_calibrator`, `_load_or_fit_direction_calibrator`,
  `_load_bot_calibrators`) проверяют возраст: если `age >= CALIB_REFIT_INTERVAL_SEC=3600` —
  принудительный рефит, результат сохраняется в БД.
- Fallback: если рефит не удался (мало данных) — продолжает использовать стale-версию
  вместо деградации до не-откалиброванного состояния.
- Старые записи в БД без поля `ts` получат `saved_ts=0` → немедленный рефит при апгрейде.

### 2. Инверсия диапазона Grid-бота (recommender.py) — критично

**Проблема**: при направлении `long` диапазон смещался **вниз** (lower_mul=1.20,
upper_mul=0.80), а не вверх. Оператор получал перекошенный диапазон в обратную сторону.

**Исправление**:
- `long`:  `lower_mul=0.80, upper_mul=1.20` → диапазон смещён **вверх** (больше пространства над ценой)
- `short`: `lower_mul=1.20, upper_mul=0.80` → диапазон смещён **вниз**

### 3. Overflow защита в Platt predict (calibration.py)

Для экстремальных значений `score` (например, после апгрейда с другой нормировкой)
`math.exp(-z)` мог выбросить `OverflowError`. Добавлен clamp `z ∈ [-500, +500]`
в `PlattScaler.predict()` и в цикле градиентного спуска `fit_platt()`.

### 4. UI — Layout панели управления (styles.css + index.html)

**Проблема**: `grid-template-columns: 1.1fr 0.7fr 0.8fr 1.6fr auto` растягивал поля
ввода на всю ширину, кнопки наезжали на поле «Мин. уверенность».

**Решение**: controls переведён на `flexbox` с фиксированными размерами:
- Площадка: `140px`, Top N: `90px`, Мин. уверенность: `130px`
- Кнопки вынесены в `div.btn-row { display: flex; gap: 6px }`
- Поля ввода получили `box-sizing: border-box` (padding больше не распирает ширину)
- Responsive breakpoint: `@media (max-width: 900px)` — каждый контрол на отдельной строке

### 5. UI — Кнопка «Обновить» в панели Детали (index.html + app.js)

Добавлена кнопка **Обновить** рядом с «Скопировать параметры». Хранит `currentRecId`
и при клике вызывает `loadDetails(currentRecId)` — частичное обновление без перегрузки
всей таблицы рекомендаций.

### 6. UI — Кнопки ✓/✗ больше не вызывают мерцание (app.js)

**Проблема**: нажатие ✓/✗ вызывало `refreshAll()`, который делал `body.innerHTML = ""`
→ строка пропадала, а если рекомендер успевал дать новый цикл для того же символа — 
появлялась снова.

**Решение**: DOM обновляется **in-place**:
1. Строка тускнеет (`opacity: 0.45`) и теряет класс `row-recommended`
2. Ячейка статуса заменяется меткой `executed`/`ignored` без пересборки таблицы
3. Кнопки ✓/✗ удаляются из `<td>`, кнопки «Детали» и «JSON» остаются
4. `refreshAll()` больше НЕ вызывается — строка исчезнет сама на следующем авто-обновлении

### 7. UI — Модальные окна (index.html + styles.css + app.js)

- Кнопка «Close» → **«Закрыть»**
- Заголовок «Risk status» → **«Статус рисков»**
- Скроллируется только контентная область: `.modal-card` теперь `display:flex; flex-direction:column; overflow:hidden`, отдельный `div.modal-scroll { overflow-y:auto; flex:1 }` — заголовок с кнопкой закрытия остаётся видимым при прокрутке длинных журналов

### 8. Исправлены баги JS (app.js)

**Variable shadowing в `loadHealth()`**: `const s = data.summary` перекрывалась в
`symbols.filter(s => ...)` и `bad.forEach(s => ...)`. Переименовано: `sum` (summary),
`sym` (per-symbol item). Дополнительно исправлен edge-case: `🟢` не присваивается
если `total === 0` (нет данных вообще).

**`showModal()` теперь принимает строку напрямую**: функция проверяет `typeof obj === "string"`
— избавлено от двойного вызова (передача `{_text: lines.join("\n")}` + `modalBody.textContent = ...`).

**Обработка ошибок в `loadDetails()`**: добавлены `try/catch` с понятным сообщением
пользователю при HTTP-ошибке (404) или сетевой ошибке.

### 9. Улучшение `fit_platt` (calibration.py)

- Итераций: `300` (было `250`) — лучшая сходимость
- Learning rate: `0.06` (было `0.08`) — стабильнее на зашумлённых данных
- Чуть меньше вероятность overshooting в области малой выборки

# Changelog

## V3.7 — Health screen, per-bot horizons, calibrator per bot_type, risk gate, Telegram alerts

### 1. Health-экран символов (кнопка "Здоровье")

GET /api/v1/health/symbols
  summary: {ok, stale, missing, errors_10m}
  symbols: [{venue, symbol, last_candle_ts, age_sec, status, error_count_10m, stale_skips_1h, disabled}]
  Отсортировано: missing → stale → ok

UI: кнопка "Здоровье" открывает модал.
  🔴 missing (нет данных вообще)
  🟠 stale (данные старше STALE_DATA_MAX_SEC)
  🟢 ok
  Показывает ошибки за 10 мин, skip-счётчик за 1ч, флаг DISABLED.

db.py: get_symbol_health(conn, symbols_spot, symbols_linear, stale_sec)

### 2. Горизонт исходов по bot_type (outcomes.py)

Было: HORIZON_SEC_DEFAULT = 1800 (30 мин) для всех.
Стало (BOT_HORIZONS):
  spot_grid / futures_grid:  4h  (grid живёт часами)
  dca_bot:                  24h  (DCA накапливает весь день)
  futures_martingale:         1h  (разрешается быстро)
  futures_combo:              2h

### 3. Calibrator per bot_type (recommender.py + calibration.py)

_fit_bot_calibrators(): один Platt на каждый bot_type, хранится в app_config.
  Ключи: platt_spot_grid_v1, platt_futures_grid_v1, platt_dca_v1, platt_martingale_v1, platt_combo_v1

При расчёте confidence:
  bot_cal (per-bot) → приоритет над global calibrator
  Fallback: global calibrator → conf0

### 4. Risk gate подключён в рекомендер

risk.gate_candidate() теперь вызывается для каждого символа/бота.
Блоки добавляются в feasibility_blocks:
  MAX_CONCURRENT_BOTS, MAX_DD_DAY, COOLDOWN_ACTIVE, MAX_SYMBOL_BOTS

### 5. Telegram алерты (alerts.py)

Настройка: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID в .env (пустые = выключено).
Алерты с cooldown 10 мин:
  ⚡ collect_errors ≥ 5 за 10 мин
  🔴 ≥ 50% символов stale/missing
  ⚠ 0 рекомендаций в цикле

Вызывается в конце каждого цикла рекомендера.

## V3.6 — Operator actions, TTL expiry, dynamic regime confidence, outcomes screen, copy params

### 1. Кнопки оператора (UI + API)

Каждая `recommended` рекомендация теперь имеет кнопки:
  ✓ — отметить исполненной → status = "executed"
  ✗ — проигнорировать     → status = "ignored"

После действия строка показывает метку вместо кнопок:
  executed (зелёный) / ignored (серый) / expired (тёмный)

Новый эндпоинт: `POST /api/v1/recommendations/{rec_id}/action`
  Body: {"action": "executed"|"ignored", "operator": "ui"}
  Логируется в decision_log как STATUS_UPDATE.

### 2. TTL-инвалидация рекомендаций

db.expire_stale_recommendations():
  UPDATE recommendations SET status='expired'
  WHERE status='recommended' AND (ts + ttl_sec) < now

Вызывается автоматически в конце каждого цикла рекомендера.
Логируется как TTL_EXPIRED {count, ts}.
Оператор-установленные статусы (executed/ignored) не затрагиваются.

### 3. Динамический Regime confidence (regime.py)

Раньше: hardcoded 0.65.
Теперь: confidence = 0.45 + 0.40 × agreement + sample_bonus
  agreement = 1 - 0.5×CV(atr) - 0.5×CV(trend)
  CV = coefficient of variation (std/mean) — мера разброса между символами
  sample_bonus = min(0.10, (n-1) × 0.005)
  Диапазон: [0.20, 0.95]

В ответ добавлено confidence_detail: {agreement, cv_atr, cv_trend, n_symbols}

### 4. Экран исходов (кнопка "Исходы")

GET /api/v1/outcomes/stats
  summary: {total, wins, win_rate}
  by_bot:  [{bot_type, direction, total, wins, win_rate, avg_ret, avg_abs_ret}]
  by_symbol: [{symbol, bot_type, total, wins, win_rate, avg_ret}]

UI: кнопка "Исходы" открывает модал с таблицами в текстовом формате.
Если данных нет — пишет "появятся через ~15 мин после первых рекомендаций".

### 5. Кнопка "Скопировать параметры" (UI)

В панели деталей появляется кнопка "Скопировать параметры" после выбора рекомендации.
Копирует params JSON в буфер обмена — готово для вставки в Bybit UI.
После копирования: "✓ Скопировано" на 2 секунды.

## V3.5 — Stale gate, outcomes fix, BTC beta

### Fix 1: Stale data gate (recommender.py + settings.py)

Если коллектор упал и данные по символу устарели — рекомендер
молча строил решение на старых числах. Теперь:

  if ts_now - f["ts_last"] > STALE_DATA_MAX_SEC: skip + log STALE_DATA_SKIP

Порог: `STALE_DATA_MAX_SEC=300` (5 мин) в `.env`.
Пропущенные символы видны в журнале как `STALE_DATA_SKIP`.

### Fix 2: Outcomes — неверные лейблы для grid (outcomes.py)

Старый код: `success = 1 if price_moved_in_direction else 0`
Это верно для DCA/Martingale, но grid-бот зарабатывает на флете,
а не на направленном движении. Калибратор обучался на мусорных лейблах.

Новая логика:
  Grid (spot_grid, futures_grid):
    success = цена вышла из горизонта внутри рекомендованного диапазона
              И не выбивала wick за range_lower/upper × 0.995/1.005
    Fallback (если диапазон не записан): |ret| < 1.5% за горизонт = grid-friendly

  Directional (dca_bot, futures_martingale, futures_combo):
    success = ret > 0 в направлении (без изменений, это верно)

Дополнительно: `_get_price_range_in_window()` — проверяет min/max
за весь горизонт по 1m свечам, не только exit-цену.

### Fix 3: BTC beta / correlation (features.py + recommender.py)

`btc_beta(symbol_closes, btc_closes, window=24)`:
  - Pearson correlation r за 24 × 1h log-returns
  - Beta (slope symbol/BTC)
  - is_btc_driven: |r| > 0.80 — сигнал отражает BTC, не актив
  - independent_signal: |r| < 0.50 — актив торгуется самостоятельно

В рекомендере:
  - BTC 1h closes загружаются один раз на цикл
  - Для каждого символа (кроме BTC) считается beta за 24h
  - Если is_btc_driven: dir_conf × 0.88 (направление менее независимо)
  - reasons.btc_beta: {correlation, beta, is_btc_driven, independent_signal}

В UI деталей:
  🔗 r=0.91 β=1.3 — сигнал отражает BTC, не сам актив
  🆓 r=0.32 β=0.4 — независимый сигнал
  〰 r=0.65 β=0.9 — частичная корреляция

## V3.4 — Funding rate, Open Interest, Liquidity tier

### Новые данные

**Funding rate (Bybit /v5/market/tickers)**
- Собирается каждый цикл для всех linear символов
- `funding_signal()`: bullish < -0.01% / bearish > 0.03% / neutral
- В скоринге: bullish → +0.04, bearish → -0.06
- Гейт FUNDING_EXTREME: > 0.06%/8h → блок рекомендации

**Open Interest (Bybit /v5/market/open-interest)**
- 48 × 1h свечей, upsert в таблицу `open_interest`
- `oi_trend()`: Δ4h, Δ24h, trend=growing/falling/stable
- Сигнал комбинируется с price direction:
  price up + OI growing = bullish, price down + OI growing = bearish
- OI caution (unwinding) → conf × 0.88

**Ликвидность / тир**
- `liquidity_tier(turnover24h_usd)`: high/medium/low/micro
- Гейты:
  micro (< $500K/day) → LIQUIDITY_TOO_LOW: grid запрещён
  low (< $2M/day) + futures_martingale/combo → LIQUIDITY_LOW_FUTURES: запрещён

### Новые таблицы (migrations/init.sql)
- `funding_rate(symbol, ts, funding_rate, next_funding_ts)`
- `open_interest(symbol, ts, oi)`

### ⚠ Требуется пересоздание БД или применение миграции вручную:
```sql
CREATE TABLE IF NOT EXISTS funding_rate (
  symbol TEXT NOT NULL, ts INTEGER NOT NULL,
  funding_rate REAL NOT NULL, next_funding_ts INTEGER,
  PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_funding_ts ON funding_rate(ts DESC);

CREATE TABLE IF NOT EXISTS open_interest (
  symbol TEXT NOT NULL, ts INTEGER NOT NULL, oi REAL NOT NULL,
  PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_oi_ts ON open_interest(ts DESC);
```

### UI (Детали)
Добавлены блоки: Ликвидность / Funding Rate / Open Interest с emoji-индикаторами.

## V3.3 — Per-symbol sentiment: RSS + Reddit + CoinGecko trending + momentum

### Проблема
Сентимент был один на всех — `scope="global", key="crypto"`.
XAUTUSDT, HYPEUSDT, PEPE получали одинаковый сигнал несмотря на разные драйверы.

### Новые источники (все бесплатные, без регистрации)

**1. RSS per-symbol (существующие CoinDesk + CoinTelegraph)**
   Заголовки фильтруются по словарю SYMBOL_KEYWORDS для 30 символов.
   Упоминание → score идёт в scope="symbol", key="BTCUSDT" и т.д.

**2. Reddit per-symbol (BTC/ETH/SOL/XRP/DOGE)**
   JSON-фид `/r/{coin}/hot.json?limit=25` — публичный, без auth.
   Сигнал: 60% text sentiment + 40% upvote_ratio → [-1, 1].

**3. CoinGecko trending**
   `/api/v3/search/trending` — топ-7 трендовых монет.
   Попадание в топ → +0.6 sentiment для символа.

**4. CoinGecko price momentum (авторское дополнение)**
   `/api/v3/coins/markets` — 24h + 7d изменение цены → нормировано в [-1, 1].
   Формула: 0.6×clamp(Δ24h/10%, -1,1) + 0.4×clamp(Δ7d/20%, -1,1)
   Самый надёжный сигнал: рыночные данные в реальном времени.

### Веса блендинга per-symbol

| Источник              | Вес  |
|-----------------------|------|
| coingecko_momentum    | 0.45 |
| reddit                | 0.30 |
| news_rss              | 0.15 |
| coingecko_trending    | 0.10 |

### Использование в рекомендере

При наличии per-symbol данных:
  effective_sent = 0.5 × global_sent + 0.5 × symbol_sent

При отсутствии:
  effective_sent = global_sent (без деградации)

Все feasibility checks (MARTINGALE_BLOCKED, DCA_BLOCKED_PANIC) и скоринг
теперь используют effective_sent. В reasons добавлен блок symbol_sentiment
с полями value/effective/global/blended.

### Новые функции

- sentiment.py: полностью переписан, 8 модульных функций
- sentiment_features.py: `compute_symbol_sentiment_map(conn)` → {SYMBOL: float}
- recommender.py: `symbol_sent_map`, `effective_sent`, `reasons.symbol_sentiment`

## V3.2.1 — Hotfix: symbol auto-disable не работал

### Root cause

`_is_not_supported_symbol()` проверял только строку `"Not supported symbols"`,
но Bybit для pre-market / delisted символов возвращает `"params error: symbol invalid"`.
Строки не совпадали → `raise` → символ никогда не попадал в `_DISABLED_SYMBOLS`
→ ошибка повторялась каждый цикл несмотря на рестарты.

Дополнительно: `COLLECT_ERROR` не логировал имя символа — невозможно было найти виновника.

### Fixes (collector.py)

1. `_is_not_supported_symbol`: расширен список совпадений:
   `"Not supported symbols"` | `"symbol invalid"` | `"Symbol invalid"`
   Теперь любой вариант ошибки 10001 на символ → auto-disable

2. Per-symbol ошибки: `raise` → `log + continue`
   Один плохой символ больше не роняет весь цикл сбора.
   `COLLECT_ERROR` теперь содержит поле `"symbol"` для диагностики.

3. Outer handler в main.py: добавлен `"symbol": "UNKNOWN"` — маркер
   неожиданной ошибки уровня коллектора (не per-symbol).

### После деплоя

- На первом цикле проблемный символ получит `SYMBOL_DISABLED` в журнале
  с его именем и причиной — станет виден в логе.
- Повторных `COLLECT_ERROR` по этому символу не будет.

## V3.2 — UI overhaul + /api/v1/status + audit

### Backend

**Новый эндпоинт `/api/v1/status`**
- `calibrator_fitted` + параметры Platt (a, b)
- `outcome_count` / `calib_min_samples` — прогресс обучения
- `collect_errors_10m` — счётчик ошибок сбора за 10 мин
- `sentiment` — ewma_6h, regime, flags (panic/euphoria)
- `last_reco_ts` — время последнего цикла рекомендаций

### UI — новые компоненты

**Баннер некалиброванной уверенности**
Пока `calibrator_fitted=false`: показывается предупреждение с прогресс-баром
"Накоплено N / 80 исходов". Исчезает автоматически после обучения.
Заголовок колонки Увер меняется на "Увер ⚠" / "Увер ✓".

**Индикатор сентимента в шапке**
`Сент: +0.13 (risk_on)` — цвет зелёный/красный/серый по знаку.
Флаги panic 🚨 / euphoria 🔥 показываются рядом.

**Баннер ошибок коллектора**
Если за последние 10 мин есть COLLECT_ERROR — показывается
"⚡ N ошибок сбора за 10 мин" со ссылкой на журнал.

**Direction confidence в таблице**
Новая колонка "Уд.напр" — `direction_confidence_calibrated`.
Цвет: зелёный ≥0.75 / жёлтый ≥0.55 / красный <0.55.

**Цветовая шкала уверенности**
- Не откалибровано: серый курсив + ⚠ у каждого числа
- Откалибровано: зелёный ≥0.75 / жёлтый ≥0.60 / красный <0.60

**Счётчик авто-обновления**
"↻ 10s" в шапке. Цикл: 10 сек (было 5 сек, снижена нагрузка).

**Детали — расширены**
Добавлены блоки: направление (scores/coherence/veto), сентимент по горизонтам,
статус калибровки (fitted + a/b). Форматирование по секциям.

### Исправлено (аудит)

- `minConf` дефолт в HTML: 0.2 → 0.52 (согласовано с settings.py)
- `topN` дефолт: 200 → 50 (31 символ × 5 ботов = max 155)
- Авто-обновление: 5s → 10s (меньше нагрузка на БД)

### Выводы аудита (не код)

**Отсутствует (TODO):**
- Экран истории исходов — нельзя увидеть как рекомендации отработали
- Дашборд здоровья символов — какие собирают данные, какие падают
- UI отключения символа без правки .env + рестарта

**Лишнее:**
- `snapshot` параметр в API (есть, в UI не используется — dead feature)

## V3.1.1 — Hotfix: Invalid period + log analysis

### Баг из журнала (COLLECT_ERROR: Invalid period!)

**🔴 collector.py — неверный интервал `"1440"` для Bybit v5 API**
Bybit v5 kline endpoint поддерживает: `1, 3, 5, 15, 30, 60, 120, 240, 360, 720, D, W, M`
Значение `"1440"` не входит в список допустимых → `retCode 10001: Invalid period!`
Исправлено: `"1440"` → `"D"` (дневная свеча), `tf_sec` остаётся `86400`.

Ошибки в журнале до фикса:
- `COLLECT_ERROR spot: Bybit error 10001: Invalid period!` — каждые 20 сек по всем символам
- `COLLECT_ERROR linear: Bybit error 10001: symbol invalid` — ROBOUSDT pre-market (фикс в v3.1)

## V3.1 — Code Review + Symbol Verification

### Баги, найденные при ревью

**🔴 recommender.py — двойной append у мартингейла (критический баг)**
```python
# БЫЛО: при срабатывании условия добавлялось ДВА блока вместо одного
feasibility_blocks.append({"code": "DIR_CONF_TOO_LOW" ...})  # первый
# skip further checks... (комментарий без continue!)
feasibility_blocks.append({"code": "MARTINGALE_BLOCKED" ...})  # всегда выполнялся!
```
Мартингейл всегда блокировался двойным блоком — один из которых был дублем с неверным кодом.
Исправлено: одна ветка с правильным кодом (`DIR_CONF_TOO_LOW` или `MARTINGALE_BLOCKED`).

**🔴 recommender.py — мёртвый код в `_direction()` (dead code)**
После `return "neutral"` находился блок из 14 строк `if bot_type in ("spot_grid"...):`
— недостижимый код из старой версии. Полностью удалён.

**🟡 settings.py — коллега откатил улучшения v3.0**
| Параметр | v3.0 | Откат коллеги | v3.1 (восстановлено) |
|---|---|---|---|
| `REQUIRE_CONF_GATE` | `"1"` | `"0"` | `"1"` |
| `MIN_SCORE_TO_RECOMMEND` | `0.08` | `0.05` | `0.08` |
| `MIN_CONF_TO_RECOMMEND` | `0.52` | `0.20` | `0.52` |

**🟡 recommender.py — логика confidence (добавлено коллегой)**
Блендинг `0.5*conf_raw + 0.5*conf_cal` — корректное консервативное решение, сохранено.

### Верификация символов с Bybit API (Live, 01.03.2026)

Все символы проверены через официальные анонсы Bybit.

**🔴 XRPUSDT — дубликат в обоих списках**
Был указан дважды (`SYMBOLS_SPOT` и `SYMBOLS_LINEAR`). Дубликат удалён.

**🔴 ROBOUSDT — исключён из SYMBOLS_LINEAR**
ROBOUSDT был добавлен в Perpetual **Pre-Market** на Bybit Feb 25, 2026 (`isPreListing=true`).
Pre-market контракты не отдают kline данные через стандартный API → ошибка "Not supported symbols".
Остаётся в SYMBOLS_SPOT (spot листинг от Feb 27, 2025 — стабильный).
Вернуть в LINEAR когда Bybit переведёт в стандартный режим (`isPreListing=false`).

**✅ Все остальные символы подтверждены:**
| Символ | Spot | Linear | Источник |
|---|---|---|---|
| MONUSDT | ✅ | ✅ | Spot Nov 24, Perp Nov 25, 2025 (50x) |
| BIRBUSDT | ✅ | ✅ | Spot + Innovation Zone Perp Jan 28, 2026 |
| VIRTUALUSDT | ✅ | ✅ | Spot + Innovation Zone Perp Nov 2024 |
| ZROUSDT | ✅ | ✅ | Spot + Perp Jun 2024 (25x) |
| PUMPUSDT | ✅ | ✅ | Spot + Perp подтверждён |
| ASTERUSDT | ✅ | ✅ | Spot + Pre-market Sep 2025 → regular |
| BARDUSDT | ✅ | ✅ | Spot + Pre-market Sep 2025 → regular |
| GRASSUSDT | ✅ | ✅ | Perp Oct 2024 → regular |
| HYPEUSDT | ✅ | ✅ | Perp Dec 2024 (Hyperliquid) |
| XAUTUSDT | ✅ | ✅ | Spot + Perp (Tether Gold) |

## V3.0 — Rational Confidence Fix

**Проблема**: низкая уверенность (conf ~0.45–0.52) блокировала почти все рекомендации.

**Причины (root causes)**:
1. Cost penalty в `_score()` вычитал 0.5–1.0 из каждого raw score (`rule - 0.7 * cost_bps/30`)
2. Нормализация `/ 2.2` дополнительно сжимала score до ≈0
3. `sigmoid(raw ≈ 0)` → conf ≈ 0.50 всегда — нет дискриминации
4. Direction confidence: base `0.45+…` давала минимум 0.57 даже без сигнала (ложная уверенность)

**Исправления `direction.py`**:
- Slope sensitivity: `slope_norm * 0.22` (было 0.15) — MA slope надёжнее
- Добавлен 4-й индикатор: **Bollinger Band %B** (позиция цены в BB, вес 0.08) — улучшает контекст тренда vs флет
- MACD нормализация: `* 900` (было 800) + RSI нормализатор `/ 30` (было 35) — более острые сигналы
- Базовая уверенность: `0.30 + 0.52 * strength_all + 0.18 * coherence` (было `0.45+0.35+0.20`)
  → динамический диапазон 0.30–0.99 вместо 0.55–0.99
- Threshold для тренда: `trendiness >= 0.48` (было 0.55) — раньше обнаруживаем тренд
- Sign threshold для когерентности: `0.10` (было 0.12)
- Trend regime bonus: +0.08 (было +0.05), range penalty: -0.08 (было -0.10)

**Исправления `recommender.py`**:
- Cost penalty: `0.35 * cost_bps/50` (было `0.70 * cost_bps/30`) — в 4× мягче
- Score нормализация: `/ 1.5` (было `/ 2.2`) — score поляризован
- Confidence: `sigmoid(raw * 2.5)` (было `sigmoid(raw)`) — conf теперь в диапазоне [0.1, 0.9]
- Мартингейл: добавлен бонус `0.3 * dir_coherence * dir_strength` — вознаграждает ситуации с чётким направлением

**Исправления `settings.py`**:
- `REQUIRE_CONF_GATE` default: `"1"` (было `"0"`) — фильтр уверенности включён по умолчанию
- `MIN_SCORE_TO_RECOMMEND` default: `0.08` (было `0.0`) — отсекаем случайный шум
- `MIN_CONF_TO_RECOMMEND` default: `0.52` (было `0.30`) — осмысленный порог

**Итог (типичный flat market, spot_grid)**:
- raw_old = -0.04 → conf_old = 0.49 → `no_trade`
- raw_new = +0.25 → conf_new = 0.65 → `recommended`

## V2.9

- Soft multi-timeframe direction aggregation (15m..1d)
- Coherence + structural veto
- Tactical vs structural scores
- Separate direction-confidence calibration persisted in SQLite
- Multi-horizon sentiment EWMA voting (1h/6h/1d/7d) with risk_on/off/neutral
