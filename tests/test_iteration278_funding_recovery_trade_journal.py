from __future__ import annotations

from pathlib import Path
import json
import re
import subprocess

import pytest

from app import collector, db
from app.bybit_client import BybitPublicClient
from app.outcomes import _grid_outcome


def _extract_js_async_function(source: str, name: str) -> str:
    match = re.search(rf"async function {re.escape(name)}\([^)]*\) \{{", source)
    assert match, f"async function {name} not found"
    start = match.start()
    pos = match.end()
    depth = 1
    while pos < len(source) and depth:
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
        pos += 1
    return source[start:pos]


def _grid_params() -> dict:
    return {
        "grid_count": 4,
        "grid_levels": 4,
        "price_range_lower": 98.0,
        "price_range_upper": 102.0,
        "cost_model": {"execution_cost_bps": 0.0, "expected_funding_bps": 0.0},
        "trade_plan": {
            "grid_count": 4,
            "cost_model": {"execution_cost_bps": 0.0, "expected_funding_bps": 0.0},
            "levels": {
                "range": {"lower": 98.0, "upper": 102.0},
                "kill_switch": {"lower": 97.0, "upper": 103.0},
                "tp_per_leg": {"abs": 1.0},
            },
        },
    }


def _seed_ambiguous_candle(conn, base_ts: int) -> None:
    db.upsert_ohlcv(conn, [{
        "venue": "linear",
        "symbol": "BTCUSDT",
        "tf_sec": 60,
        "ts": base_ts,
        "open": 99.0,
        "high": 104.0,
        "low": 96.0,
        "close": 98.5,
        "volume": 1_000.0,
    }])


def test_failed_funding_history_attempt_uses_short_retry_not_hourly_lockout(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "funding-retry.db"))
    db.init_db(conn)
    collector._FUNDING_SETTLEMENT_FETCH_STATE.clear()

    class StubClient:
        def __init__(self) -> None:
            self.calls = 0

        def get_funding_rate_history(self, symbol: str, *, start_ms: int, end_ms: int, limit: int):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary transport failure")
            return [{"symbol": symbol, "ts": 1_720_000_060, "funding_rate": 0.0001}]

    client = StubClient()
    with pytest.raises(RuntimeError, match="temporary transport failure"):
        collector._fetch_funding_settlements_for_symbol(
            conn, client, "linear", "BTCUSDT", 1_720_000_000
        )
    rows = collector._fetch_funding_settlements_for_symbol(
        conn, client, "linear", "BTCUSDT", 1_720_000_060
    )
    assert client.calls == 2
    assert rows == [{"symbol": "BTCUSDT", "ts": 1_720_000_060, "funding_rate": 0.0001}]
    conn.close()


def test_targeted_funding_repair_queue_is_additive_and_idempotent(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "repair.db"))
    db.init_db(conn)
    first = db.request_funding_settlement_repair(
        conn,
        symbol="LTCUSDT",
        expected_ts=1_784_836_800,
        range_start_ts=1_784_836_800,
        range_end_ts=1_784_836_800,
        reason="missing_funding_settlement",
    )
    second = db.request_funding_settlement_repair(
        conn,
        symbol="LTCUSDT",
        expected_ts=1_784_836_800,
        range_start_ts=1_784_836_800,
        range_end_ts=1_784_836_800,
        reason="missing_funding_settlement",
    )
    due = db.list_due_funding_settlement_repairs(conn, now_ts_value=1_900_000_000, limit=10)
    assert first == second
    assert len(due) == 1
    assert due[0]["symbol"] == "LTCUSDT"
    assert due[0]["expected_ts"] == 1_784_836_800
    conn.close()


