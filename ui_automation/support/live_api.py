from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlencode

from .artifacts import CaseEvidence


@dataclass(frozen=True)
class LiveResponse:
    status: int
    body: bytes
    headers: dict[str, str]

    def json(self):
        return json.loads(self.body.decode("utf-8"))

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class LiveApi:
    """ブラウザとは別接続で、同じ実Sherpaを検証する最小HTTPクライアント。"""

    def __init__(self, base_url: str, context, evidence: CaseEvidence, timeout_seconds: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.context = context
        self.evidence = evidence
        self.timeout_seconds = timeout_seconds
        self._last_sse_timings: list[dict] = []
        self._structured_errors: list[dict] = []

    def last_sse_timings(self) -> list[dict]:
        """直近SSEの受信時刻を返す（生event JSONLとは分離したローカル観測）。"""

        return [dict(row) for row in self._last_sse_timings]

    def structured_error_count(self) -> int:
        return len(self._structured_errors)

    def structured_errors_since(self, index: int) -> list[dict]:
        """HTTP bodyそのものを保存せず、明示エラー文字列を実行中だけ参照する。"""

        return [dict(row) for row in self._structured_errors[max(0, index) :]]

    def _cookie_header(self) -> str:
        cookies = self.context.cookies(self.base_url)
        return "; ".join(f"{c['name']}={c['value']}" for c in cookies)

    def request(
        self,
        method: str,
        path: str,
        body=None,
        *,
        expected: int | set[int] | None = None,
        headers: dict[str, str] | None = None,
    ) -> LiveResponse:
        url = self.base_url + (path if path.startswith("/") else "/" + path)
        data = None
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "sherpa-ui-automation-independent-client",
        }
        cookie = self._cookie_header()
        if cookie:
            request_headers["Cookie"] = cookie
        if headers:
            request_headers.update(headers)
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                result = LiveResponse(response.status, response.read(), dict(response.headers.items()))
        except urllib.error.HTTPError as exc:
            result = LiveResponse(exc.code, exc.read(), dict(exc.headers.items()))
        self.evidence.record_api(
            method=method,
            url=url,
            status=result.status,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        try:
            payload = result.json()
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict) and (payload.get("ok") is False or result.status >= 400):
            message_parts = []
            for key in ("error", "detail", "message", "reason"):
                value = payload.get(key)
                if value is not None and value != "":
                    message_parts.append(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True))
            if message_parts:
                self._structured_errors.append(
                    {
                        "method": method,
                        "path": path.split("?", 1)[0],
                        "status": result.status,
                        "message": "\n".join(message_parts)[:4000],
                    }
                )
        if expected is not None:
            allowed = {expected} if isinstance(expected, int) else expected
            assert result.status in allowed, f"{method} {path} -> {result.status}: {result.text[:500]}"
        return result

    def get_json(self, path: str, *, expected: int = 200, save_as: str | None = None):
        response = self.request("GET", path, expected=expected)
        data = response.json()
        if save_as:
            self.evidence.write_json(save_as, data)
        return data

    def post_json(self, path: str, body=None, *, expected: int = 200, save_as: str | None = None):
        response = self.request("POST", path, body, expected=expected)
        data = response.json()
        if save_as:
            self.evidence.write_json(save_as, data)
        return data

    def put_json(self, path: str, body=None, *, expected: int = 200, save_as: str | None = None):
        response = self.request("PUT", path, body, expected=expected)
        data = response.json()
        if save_as:
            self.evidence.write_json(save_as, data)
        return data

    def patch_json(self, path: str, body=None, *, expected: int = 200, save_as: str | None = None):
        response = self.request("PATCH", path, body, expected=expected)
        data = response.json()
        if save_as:
            self.evidence.write_json(save_as, data)
        return data

    def delete_json(self, path: str, *, expected: int = 200, save_as: str | None = None):
        response = self.request("DELETE", path, expected=expected)
        data = response.json()
        if save_as:
            self.evidence.write_json(save_as, data)
        return data

    def collect_sse(self, path: str, *, save_as: str = "network/sse.jsonl") -> list[dict]:
        url = self.base_url + path
        headers = {"Accept": "text/event-stream", "User-Agent": "sherpa-ui-automation-sse-client"}
        cookie = self._cookie_header()
        if cookie:
            headers["Cookie"] = cookie
        request = urllib.request.Request(url, headers=headers, method="GET")
        started = time.monotonic()
        events: list[dict] = []
        timings: list[dict] = []
        status = 0
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                status = response.status
                assert status == 200, f"SSE {path} -> {status}"
                for raw in response:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload:
                        event = json.loads(payload)
                        events.append(event)
                        timings.append(
                            {
                                "index": len(events) - 1,
                                "elapsed_since_open_ms": int((time.monotonic() - started) * 1000),
                                "type": event.get("type"),
                                "node_id": event.get("id"),
                                "status": event.get("status"),
                            }
                        )
        except urllib.error.HTTPError as exc:
            status = exc.code
            raise AssertionError(f"SSE {path} -> {exc.code}: {exc.read().decode(errors='replace')[:500]}") from exc
        finally:
            self.evidence.record_api(
                method="GET",
                url=url,
                status=status,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            self._last_sse_timings = timings
            self.evidence.write_jsonl(save_as, events)
            timing_path = save_as.removesuffix(".jsonl") + "-timing.jsonl" if save_as.endswith(".jsonl") else save_as + "-timing.jsonl"
            self.evidence.write_jsonl(timing_path, timings)
            self.evidence.record_sse_collection(
                path=path,
                status=status,
                events=events,
                timings=timings,
            )
        return events

    @staticmethod
    def query(path: str, **values) -> str:
        return path + "?" + urlencode(values, doseq=True)
