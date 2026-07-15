from __future__ import annotations

import json
import time
from types import SimpleNamespace

from app import db, db_backend, recommender


class _BatchOnlyCursor:
    def __init__(self, rows):
        self._rows = list(rows)
        self._offset = 0
        self.fetchmany_sizes: list[int] = []
        self.closed = False

    def fetchall(self):
        raise AssertionError("hot-path query must not materialize the full result with fetchall()")

    def fetchmany(self, size: int):
        self.fetchmany_sizes.append(int(size))
        batch = self._rows[self._offset : self._offset + int(size)]
        self._offset += len(batch)
        return batch

    def close(self):
        self.closed = True


class _StreamingGuardConnection:
    def __init__(self, rows):
        self.cursor = _BatchOnlyCursor(rows)
        self.last_sql = ""
        self.last_params = ()

    def execute(self, sql, params=()):
        self.last_sql = str(sql)
        self.last_params = tuple(params or ())
        return self.cursor


def _outcome_row(*, fingerprint: str) -> dict:
    ts = int(time.time()) - 13 * 3600
    contract = {"schema_version": "candidate-policy-v1", "floor": 0.25}
    return {
        "rec_id": "R-254-1",
        "ts": ts,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "recommendation_direction": "long",
        "score": 0.5,
        "status": "executed",
        "reasons_json": json.dumps({
            "feature_snapshot": {
                "mean_reversion_evidence_valid": 1,
                "mean_reversion_score": 0.5,
                "range_score": 0.7,
            },
            "outcome_policy": {
                "eligible": True,
                "policy_evaluation_eligible": True,
                "policy_fingerprint": fingerprint,
                "policy_contract": contract,
                "label_due_ts": ts + 12 * 3600 + 120,
            },
            "direction_agg": {"direction": "long", "direction_confidence": 0.8},
            "large_unneeded_diagnostic": "x" * 100_000,
        }),
        "model_version": recommender.RECOMMENDER_MODEL_VERSION,
        "is_outcome_label_root": 1,
        "outcome_rec_id": "R-254-1",
        "outcome_ts": ts,
        "outcome_venue": "linear",
        "outcome_symbol": "BTCUSDT",
        "outcome_bot_type": "futures_grid",
        "outcome_direction": "long",
        "horizon_sec": 12 * 3600,
        "label_available_ts": ts + 12 * 3600 + 60,
        "entry_close": 100.0,
        "exit_close": 101.0,
        "success": 1,
        "ret": 0.01,
        "observability_state": "labeled",
        "observability_reason": "outcome_inserted",
    }


def test_policy_observability_uses_bounded_batches_and_sql_bot_filter() -> None:
    fingerprint = recommender.calibration_policy_contract_fingerprint(
        {"schema_version": "candidate-policy-v1", "floor": 0.25}
    )
    row = _outcome_row(fingerprint=fingerprint)
    conn = _StreamingGuardConnection([row])

    diagnostics = db.get_policy_outcome_observability(
        conn,
        model_version=recommender.RECOMMENDER_MODEL_VERSION,
        policy_fingerprint=fingerprint,
        bot_type="futures_grid",
        now_ts_value=int(time.time()),
    )

    assert diagnostics["matured_total"] == 1
    assert diagnostics["labeled_total"] == 1
    assert conn.cursor.fetchmany_sizes
    assert max(conn.cursor.fetchmany_sizes) <= 512
    assert conn.cursor.closed is True
    normalized_sql = " ".join(conn.last_sql.split())
    assert "r.bot_type=?" in normalized_sql
    assert "ORDER BY" not in normalized_sql.upper()


def test_calibration_outcome_reader_streams_and_compacts_reasons() -> None:
    fingerprint = recommender.calibration_policy_contract_fingerprint(
        {"schema_version": "candidate-policy-v1", "floor": 0.25}
    )
    raw = _outcome_row(fingerprint=fingerprint)
    row = {
        "rec_id": raw["rec_id"],
        "ts": raw["ts"],
        "venue": raw["venue"],
        "symbol": raw["symbol"],
        "bot_type": raw["bot_type"],
        "direction": raw["recommendation_direction"],
        "horizon_sec": raw["horizon_sec"],
        "label_available_ts": raw["label_available_ts"],
        "entry_close": raw["entry_close"],
        "exit_close": raw["exit_close"],
        "success": raw["success"],
        "ret": raw["ret"],
        "score": raw["score"],
        "status": raw["status"],
        "reasons_json": raw["reasons_json"],
        "model_version": raw["model_version"],
        "publication_root_rec_id": raw["rec_id"],
        "is_outcome_label_root": 1,
    }
    conn = _StreamingGuardConnection([row])

    rows = db.get_outcomes_with_recs(
        conn,
        limit=200_000,
        calibration_compact=True,
        batch_size=64,
    )

    assert len(rows) == 1
    reasons = rows[0]["reasons"]
    assert set(reasons) == {"feature_snapshot", "outcome_policy", "direction_agg"}
    assert "large_unneeded_diagnostic" not in reasons
    assert conn.cursor.fetchmany_sizes == [64, 64]
    assert conn.cursor.closed is True


