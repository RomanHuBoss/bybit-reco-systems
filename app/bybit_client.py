from __future__ import annotations

import math
import time
from typing import Any, Mapping

import httpx


RETRYABLE_BYBIT_RETCODES = {10000, 10006, 10016, 10018, 30034}


def _safe_float(value: Any) -> float | None:
    try:
        num = float(value)
    except Exception:
        return None
    if not math.isfinite(num):
        return None
    return num


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _mapping_or_none(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _normalize_linear_usdt_symbol(symbol: str | None) -> str | None:
    if symbol is None:
        return None
    normalized = str(symbol or "").strip().upper()
    if not normalized:
        return None
    base = normalized[:-4] if normalized.endswith("USDT") else ""
    if not base or not normalized.endswith("USDT") or not normalized.isalnum():
        raise ValueError(
            f"Unsupported symbol for this service: {normalized}. "
            "Only Bybit Linear USDT perpetual symbols are allowed; use exact symbols like BTCUSDT."
        )
    return normalized


def _ensure_linear_category(category: str) -> str:
    normalized = str(category or "").strip().lower()
    if normalized != "linear":
        raise ValueError(f"Unsupported Bybit category for this service: {category!r}. Only category='linear' is allowed.")
    return normalized


def _delivery_time_is_perpetual(value: Any) -> bool:
    if value in (None, ""):
        return True
    try:
        return int(str(value).strip() or "0") == 0
    except Exception:
        # Unknown deliveryTime is safer than pretending a delivery contract is perpetual.
        return False


def _is_linear_usdt_perpetual_ticker(item: Mapping[str, Any]) -> bool:
    symbol = str(item.get("symbol") or "").strip().upper()
    if not symbol.endswith("USDT"):
        return False
    if not _delivery_time_is_perpetual(item.get("deliveryTime")):
        return False
    prelisting_phase = str(item.get("curPreListingPhase") or "").strip()
    if prelisting_phase:
        return False
    return True


def _filter_exact_symbol(items: list[dict[str, Any]], symbol: str | None) -> list[dict[str, Any]]:
    target = _normalize_linear_usdt_symbol(symbol)
    scoped = [item for item in items if _is_linear_usdt_perpetual_ticker(item)]
    if target is None:
        return scoped
    return [item for item in scoped if str(item.get("symbol") or "").strip().upper() == target]


def _header_value(response: Any, name: str) -> str | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get(name)
    except Exception:
        return None
    return None if value in (None, "") else str(value)

def _result_list(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Возвращает list[dict] из `result.list` без протечки битой формы выше по стеку.

    Upstream API в норме всегда отдаёт dict -> result -> list, но в реальной жизни
    прокси, WAF или partially broken mock/stub могут вернуть иную форму. Для
    публичного клиента лучше деградировать к пустому списку, чем выбросить
    `AttributeError` из `.get(...)` в бизнес-слое.
    """
    result = _mapping_or_none(data.get("result")) or {}
    items = result.get("list")
    if not isinstance(items, list):
        return []
    return [dict(item) for item in items if isinstance(item, Mapping)]


class BybitPublicClient:
    def __init__(self, base_url: str, timeout: float = 10.0, max_retries: int = 2, backoff_base_sec: float = 0.25):
        self.base_url = base_url.rstrip("/")
        self.max_retries = max(0, int(max_retries))
        self.backoff_base_sec = max(0.05, float(backoff_base_sec))
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def _retry_delay(self, attempt: int, retry_after: str | None = None) -> float:
        delay = self.backoff_base_sec * (2 ** max(0, int(attempt)))
        if retry_after not in (None, ""):
            try:
                header_delay = float(str(retry_after).strip())
            except Exception:
                header_delay = None
            if header_delay is not None and math.isfinite(header_delay):
                delay = max(delay, max(0.0, header_delay))
        return delay

    def _is_retryable_http_status(self, status_code: int) -> bool:
        return int(status_code) in {408, 429} or 500 <= int(status_code) <= 599

    def _is_retryable_bybit_error(self, ret_code: int, ret_msg: str) -> bool:
        msg = str(ret_msg or "").lower()
        if int(ret_code) in RETRYABLE_BYBIT_RETCODES:
            return True
        return any(
            token in msg
            for token in (
                "too many",
                "rate limit",
                "system busy",
                "server error",
                "service unavailable",
                "timeout",
                "temporarily unavailable",
            )
        )

    def _is_retryable_runtime_error(self, exc: RuntimeError) -> bool:
        msg = str(exc).lower()
        return any(
            token in msg
            for token in (
                "retryable upstream error",
                "response decode error",
                "response shape error",
            )
        )

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.get(url, params=params)
                retry_after = _header_value(response, "Retry-After") if response is not None else None
                if self._is_retryable_http_status(response.status_code):
                    if attempt < self.max_retries:
                        time.sleep(self._retry_delay(attempt, retry_after))
                        continue
                    raise RuntimeError(f"Bybit HTTP {response.status_code}: retryable upstream error")
                response.raise_for_status()
                try:
                    data = response.json()
                except Exception as exc:
                    if attempt < self.max_retries:
                        time.sleep(self._retry_delay(attempt, retry_after))
                        continue
                    raise RuntimeError("Bybit response decode error") from exc
                if not isinstance(data, dict):
                    if attempt < self.max_retries:
                        time.sleep(self._retry_delay(attempt, retry_after))
                        continue
                    raise RuntimeError("Bybit response shape error: expected JSON object")

                ret_code_raw = data.get("retCode", 0)
                try:
                    ret_code = int(ret_code_raw or 0)
                except Exception as exc:
                    if attempt < self.max_retries:
                        time.sleep(self._retry_delay(attempt, retry_after))
                        continue
                    raise RuntimeError(f"Bybit response shape error: invalid retCode={ret_code_raw!r}") from exc
                if ret_code != 0:
                    ret_msg = str(data.get("retMsg") or "")
                    if attempt < self.max_retries and self._is_retryable_bybit_error(ret_code, ret_msg):
                        time.sleep(self._retry_delay(attempt, _header_value(response, "Retry-After")))
                        continue
                    raise RuntimeError(f"Bybit error {ret_code}: {ret_msg}")
                return data
            except Exception as exc:
                last_exc = exc
                retryable = False
                if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
                    retryable = True
                elif isinstance(exc, RuntimeError):
                    retryable = self._is_retryable_runtime_error(exc)
                if attempt >= self.max_retries or not retryable:
                    raise
                time.sleep(self._retry_delay(attempt))
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Bybit request failed")

    def get_tickers(self, category: str, symbol: str | None = None) -> list[dict[str, Any]]:
        category_norm = _ensure_linear_category(category)
        symbol_norm = _normalize_linear_usdt_symbol(symbol)
        params: dict[str, Any] = {"category": category_norm}
        if symbol_norm:
            params["symbol"] = symbol_norm
        data = self._get("/v5/market/tickers", params=params)
        # Bybit category=linear can include non-USDT linear products when broad
        # filters are used. Product scope here is stricter, and symbol-specific
        # calls must not let a malformed upstream/stub row be relabelled as the
        # requested instrument by the collector.
        return _filter_exact_symbol(_result_list(data), symbol_norm)

    def get_kline(
        self,
        category: str,
        symbol: str,
        interval: str = "1",
        limit: int = 200,
        start: int | None = None,
        end: int | None = None,
    ) -> list[list[str]]:
        category_norm = _ensure_linear_category(category)
        symbol_norm = _normalize_linear_usdt_symbol(symbol)
        if not symbol_norm:
            raise ValueError("symbol is required for Bybit linear USDT kline requests")
        params: dict[str, str] = {
            "category": category_norm,
            "symbol": symbol_norm,
            "interval": interval,
            "limit": str(max(1, min(int(limit), 1000))),
        }
        if start is not None:
            params["start"] = str(int(start))
        if end is not None:
            params["end"] = str(int(end))
        data = self._get("/v5/market/kline", params=params)
        result = _mapping_or_none(data.get("result")) or {}
        items = result.get("list")
        return items if isinstance(items, list) else []

    def get_funding_rate(self, symbol: str) -> dict[str, Any] | None:
        """Current funding rate from the Linear USDT perpetual ticker endpoint."""
        target = _normalize_linear_usdt_symbol(symbol)
        if not target:
            raise ValueError("symbol is required for Bybit linear USDT funding requests")
        data = self._get("/v5/market/tickers", {"category": "linear", "symbol": target})
        items = _filter_exact_symbol(_result_list(data), target)
        if not items:
            return None
        ticker = None
        for item in items:
            item_symbol = str(item.get("symbol") or "").strip().upper()
            if item_symbol == target:
                ticker = item
                break
        if ticker is None:
            return None
        next_funding_raw = _safe_int(ticker.get("nextFundingTime") or 0)
        next_funding_ts = next_funding_raw // 1000 if next_funding_raw > 10**11 else next_funding_raw
        funding_rate = _safe_float(ticker.get("fundingRate"))
        funding_interval_min = None
        interval_hours = _safe_float(ticker.get("fundingIntervalHour"))
        if interval_hours is not None and interval_hours > 0:
            funding_interval_min = int(round(interval_hours * 60.0))

        if funding_interval_min is None:
            # Official V5 funding-history docs point operators to instruments-info
            # for the per-symbol funding interval. Some ticker payloads/stubs do not
            # include fundingIntervalHour, so falling back to instruments-info avoids
            # a silent 8h assumption later in the recommender. Keep this fail-open for
            # collection: if metadata cannot be fetched, downstream approval gates keep
            # funding_interval_source=fallback_8h_missing_interval and can block when
            # the carry is material.
            try:
                info = self.get_instrument_info("linear", target)
            except Exception:
                info = None
            if isinstance(info, Mapping):
                interval_min = _safe_float(info.get("fundingInterval"))
                if interval_min is not None and interval_min > 0:
                    funding_interval_min = int(round(interval_min))
        return {
            "symbol": target,
            "funding_rate": funding_rate,
            "next_funding_ts": next_funding_ts,
            "funding_interval_min": funding_interval_min,
        }

    def get_open_interest_page(
        self,
        symbol: str,
        interval: str = "1h",
        limit: int = 48,
        start_ms: int | None = None,
        end_ms: int | None = None,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Historical open interest page for a linear perpetual.
        interval: 5min / 15min / 30min / 1h / 4h / 1d
        Returns sanitized rows plus optional pagination cursor.
        """
        symbol_norm = _normalize_linear_usdt_symbol(symbol)
        if not symbol_norm:
            raise ValueError("symbol is required for Bybit linear USDT open-interest requests")
        params: dict[str, str] = {
            "category": "linear",
            "symbol": symbol_norm,
            "intervalTime": interval,
            "limit": str(max(1, min(int(limit), 200))),
        }
        if start_ms is not None:
            params["startTime"] = str(int(start_ms))
        if end_ms is not None:
            params["endTime"] = str(int(end_ms))
        if cursor:
            params["cursor"] = str(cursor)
        data = self._get(
            "/v5/market/open-interest",
            params,
        )
        result = _mapping_or_none(data.get("result")) or {}
        items = result.get("list")
        out: list[dict[str, Any]] = []
        if isinstance(items, list):
            for row in items:
                if not isinstance(row, Mapping):
                    continue
                ts_raw = _safe_int(row.get("timestamp"))
                oi = _safe_float(row.get("openInterest"))
                if ts_raw <= 0 or oi is None or oi < 0:
                    continue
                ts = ts_raw // 1000 if ts_raw > 10**11 else ts_raw
                out.append({"ts": ts, "oi": oi})
        next_cursor = result.get("nextPageCursor") or result.get("cursor") or None
        if next_cursor is not None:
            next_cursor = str(next_cursor)
            if not next_cursor.strip():
                next_cursor = None
        return out, next_cursor

    def get_open_interest(
        self,
        symbol: str,
        interval: str = "1h",
        limit: int = 48,
        start_ms: int | None = None,
        end_ms: int | None = None,
        cursor: str | None = None,
    ) -> list[dict[str, Any]]:
        rows, _cursor = self.get_open_interest_page(
            symbol,
            interval=interval,
            limit=limit,
            start_ms=start_ms,
            end_ms=end_ms,
            cursor=cursor,
        )
        return rows

    def get_instrument_info(self, category: str, symbol: str) -> dict[str, Any] | None:
        """Metadata for one Bybit Linear USDT perpetual instrument.

        Bybit normally honours the ``symbol`` filter, но для fail-closed execution
        validation нельзя опираться только на это предположение. Если upstream,
        прокси или тестовый stub вернул список без точного совпадения symbol,
        безопаснее считать metadata непригодной и вернуть ``None``, чем молча
        взять первый инструмент из списка и валидировать чужими ограничениями.

        The endpoint reports ``category`` at ``result.category`` rather than inside
        every instrument object. Preserve that category on the returned item so
        downstream validation can detect malformed responses instead of silently
        treating them as compatible execution metadata.
        """
        category_norm = _ensure_linear_category(category)
        symbol_norm = _normalize_linear_usdt_symbol(symbol)
        if not symbol_norm:
            raise ValueError("symbol is required for Bybit linear USDT instrument metadata")
        data = self._get("/v5/market/instruments-info", {"category": category_norm, "symbol": symbol_norm})
        result = _mapping_or_none(data.get("result")) or {}
        result_category = str(result.get("category") or "").strip().lower()
        items = _result_list(data)
        target = symbol_norm
        for item in items:
            item_symbol = str(item.get("symbol") or "").strip().upper()
            if item_symbol and item_symbol == target:
                out = dict(item)
                if result_category and not str(out.get("category") or "").strip():
                    out["category"] = result_category
                return out
        return None