def test_bybit_public_trade_history_is_strictly_sanitized() -> None:
    client = BybitPublicClient("https://example.invalid")
    try:
        def fake_get(path: str, params: dict):
            assert path == "/v5/market/recent-trade"
            return {
                "time": 1_709_200_059_900,
                "result": {"list": [
                    {"execId": "t2", "symbol": "BTCUSDT", "price": "99.5", "size": "2", "side": "Buy", "time": "1709200030000", "seq": "12", "isBlockTrade": False, "isRPITrade": False},
                    {"execId": "wrong", "symbol": "ETHUSDT", "price": "1", "size": "1", "side": "Sell", "time": "1709200030000", "seq": "13"},
                    {"execId": "bad", "symbol": "BTCUSDT", "price": "NaN", "size": "1", "side": "Sell", "time": "1709200031000", "seq": "14"},
                    {"execId": "t1", "symbol": "BTCUSDT", "price": "97.9", "size": "1", "side": "Sell", "time": "1709200020000", "seq": "11", "isBlockTrade": False, "isRPITrade": True},
                ]},
            }

        client._get = fake_get  # type: ignore[method-assign]
        payload = client.get_recent_public_trades("BTCUSDT", limit=1000)
        assert payload["snapshot_ts_ms"] == 1_709_200_059_900
        assert [row["trade_id"] for row in payload["items"]] == ["t1", "t2"]
        assert payload["items"][0]["is_rpi_trade"] is True
    finally:
        client.close()


def test_complete_trade_journal_resolves_two_sided_intrabar_order(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "trade-journal.db"))
    db.init_db(conn)
    base_ts = 1_709_200_000
    _seed_ambiguous_candle(conn, base_ts)
    rows = [
        {"venue": "linear", "symbol": "BTCUSDT", "trade_id": "a", "trade_ts_ms": base_ts * 1000 + 1_000, "seq": 1, "side": "Sell", "price": 99.0, "qty": 1.0, "received_ts_ms": base_ts * 1000 + 59_900, "source": "rest_recent_trade_v1", "is_block_trade": False, "is_rpi_trade": False},
        {"venue": "linear", "symbol": "BTCUSDT", "trade_id": "b", "trade_ts_ms": base_ts * 1000 + 10_000, "seq": 2, "side": "Sell", "price": 96.0, "qty": 1.0, "received_ts_ms": base_ts * 1000 + 59_900, "source": "rest_recent_trade_v1", "is_block_trade": False, "is_rpi_trade": False},
        {"venue": "linear", "symbol": "BTCUSDT", "trade_id": "c", "trade_ts_ms": base_ts * 1000 + 30_000, "seq": 3, "side": "Buy", "price": 104.0, "qty": 1.0, "received_ts_ms": base_ts * 1000 + 59_900, "source": "rest_recent_trade_v1", "is_block_trade": False, "is_rpi_trade": False},
        {"venue": "linear", "symbol": "BTCUSDT", "trade_id": "d", "trade_ts_ms": base_ts * 1000 + 50_000, "seq": 4, "side": "Sell", "price": 98.5, "qty": 1.0, "received_ts_ms": base_ts * 1000 + 59_900, "source": "rest_recent_trade_v1", "is_block_trade": False, "is_rpi_trade": False},
    ]
    db.upsert_market_trades(conn, rows)
    db.insert_market_trade_coverage(
        conn,
        coverage_id="coverage-a",
        venue="linear",
        symbol="BTCUSDT",
        coverage_start_ms=base_ts * 1000,
        coverage_end_ms=(base_ts + 60) * 1000,
        state="closed",
        source="rest_recent_trade_v1",
    )
    diagnostics: dict[str, object] = {}
    result = _grid_outcome(
        conn, "linear", "BTCUSDT", 99.0, 98.5,
        base_ts, base_ts + 60, "neutral", _grid_params(), diagnostics=diagnostics,
    )
    assert result is not None
    assert diagnostics["intrabar_observation_method"] == "public_trade_journal_v1"
    assert diagnostics["trade_journal_replayed_candles"] == 1
    conn.close()


def test_observation_upgrade_does_not_change_model_or_outcome_label_lineage() -> None:
    main_source = Path("app/main.py").read_text(encoding="utf-8")
    recommender_source = Path("app/recommender.py").read_text(encoding="utf-8")
    assert 'OUTCOME_LABEL_VERSION = "grid_label_v26"' in main_source
    assert 'RECOMMENDER_MODEL_VERSION = "bybit-taxonomy-v13-log-symmetric-direction"' in recommender_source


