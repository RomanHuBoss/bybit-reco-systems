# Changelog

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
