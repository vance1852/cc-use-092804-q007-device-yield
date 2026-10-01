"""器件台账、测量序列、版本化分析与放行决定的服务层测试。"""

from __future__ import annotations

import unittest

from component_qualification.errors import Conflict, InvalidLotState, InvalidSample, NotFound
from component_qualification.service import ComponentService


FREQUENCIES = (450.0, 520.0, 650.0)
PASSING = (0.9, 0.93, 0.84)


class ComponentServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ComponentService()
        self.service.bootstrap_admin()
        self.token = self.service.auth.login("admin", "component-admin")

    def _lot_with_two_units(self) -> None:
        self.service.create_lot(self.token, "LOT-1", "signal-processor", "P1", 2)
        self.service.register_units(self.token, "LOT-1", ["U1", "U2"])

    def _measure_complete(self, unit_id: str, responses=PASSING) -> list[str]:
        ids = []
        for frequency, response in zip(FREQUENCIES, responses):
            ids.append(self.service.add_measurement(
                self.token, "LOT-1", unit_id, frequency, response, 0.01, "spec-1", 1
            )["measurement_id"])
        return ids

    def test_registration_count_must_match_lot_count(self) -> None:
        self.service.create_lot(self.token, "LOT-X", "chip", "P1", 2)
        with self.assertRaises(InvalidLotState):
            self.service.register_units(self.token, "LOT-X", ["only-one"])
        with self.assertRaises(InvalidSample):
            self.service.register_units(self.token, "LOT-X", ["A", "A"])
        self.service.register_units(self.token, "LOT-X", ["A", "B"])
        with self.assertRaises(Conflict):
            self.service.register_units(self.token, "LOT-X", ["A", "B"])

    def test_measurement_requires_registered_unit(self) -> None:
        self.service.create_lot(self.token, "LOT-X", "chip", "P1", 1)
        self.service.register_units(self.token, "LOT-X", ["A"])
        with self.assertRaises(InvalidSample):
            self.service.add_measurement(self.token, "LOT-X", "GHOST", 450.0, 0.9, 0.01, "spec-1")

    def test_non_finite_sample_is_business_error_not_crash(self) -> None:
        self._lot_with_two_units()
        with self.assertRaises(InvalidSample):
            self.service.add_measurement(self.token, "LOT-1", "U1", 450.0, float("nan"), 0.01, "spec-1")
        with self.assertRaises(InvalidSample):
            self.service.add_measurement(self.token, "LOT-1", "U1", float("inf"), 0.9, 0.01, "spec-1")

    def test_analyze_refuses_when_ledger_incomplete(self) -> None:
        self.service.create_lot(self.token, "LOT-Z", "chip", "P1", 2)
        self.service.register_units(self.token, "LOT-Z", ["A", "B"])
        # 台账人为缺一个器件时拒绝计算（模拟登记数量与声明不一致）。
        with self.service.db:
            self.service.db.execute("DELETE FROM lot_units WHERE lot_id='LOT-Z' AND unit_id='B'")
        with self.assertRaises(InvalidLotState):
            self.service.analyze(self.token, "LOT-Z")

    def test_device_level_yield_and_traceability(self) -> None:
        self._lot_with_two_units()
        self._measure_complete("U1")
        # U2 缺 650 Hz。
        self.service.add_measurement(self.token, "LOT-1", "U2", 450.0, 0.71, 0.01, "spec-1", 1)
        self.service.add_measurement(self.token, "LOT-1", "U2", 520.0, 0.93, 0.01, "spec-1", 1)
        analysis = self.service.analyze(self.token, "LOT-1")
        result = analysis["result"]
        self.assertEqual(result["rule_version"], "device-yield/2")
        self.assertEqual((result["passed"], result["rejected"], result["unknown"]), (1, 0, 1))
        self.assertEqual(result["total_units"], 2)
        self.assertAlmostEqual(sum(result["rates"].values()), 1.0)
        # 良率数字可追溯到具体器件结论与逐点证据。
        units = {unit["unit_id"]: unit for unit in result["units"]}
        self.assertEqual(units["U1"]["conclusion"], "pass")
        self.assertEqual(units["U2"]["conclusion"], "unknown")
        self.assertEqual(units["U2"]["missing_frequencies_hz"], [650.0])
        self.assertEqual(len(units["U1"]["selected"]), 3)
        self.assertTrue(all(point["measurement_id"] for point in units["U1"]["selected"]))
        # 审计事件携带器件级结论。
        events = self.service.audit(self.token, "LOT-1")
        recorded = next(event for event in events if event["event_type"] == "analysis.recorded")
        self.assertEqual({u["unit_id"]: u["conclusion"] for u in recorded["payload"]["units"]},
                         {"U1": "pass", "U2": "unknown"})

    def test_retest_and_revoke_create_new_immutable_analysis(self) -> None:
        self._lot_with_two_units()
        self._measure_complete("U1")
        self._measure_complete("U2")
        first = self.service.analyze(self.token, "LOT-1")
        self.assertEqual(first["result"]["passed"], 2)

        # U1 的 520 Hz 在另一台仪器复测失败 → 新版本：U1 reject。
        self.service.add_measurement(
            self.token, "LOT-1", "U1", 520.0, 0.40, 0.01, "spectrometer-2", 2
        )
        second = self.service.analyze(self.token, "LOT-1")
        self.assertNotEqual(second["analysis_id"], first["analysis_id"])
        self.assertEqual(second["result"]["rejected"], 1)
        self.assertEqual(second["result"]["passed"], 1)

        # 撤销该复测数据后再分析：新版本恢复 pass。
        bad_id = self.service.db.execute(
            "SELECT measurement_id FROM measurements WHERE unit_id='U1' AND sequence_no=2"
        ).fetchone()[0]
        self.service.revoke_measurement(self.token, bad_id, "复测接触不良，数据作废")
        with self.assertRaises(Conflict):
            self.service.revoke_measurement(self.token, bad_id, "再次撤销")
        third = self.service.analyze(self.token, "LOT-1")
        self.assertEqual(third["result"]["passed"], 2)
        self.assertTrue(any(
            point["status"] == "revoked" for point in third["result"]["units"][0]["revoked"]
        ))

        # 旧分析原样可读，未被静默改写。
        old = self.service.get_analysis(self.token, first["analysis_id"])
        self.assertEqual(old["result"]["passed"], 2)
        versions = self.service.list_analyses(self.token, "LOT-1")["analyses"]
        self.assertEqual([item["rule_version"] for item in versions].count("device-yield/2"), 3)

    def test_same_input_is_idempotent_not_duplicate_version(self) -> None:
        self._lot_with_two_units()
        self._measure_complete("U1")
        self._measure_complete("U2")
        first = self.service.analyze(self.token, "LOT-1")
        again = self.service.analyze(self.token, "LOT-1")
        self.assertEqual(again["analysis_id"], first["analysis_id"])
        self.assertTrue(again["reused"])

    def test_decisions_are_append_only_and_bound_to_analysis(self) -> None:
        self._lot_with_two_units()
        self._measure_complete("U1")
        self._measure_complete("U2")
        first = self.service.analyze(self.token, "LOT-1")
        self.service.approve(self.token, "LOT-1", first["analysis_id"], "release", "两器件全频点达标")
        # 同一分析版本不能重复决定。
        with self.assertRaises(Conflict):
            self.service.approve(self.token, "LOT-1", first["analysis_id"], "reject", "翻案")
        # 不存在的分析版本不能决定。
        with self.assertRaises(NotFound):
            self.service.approve(self.token, "LOT-1", 999, "hold", "x")
        # 新分析版本可挂新决定；旧决定仍挂在旧版本上。
        self.service.add_measurement(self.token, "LOT-1", "U2", 650.0, 0.40, 0.01, "spec-2", 2)
        second = self.service.analyze(self.token, "LOT-1")
        self.service.approve(self.token, "LOT-1", second["analysis_id"], "reject", "650 Hz 复测失败")
        self.assertEqual(self.service.get_analysis(self.token, first["analysis_id"])["decision"]["decision"],
                         "release")
        self.assertEqual(self.service.get_lot(self.token, "LOT-1")["status"], "rejected")

    def test_legacy_algorithm_kept_and_reports_business_error_on_count_conflict(self) -> None:
        self._lot_with_two_units()
        self._measure_complete("U1")  # 3 个达标点，分母 2 → 旧模型计数矛盾
        with self.assertRaises(InvalidLotState):
            self.service.analyze_legacy(self.token, "LOT-1")
        # 达标测量点不超过器件数的退化情形下，旧算法照常留档，规则版本不伪装成新版。
        with self.service.db:
            self.service.db.execute("DELETE FROM measurements WHERE lot_id='LOT-1'")
        self.service.add_measurement(self.token, "LOT-1", "U1", 450.0, 0.90, 0.01, "spec-1", 1)
        self.service.add_measurement(self.token, "LOT-1", "U2", 520.0, 0.93, 0.01, "spec-1", 1)
        self.service.add_measurement(self.token, "LOT-1", "U2", 650.0, 0.40, 0.01, "spec-1", 1)
        legacy = self.service.analyze_legacy(self.token, "LOT-1")
        self.assertEqual(legacy["rule_version"], "point-count-yield/1")
        self.assertIn("signal_profile", legacy["result"])
        self.assertEqual(legacy["result"]["yield"]["yield"], 1.0)


if __name__ == "__main__":
    unittest.main()
