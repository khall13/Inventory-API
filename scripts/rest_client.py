"""Generic paginated REST client — connector-agnostic.

One engine for every API we integrate. Auth scheme, paging field names, base URL,
and error format are all supplied per-connector (see connectors.yaml / connectors.py),
so onboarding a new tool is config, not code (see docs/ADD_AN_API.md).

Error handling contract (configurable per connector):
  - 400 / 401 / 403 / 404 → fail fast (do not retry a bad/unauthorized request)
  - 429 / 500 / 502 / 503 / 504 → retry with exponential backoff (transient)
Read-only: this client issues GET only.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterator

import requests

log = logging.getLogger("rest_client")

RETRY_STATUS = {429, 500, 502, 503, 504}
FAIL_FAST_STATUS = {400, 401, 403, 404}


class ApiError(RuntimeError):
    """Non-retryable API error carrying parsed messages + optional trace id."""

    def __init__(self, status: int, messages: list[str], trace_id: str | None = None):
        self.status = status
        self.messages = messages
        self.trace_id = trace_id
        super().__init__(
            f"HTTP {status}"
            + (f" (trace {trace_id})" if trace_id else "")
            + ": " + ("; ".join(messages) if messages else "no message")
        )


class RestClient:
    def __init__(
        self,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        query_auth: dict[str, str] | None = None,
        basic_auth: tuple[str, str] | None = None,
        error_parser: str = "generic",     # "generic" | "spring"
        trace_header: str | None = None,
        paging: dict[str, Any] | None = None,
        max_retries: int = 5,
        backoff_base: float = 1.5,
        timeout: float = 60.0,
    ):
        if not base_url:
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self.error_parser = error_parser
        self.trace_header = trace_header
        self.query_auth = query_auth or {}
        self.paging = {
            "page_param": "pageNumber",
            "size_param": "pageSize",
            "has_next_field": "hasNext",
            "total_pages_field": "totalPages",
            **(paging or {}),
        }
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.timeout = timeout

        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json", **(headers or {})})
        if basic_auth:
            self._session.auth = basic_auth

    # ── low-level GET with retry/backoff ──────────────────────────────────────
    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self.base_url + path
        merged = {**self.query_auth, **(params or {})}
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._session.get(url, params=merged, timeout=self.timeout)
            except requests.RequestException as e:
                if attempt > self.max_retries:
                    raise ApiError(0, [f"network error after {attempt} tries: {e}"])
                self._sleep(attempt, f"network error: {e}")
                continue

            trace_id = resp.headers.get(self.trace_header) if self.trace_header else None
            if resp.ok:
                return resp.json()

            messages = self._parse_errors(resp)
            if resp.status_code in FAIL_FAST_STATUS:
                raise ApiError(resp.status_code, messages, trace_id)
            if resp.status_code in RETRY_STATUS and attempt <= self.max_retries:
                self._sleep(attempt, f"HTTP {resp.status_code} (trace {trace_id}): {messages}")
                continue
            raise ApiError(resp.status_code, messages, trace_id)

    def _parse_errors(self, resp: requests.Response) -> list[str]:
        try:
            body = resp.json()
        except ValueError:
            return [resp.text[:300]] if resp.text else []
        items = body if isinstance(body, list) else [body]
        out = []
        for e in items:
            if not isinstance(e, dict):
                out.append(str(e))
                continue
            if self.error_parser == "spring":
                msg = e.get("defaultMessage") or e.get("message") or "error"
                meta = "/".join(str(e[k]) for k in ("code", "objectName") if e.get(k))
                out.append(msg + (f" [{meta}]" if meta else ""))
            else:  # generic
                out.append(str(e.get("message") or e.get("error") or e.get("detail")
                               or e.get("defaultMessage") or e))
        return out

    def _sleep(self, attempt: int, why: str) -> None:
        delay = self.backoff_base ** attempt
        log.warning("Retry %d/%d in %.1fs — %s", attempt, self.max_retries, delay, why)
        time.sleep(delay)

    # ── pagination ────────────────────────────────────────────────────────────
    def paginate(
        self,
        path: str,
        *,
        records_key: str,
        page_size: int = 200,
        params: dict[str, Any] | None = None,
        max_pages: int | None = None,
        paged: bool = True,
    ) -> Iterator[tuple[int, list[dict]]]:
        """Yield (page_number, records).

        paged=False           → single GET, no paging params (small reference endpoints).
        paging.style 'flat'   → page/size as flat query params; stop on hasNext/totalPages/empty.
        paging.style 'options_json' → page nested in a JSON `options` query param
                                 (MenuWorks-style); stop via paginationDetails count / short page.
        """
        if not paged:
            body = self.get(path, dict(params or {}))
            yield 1, self._extract_records(body, records_key)
            return
        if self.paging.get("style") == "options_json":
            yield from self._paginate_options_json(path, records_key, page_size, params, max_pages)
        else:
            yield from self._paginate_flat(path, records_key, page_size, params, max_pages)

    def _paginate_flat(self, path, records_key, page_size, params, max_pages):
        p = self.paging
        base = dict(params or {})
        base[p["size_param"]] = page_size
        page = 1
        while True:
            base[p["page_param"]] = page
            body = self.get(path, base)
            records = self._extract_records(body, records_key)
            yield page, records
            has_next = body.get(p["has_next_field"]) if isinstance(body, dict) else None
            total_pages = body.get(p["total_pages_field"]) if isinstance(body, dict) else None
            if max_pages is not None and page >= max_pages:
                return
            if has_next is True:
                page += 1
            elif has_next is False:
                return
            elif total_pages is not None:
                if page >= total_pages:
                    return
                page += 1
            else:
                if not records:
                    return
                page += 1

    def _paginate_options_json(self, path, records_key, page_size, params, max_pages):
        """MenuWorks: options={"...":..., "page":{"offset":N,"limit":M}}; the domain's
        params become other options keys (filter/include). End = short page or count exhausted."""
        p = self.paging
        offset = p.get("offset_start", 1)
        extras = dict(params or {})       # filter/include for this domain → merged into options
        seen = 0
        page_no = 0
        while True:
            page_no += 1
            options = {**extras, "page": {"offset": offset, "limit": page_size}}
            body = self.get(path, {"options": json.dumps(options)})
            records = self._extract_records(body, records_key)
            yield page_no, records
            seen += len(records)
            total = (((body.get("paginationDetails") or {}).get("count") or {})
                     .get("totalRecordsAvailable") if isinstance(body, dict) else None)
            if max_pages is not None and page_no >= max_pages:
                return
            if not records or len(records) < page_size:
                return
            if isinstance(total, int) and seen >= total:
                return
            offset += 1

    @staticmethod
    def _extract_records(body: Any, records_key: str) -> list[dict]:
        if isinstance(body, list):
            return body
        # records_key may be a dotted path (e.g. "data.products") for nested envelopes.
        cur = body
        for part in records_key.split("."):
            if not isinstance(cur, dict):
                cur = None
                break
            cur = cur.get(part)
        if isinstance(cur, list):
            return cur
        log.warning("records_key %r not found; top-level keys: %s",
                    records_key, list(body.keys()) if isinstance(body, dict) else type(body).__name__)
        return []
