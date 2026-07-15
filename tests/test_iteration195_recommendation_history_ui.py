from __future__ import annotations

import importlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from conftest import safe_linear_grid_params


@pytest.fixture()
def history_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "history.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT,ETHUSDT")
    monkeypatch.setenv("LLM_REVIEWER_ENABLED", "true")

    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    app_main.app.router.on_startup.clear()
    monkeypatch.setattr(
        app_main,
        "_fetch_bybit_instrument_meta",
        lambda venue, symbol: {
            "category": "linear",
            "symbol": str(symbol or "BTCUSDT").upper(),
            "status": "Trading",
            "contract_type": "LinearPerpetual",
            "quote_coin": "USDT",
            "settle_coin": "USDT",
            "tick_size": "0.1",
            "qty_step": "0.001",
            "min_order_qty": "0.001",
            "max_order_qty": "1000",
            "min_notional": "5",
            "min_leverage": "1",
            "max_leverage": "100",
            "leverage_step": "0.01",
        },
    )

    conn = db.connect(str(db_path))
    client = TestClient(app_main.app)
    try:
        yield client, conn, app_main
    finally:
        conn.close()
        client.close()


def _row(rec_id: str, ts: int, *, direction: str, status: str, llm_status: str, root: str | None = None):
    return {
        "rec_id": rec_id,
        "ts": ts,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": direction,
        "account_mode": "one_way",
        "margin_mode": "cross",
        "score": 0.40,
        "confidence": 0.70,
        "expected_rr": 1.2,
        "risk_score": 0.2,
        "params": safe_linear_grid_params({"grid_levels": 8}),
        "reasons": {
            "llm_review": {
                "status": llm_status,
                "mode": "advisory",
                "publish_target_status": "recommended",
            }
        },
        "blocks": [],
        "status": status,
        "ttl_sec": 1800,
        "model_version": "test",
        "features_ref_ts": ts,
        "publication_root_rec_id": root or rec_id,
        "is_outcome_label_root": root is None,
    }


