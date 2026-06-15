#!/usr/bin/env python3
"""
Диагностика: почему нет ни одной "зелёной" (recommended) рекомендации.

Читает ТУ ЖЕ базу, что и сервис (sqlite или postgres — определяется так же,
как в app.settings), берёт последний цикл рекомендаций и агрегирует:
  - распределение статусов,
  - какие block-коды сработали (blocks_json),
  - какие no_trade-причины сработали (reasons_json / params.risk_report),
  - на каком слое идея умирает (thesis vs execution),
  - сводку по leverage_policy (3-5x профиль).

Запуск из корня проекта:
    python scripts_diag_why_no_green.py
    python scripts_diag_why_no_green.py --since-min 180   # окно вместо последнего цикла
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

from app.settings import load_settings
from app import db


def _loads(x, default):
    if isinstance(x, (dict, list)):
        return x
    try:
        v = json.loads(x or "")
        return v if isinstance(v, type(default)) else default
    except Exception:
        return default


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-min", type=int, default=0,
                    help="смотреть рекомендации за последние N минут; 0 = только последний цикл")
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    s = load_settings()
    conn = db.connect(s.db_path)
    print(f"DB: {s.db_path}")
    print(f"Профиль: min_score={s.min_score_to_recommend}  min_conf={s.min_conf_to_recommend}  "
          f"require_conf_gate={s.require_conf_gate}\n")

    # окно отбора
    if args.since_min > 0:
        cutoff = db.now_ts() - args.since_min * 60
        where = "WHERE ts >= ?"
        params = (cutoff,)
        scope = f"последние {args.since_min} мин"
    else:
        row = conn.execute("SELECT MAX(ts) AS m FROM recommendations").fetchone()
        last_ts = (row["m"] if row and row["m"] is not None else 0)
        # цикл = всё, что записано в пределах 90 секунд от последней записи
        where = "WHERE ts >= ?"
        params = (int(last_ts) - 90,)
        scope = f"последний цикл (ts≈{last_ts})"

    rows = conn.execute(
        f"SELECT rec_id, ts, symbol, status, score, confidence, "
        f"params_json, reasons_json, blocks_json FROM recommendations {where} "
        f"ORDER BY ts DESC", params,
    ).fetchall()

    print(f"Область: {scope} — строк: {len(rows)}\n")
    if not rows:
        print("Пусто. Либо коллектор/генератор не пишет (см. ниже), либо неверная БД.")
        return 0

    status_c = Counter()
    block_c = Counter()
    notrade_c = Counter()
    layer_c = Counter()
    lev_note_c = Counter()
    lev_approved = Counter()
    score_below = conf_below = 0

    for r in rows:
        status = str(r["status"] or "").lower()
        status_c[status] += 1

        params = _loads(r["params_json"], {})
        reasons = _loads(r["reasons_json"], {})
        blocks = _loads(r["blocks_json"], [])

        for b in blocks:
            if isinstance(b, dict):
                block_c[str(b.get("code") or "?")] += 1

        rr = params.get("risk_report") if isinstance(params, dict) else {}
        for code in (rr.get("rejection_reasons") or []):
            block_c[str(code).split(":")[0][:48]] += 1

        dl = reasons.get("decision_layers") if isinstance(reasons, dict) else {}
        for nt in (dl.get("no_trade_reasons") or []):
            if isinstance(nt, dict):
                notrade_c[str(nt.get("code") or "?")] += 1
        if isinstance(dl, dict):
            layer_c[f"thesis={dl.get('thesis_status')} / exec={dl.get('execution_status')}"] += 1

        lp = params.get("leverage_policy") if isinstance(params, dict) else {}
        if isinstance(lp, dict) and lp:
            lev_note_c[str(lp.get("not_actionable_reason") or lp.get("note") or "?")] += 1
            lev_approved[str(lp.get("operator_minimum_approved"))] += 1

        try:
            if float(r["score"]) < s.min_score_to_recommend:
                score_below += 1
        except Exception:
            pass
        try:
            if s.require_conf_gate and float(r["confidence"]) < s.min_conf_to_recommend:
                conf_below += 1
        except Exception:
            pass

    def dump(title, c):
        print(f"== {title} ==")
        if not c:
            print("  (нет)")
        for k, v in c.most_common(args.top):
            print(f"  {v:5d}  {k}")
        print()

    dump("СТАТУСЫ", status_c)
    dump("BLOCK-коды (→ status=blocked)", block_c)
    dump("NO_TRADE причины (→ status=no_trade)", notrade_c)
    dump("Слой принятия решения", layer_c)
    dump("leverage_policy.note", lev_note_c)
    dump("leverage_policy.operator_minimum_approved", lev_approved)

    print("== Пороговые срезы ==")
    print(f"  score  < {s.min_score_to_recommend}: {score_below} строк")
    print(f"  conf   < {s.min_conf_to_recommend} (с активным conf-gate): {conf_below} строк")
    print()

    # быстрая проверка глобальных риск-гейтов и свежести данных
    print("== Глобальные риск-гейты (могут блокировать ВСЕ пары разом) ==")
    try:
        active = conn.execute(
            "SELECT COUNT(*) AS n FROM bot_instances WHERE status IN ('active','running','open')"
        ).fetchone()
        print(f"  активных ботов: {active['n']}  (если >= max_concurrent_bots → MAX_CONCURRENT_BOTS на всех)")
    except Exception as e:
        print(f"  bot_instances: {e}")
    try:
        fr = conn.execute("SELECT MAX(ts) AS m FROM funding_rate").fetchone()
        tk = conn.execute("SELECT MAX(ts) AS m FROM ticker_snap").fetchone()
        now = db.now_ts()
        print(f"  свежесть funding_rate: {now - (fr['m'] or 0)} c назад")
        print(f"  свежесть ticker_snap: {now - (tk['m'] or 0)} c назад")
        print("  (большие значения → FUNDING_RATE_UNKNOWN / устаревшая цена → blocked)")
    except Exception as e:
        print(f"  freshness: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
