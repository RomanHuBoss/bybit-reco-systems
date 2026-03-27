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
DEFAULT_KEEP_ALIVE = "15m"


SYSTEM_PROMPT = (
    "Ты проверяешь пригодность крипторынка для grid-ботов. "
    "Используй только переданные данные. Верни только JSON, без markdown и пояснений. "
    "Если картина неоднозначна, предпочитай neutral. "
    "Сильный однонаправленный пробой обычно плох для grid-ботов. "
    "Для spot_grid поле execution_direction может быть только long или neutral; если thesis_direction=short, то execution_direction обязан быть neutral. "
    "Значения thesis_direction и execution_direction возвращай только как long, short или neutral. "
    "Поля regime_view, risk_flags и summary пиши по-русски. "
    "summary должен быть коротким, понятным и читабельным для оператора. "
    "Response schema: "
    '{"thesis_direction":"long|short|neutral","execution_direction":"long|short|neutral",'
    '"confidence":0.0,"regime_view":"русский текст","risk_flags":["русский флаг"],"summary":"краткое русское резюме"}'
)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _trim_text(value: Any, limit: int = 600) -> str | None:
    if value is None:
        return None
    s = str(value)
    return s if len(s) <= limit else s[:limit]


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
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        if out.get("raw_response") and len(str(out["raw_response"])) > 1200:
            out["raw_response"] = str(out["raw_response"])[:1200]
        diag = out.get("diagnostics") or {}
        if isinstance(diag, dict):
            cleaned: dict[str, Any] = {}
            for key, value in diag.items():
                if isinstance(value, (str, int, float, bool)) or value is None:
                    cleaned[key] = _trim_text(value, 800) if isinstance(value, str) else value
                elif isinstance(value, dict):
                    cleaned[key] = {str(k): _trim_text(v, 400) if isinstance(v, str) else v for k, v in value.items()}
                else:
                    cleaned[key] = _trim_text(value, 400)
            out["diagnostics"] = cleaned
        return out

    @classmethod
    def error_result(
        cls,
        provider: str,
        model: str,
        error: str,
        latency_ms: int | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> "LLMReviewResult":
        return cls(
            provider=provider,
            model=model,
            status="error",
            error=str(error),
            latency_ms=latency_ms,
            diagnostics=diagnostics or {},
        )

    @classmethod
    def skipped_result(cls, provider: str, model: str, error: str | None = None) -> "LLMReviewResult":
        return cls(provider=provider, model=model, status="skipped", error=error)



def normalize_direction(value: Any, *, allow_short: bool = True) -> str:
    s = str(value or "").strip().lower()
    aliases = {
        "лонг": "long",
        "длинная": "long",
        "длинный": "long",
        "длинная позиция": "long",
        "long bias": "long",
        "шорт": "short",
        "короткая": "short",
        "короткий": "short",
        "короткая позиция": "short",
        "short bias": "short",
        "нейтрал": "neutral",
        "нейтрально": "neutral",
        "нейтральный": "neutral",
        "нейтральная": "neutral",
        "без сделки": "neutral",
        "no_trade": "neutral",
    }
    s = aliases.get(s, s)
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

    def __init__(self, *, base_url: str, model: str, timeout_sec: int = 60, keep_alive: str = DEFAULT_KEEP_ALIVE):
        self.base_url = str(base_url or "http://127.0.0.1:11434").rstrip("/")
        self.model = str(model or "").strip()
        self.timeout_sec = max(5, int(timeout_sec or 60))
        self.keep_alive = str(keep_alive or DEFAULT_KEEP_ALIVE).strip() or DEFAULT_KEEP_ALIVE

    def _http_timeout(self) -> httpx.Timeout:
        read_timeout = float(self.timeout_sec)
        connect_timeout = min(10.0, max(3.0, read_timeout / 3.0))
        return httpx.Timeout(connect=connect_timeout, read=read_timeout, write=15.0, pool=15.0)

    def _base_request_fields(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "stream": False,
            "format": "json",
            "think": False,
            "keep_alive": self.keep_alive,
            "options": {"temperature": 0},
        }

    def _post_json(self, endpoint: str, req: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        with httpx.Client(timeout=self._http_timeout()) as client:
            resp = client.post(f"{self.base_url}{endpoint}", json=req)
            status_code = resp.status_code
            text = resp.text
            resp.raise_for_status()
            data = resp.json()
        meta = {
            "endpoint": endpoint,
            "http_status": status_code,
            "done": data.get("done") if isinstance(data, dict) else None,
            "done_reason": data.get("done_reason") if isinstance(data, dict) else None,
            "eval_count": data.get("eval_count") if isinstance(data, dict) else None,
            "total_duration_ns": data.get("total_duration") if isinstance(data, dict) else None,
            "load_duration_ns": data.get("load_duration") if isinstance(data, dict) else None,
            "response_preview": _trim_text(text, 400),
        }
        return data, meta

    def _request_chat(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        req = {
            **self._base_request_fields(),
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
            ],
        }
        data, meta = self._post_json("/api/chat", req)
        message = data.get("message") if isinstance(data, dict) else None
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content, meta
        fallback_response = data.get("response") if isinstance(data, dict) else None
        if isinstance(fallback_response, str) and fallback_response.strip():
            meta["fallback_field"] = "response"
            return fallback_response, meta
        raise ValueError(
            "ollama /api/chat returned no message.content"
            f" (done={meta.get('done')}, done_reason={meta.get('done_reason')}, eval_count={meta.get('eval_count')})"
        )

    def _request_generate(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        prompt = SYSTEM_PROMPT + "\n\nINPUT:\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        req = {
            **self._base_request_fields(),
            "prompt": prompt,
        }
        data, meta = self._post_json("/api/generate", req)
        content = data.get("response") if isinstance(data, dict) else None
        if isinstance(content, str) and content.strip():
            return content, meta
        message = data.get("message") if isinstance(data, dict) else None
        if isinstance(message, dict):
            msg_content = message.get("content")
            if isinstance(msg_content, str) and msg_content.strip():
                meta["fallback_field"] = "message.content"
                return msg_content, meta
        raise ValueError(
            "ollama /api/generate returned no response"
            f" (done={meta.get('done')}, done_reason={meta.get('done_reason')}, eval_count={meta.get('eval_count')})"
        )

    def review(self, payload: dict[str, Any]) -> LLMReviewResult:
        t0 = time.time()
        if not self.model:
            return LLMReviewResult.skipped_result(self.provider, self.model, error="model is empty")
        candidate = payload.get("candidate") or {}
        bot_type = str(candidate.get("bot_type") or "")
        engine_direction = str(candidate.get("engine_execution_direction") or "neutral")
        diagnostics: dict[str, Any] = {
            "base_url": self.base_url,
            "timeout_sec": self.timeout_sec,
            "keep_alive": self.keep_alive,
            "payload_bytes": len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
        }
        try:
            content: str | None = None
            content_meta: dict[str, Any] = {}
            chat_exc: Exception | None = None
            try:
                content, content_meta = self._request_chat(payload)
                diagnostics["path"] = "chat"
                diagnostics.update({f"chat_{k}": v for k, v in content_meta.items()})
            except Exception as exc:
                chat_exc = exc
                diagnostics["chat_error"] = _trim_text(exc, 500)
                try:
                    content, content_meta = self._request_generate(payload)
                    diagnostics["path"] = "generate"
                    diagnostics.update({f"generate_{k}": v for k, v in content_meta.items()})
                except Exception as gen_exc:
                    diagnostics["generate_error"] = _trim_text(gen_exc, 500)
                    error_parts = []
                    if chat_exc is not None:
                        error_parts.append(f"chat: {chat_exc}")
                    error_parts.append(f"generate: {gen_exc}")
                    raise RuntimeError("; ".join(error_parts)) from gen_exc
            parsed = parse_review_content(str(content or ""), bot_type=bot_type, engine_direction=engine_direction)
            return LLMReviewResult(
                provider=self.provider,
                model=self.model,
                latency_ms=int((time.time() - t0) * 1000),
                diagnostics=diagnostics,
                **parsed,
            )
        except Exception as exc:
            return LLMReviewResult.error_result(
                self.provider,
                self.model,
                str(exc),
                latency_ms=int((time.time() - t0) * 1000),
                diagnostics=diagnostics,
            )