def test_latest_operator_uses_actual_latest_cycle_instead_of_old_llm_ready_snapshot(history_client):
    client, conn, _app_main = history_client
    now = int(time.time())
    db.insert_recommendations(
        conn,
        [
            _row("R-reviewed-old", now - 120, direction="long", status="recommended", llm_status="ok"),
            _row("R-pending-new", now, direction="short", status="recommended", llm_status="pending"),
        ],
    )

    response = client.get(
        "/api/v1/recommendations?snapshot=latest_operator&min_conf=0&show_recommended=false&show_pending=true"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["snapshot_ts"] == now
    assert [item["rec_id"] for item in payload["items"]] == ["R-pending-new"]
    assert payload["items"][0]["effective_status"] == "pending"

    default_response = client.get("/api/v1/recommendations?snapshot=latest_operator&min_conf=0")
    assert default_response.status_code == 200
    default_payload = default_response.json()
    assert default_payload["snapshot_ts"] == now
    assert default_payload["items"] == []
    assert default_payload["effective_status_counts"]["pending"] == 1


def test_history_endpoint_returns_ordered_publications_and_latest_identity(history_client):
    client, conn, _app_main = history_client
    now = int(time.time())
    db.insert_recommendations(
        conn,
        [
            _row("R-root", now - 300, direction="long", status="recommended", llm_status="ok"),
            _row("R-update", now - 180, direction="long", status="active", llm_status="ok", root="R-root"),
            _row("R-flip", now - 60, direction="short", status="pending", llm_status="pending"),
        ],
    )

    response = client.get(
        "/api/v1/recommendations/history?venue=linear&symbol=BTCUSDT&bot_type=futures_grid&limit=50"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["latest_rec_id"] == "R-flip"
    assert payload["items_total"] == 3
    assert [item["rec_id"] for item in payload["items"]] == ["R-root", "R-update", "R-flip"]
    assert payload["items"][0]["publication_kind"] == "root"
    assert payload["items"][1]["publication_kind"] == "update"
    assert payload["items"][2]["direction_changed"] is True
    assert payload["items"][2]["llm_status"] == "pending"


def test_ui_has_modal_timeline_and_preserves_selected_recommendation_identity():
    root = Path(__file__).resolve().parents[1]
    js = (root / "app" / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    css = (root / "app" / "ui" / "static" / "styles.css").read_text(encoding="utf-8")
    html = (root / "app" / "ui" / "static" / "index.html").read_text(encoding="utf-8")

    assert 'data-act="show-recommendation-history"' in js
    assert "loadRecommendationHistory" in js
    assert "buildRecommendationTimelineSvg" in js
    assert "/api/v1/recommendations/history?" in js
    assert "resolveLatestDetailsRecId" not in js
    assert "refreshInFlight" in js
    assert "effective_status_counts" in js
    assert ".recommendation-timeline" in css
    assert "manual-ui-v49-russian-operator-language" in html


def test_future_recommendation_timestamp_is_not_reported_as_zero_age(history_client):
    client, conn, app_main = history_client
    now = int(time.time())
    future = _row(
        "R-future-clock",
        now + 3600,
        direction="long",
        status="recommended",
        llm_status="ok",
    )
    db.insert_recommendations(conn, [future])

    chain = app_main._publication_chain_context_for_reco(conn, future, now_ts=now)
    assert chain["recommendation_timestamp_valid"] is False
    assert chain["recommendation_timestamp_invalid_reason"] == "future_clock_skew"
    assert chain["recommendation_row_age_sec"] is None

    blocks = app_main._execution_recommendation_freshness_blocks(conn, future, now_ts=now)
    assert "RECOMMENDATION_TIMESTAMP_INVALID" in {item["code"] for item in blocks}

    list_response = client.get(
        "/api/v1/recommendations?min_conf=0&show_recommended=false&show_blocked=true"
    )
    assert list_response.status_code == 200
    listed = list_response.json()["items"]
    assert [item["rec_id"] for item in listed] == ["R-future-clock"]
    assert listed[0]["effective_status"] == "blocked"
    assert "RECOMMENDATION_TIMESTAMP_INVALID" in {
        item["code"] for item in listed[0]["bybit_operator_guard"]["errors"]
    }

    response = client.get(
        "/api/v1/recommendations/history?venue=linear&symbol=BTCUSDT&bot_type=futures_grid"
    )
    assert response.status_code == 200
    item = response.json()["items"][-1]
    assert item["timestamp_valid"] is False
    assert item["timestamp_invalid_reason"] == "future_clock_skew"
    assert item["age_sec"] is None


def test_ui_renders_invalid_recommendation_time_explicitly():
    js = Path("app/ui/static/app.js").read_text(encoding="utf-8")

    assert "recommendation_timestamp_valid" in js
    assert "Некорректная метка времени" in js
    assert "renderModalSummaryCards(summary)" in js


def test_history_table_rows_are_sorted_newest_first():
    root = Path(__file__).resolve().parents[1]
    js = (root / "app" / "ui" / "static" / "app.js").read_text(encoding="utf-8")

    marker = "function sortRecommendationHistoryRowsNewestFirst(items)"
    start = js.index(marker)
    end = js.index("\nfunction buildRecommendationHistoryHtml", start)
    helper_source = js[start:end]
    node_script = f"""
function toFiniteNumber(value) {{
  if (value === null || value === undefined || typeof value === "boolean") return null;
  if (typeof value === "string" && value.trim() === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}}
{helper_source}
const rows = [
  {{rec_id: "old", ts: 100, sequence: 1}},
  {{rec_id: "new", ts: 300, sequence: 4}},
  {{rec_id: "same-second-newer", ts: 300, sequence: 5}},
  {{rec_id: "middle", ts: 200, sequence: 3}},
  {{rec_id: "invalid", ts: null, sequence: 6}}
];
console.log(JSON.stringify(sortRecommendationHistoryRowsNewestFirst(rows).map(row => row.rec_id)));
console.log(JSON.stringify(rows.map(row => row.rec_id)));
"""
    completed = subprocess.run(
        ["node", "-e", node_script],
        check=True,
        capture_output=True,
        text=True,
    )
    output_lines = completed.stdout.strip().splitlines()

    assert json.loads(output_lines[0]) == ["same-second-newer", "new", "middle", "old", "invalid"]
    assert json.loads(output_lines[1]) == ["old", "new", "same-second-newer", "middle", "invalid"]
    assert "const tableItems = sortRecommendationHistoryRowsNewestFirst(items);" in js
    assert "], tableItems, { emptyText:" in js