def test_funding_repair_worker_persists_settlement_and_resolves_job(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "repair-worker.db"))
    db.init_db(conn)
    expected_ts = 1_784_836_800
    db.request_funding_settlement_repair(
        conn,
        symbol="LTCUSDT",
        expected_ts=expected_ts,
        range_start_ts=expected_ts,
        range_end_ts=expected_ts,
        reason="missing_funding_settlement",
    )

    class StubClient:
        def get_funding_rate_history(self, symbol: str, *, start_ms: int, end_ms: int, limit: int):
            assert symbol == "LTCUSDT"
            assert start_ms < expected_ts * 1000 < end_ms
            return [{"symbol": symbol, "ts": expected_ts, "funding_rate": 0.0002}]

    stats = collector._process_funding_repair_queue(
        conn, StubClient(), "linear", 1_900_000_000, max_jobs=10
    )
    assert stats["resolved"] == 1
    stored = db.get_funding_settlements(conn, "LTCUSDT", expected_ts, expected_ts)
    assert stored == [{"symbol": "LTCUSDT", "ts": expected_ts, "funding_rate": 0.0002}]
    assert db.get_funding_settlement_repair_status(conn)["resolved"] == 1
    conn.close()


def test_trade_poll_coverage_requires_overlap_and_records_gap(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "coverage.db"))
    db.init_db(conn)

    def row(trade_id: str, ts_ms: int, seq: int) -> dict:
        return {
            "venue": "linear", "symbol": "BTCUSDT", "trade_id": trade_id,
            "trade_ts_ms": ts_ms, "seq": seq, "side": "Buy", "price": 100.0,
            "qty": 1.0, "received_ts_ms": ts_ms + 1000,
            "source": "rest_recent_trade_v1", "is_block_trade": False,
            "is_rpi_trade": False,
        }

    first = db.record_market_trade_poll(
        conn, venue="linear", symbol="BTCUSDT",
        rows=[row("a", 10_000, 1), row("b", 11_000, 2)],
        snapshot_ts_ms=12_000,
    )
    assert first["coverage_extended"] is False
    second = db.record_market_trade_poll(
        conn, venue="linear", symbol="BTCUSDT",
        rows=[row("b", 11_000, 2), row("c", 13_000, 3)],
        snapshot_ts_ms=14_000,
    )
    assert second["coverage_extended"] is True
    third = db.record_market_trade_poll(
        conn, venue="linear", symbol="BTCUSDT",
        rows=[row("x", 20_000, 20), row("y", 21_000, 21)],
        snapshot_ts_ms=22_000,
    )
    assert third["gap_detected"] is True
    assert db.get_market_trade_journal_status(conn, now_ms=22_000)["closed_gap_spans_total"] == 1
    conn.close()


def test_ambiguous_ohlcv_without_complete_trade_coverage_remains_censored(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "no-coverage.db"))
    db.init_db(conn)
    base_ts = 1_709_300_000
    db.upsert_ohlcv(conn, [{
        "venue": "linear", "symbol": "BTCUSDT", "tf_sec": 60, "ts": base_ts,
        "open": 99.0, "high": 99.5, "low": 97.9, "close": 98.5, "volume": 1000.0,
    }])
    diagnostics: dict[str, object] = {}
    result = _grid_outcome(
        conn, "linear", "BTCUSDT", 99.0, 98.5,
        base_ts, base_ts + 60, "neutral", _grid_params(), diagnostics=diagnostics,
    )
    assert result is None
    assert diagnostics["reason"] == "intrabar_extreme_order_unobservable"
    conn.close()


def test_existing_database_upgrade_adds_observability_tables_idempotently(tmp_path: Path) -> None:
    path = tmp_path / "upgrade.db"
    conn = db.connect(str(path))
    db.init_db(conn)
    for table in ("funding_settlement_repair", "market_trade", "market_trade_coverage"):
        conn.execute(f"DROP TABLE {table}")
    conn.commit()
    db.init_db(conn)
    db.init_db(conn)
    for table in ("funding_settlement_repair", "market_trade", "market_trade_coverage"):
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None
    conn.close()


