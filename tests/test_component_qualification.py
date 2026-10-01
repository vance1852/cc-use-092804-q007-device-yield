from __future__ import annotations

import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from component_qualification.api import Handler
from component_qualification.errors import InvalidSample, NotFound, ValidationFailed
from component_qualification.legacy import LEGACY_ALGORITHM_VERSION
from component_qualification.rules import RULE_VERSION, classify_devices, default_rule
from component_qualification.service import ComponentService


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ComponentService()
        self.service.bootstrap_admin()
        self.token = self.service.auth.login("admin", "component-admin")

    def _lot(self, unit_count: int = 2, devices=("device-1", "device-2")) -> None:
        self.service.create_lot(
            self.token, "LOT", "signal-processor", "P3.2", unit_count, device_ids=list(devices)
        )

    def _measure(self, device, frequency, response, instrument="A", sequence_no=None):
        return self.service.add_measurement(
            self.token, "LOT", device, frequency, response, 0.01, instrument, sequence_no
        )

    def test_pass_points_are_not_devices(self) -> None:
        # 旧缺陷：每颗器件三个达标频点会被算成 6 颗合格器件 / 2 颗批次。
        self._lot()
        for device in ("device-1", "device-2"):
            for frequency, response in ((450, .9), (520, .95), (650, .9)):
                self._measure(device, frequency, response)
        result = self.service.analyze(self.token, "LOT")
        self.assertEqual(result["algorithm_version"], RULE_VERSION)
        self.assertEqual(result["result"]["unit_count"], 2)
        self.assertEqual(result["result"]["passed"], 2)
        self.assertEqual(result["result"]["rates"]["yield_rate"], 1.0)

    def test_repeated_and_cross_instrument_measurements_take_latest_sequence(self) -> None:
        self._lot()
        self._measure("device-1", 450, .40, "A")          # 早期失败读数
        self._measure("device-1", 450, .95, "B")          # 另一台仪器复测
        for frequency, response in ((520, .95), (650, .9)):
            self._measure("device-1", frequency, response)
        for frequency, response in ((450, .5), (520, .9), (650, .9)):
            self._measure("device-2", frequency, response)
        result = self.service.analyze(self.token, "LOT")
        ids = result["traceability"]
        self.assertEqual(ids["passed_device_ids"], ["device-1"])
        self.assertEqual(ids["rejected_device_ids"], ["device-2"])

    def test_retracted_measurement_is_excluded_but_retained(self) -> None:
        self._lot()
        bad = self._measure("device-1", 520, .30, "A")
        self.service.retract_measurement(self.token, bad["measurement_id"], "接触不良")
        for device in ("device-1", "device-2"):
            for frequency, response in ((450, .9), (520, .95), (650, .9)):
                if device == "device-1" and frequency == 520:
                    continue
                self._measure(device, frequency, response)
        result = self.service.analyze(self.token, "LOT")
        # device-1 的 520Hz 仅剩撤销记录 => 该频点缺失 => unknown，而不是被坏读数判 reject。
        self.assertEqual(result["traceability"]["unknown_device_ids"], ["device-1"])
        self.assertEqual(result["traceability"]["passed_device_ids"], ["device-2"])
        stored = self.service.list_measurements(self.token, "LOT")
        retracted = [m for m in stored if m["measurement_id"] == bad["measurement_id"]][0]
        self.assertTrue(retracted["retracted"])
        self.assertEqual(retracted["retraction_reason"], "接触不良")

    def test_missing_frequency_yields_unknown_not_reject(self) -> None:
        self._lot()
        self._measure("device-1", 450, .4)
        self._measure("device-1", 520, .4)
        self._measure("device-1", 650, .4)   # 全不达标 => reject
        self._measure("device-2", 450, .9)
        self._measure("device-2", 520, .9)   # 缺 650 => unknown
        result = self.service.analyze(self.token, "LOT")["result"]
        self.assertEqual((result["passed"], result["rejected"], result["unknown"]), (0, 1, 1))
        self.assertAlmostEqual(result["rates"]["reject_rate"], 0.5)
        self.assertAlmostEqual(result["rates"]["unknown_rate"], 0.5)

    def test_traceability_links_yield_to_device_conclusions(self) -> None:
        self._lot()
        d1 = self.service.analyze(self.token, "LOT")
        self.assertEqual(d1["traceability"]["passed_device_ids"], [])
        for frequency, response in ((450, .9), (520, .95), (650, .9)):
            self._measure("device-1", frequency, response)
        d2 = self.service.analyze(self.token, "LOT")
        self.assertNotEqual(d1["analysis_id"], d2["analysis_id"])
        fetched = self.service.get_analysis(self.token, "LOT", d1["analysis_id"])
        # 旧分析不可变：补测之后仍是 0 通过。
        self.assertEqual(fetched["traceability"]["passed_device_ids"], [])
        self.assertEqual(d2["input_sha256"] != d1["input_sha256"], True)

    def test_same_input_is_idempotent_and_cached(self) -> None:
        self._lot()
        for frequency, response in ((450, .9), (520, .95), (650, .9)):
            self._measure("device-1", frequency, response)
        first = self.service.analyze(self.token, "LOT")
        second = self.service.analyze(self.token, "LOT")
        self.assertEqual(first["analysis_id"], second["analysis_id"])
        self.assertTrue(second["cached"])
        self.assertEqual(len(self.service.list_analyses(self.token, "LOT")), 1)

    def test_invalid_samples_raise_business_error(self) -> None:
        self._lot()
        with self.assertRaises(InvalidSample):
            self._measure("device-1", 450, float("nan"))
        with self.assertRaises(InvalidSample):
            self._measure("device-1", 999, .9)          # 计划外频点
        with self.assertRaises(InvalidSample):
            self.service.add_measurement(
                self.token, "LOT", "device-1", 450, .9, .01, ""
            )
        with self.assertRaises(InvalidSample):
            self._measure("device-1", 450, .9, sequence_no=0)
        # 未知器件
        with self.assertRaises(NotFound):
            self._measure("ghost", 450, .9)

    def test_device_count_must_match_unit_count(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.create_lot(
                self.token, "BAD", "x", "P1", 2, device_ids=["only-one"]
            )
        with self.assertRaises(ValidationFailed):
            self.service.create_lot(
                self.token, "BAD", "x", "P1", 2, device_ids=["same", "same"]
            )

    def test_legacy_report_preserves_old_algorithm(self) -> None:
        self._lot()
        # 7 个测量点、其中多个达标 —— 旧口径下 passed_points(6) > unit_count(2)。
        points = ((450, .9), (520, .95), (650, .9), (450, .85), (520, .9), (650, .82), (450, .4))
        for index, (frequency, response) in enumerate(points, start=1):
            self._measure("device-1", frequency, response, sequence_no=index)
        report = self.service.legacy_report(self.token, "LOT")
        self.assertTrue(report["historical"])
        self.assertEqual(report["algorithm_version"], LEGACY_ALGORITHM_VERSION)
        self.assertEqual(report["input_summary"]["measurement_point_count"], 7)
        self.assertTrue(report["input_summary"]["device_identities_ignored"])
        self.assertIn("inconsistent lot counts", report["reproduced"]["error"])
        # 新分析独立存在，规则版本不同。
        self.assertEqual(self.service.analyze(self.token, "LOT")["rule_version"], RULE_VERSION)

    def test_explicit_sequence_numbers_are_honored(self) -> None:
        self._lot(unit_count=1, devices=["device-1"])
        self._measure("device-1", 450, .95, "A", sequence_no=10)
        self._measure("device-1", 450, .40, "B", sequence_no=3)  # 序号小，不覆盖
        self._measure("device-1", 520, .9, "A")
        self._measure("device-1", 650, .9, "A")
        result = self.service.analyze(self.token, "LOT")
        self.assertEqual(result["traceability"]["passed_device_ids"], ["device-1"])

    def test_pure_rule_engine_is_deterministic(self) -> None:
        from component_qualification.rules import MeasurementObservation

        rule = default_rule()
        obs = [
            MeasurementObservation("m1", "d1", 1, 450, .9, .01, "A", "t1"),
            MeasurementObservation("m2", "d1", 2, 450, .4, .01, "B", "t2"),
            MeasurementObservation("m3", "d1", 1, 520, .9, .01, "A", "t1"),
            MeasurementObservation("m4", "d1", 1, 650, .9, .01, "A", "t1"),
        ]
        first = classify_devices(["d1"], obs, rule)
        second = classify_devices(list(reversed(["d1"])), list(reversed(obs)), rule)
        self.assertEqual(first, second)
        self.assertEqual(first[0].verdict, "reject")  # 450Hz 取序号 2 的坏读数


class HttpApiTest(unittest.TestCase):
    def setUp(self) -> None:
        Handler.service = ComponentService(":memory:")
        Handler.service.bootstrap_admin()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.token = self._call("POST", "/login", {"user_id": "admin", "password": "component-admin"})["token"]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _call(self, method: str, path: str, body: dict | None = None, token: str | None = None):
        data = json.dumps(body or {}).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data if method == "POST" else None, method=method
        )
        request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_invalid_sample_returns_business_error_shape(self) -> None:
        self._call("POST", "/lots", {
            "lot_id": "L", "product": "x", "process_rev": "P1",
            "unit_count": 1, "device_ids": ["d1"],
        }, self.token)
        status, body = self._call("POST", "/lots/L/measurements", {
            "device_id": "d1", "signal_frequency_hz": 999, "response": .9, "instrument": "A",
        }, self.token)
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "invalid_sample")

    def test_unknown_device_is_404(self) -> None:
        self._call("POST", "/lots", {
            "lot_id": "L", "product": "x", "process_rev": "P1",
            "unit_count": 1, "device_ids": ["d1"],
        }, self.token)
        status, body = self._call("POST", "/lots/L/measurements", {
            "device_id": "ghost", "signal_frequency_hz": 450, "response": .9, "instrument": "A",
        }, self.token)
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

    def test_analysis_traceability_endpoint(self) -> None:
        self._call("POST", "/lots", {
            "lot_id": "L", "product": "x", "process_rev": "P1",
            "unit_count": 1, "device_ids": ["d1"],
        }, self.token)
        for frequency in (450, 520, 650):
            self._call("POST", "/lots/L/measurements", {
                "device_id": "d1", "signal_frequency_hz": frequency, "response": .9, "instrument": "A",
            }, self.token)
        result = self._call("POST", "/lots/L/analysis", {}, self.token)
        self.assertEqual(result["traceability"]["passed_device_ids"], ["d1"])
        fetched = self._call("GET", f"/lots/L/analyses/{result['analysis_id']}", token=self.token)
        self.assertEqual(fetched["traceability"]["passed_device_ids"], ["d1"])


if __name__ == "__main__":
    unittest.main()
