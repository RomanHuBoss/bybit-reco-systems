from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

ALLOWED_DIRECTIONS = {"long", "short", "neutral"}
PROMPT_VERSION = "ohlcv_multitf_v1"
SUPPORTED_TF_SECS = (60, 15 * 60, 30 * 60, 60 * 60, 4 * 60 * 60, 24 * 60 * 60)


SYSTEM_PROMPT = (
    "You review crypto market data for grid-bot suitability. "
    "Use only the supplied data. Return JSON only. Prefer neutral when ambiguous. "
    "A strong one-way breakout is usually bad for grid bots. "
    "For spot_grid, execution_direction must be long or neutral; if thesis is short, execution_direction must be neutral. "
    "Response schema: "
    '{"thesis_direction":"long|short|neutral","execution_direction":"long|short|neutral",'
    '"confidence":0.0,"regime_view":"string","risk_flags":["flag"],"summary":"short text"}'
)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


@dataclass
class LLMReviewResult:
    provider: str
    model: str
    prompt_version: str = PROMPT_VERSION
    status: str = "ok"
    thesis_direction: str = "neutral"
    execution_direction: str = "neutral"
    confidence: float = 0.0
    regime_view: str = "unknown"
    risk_flags: list[str] = field(default_factory=list)
    summary: str | None = None
    agree_with_engine: bool | None = None
    latency_ms: int | None = None
    error: str | None = None
    raw_response: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        if out.get("raw_response") and len(str(out["raw_response"])) > 1200:
            out["raw_response"] = str(out["raw_response"])[:1200]
        return out

    @classmethod
    def error_result(cls, provider: str, model: str, error: str, latency_ms: int | None = None) -> "LLMReviewResult":
        return cls(provider=provider, model=model, status="error", error=str(error), latency_ms=latency_ms)

    @classmethod
    def skipped_result(cls, provider: str, model: str, error: str | None = None) -> "LLMReviewResult":
        return cls(provider=provider, model=model, status="skipped", error=error)



def normalize_direction(value: Any, *, allow_short: bool = True) -> str:
    s = str(value or "").strip().lower()
    if s in ALLOWED_DIRECTIONS:
        if s == "short" and not allow_short:
            return "neutral"
        return s
    return "neutral"



def parse_tf_secs(raw: str | None) -> list[int]:
    if not raw:
        return [15 * 60, 60 * 60, 4 * 60 * 60]
    out: list[int] = []
    seen: set[int] = set()
    for part in str(raw).split(","):
        p = part.strip().lower()
        if not p:
            continue
        if p.endswith("m"):
            val = int(p[:-1]) * 60
        elif p.endswith("h"):
            val = int(p[:-1]) * 3600
        elif p.endswith("d"):
            val = int(p[:-1]) * 86400
        else:
            val = int(float(p))
        if val in SUPPORTED_TF_SECS and val not in seen:
            out.append(val)
            seen.add(val)
    return out or [15 * 60, 60 * 60, 4 * 60 * 60]



def tf_label(tf_sec: int) -> str:
    if tf_sec % 86400 == 0:
        return f"{tf_sec // 86400}d"
    if tf_sec % 3600 == 0:
        return f"{tf_sec // 3600}h"
    if tf_sec % 60 == 0:
        return f"{tf_sec // 60}m"
    return f"{tf_sec}s"



def _extract_first_json_object(text: str) -> str | None:
    if not text:
        return None
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        return text
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        return fenced.group(1)
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:idx + 1]
    return None



def parse_review_content(text: str, *, bot_type: str, engine_direction: str) -> dict[str, Any]:
    raw = (text or "").strip()
    obj: dict[str, Any] | None = None
    last_error: Exception | None = None

    for candidate in (raw, _extract_first_json_object(raw)):
        if not candidate:
            continue
        try:
            loaded = json.loads(candidate)
            if isinstance(loaded, dict):
                obj = loaded
                break
        except Exception as exc:  # pragma: no cover - exercised via fallback path
            last_error = exc

    if obj is None:
        raise ValueError(f"cannot parse LLM JSON: {last_error or 'no json object found'}")

    allow_short_exec = bot_type != "spot_grid"
    thesis_direction = normalize_direction(
        obj.get("thesis_direction") or obj.get("direction") or obj.get("verdict"),
        allow_short=True,
    )
    execution_direction = normalize_direction(
        obj.get("execution_direction") or obj.get("verdict") or thesis_direction,
        allow_short=allow_short_exec,
    )
    if bot_type == "spot_grid" and thesis_direction == "short":
        execution_direction = "neutral"

    confidence = _clamp(float(obj.get("confidence", 0.0) or 0.0), 0.0, 1.0)
    regime_view = str(obj.get("regime_view") or obj.get("regime") or "unknown").strip() or "unknown"
    risk_flags_raw = obj.get("risk_flags") or []
    if isinstance(risk_flags_raw, str):
        risk_flags = [risk_flags_raw.strip()] if risk_flags_raw.strip() else []
    elif isinstance(risk_flags_raw, list):
        risk_flags = [str(x).strip() for x in risk_flags_raw if str(x).strip()][:8]
    else:
        risk_flags = []
    summary = str(obj.get("summary") or obj.get("comment") or "").strip() or None
    if summary and len(summary) > 240:
        summary = summary[:240]

    return {
        "thesis_direction": thesis_direction,
        "execution_direction": execution_direction,
        "confidence": confidence,
        "regime_view": regime_view,
        "risk_flags": risk_flags,
        "summary": summary,
        "agree_with_engine": execution_direction == normalize_direction(engine_direction, allow_short=allow_short_exec),
        "raw_response": raw,
    }



