"""用于离线验收的无依赖 JSON HTTP API。

所有业务失败返回结构化错误码（ComponentError），
不向调用方泄露底层 ValueError/ZeroDivisionError。
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .errors import ComponentError
from .service import ComponentService


class Handler(BaseHTTPRequestHandler):
    service = ComponentService()

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, exc: Exception) -> None:
        if isinstance(exc, ComponentError):
            return self._json(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        return self._json(400, {"error": {"code": "bad_request", "message": str(exc)}})

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ComponentError("请求体必须是 JSON 对象")
        return value

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"status": "ok", "service": "component-qualification"})
        try:
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            parts = [part for part in self.path.split("?")[0].split("/") if part]
            if len(parts) == 2 and parts[0] == "lots":
                return self._json(200, self.service.get_lot(token, parts[1]))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "units":
                return self._json(200, {"lot_id": parts[1], "units": self.service.list_units(token, parts[1])})
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "analyses":
                return self._json(200, self.service.list_analyses(token, parts[1]))
            if len(parts) == 2 and parts[0] == "analyses":
                return self._json(200, self.service.get_analysis(token, int(parts[1])))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "audit":
                return self._json(200, {"lot_id": parts[1], "events": self.service.audit(token, parts[1])})
            return self._json(404, {"error": {"code": "not_found", "message": "接口不存在"}})
        except Exception as exc:
            return self._error(exc)

    def do_POST(self):
        try:
            body = self._body()
            if self.path == "/login":
                return self._json(200, {"token": self.service.auth.login(body["user_id"], body["password"])})
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            parts = [part for part in self.path.split("?")[0].split("/") if part]
            if parts == ["lots"]:
                return self._json(201, self.service.create_lot(
                    token, body["lot_id"], body["product"], body["process_rev"], int(body["unit_count"])))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "units":
                return self._json(201, self.service.register_units(token, parts[1], body["unit_ids"]))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "measurements":
                return self._json(201, self.service.add_measurement(
                    token, parts[1], body["unit_id"], body["signal_frequency_hz"], body["response"],
                    body.get("noise", 0.0), body["instrument"], body.get("sequence_no")))
            if parts == ["measurements", "revoke"]:
                return self._json(200, self.service.revoke_measurement(
                    token, body["measurement_id"], body["reason"]))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "analysis":
                return self._json(200, self.service.analyze(
                    token, parts[1],
                    body.get("required_frequencies_hz", (450.0, 520.0, 650.0)),
                    float(body.get("response_threshold", 0.8))))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "analysis-legacy":
                return self._json(200, self.service.analyze_legacy(token, parts[1]))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "decisions":
                return self._json(201, self.service.approve(
                    token, parts[1], int(body["analysis_id"]), body["decision"], body["reason"]))
            return self._json(404, {"error": {"code": "not_found", "message": "接口不存在"}})
        except PermissionError as exc:
            return self._json(403, {"error": {"code": "forbidden", "message": str(exc)}})
        except Exception as exc:
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