def test_dual_database_schema_files_define_observability_tables() -> None:
    sqlite_sql = Path("migrations/init.sql").read_text(encoding="utf-8")
    postgres_sql = Path("migrations/init_postgres.sql").read_text(encoding="utf-8")
    for table in ("funding_settlement_repair", "market_trade", "market_trade_coverage"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sqlite_sql
        assert f"CREATE TABLE IF NOT EXISTS {table}" in postgres_sql


def test_health_renderer_executes_with_funding_repair_and_trade_journal_status() -> None:
    source = Path("app/ui/static/app.js").read_text(encoding="utf-8")
    fn = _extract_js_async_function(source, "loadHealth")
    script = f"""
let rendered = [];
let lastHealthDiagnostics = null;
const window = {{ location: {{ href: "http://localhost/" }} }};
function showModalHtml(title, html) {{ rendered.push([title, String(html)]); }}
function showModal(title, payload) {{ throw new Error(title + JSON.stringify(payload)); }}
function healthStatusRu(value) {{ return String(value || "unknown"); }}
function formatTs(value) {{ return value == null ? "—" : String(value); }}
function formatAgeHuman(value) {{ return value == null ? "—" : String(value); }}
function empiricalStatusRu(value) {{ return String(value || "insufficient"); }}
function renderModalSummaryCards(rows) {{ return rows.map(row => `${{row.label}}=${{row.value}}`).join("|"); }}
function escapeHtml(value) {{ return String(value ?? ""); }}
function humanizeOperatorText(value) {{ return String(value ?? ""); }}
function renderHealthStatus(value) {{ return String(value ?? ""); }}
function buildModalTable(columns, rows) {{ return rows.map(row => Object.values(row).join(" ")).join("\\n"); }}
function renderModalDisclosure(title, body) {{ return title + "\\n" + body; }}
const statusPayload = {{
  app_version: "1.4.8",
  operator_readiness: {{ state: "healthy_not_actionable", explanations: [] }},
  recommendation_readiness: {{ status_counts: {{}}, actionable_count: 0, no_trade_reason_counts: [], blocked_reason_counts: [] }},
  outcome_worker: {{ by_bot_type: {{ futures_grid: {{}}, directional_trend: {{}} }} }},
  database_schema: {{ migration_applied: true, materialization_pending: 0 }},
  database_continuity: {{ outcome_semantic_integrity: {{ ok: true }} }},
  funding_settlement_repair: {{ pending: 2, resolved: 7, next_due_ts: 123 }},
  market_trade_journal: {{ enabled: true, trade_rows_total: 42, symbols: {{ BTCUSDT: {{}} }}, closed_gap_spans_total: 1, retention_hours: 72, poll_limit: 1000, evidence_boundary: "public chronology only" }},
  bot_calibrators: {{ futures_grid: {{}}, directional_trend: {{}} }},
  trend_first_touch_model: {{}}, background_threads: {{}}, runtime_provenance: {{}}
}};
const responses = [
  {{ ok: true, json: async () => ({{ summary: {{}}, llm_reviewer: {{}}, warmup: {{}}, runtime: {{}}, collector: {{}}, backfill: {{}}, symbols: [] }}) }},
  {{ ok: true, json: async () => statusPayload }},
  {{ ok: true, json: async () => [] }},
];
async function fetch() {{ return responses.shift(); }}
{fn}
(async () => {{
  await loadHealth();
  process.stdout.write(JSON.stringify(rendered.at(-1)));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    title, html = json.loads(completed.stdout)
    assert title == "Здоровье системы"
    assert "Funding settlement recovery" in html
    assert "ожидают 2; восстановлено 7" in html
    assert "Intrabar trade journal" in html
    assert "строк 42" in html
    assert "public chronology only" in html


def test_recent_trade_snapshot_rejects_invalid_server_time() -> None:
    client = BybitPublicClient("https://example.invalid")
    try:
        client._get = lambda path, params: {"time": False, "result": {"list": []}}  # type: ignore[method-assign]
        with pytest.raises(ValueError, match="invalid server timestamp"):
            client.get_recent_public_trades("BTCUSDT")
    finally:
        client.close()


def test_trade_coverage_rejects_cross_symbol_rows_and_future_trades(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "strict-coverage.db"))
    db.init_db(conn)
    base = {
        "venue": "linear", "trade_id": "x", "seq": 1, "side": "Buy",
        "price": 100.0, "qty": 1.0, "received_ts_ms": 12_000,
        "source": "rest_recent_trade_v1", "is_block_trade": False, "is_rpi_trade": False,
    }
    mixed = {**base, "symbol": "ETHUSDT", "trade_ts_ms": 11_000}
    result = db.record_market_trade_poll(
        conn, venue="linear", symbol="BTCUSDT", rows=[mixed], snapshot_ts_ms=12_000
    )
    assert result["inserted"] == 0
    assert db.get_market_trade_journal_status(conn, now_ms=12_000)["coverage_spans_total"] == 0

    future = {**base, "symbol": "BTCUSDT", "trade_ts_ms": 13_000}
    with pytest.raises(ValueError, match="newer than the snapshot"):
        db.record_market_trade_poll(
            conn, venue="linear", symbol="BTCUSDT", rows=[future], snapshot_ts_ms=12_000
        )
    conn.close()


def test_iterative_protocol_documents_funding_recovery_and_trade_evidence_boundary() -> None:
    prompt = Path("docs/Bybit_Recommender_Iteration_Prompt.md").read_text(encoding="utf-8")
    assert "Контракт: v1.4.8" in prompt
    assert "FUNDING RECOVERY И PUBLIC TRADE CHRONOLOGY" in prompt
    assert "не queue priority" in prompt
    assert "не начинает новую trading-model lineage" in prompt


def test_public_trade_websocket_parser_is_strict_and_chronological() -> None:
    from app.trade_stream import parse_public_trade_message

    now_ms = 1_800_000_000_000
    parsed = parse_public_trade_message({
        "topic": "publicTrade.BTCUSDT",
        "ts": now_ms,
        "data": [
            {"T": now_ms - 20, "s": "BTCUSDT", "S": "Sell", "v": "0.2", "p": "101.5", "i": "a", "BT": False, "RPI": False, "seq": 10},
            {"T": now_ms - 10, "s": "BTCUSDT", "S": "Buy", "v": "0.3", "p": "101.6", "i": "b", "BT": False, "RPI": True, "seq": 11},
        ],
    }, received_ts_ms=now_ms + 5)
    assert parsed is not None
    assert parsed["symbol"] == "BTCUSDT"
    assert [row["trade_id"] for row in parsed["rows"]] == ["a", "b"]
    assert parsed["rows"][1]["is_rpi_trade"] is True

    with pytest.raises(ValueError, match="non-monotonic"):
        parse_public_trade_message({
            "topic": "publicTrade.BTCUSDT",
            "ts": now_ms,
            "data": [
                {"T": now_ms - 10, "s": "BTCUSDT", "S": "Buy", "v": "1", "p": "100", "i": "b", "BT": False, "seq": 11},
                {"T": now_ms - 20, "s": "BTCUSDT", "S": "Sell", "v": "1", "p": "99", "i": "a", "BT": False, "seq": 10},
            ],
        }, received_ts_ms=now_ms + 5)


def test_public_trade_stream_session_subscribes_ingests_and_closes_coverage(tmp_path: Path) -> None:
    import time
    from app.trade_stream import run_public_trade_stream_session

    conn = db.connect(str(tmp_path / "ws-stream.db"))
    db.init_db(conn)
    now_ms = int(time.time() * 1000)
    stopped = {"value": False}
    sent: list[dict] = []

    class FakeWebSocket:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def send(self, payload: str) -> None:
            sent.append(json.loads(payload))

        def recv(self, timeout: float):
            assert timeout > 0
            stopped["value"] = True
            return json.dumps({
                "topic": "publicTrade.BTCUSDT",
                "type": "snapshot",
                "ts": now_ms,
                "data": [{
                    "T": now_ms - 1,
                    "s": "BTCUSDT",
                    "S": "Buy",
                    "v": "0.01",
                    "p": "100.5",
                    "i": "trade-1",
                    "BT": False,
                    "RPI": False,
                    "seq": 1,
                }],
            })

    def fake_connect(url: str, **kwargs):
        assert url == "wss://stream.bybit.com/v5/public/linear"
        assert kwargs["ping_interval"] == 20
        return FakeWebSocket()

    stats = run_public_trade_stream_session(
        conn,
        bybit_http_base_url="https://api.bybit.com",
        symbols=["BTCUSDT"],
        stop_requested=lambda: stopped["value"],
        heartbeat=lambda: True,
        connect_fn=fake_connect,
    )
    assert sent == [{"op": "subscribe", "args": ["publicTrade.BTCUSDT"]}]
    assert stats["trades"] == 1
    assert conn.execute("SELECT COUNT(*) AS c FROM market_trade").fetchone()["c"] == 1
    coverage = conn.execute(
        "SELECT state, gap_reason, source FROM market_trade_coverage"
    ).fetchone()
    assert dict(coverage) == {
        "state": "closed",
        "gap_reason": "stream_shutdown",
        "source": "websocket_public_trade_v1",
    }
    conn.close()


def test_websocket_sessions_never_bridge_coverage_across_disconnect(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "ws-gap.db"))
    db.init_db(conn)

    def stream_row(trade_id: str, ts_ms: int, seq: int) -> dict:
        return {
            "venue": "linear", "symbol": "BTCUSDT", "trade_id": trade_id,
            "trade_ts_ms": ts_ms, "seq": seq, "side": "Buy", "price": 100.0,
            "qty": 1.0, "received_ts_ms": ts_ms + 1,
            "source": "websocket_public_trade_v1", "is_block_trade": False,
            "is_rpi_trade": False,
        }

    first = db.record_market_trade_stream_batch(
        conn, venue="linear", symbol="BTCUSDT", rows=[stream_row("a", 10_000, 1)],
        message_ts_ms=10_100, session_id="session-a",
    )
    assert db.close_market_trade_coverage(
        conn, first["coverage_id"], gap_reason="websocket_disconnect"
    )
    second = db.record_market_trade_stream_batch(
        conn, venue="linear", symbol="BTCUSDT", rows=[stream_row("b", 20_000, 2)],
        message_ts_ms=20_100, session_id="session-b",
    )
    assert first["coverage_id"] != second["coverage_id"]
    spans = conn.execute(
        "SELECT state, gap_reason, coverage_start_ms, coverage_end_ms FROM market_trade_coverage ORDER BY coverage_start_ms"
    ).fetchall()
    assert len(spans) == 2
    assert spans[0]["state"] == "closed"
    assert spans[0]["gap_reason"] == "websocket_disconnect"
    assert int(spans[0]["coverage_end_ms"]) < int(spans[1]["coverage_start_ms"])
    conn.close()


def test_public_trade_stream_is_explicitly_configured_and_supervised() -> None:
    requirements = Path("requirements.txt").read_text(encoding="utf-8")
    env_example = Path(".env.example").read_text(encoding="utf-8")
    settings_source = Path("app/settings.py").read_text(encoding="utf-8")
    main_source = Path("app/main.py").read_text(encoding="utf-8")
    assert "websockets==16.0" in requirements
    assert "MARKET_TRADE_STREAM_ENABLED=1" in env_example
    assert "market_trade_stream_enabled: bool = True" in settings_source
    assert '"market_trade_stream": "runtime:market_trade_stream"' in main_source
    assert '_start_background_thread(\n            "market_trade_stream"' in main_source
    assert '"outcomes", "market_trade_stream", "llm_reviewer"' in main_source


def test_rest_fallback_does_not_mutate_open_websocket_coverage(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "source-isolation.db"))
    db.init_db(conn)
    ws_row = {
        "venue": "linear", "symbol": "BTCUSDT", "trade_id": "ws-a",
        "trade_ts_ms": 10_000, "seq": 1, "side": "Buy", "price": 100.0,
        "qty": 1.0, "received_ts_ms": 10_001,
        "source": "websocket_public_trade_v1", "is_block_trade": False,
        "is_rpi_trade": False,
    }
    ws = db.record_market_trade_stream_batch(
        conn, venue="linear", symbol="BTCUSDT", rows=[ws_row],
        message_ts_ms=10_100, session_id="ws-session",
    )
    rest_row = {
        **ws_row,
        "trade_id": "rest-a",
        "trade_ts_ms": 20_000,
        "received_ts_ms": 20_001,
        "source": "rest_recent_trade_v1",
    }
    rest = db.record_market_trade_poll(
        conn, venue="linear", symbol="BTCUSDT", rows=[rest_row],
        snapshot_ts_ms=20_100,
    )
    assert ws["coverage_id"] != rest["coverage_id"]
    sources = {
        row["coverage_id"]: (row["state"], row["source"])
        for row in conn.execute(
            "SELECT coverage_id, state, source FROM market_trade_coverage"
        ).fetchall()
    }
    assert sources[ws["coverage_id"]] == ("open", "websocket_public_trade_v1")
    assert sources[rest["coverage_id"]] == ("open", "rest_recent_trade_v1")
    conn.close()
