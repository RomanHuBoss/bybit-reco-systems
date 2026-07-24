from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app" / "ui" / "static" / "app.js"
STYLES = ROOT / "app" / "ui" / "static" / "styles.css"
INDEX = ROOT / "app" / "ui" / "static" / "index.html"


def _extract_function(source: str, name: str) -> str:
    start = source.index(f"function {name}")
    next_function = source.index("\n\nfunction ", start + 1)
    return source[start:next_function]


def test_results_keeps_one_canonical_strategy_table_in_primary_section() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index('<div class="modal-section-title">Стратегии</div>')
    end = source.index('<div class="modal-section-title">На что смотреть в первую очередь</div>', start)
    section = source[start:end]

    assert section.count("buildModalTable([") == 1
    assert "byBot" in section
    assert "eventTypeByBotRows" not in section
    assert "eventTypeRows" not in section
    assert "Успех по контракту" in section


def test_health_and_outcomes_use_compact_1600px_modal_contract() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    styles = STYLES.read_text(encoding="utf-8")
    index = INDEX.read_text(encoding="utf-8")

    assert 'showModalHtml("Здоровье системы", html, { wide: true })' in source
    assert 'showModalHtml("Результаты наблюдений", html, { wide: true })' in source
    assert 'width: min(1600px, calc(100vw - 32px));' in styles
    assert 'height: min(88vh, 900px);' in styles
    assert 'max-height: calc(100vh - 32px);' in styles
    assert ".modal-table-two-column" in styles
    assert ".modal-table-many-columns" in styles
    assert "maxHeight: 520" in source
    assert "maxHeight: 560" in source
    assert "ui=direction-observability-journal-v3" in index


def test_modal_layout_toggles_wide_class_in_javascript_runtime() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    fn = _extract_function(source, "configureModalLayout")
    script = f"""
const toggles = [];
const card = {{ classList: {{ toggle: (name, enabled) => toggles.push([name, enabled]) }} }};
global.document = {{ querySelector: (selector) => selector === '#modal .modal-card' ? card : null }};
{fn}
configureModalLayout({{ wide: true }});
configureModalLayout();
process.stdout.write(JSON.stringify(toggles));
"""
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    assert json.loads(completed.stdout) == [
        ["modal-card-wide", True],
        ["modal-card-wide", False],
    ]
