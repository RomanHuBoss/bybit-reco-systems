from __future__ import annotations

import json
import subprocess
from pathlib import Path

from app import db


def _extract_js_function(source: str, name: str) -> str:
    marker = f"function {name}("
    start = source.find(marker)
    assert start >= 0, f"missing production JS function {name}"
    brace = source.find("{", start)
    assert brace >= 0
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(brace, len(source)):
        char = source[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"', "`"}:
            quote = char
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated JS function {name}")


def test_labeled_outcome_preserves_terminal_diagnostics_in_audit_read_model(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "outcome-audit.db"))
    db.init_db(conn)
    try:
        db.insert_outcome(
            conn,
            {
                "rec_id": "R-outcome-audit-263",
                "ts": 1_784_000_000,
                "venue": "linear",
                "symbol": "WLDUSDT",
                "bot_type": "futures_grid",
                "direction": "neutral",
                "horizon_sec": 43_200,
                "label_available_ts": 1_784_043_200,
                "entry_close": 1.0,
                "exit_close": 1.02,
                "ret": 0.0135,
                "success": 0,
                "diagnostics": {
                    "stopped": True,
                    "terminal_reason": "kill_switch_breached",
                    "kill_switch_breach_side": "upper",
                    "kill_switch_boundary_price": 1.01,
                    "kill_switch_observed_extreme": 1.02,
                    "kill_switch_liquidation_price": 1.02,
                },
            },
        )

        stored = conn.execute(
            "SELECT details_json FROM reco_outcome_observability WHERE rec_id=?",
            ("R-outcome-audit-263",),
        ).fetchone()
        assert stored is not None
        details = json.loads(stored["details_json"])
        assert details["stopped"] is True
        assert details["terminal_reason"] == "kill_switch_breached"
        assert details["kill_switch_breach_side"] == "upper"

        recent = db.get_outcomes_recent_enriched(conn, limit=10, scope="archive")
        assert len(recent) == 1
        row = recent[0]
        assert row["outcome_diagnostics"]["stopped"] is True
        assert row["outcome_diagnostics"]["kill_switch_boundary_price"] == 1.01
    finally:
        conn.close()


def test_outcome_ui_rejects_boolean_numeric_payloads_and_explains_kill_switch() -> None:
    source = Path("app/ui/static/app.js").read_text(encoding="utf-8")
    functions = "\n".join(
        _extract_js_function(source, name)
        for name in (
            "toFiniteNumber",
            "toStrictInteger",
            "fmtPct",
            "renderOutcomeResult",
            "renderOutcomeReturn",
            "outcomeReasonText",
        )
    )
    script = f"""
function escapeHtml(value) {{ return String(value ?? ""); }}
{functions}
const cases = {{
  booleanSuccess: renderOutcomeResult(true, {{}}),
  integerSuccess: renderOutcomeResult(1, {{}}),
  booleanReturn: renderOutcomeReturn(true),
  zeroReturn: renderOutcomeReturn(0),
  reason: outcomeReasonText({{
    success: 0,
    ret: 0.0135,
    outcome_diagnostics: {{
      stopped: true,
      terminal_reason: 'kill_switch_breached',
      kill_switch_breach_side: 'upper',
      kill_switch_boundary_price: 1.01,
      kill_switch_observed_extreme: 1.02
    }}
  }})
}};
console.log(JSON.stringify(cases));
"""
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        text=True,
        capture_output=True,
    )
    payload = json.loads(result.stdout)
    assert "Неизвестно" in payload["booleanSuccess"]
    assert "Успех" in payload["integerSuccess"]
    assert payload["booleanReturn"] == "—"
    assert payload["zeroReturn"] == "+0.00%"
    assert "верх" in payload["reason"].lower()
    assert "1.01" in payload["reason"]
