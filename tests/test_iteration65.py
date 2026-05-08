from __future__ import annotations

from pathlib import Path

import pytest

from app import collector, db


class _TickerOnlyClient:
    def get_tickers(self, *, category: str, symbol: str):
        return [{
            "symbol": symbol,
            "lastPrice": "100",
            "bid1Price": "99",
            "ask1Price": "101",
            "volume24h": "1000",
            "turnover24h": "100000",
        }]

    def get_kline(self, *, category: str, symbol: str, interval: str, limit: int, start: int | None = None):
        return []



def test_collect_once_uses_non_committing_decision_logs_for_symbol_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    collector._DISABLED_SYMBOLS["linear"].clear()
    collector._LAST_TF_FETCH_ATTEMPT_TS.clear()

    conn = db.connect(str(tmp_path / "collector_log_commit.db"))
    db.init_db(conn)

    class BrokenClient:
        def get_tickers(self, *, category: str, symbol: str):
            return [{
                "symbol": symbol,
                "lastPrice": "100",
                "bid1Price": "99",
                "ask1Price": "101",
                "volume24h": "1000",
                "turnover24h": "100000",
            }]

        def get_kline(self, *, category: str, symbol: str, interval: str, limit: int, start: int | None = None):
            raise RuntimeError("simulated kline failure")

    commit_flags: list[bool] = []

    def _fake_log_decision(conn, action, rec_id, operator, details, *, commit=True):
        commit_flags.append(bool(commit))

    monkeypatch.setattr(collector.db, "log_decision", _fake_log_decision)

    stats = collector.collect_once(conn, BrokenClient(), "linear", ["BTCUSDT"])

    assert stats["tickers_written"] == 1
    assert commit_flags
    assert set(commit_flags) == {False}
    conn.close()



def test_collect_once_aborts_when_runtime_lock_is_lost(tmp_path: Path):
    collector._DISABLED_SYMBOLS["linear"].clear()
    collector._LAST_TF_FETCH_ATTEMPT_TS.clear()

    conn = db.connect(str(tmp_path / "collector_lock_lost.db"))
    db.init_db(conn)

    with pytest.raises(collector.RuntimeLockLostError):
        collector.collect_once(conn, _TickerOnlyClient(), "linear", ["BTCUSDT"], heartbeat=lambda: False)
    conn.close()



def test_collect_once_parallel_mode_survives_multi_cycle_soak(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    collector._DISABLED_SYMBOLS["linear"].clear()
    collector._LAST_TF_FETCH_ATTEMPT_TS.clear()

    conn = db.connect(str(tmp_path / "collector_parallel_soak.db"))
    db.init_db(conn)

    symbols = [f"SYM{i}USDT" for i in range(6)]
    base_ts = 1_700_000_000 - (1_700_000_000 % 60)
    current_cycle = {"idx": 0}

    class SoakClient:
        def get_tickers(self, *, category: str, symbol: str):
            return [{
                "symbol": symbol,
                "lastPrice": "100",
                "bid1Price": "99",
                "ask1Price": "101",
                "volume24h": "1000",
                "turnover24h": "100000",
            }]

        def get_kline(self, *, category: str, symbol: str, interval: str, limit: int, start: int | None = None):
            cycle_end = base_ts + current_cycle["idx"] * 60
            if interval == "1":
                step = 60
                end_ts = cycle_end
            elif interval == "60":
                step = 3600
                end_ts = cycle_end - (cycle_end % 3600)
            elif interval == "D":
                step = 86400
                end_ts = cycle_end - (cycle_end % 86400)
            elif interval == "15":
                step = 900
                end_ts = cycle_end - (cycle_end % 900)
            elif interval == "30":
                step = 1800
                end_ts = cycle_end - (cycle_end % 1800)
            else:
                raise AssertionError(interval)
            return [
                [str((end_ts - (limit - 1 - idx) * step) * 1000), "100", "101", "99", "100.5", "10", "0"]
                for idx in range(limit)
            ]

    client = SoakClient()
    for idx in range(5):
        current_cycle["idx"] = idx
        monkeypatch.setattr(db, "now_ts", lambda idx=idx: base_ts + idx * 60)
        stats = collector.collect_once(conn, client, "linear", symbols, max_workers=4)
        assert stats["tickers_written"] == len(symbols)
        assert stats["api_tf_fetches"].get("60", 0) == len(symbols)

    rows = db.get_latest_ohlcv(conn, "linear", symbols[0], 60, limit=400)
    latest_ts = max(int(r["ts"]) for r in rows)
    assert latest_ts == base_ts + 4 * 60
    assert len(rows) == 364
    conn.close()
