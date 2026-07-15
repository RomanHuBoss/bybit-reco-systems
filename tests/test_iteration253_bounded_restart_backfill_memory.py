from __future__ import annotations

from app import collector, db


def _ohlcv_row(symbol: str, ts: int, tf_sec: int = 60) -> dict:
    return {
        "venue": "linear",
        "symbol": symbol,
        "tf_sec": tf_sec,
        "ts": ts,
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 1.0,
    }


def _raw_kline(ts: int) -> list[str]:
    return [str(ts * 1000), "100", "101", "99", "100.5", "1"]


def test_long_restart_gap_fetches_recent_tail_once_and_persists_gap_job(tmp_path, monkeypatch) -> None:
    conn = db.connect(str(tmp_path / "restart_gap.db"))
    db.init_db(conn)
    symbol = "BTCUSDT"
    now_ts = 1_700_000_040
    stale_ts = now_ts - 15 * 24 * 60 * 60
    db.upsert_ohlcv(conn, [_ohlcv_row(symbol, stale_ts)])

    monkeypatch.setattr(db, "now_ts", lambda: now_ts)
    monkeypatch.setattr(collector, "_fetch_ticker_payloads", lambda *_args, **_kwargs: ([], [], []))
    monkeypatch.setattr(collector, "_fetch_funding_settlements_for_symbol", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(collector, "_derive_local_tf_rows", lambda *_args, **_kwargs: [])

    calls: list[dict] = []

    class _Client:
        def get_kline(self, **kwargs):
            calls.append(dict(kwargs))
            first = int(kwargs["start"]) // 1000
            return [_raw_kline(first + offset * 60) for offset in range(int(kwargs["limit"]))]

    stats = collector.collect_once(
        conn,
        _Client(),
        "linear",
        [symbol],
        max_workers=8,
        api_fetch_tfs=(60,),
        allow_derived_bootstrap=False,
    )

    assert len(calls) == 1
    assert calls[0]["limit"] == 360
    assert calls[0]["start"] == (now_ts - 359 * 60) * 1000
    assert calls[0]["end"] == (now_ts + 60) * 1000
    assert stats["recent_tail_resets"] == 1
    assert db.get_latest_ohlcv_ts(conn, "linear", symbol, 60) == now_ts

    job = db.get_app_config_json(conn, collector._gap_backfill_config_key("linear", symbol, 60))
    assert job["status"] == "pending"
    assert job["next_start_ts"] == stale_ts + 60
    assert job["target_end_ts"] == now_ts - 360 * 60
    conn.close()


def test_gap_backfill_advances_one_bounded_chunk_without_refetching_whole_gap(tmp_path, monkeypatch) -> None:
    conn = db.connect(str(tmp_path / "gap_chunk.db"))
    db.init_db(conn)
    symbol = "BTCUSDT"
    now_ts = 1_700_000_040
    start_ts = now_ts - 15 * 24 * 60 * 60 + 60
    target_end_ts = now_ts - 360 * 60
    key = collector._gap_backfill_config_key("linear", symbol, 60)
    db.set_app_config_json(conn, key, {
        "status": "pending",
        "venue": "linear",
        "symbol": symbol,
        "tf_sec": 60,
        "next_start_ts": start_ts,
        "target_end_ts": target_end_ts,
    })

    monkeypatch.setattr(db, "now_ts", lambda: now_ts)
    monkeypatch.setattr(collector, "_api_tf_fetch_state", lambda *_args, **_kwargs: (False, None))
    monkeypatch.setattr(collector, "_should_bootstrap_derived_tf", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(collector, "_derive_local_tf_rows", lambda *_args, **_kwargs: [])

    calls: list[dict] = []

    class _Client:
        def get_kline(self, **kwargs):
            calls.append(dict(kwargs))
            first = int(kwargs["start"]) // 1000
            limit = int(kwargs["limit"])
            return [_raw_kline(first + offset * 60) for offset in range(limit)]

    stats = collector.collect_backfill_once(
        conn,
        _Client(),
        "linear",
        [symbol],
        max_workers=8,
        per_tf_budget=1,
    )

    assert len(calls) == 1
    assert 1 <= calls[0]["limit"] <= 360
    assert calls[0]["start"] == start_ts * 1000
    assert stats["gap_backfill_fetches"] == 1
    assert stats["gap_backfill_rows"] == calls[0]["limit"]

    job = db.get_app_config_json(conn, key)
    assert job["status"] == "pending"
    assert job["next_start_ts"] == start_ts + calls[0]["limit"] * 60
    conn.close()


def test_task_runner_is_lazy_and_keeps_only_worker_count_in_flight() -> None:
    started: list[int] = []

    def worker(task: int) -> int:
        started.append(task)
        return task

    results = collector._run_tasks_bounded(list(range(20)), worker, max_workers=2)
    assert not isinstance(results, list)
    iterator = iter(results)
    first = next(iterator)
    assert first[0] in {0, 1}
    assert len(started) <= 2
    remaining = list(iterator)
    all_results = [first, *remaining]
    assert sorted(result for _task, result, error in all_results if error is None) == list(range(20))
    assert all(error is None for _task, _result, error in all_results)


def test_release_exposes_safe_backfill_defaults_and_memory_diagnostics() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    settings_source = (root / "app" / "settings.py").read_text(encoding="utf-8")
    main_source = (root / "app" / "main.py").read_text(encoding="utf-8")
    ui_source = (root / "app" / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    env_example = (root / ".env.example").read_text(encoding="utf-8")

    assert '_env("BACKFILL_FULL_SWEEP_ON_WARMUP", "0")' in settings_source
    assert '_env_int("BACKFILL_PER_TF_BUDGET", 8, minimum=1' in settings_source
    assert "BACKFILL_FULL_SWEEP_ON_WARMUP=0" in env_example
    assert "BACKFILL_PER_TF_BUDGET=8" in env_example
    assert '"rss_mb": None' in main_source
    assert '"peak_rss_mb": None' in main_source
    assert "Память Python" in ui_source
    assert "Оставшихся заданий восстановления" in ui_source