def build_review_payload(
    *,
    rec: dict[str, Any],
    feature_snapshot: dict[str, Any],
    direction_agg: dict[str, Any],
    market_shock: dict[str, Any],
    sentiment_summary: dict[str, Any],
    candles_by_tf: dict[int, list[list[float | int]]],
) -> dict[str, Any]:
    params = rec.get("params") or {}
    reasons = rec.get("reasons") or {}
    execution_constraints = reasons.get("execution_constraints") or {}
    funding = reasons.get("funding") or {}
    oi = reasons.get("open_interest") or {}
    fast_veto = reasons.get("fast_veto") or {}

    return {
        "schema_version": PROMPT_VERSION,
        "candidate": {
            "venue": rec.get("venue"),
            "symbol": rec.get("symbol"),
            "bot_type": rec.get("bot_type"),
            "engine_execution_direction": rec.get("direction"),
            "engine_raw_direction": execution_constraints.get("raw_direction") or direction_agg.get("raw_direction") or direction_agg.get("direction"),
            "engine_status": rec.get("status"),
            "score": round(float(rec.get("score") or 0.0), 6),
            "confidence": round(float(rec.get("confidence") or 0.0), 6),
            "expected_rr": round(float(rec.get("expected_rr") or 0.0), 6),
            "risk_score": round(float(rec.get("risk_score") or 0.0), 6),
            "grid_levels": params.get("grid_levels"),
            "grid_spacing_pct": params.get("grid_spacing_pct"),
            "price_range_lower": params.get("price_range_lower"),
            "price_range_upper": params.get("price_range_upper"),
        },
        "market_context": {
            "feature_snapshot": feature_snapshot,
            "direction_agg": {
                "direction": direction_agg.get("direction"),
                "raw_direction": direction_agg.get("raw_direction"),
                "bias": direction_agg.get("bias"),
                "direction_mode": direction_agg.get("direction_mode"),
                "regime": direction_agg.get("regime"),
                "regime_confidence": direction_agg.get("regime_confidence"),
                "coherence": direction_agg.get("coherence"),
                "trendiness": direction_agg.get("trendiness"),
                "score_all": (direction_agg.get("scores") or {}).get("all"),
            },
            "market_shock": {
                "state": market_shock.get("state"),
                "guard_blocks_neutral": market_shock.get("guard_blocks_neutral"),
            },
            "sentiment": sentiment_summary,
            "funding": {
                "signal": funding.get("signal"),
                "value": funding.get("value"),
                "expected_funding_bps": funding.get("expected_funding_bps"),
                "expected_funding_events": funding.get("expected_funding_events"),
            },
            "open_interest": {
                "trend": oi.get("trend"),
                "signal": oi.get("signal"),
                "oi_4h_chg_pct": oi.get("oi_4h_chg_pct"),
                "oi_24h_chg_pct": oi.get("oi_24h_chg_pct"),
            },
            "execution_constraints": {
                "spot_short_neutralized": bool(execution_constraints.get("spot_short_neutralized")),
                "fast_veto_state": fast_veto.get("state"),
            },
            "candles_by_tf": {tf_label(tf): rows for tf, rows in sorted(candles_by_tf.items()) if rows},
        },
    }


class OllamaCandleReviewer:
    provider = "ollama"

    def __init__(self, *, base_url: str, model: str, timeout_sec: int = 20):
        self.base_url = str(base_url or "http://127.0.0.1:11434").rstrip("/")
        self.model = str(model or "").strip()
        self.timeout_sec = max(3, int(timeout_sec or 20))

    def _request_chat(self, payload: dict[str, Any]) -> str:
        req = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
            ],
        }
        with httpx.Client(timeout=self.timeout_sec) as client:
            resp = client.post(f"{self.base_url}/api/chat", json=req)
            resp.raise_for_status()
            data = resp.json()
        message = data.get("message") if isinstance(data, dict) else None
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content
        raise ValueError("ollama /api/chat returned no message.content")

    def _request_generate(self, payload: dict[str, Any]) -> str:
        prompt = SYSTEM_PROMPT + "\n\nINPUT:\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        req = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
            "prompt": prompt,
        }
        with httpx.Client(timeout=self.timeout_sec) as client:
            resp = client.post(f"{self.base_url}/api/generate", json=req)
            resp.raise_for_status()
            data = resp.json()
        content = data.get("response") if isinstance(data, dict) else None
        if isinstance(content, str) and content.strip():
            return content
        raise ValueError("ollama /api/generate returned no response")

    def review(self, payload: dict[str, Any]) -> LLMReviewResult:
        t0 = time.time()
        if not self.model:
            return LLMReviewResult.skipped_result(self.provider, self.model, error="model is empty")
        candidate = payload.get("candidate") or {}
        bot_type = str(candidate.get("bot_type") or "")
        engine_direction = str(candidate.get("engine_execution_direction") or "neutral")
        try:
            try:
                content = self._request_chat(payload)
            except Exception:
                content = self._request_generate(payload)
            parsed = parse_review_content(content, bot_type=bot_type, engine_direction=engine_direction)
            return LLMReviewResult(
                provider=self.provider,
                model=self.model,
                latency_ms=int((time.time() - t0) * 1000),
                **parsed,
            )
        except Exception as exc:
            return LLMReviewResult.error_result(
                self.provider,
                self.model,
                str(exc),
                latency_ms=int((time.time() - t0) * 1000),
            )
