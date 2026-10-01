"""用于离线验收的无依赖 JSON HTTP API。

业务错误统一返回 ``{"error": {"code", "message"}}``；无效样本等数据问题
返回 422/invalid_sample，而不是让底层计算异常逃逸成 500。
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .errors import ServiceError
from .service import ComponentService


class Handler(BaseHTTPRequestHandler):
    service = ComponentService()

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, exc: Exception) -> None:
        if isinstance(exc, ServiceError):
            return self._json(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        if isinstance(exc, PermissionError):
            return self._json(403, {"error": {"code": "forbidden", "message": str(exc)}})
        return self._json(422, {"error": {"code": "invalid_request", "message": str(exc)}})

    def _body(self) -> dict:
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ServiceError("请求体必须是 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ServiceError("请求体必须是 JSON 对象")
        return value

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"status": "ok", "service": "component-qualification"})
        try:
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            parts = self.path.strip("/").split("/")
            if len(parts) == 2 and parts[0] == "lots":
                return self._json(200, self.service.get_lot(token, parts[1]))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "devices":
                return self._json(200, {"devices": self.service.list_devices(token, parts[1])})
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "measurements":
                return self._json(200, {"measurements": self.service.list_measurements(token, parts[1])})
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "analyses":
                return self._json(200, {"analyses": self.service.list_analyses(token, parts[1])})
            if len(parts) == 4 and parts[0] == "lots" and parts[2] == "analyses":
                return self._json(200, self.service.get_analysis(token, parts[1], int(parts[3])))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "legacy-report":
                return self._json(200, self.service.legacy_report(token, parts[1]))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "signal-profile":
                return self._json(200, self.service.signal_profile(token, parts[1]))
            return self._json(404, {"error": {"code": "not_found", "message": "接口不存在"}})
        except Exception as exc:  # noqa: BLE001 - 边界统一转换
            return self._error(exc)

    def do_POST(self):
        try:
            body = self._body()
            if self.path == "/login":
                return self._json(200, {"token": self.service.auth.login(body["user_id"], body["password"])})
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            parts = self.path.strip("/").split("/")
            if self.path == "/lots":
                return self._json(201, self.service.create_lot(
                    token, body["lot_id"], body["product"], body["process_rev"],
                    int(body["unit_count"]), body.get("device_ids"),
                    body.get("required_frequencies_hz"),
                ))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "measurements":
                return self._json(201, self.service.add_measurement(
                    token, parts[1], body["device_id"], body["signal_frequency_hz"],
                    body["response"], body.get("noise", 0.0), body["instrument"],
                    body.get("sequence_no"), body.get("measured_at"),
                ))
            if self.path == "/measurements/retract":
                return self._json(200, self.service.retract_measurement(
                    token, body["measurement_id"], body["reason"],
                ))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "analysis":
                return self._json(200, self.service.analyze(token, parts[1]))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "approval":
                return self._json(200, self.service.approve(
                    token, parts[1], body["decision"], body["reason"],
                ))
            return self._json(404, {"error": {"code": "not_found", "message": "接口不存在"}})
        except Exception as exc:  # noqa: BLE001 - 边界统一转换
            return self._error(exc)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=":memory:")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    Handler.service = ComponentService(args.database)
    Handler.service.bootstrap_admin()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
