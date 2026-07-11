from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from app import recommender
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "app" / "ui" / "static" / "app.js"


def _extract_js_function(source: str, name: str) -> str:
    match = re.search(rf"(?:async\s+)?function {re.escape(name)}\([^)]*\) \{{", source)
    assert match, f"function {name} not found in app.js"
    start = match.start()
    end = match.end()
    depth = 1
    while end < len(source) and depth:
        if source[end] == "{":
            depth += 1
        elif source[end] == "}":
            depth -= 1
        end += 1
    assert depth == 0, f"function {name} body is not balanced"
    return source[start:end]


def _high_quality_rec() -> dict:
    return {
        "score": 0.19,
        "confidence": 0.67,
        "expected_rr": 0.22,
        "reasons": {
            "direction_agg": {
                "coherence": 0.72,
                "regime_confidence": 0.63,
            }
        },
    }


def test_high_quality_grid_requires_two_distinct_closed_evidence_snapshots():
    settings = SimpleNamespace(min_score_to_recommend=0.08, min_conf_to_recommend=0.52)
    required_hits, mode = recommender._persistence_gate_requirements(_high_quality_rec(), settings)

    assert required_hits == 2
    assert mode == "distinct_evidence_confirmation"

    recommender._prev_recommended = {}
    first = recommender._advance_persistence_gate(
        "linear",
        "BTCUSDT",
        "futures_grid",
        "long",
        now_ts=1_700_000_060,
        fresh_gap=300,
        evidence_ts=1_700_000_000,
    )
    repeated_same_candle = recommender._advance_persistence_gate(
        "linear",
        "BTCUSDT",
        "futures_grid",
        "long",
        now_ts=1_700_000_120,
        fresh_gap=300,
        evidence_ts=1_700_000_000,
    )
    next_closed_candle = recommender._advance_persistence_gate(
        "linear",
        "BTCUSDT",
        "futures_grid",
        "long",
        now_ts=1_700_000_180,
        fresh_gap=300,
        evidence_ts=1_700_000_060,
    )

    assert first == 1
    assert repeated_same_candle == 1
    assert next_closed_candle == 2


def test_details_refresh_preserves_immutable_selected_recommendation_id():
    source = APP_JS.read_text(encoding="utf-8")
    refresh_fn = _extract_js_function(source, "refreshCurrentDetails")
    script = f"""
let currentRecId = "R-selected";
let currentMeta = {{ venue: "linear", symbol: "BTCUSDT", bot_type: "futures_grid" }};
let loaded = null;
async function resolveLatestDetailsRecId() {{ return "R-newer-no-trade"; }}
async function loadDetails(recId) {{ loaded = recId; }}
{refresh_fn}
(async () => {{
  await refreshCurrentDetails();
  process.stdout.write(JSON.stringify({{ loaded }}));
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    payload = json.loads(proc.stdout)

    assert payload["loaded"] == "R-selected"
