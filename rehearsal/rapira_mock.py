from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


MOCK_RATE = "91.37000000"
DEFAULT_SCENARIO = "success_without_provider_timestamp"
SCENARIOS = frozenset(
    {
        "success",
        "success_without_provider_timestamp",
        "malformed_json",
        "missing_ask_price",
        "invalid_symbol",
        "http_500",
        "timeout",
        "stale_response",
        "metadata_conflict",
    }
)


@dataclass(frozen=True)
class ResponseSpec:
    status: int
    content_type: str
    body: bytes
    delay_seconds: float = 0


def _json_response(payload: object, *, status: int = HTTPStatus.OK) -> ResponseSpec:
    return ResponseSpec(
        status=int(status),
        content_type="application/json",
        body=json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    )


def scenario_response(
    scenario: str,
    *,
    now: datetime | None = None,
) -> ResponseSpec:
    if scenario not in SCENARIOS:
        raise ValueError("unknown rehearsal Rapira scenario")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("scenario time must be timezone-aware")
    now = now.astimezone(timezone.utc)
    base_row = {
        "symbol": "USDT/RUB",
        "askPrice": MOCK_RATE,
    }

    if scenario == "success":
        return _json_response(
            {
                "data": [
                    {
                        **base_row,
                        "timestamp": now.isoformat().replace("+00:00", "Z"),
                    }
                ]
            }
        )
    if scenario == "success_without_provider_timestamp":
        return _json_response({"data": [base_row]})
    if scenario == "malformed_json":
        return ResponseSpec(
            status=HTTPStatus.OK,
            content_type="application/json",
            body=b'{"data":[',
        )
    if scenario == "missing_ask_price":
        return _json_response(
            {
                "data": [
                    {
                        "symbol": "USDT/RUB",
                        "bidPrice": "90.00",
                        "last": "92.00",
                        "close": "93.00",
                    }
                ]
            }
        )
    if scenario == "invalid_symbol":
        return _json_response(
            {
                "data": [
                    {
                        "symbol": "BTC/RUB",
                        "askPrice": MOCK_RATE,
                    }
                ]
            }
        )
    if scenario == "http_500":
        return _json_response(
            {"error": "rehearsal_mock_http_500"},
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
        )
    if scenario == "timeout":
        response = _json_response({"data": [base_row]})
        return ResponseSpec(
            status=response.status,
            content_type=response.content_type,
            body=response.body,
            delay_seconds=6,
        )
    if scenario == "stale_response":
        return _json_response(
            {
                "data": [
                    {
                        **base_row,
                        "timestamp": (
                            now - timedelta(days=1)
                        ).isoformat().replace("+00:00", "Z"),
                    }
                ]
            }
        )
    return _json_response(
        {
            "data": [
                {
                    **base_row,
                    "baseCurrency": "RUB",
                    "quoteCurrency": "USDT",
                }
            ]
        }
    )


class ScenarioState:
    def __init__(self, scenario: str = DEFAULT_SCENARIO):
        if scenario not in SCENARIOS:
            raise ValueError("invalid initial rehearsal Rapira scenario")
        self._scenario = scenario
        self._lock = threading.Lock()

    def get(self) -> str:
        with self._lock:
            return self._scenario

    def set(self, scenario: str) -> None:
        if scenario not in SCENARIOS:
            raise ValueError("unknown rehearsal Rapira scenario")
        with self._lock:
            self._scenario = scenario


STATE = ScenarioState(os.getenv("RAPIRA_MOCK_SCENARIO", DEFAULT_SCENARIO))


class RapiraMockHandler(BaseHTTPRequestHandler):
    server_version = "TradeSpaceRehearsalRapira/1"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _write(self, response: ResponseSpec) -> None:
        if response.delay_seconds:
            time.sleep(response.delay_seconds)
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(response.body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/__health":
            self._write(_json_response({"status": "ok"}))
            return
        if path == "/__scenario":
            self._write(_json_response({"scenario": STATE.get()}))
            return
        if path == "/open/market/rates":
            self._write(scenario_response(STATE.get()))
            return
        self._write(
            _json_response(
                {"error": "not_found"},
                status=HTTPStatus.NOT_FOUND,
            )
        )

    def do_POST(self) -> None:  # noqa: N802
        target = urlsplit(self.path)
        if target.path != "/__scenario":
            self._write(
                _json_response(
                    {"error": "not_found"},
                    status=HTTPStatus.NOT_FOUND,
                )
            )
            return
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        if content_length > 1024:
            self._write(
                _json_response(
                    {"error": "request_too_large"},
                    status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                )
            )
            return
        if content_length:
            self.rfile.read(content_length)
        scenario = (parse_qs(target.query).get("name") or [""])[0]
        try:
            STATE.set(scenario)
        except ValueError:
            self._write(
                _json_response(
                    {"error": "unknown_scenario"},
                    status=HTTPStatus.BAD_REQUEST,
                )
            )
            return
        self._write(_json_response({"scenario": scenario}))


def main() -> None:
    host = os.getenv("RAPIRA_MOCK_HOST", "0.0.0.0")
    port = int(os.getenv("RAPIRA_MOCK_PORT", "8080"))
    server = ThreadingHTTPServer((host, port), RapiraMockHandler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
