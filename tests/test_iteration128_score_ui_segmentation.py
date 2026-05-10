from __future__ import annotations

import json
import subprocess
from pathlib import Path


def _run_score_ui_meta(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    app_js = Path("app/ui/static/app.js").read_text(encoding="utf-8")
    prefix = app_js.split("function copyButton", 1)[0]
    script = (
        prefix
        + "\n"
        + "const rows = "
        + json.dumps(rows)
        + ";\n"
        + "const map = computeUiScoreMetaMap(rows);\n"
        + "console.log(JSON.stringify(rows.map((row) => map.get(row.rec_id))));\n"
    )
    result = subprocess.run(
        ["node", "-e", script],
        cwd=Path.cwd(),
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


def test_score_ui_groups_nearly_identical_raw_scores_instead_of_hard_rank_percentiles() -> None:
    # Previously 3 close candidates such as 0.245 / 0.242 / 0.232 were displayed as
    # 100 / 50 / 0 purely because there were only three visible rows. That is a
    # false precision signal: their raw-score spread is below the material UI delta.
    meta = _run_score_ui_meta([
        {"rec_id": "top", "score": 0.245},
        {"rec_id": "mid", "score": 0.242},
        {"rec_id": "low", "score": 0.232},
    ])

    assert {row["percentile"] for row in meta} == {50}
    assert {row["grade"] for row in meta} == {"C"}
    assert {row["groupSize"] for row in meta} == {3}
    assert all("near-tie" in row["title"] for row in meta)


def test_score_ui_still_separates_materially_different_score_groups() -> None:
    meta = _run_score_ui_meta([
        {"rec_id": "top", "score": 0.600},
        {"rec_id": "near_top", "score": 0.585},
        {"rec_id": "far", "score": 0.300},
    ])

    by_id = {row_id: row for row_id, row in zip(["top", "near_top", "far"], meta)}
    assert by_id["top"]["percentile"] == by_id["near_top"]["percentile"] == 75
    assert by_id["far"]["percentile"] == 0
    assert by_id["top"]["groupSize"] == by_id["near_top"]["groupSize"] == 2
    assert by_id["far"]["groupSize"] == 1


def test_score_ui_copy_explains_near_tie_semantics() -> None:
    app_js = Path("app/ui/static/app.js").read_text(encoding="utf-8")
    index_html = Path("app/ui/static/index.html").read_text(encoding="utf-8")

    assert "SCORE_UI_NEAR_TIE_DELTA" in app_js
    assert "near-tie группа" in app_js
    assert "перцентиль с near-tie группировкой близких raw score" in index_html
