"""部件质量 HTTP API：错误码、器件级良率与追溯端点。"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from component_qualification.api import Handler
from component_qualification.service import ComponentService


def _free_port() -> tuple:
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    return sock, sock.getsockname()[1]


class ComponentApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.service = ComponentService(":memory:")
        cls.service.bootstrap_admin()
        cls.token = cls.service.auth.login("admin", "component-admin")
        sock, port = _free_port()
        sock.close()
        Handler.service = cls.service
        from http.server import ThreadingHTTPServer

        cls.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read())
            return exc.code, payload

    def _seed_two_unit_lot(self, lot_id: str) -> None:
        self._request("POST", "/lots", {
            "lot_id": lot_id, "product": "dsp", "process_rev": "P1", "unit_count": 2})
        self._request("POST", f"/lots/{lot_id}/units", {"unit_ids": ["U1", "U2"]})
        for frequency, response in ((450.0, 0.9), (520.0, 0.93), (650.0, 0.84)):
            self._request("POST", f"/lots/{lot_id}/measurements", {
                "unit_id": "U1", "signal_frequency_hz": frequency, "response": response,
                "noise": 0.01, "instrument": "spec-1", "sequence_no": 1})
        for frequency, response in ((450.0, 0.71), (520.0, 0.93)):
            self._request("POST", f"/lots/{lot_id}/measurements", {
                "unit_id": "U2", "signal_frequency_hz": frequency, "response": response,
                "noise": 0.01, "instrument": "spec-1", "sequence_no": 1})

    def test_health(self) -> None:
        status, body = self._request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], "component-qualification")

    def test_analyze_returns_device_level_rates_and_traceability(self) -> None:
        self._seed_two_unit_lot("LOT-A")
        status, body = self._request("POST", "/lots/LOT-A/analysis", {})
        self.assertEqual(status, 200)
        result = body["result"]
        self.assertEqual(result["rule_version"], "device-yield/2")
        self.assertEqual(result["total_units"], 2)
        self.assertEqual((result["passed"], result["rejected"], result["unknown"]), (1, 0, 1))
        self.assertEqual(result["rates"]["yield"], 0.5)
        units = {unit["unit_id"]: unit for unit in result["units"]}
        self.assertEqual(units["U2"]["conclusion"], "unknown")
        self.assertTrue(units["U1"]["selected"][0]["measurement_id"])

    def test_invalid_sample_returns_business_error_code(self) -> None:
        self._seed_two_unit_lot("LOT-B")
        status, body = self._request("POST", "/lots/LOT-B/measurements", {
            "unit_id": "U1", "signal_frequency_hz": 450.0, "response": "NaN",
            "noise": 0.01, "instrument": "spec-1", "sequence_no": 9})
        # "NaN" 经 JSON 解析为 float nan（Python json 默认接受），应被规则层拦截为 422。
        self.assertIn(status, (400, 422))
        self.assertIn("code", body["error"])
        self.assertNotIn("ZeroDivision", body["error"]["message"])

    def test_unknown_unit_measurement_is_422(self) -> None:
        self._seed_two_unit_lot("LOT-C")
        status, body = self._request("POST", "/lots/LOT-C/measurements", {
            "unit_id": "GHOST", "signal_frequency_hz": 450.0, "response": 0.9,
            "noise": 0.01, "instrument": "spec-1"})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_sample")

    def test_legacy_conflict_is_business_error(self) -> None:
        self._seed_two_unit_lot("LOT-D")
        status, body = self._request("POST", "/lots/LOT-D/analysis-legacy", {})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_lot_state")

    def test_report_endpoints_link_yield_to_unit_conclusions(self) -> None:
        self._seed_two_unit_lot("LOT-E")
        _, analysis = self._request("POST", "/lots/LOT-E/analysis", {})
        analysis_id = analysis["analysis_id"]
        status, detail = self._request("GET", f"/analyses/{analysis_id}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["rule_version"], "device-yield/2")
        self.assertEqual({u["conclusion"] for u in detail["result"]["units"]}, {"pass", "unknown"})
        status, versions = self._request("GET", "/lots/LOT-E/analyses")
        self.assertEqual(status, 200)
        self.assertEqual(versions["analyses"][0]["analysis_id"], analysis_id)
        # 决定挂到该版本后，版本清单带决定信息。
        status, _ = self._request("POST", "/lots/LOT-E/decisions", {
            "analysis_id": analysis_id, "decision": "hold", "reason": "U2 缺频点"})
        self.assertEqual(status, 201)
        _, versions = self._request("GET", "/lots/LOT-E/analyses")
        self.assertEqual(versions["analyses"][0]["decision"], "hold")
        status, audit = self._request("GET", "/lots/LOT-E/audit")
        self.assertEqual(status, 200)
        kinds = {event["event_type"] for event in audit["events"]}
        self.assertIn("analysis.recorded", kinds)
        self.assertIn("decision.recorded", kinds)

    def test_unknown_route_and_missing_lot(self) -> None:
        status, body = self._request("GET", "/lots/NOPE")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")
        status, body = self._request("GET", "/wat")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