def test_shared_calibration_evidence_loads_policy_rows_once(monkeypatch) -> None:
    fingerprint = "a" * 64
    calls = {"rows": 0, "observability": 0}
    source_rows = [{
        "bot_type": "futures_grid",
        "model_version": recommender.RECOMMENDER_MODEL_VERSION,
        "reasons": {
            "feature_snapshot": {
                "mean_reversion_evidence_valid": 1,
                "mean_reversion_score": 0.5,
            },
            "outcome_policy": {
                "policy_evaluation_eligible": True,
                "policy_fingerprint": fingerprint,
                "policy_contract": {},
                "label_due_ts": 1,
            },
        },
    }]

    def _rows(*_args, **kwargs):
        calls["rows"] += 1
        assert kwargs["calibration_compact"] is True
        return list(source_rows)

    def _observability(*_args, **_kwargs):
        calls["observability"] += 1
        return {"policy_fingerprint": fingerprint, "matured_total": 0}

    monkeypatch.setattr(db, "get_outcomes_with_recs", _rows)
    monkeypatch.setattr(db, "get_policy_outcome_observability", _observability)
    monkeypatch.setattr(
        recommender,
        "_current_range_edge_calibration_rows",
        lambda rows, **_kwargs: list(rows),
    )

    evidence = recommender._CalibrationEvidenceContext(
        conn=object(),
        min_samples=80,
        policy_fingerprint=fingerprint,
        settings_obj=SimpleNamespace(
            llm_reviewer_enabled=False,
            mean_reversion_min_score=0.25,
        ),
    )

    first = evidence.policy_rows()
    second = evidence.policy_rows()
    assert first is second
    evidence.observability(None)
    evidence.observability(None)
    assert calls == {"rows": 1, "observability": 1}
    evidence.release_rows()
    assert evidence._policy_rows is None


def test_postgres_large_read_uses_named_server_cursor() -> None:
    class _RawCursor:
        def __init__(self):
            self.itersize = 0
            self.executed = None
            self.closed = False

        def execute(self, sql, params):
            self.executed = (sql, tuple(params))

        def fetchmany(self, size=None):
            return []

        def close(self):
            self.closed = True

    class _RawConnection:
        def __init__(self):
            self.cursor_name = None
            self.raw_cursor = _RawCursor()

        def cursor(self, name=None):
            self.cursor_name = name
            return self.raw_cursor

    raw = _RawConnection()
    connection = object.__new__(db_backend.PostgresConnection)
    connection._conn = raw

    cursor = connection.execute_stream(
        "SELECT ? AS value",
        (7,),
        batch_size=32,
    )

    assert str(raw.cursor_name).startswith("bybit_stream_")
    assert raw.raw_cursor.itersize == 32
    assert raw.raw_cursor.executed == ("SELECT %s AS value", (7,))
    cursor.close()
    assert raw.raw_cursor.closed is True


def test_outcome_worker_liveness_uses_bounded_batches() -> None:
    now = int(time.time())
    ts = now - 13 * 3600
    conn = _StreamingGuardConnection([{
        "rec_id": "R-254-live",
        "ts": ts,
        "bot_type": "futures_grid",
        "status": "executed",
        "reasons_json": "{}",
        "last_attempt_ts": None,
        "label_due_ts": ts + 12 * 3600 + 120,
        "observability_state": "waiting",
    }])

    status = db.get_outcome_worker_liveness(conn, now_ts_value=now)

    assert status["state"] == "stalled"
    assert status["matured_pending_total"] == 1
    assert status["unattempted_total"] == 1
    assert conn.cursor.fetchmany_sizes
    assert conn.cursor.closed is True


def test_lineage_status_mode_aggregates_without_retaining_rows(monkeypatch) -> None:
    now = 1_800_000_000
    monkeypatch.setattr(recommender.time, "time", lambda: now)
    contract = {"schema_version": "candidate-policy-v1", "floor": 0.25}
    fingerprint = recommender.calibration_policy_contract_fingerprint(contract)
    ts = now - 13 * 3600
    rows = ({
        "bot_type": "futures_grid",
        "success": success,
        "ts": ts + offset,
        "model_version": recommender.RECOMMENDER_MODEL_VERSION,
        "reasons": {
            "feature_snapshot": {
                "mean_reversion_evidence_valid": 1,
                "mean_reversion_score": 0.5,
            },
            "outcome_policy": {
                "policy_evaluation_eligible": True,
                "policy_fingerprint": fingerprint,
                "policy_contract": contract,
                "label_due_ts": ts + offset + 12 * 3600 + 120,
            },
        },
    } for offset, success in ((0, 1), (60, 0)))

    diagnostics = recommender.calibration_lineage_diagnostics(
        rows,
        policy_fingerprint=fingerprint,
        mean_reversion_min_score=0.25,
        retain_rows=False,
        recent_cutoff_ts=now - 7 * 86400,
    )

    assert diagnostics["historical_total"] == 2
    assert diagnostics["policy_eligible_total"] == 2
    assert diagnostics["current_model_rows"] == []
    assert diagnostics["feature_eligible_rows"] == []
    assert diagnostics["policy_eligible_rows"] == []
    stats = diagnostics["stats_by_bot"]["policy_eligible"]["futures_grid"]
    assert stats["total"] == 2
    assert stats["wins"] == 1
    assert stats["losses"] == 1
