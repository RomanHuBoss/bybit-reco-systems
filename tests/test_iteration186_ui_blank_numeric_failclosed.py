from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "app" / "ui" / "static" / "app.js"


def _extract_js_function(source: str, name: str) -> str:
    match = re.search(rf"function {re.escape(name)}\([^)]*\) \{{", source)
    assert match, f"function {name} not found in app.js"
    i = match.start()
    j = match.end()
    depth = 1
    while j < len(source) and depth:
        if source[j] == "{":
            depth += 1
        elif source[j] == "}":
            depth -= 1
        j += 1
    assert depth == 0, f"function {name} body is not balanced"
    return source[i:j]


def test_frontend_to_finite_number_rejects_blank_and_null_inputs() -> None:
    """Blank UI/API fields must stay unknown, not become zero prices or sizes."""
    source = APP_JS.read_text(encoding="utf-8")
    fn = _extract_js_function(source, "toFiniteNumber")
    script = fn + r'''
const out = {
  emptyString: toFiniteNumber(""),
  whitespaceString: toFiniteNumber("   \t"),
  nullValue: toFiniteNumber(null),
  undefinedValue: toFiniteNumber(undefined),
  zeroString: toFiniteNumber("0"),
  numberZero: toFiniteNumber(0),
  positiveString: toFiniteNumber("123.45")
};
console.log(JSON.stringify(out));
'''
    result = subprocess.run(["node", "-e", script], check=True, text=True, capture_output=True)
    out = json.loads(result.stdout)

    assert out["emptyString"] is None
    assert out["whitespaceString"] is None
    assert out["nullValue"] is None
    assert out["undefinedValue"] is None
    assert out["zeroString"] == 0
    assert out["numberZero"] == 0
    assert out["positiveString"] == 123.45


def test_static_asset_cache_key_bumped_after_blank_numeric_failclosed_patch() -> None:
    index = (ROOT / "app/ui/static/index.html").read_text(encoding="utf-8")

    assert "styles.css?v=manual-ui-v41" in index
    assert "app.js?v=manual-ui-v41" in index
