from __future__ import annotations

from typing import Any

import app.calibration as calibration


def test_purged_train_indices_exclude_unmatured_and_same_timestamp_labels() -> None:
    from app.calibration import _purged_train_indices

    timestamps = [100, 100, 110, 120, 130]
    label_available_tss = [101, 120, 130, 140, 150]

    # Validation begins at ts=120. Only the first label was observable strictly
    # before that decision. A label completing at ts=120 is still purged.
    assert _purged_train_indices(
        timestamps, label_available_tss, validation_start_index=3
    ) == [0]

    # A duplicate timestamp must never be split across train and validation, and
    # legacy rows without exact availability are excluded from OOF training.
    assert _purged_train_indices([100, 100, 110], [100, 100, 110], validation_start_index=1) == []
    assert _purged_train_indices([100, 110, 120], [None, 111, 121], validation_start_index=2) == [1]


def test_fit_logreg_passes_exact_label_availability_into_walk_forward_oof(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_extract_features(row: dict[str, Any]) -> list[float]:
        return [float(row["score"])]

    def fake_oof(
        X: list[list[float]],
        ys: list[int],
        ws: list[float],
        *,
        min_samples: int,
        tss: list[int] | None = None,
        label_available_tss: list[int | None] | None = None,
    ) -> tuple[list[float], list[int], list[float]]:
        captured["tss"] = tss
        captured["label_available_tss"] = label_available_tss
        return [], [], []

    monkeypatch.setattr(calibration, "extract_features", fake_extract_features)
    monkeypatch.setattr(calibration, "_time_series_oof_logits", fake_oof)
    monkeypatch.setattr(calibration, "_fit_weighted_logreg_raw", lambda X, ys, ws, **kwargs: ([1.0], 0.0))

    rows = [
        {
            "score": -0.8 + i * 0.2,
            "success": i % 2,
            "ret": 0.08 if i % 2 else -0.01,
            # This test isolates OOF timestamp forwarding, so its monetary
            # fixtures must be temporally independent rather than an overlap chain.
            "ts": 1_000 + i * 300,
            "horizon_sec": 180,
            "label_available_ts": 1_000 + i * 300 + 240,
            "reasons": {},
        }
        for i in range(8)
    ]

    model = calibration.fit_logreg(rows, min_samples=2, logreg_min_samples=4)

    assert model.fitted is True
    assert model.coef == []
    assert model.oof_status == "insufficient"
    assert model.oof_samples == 0
    assert model.oof_required_samples == 2
    assert captured["tss"] == [row["ts"] for row in rows]
    assert captured["label_available_tss"] == [
        row["label_available_ts"] for row in rows
    ]
