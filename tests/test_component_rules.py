"""device-yield/2 冻结规则的单元测试。"""

from __future__ import annotations

import math
import unittest

from component_qualification.errors import InvalidLotState, InvalidSample
from component_qualification.qualification import (
    DEFAULT_REQUIRED_FREQUENCIES_HZ,
    RULE_VERSION,
    MeasurementRecord,
    qualify_lot,
    validate_measurement,
)


def measurement(
    measurement_id: str, unit_id: str, frequency: float, response: float, *,
    sequence_no: int = 1, measured_at: str = "2026-09-21T00:00:00+00:00",
    instrument: str = "spectrometer-1", revoked: bool = False,
) -> MeasurementRecord:
    return MeasurementRecord(
        measurement_id, unit_id, frequency, response, 0.01, instrument,
        sequence_no, measured_at, revoked,
    )


def complete_unit(unit_id: str, responses=(0.9, 0.9, 0.85)) -> list[MeasurementRecord]:
    return [
        measurement(f"{unit_id}-{frequency}", unit_id, frequency, response)
        for frequency, response in zip(DEFAULT_REQUIRED_FREQUENCIES_HZ, responses)
    ]


class QualificationRuleTests(unittest.TestCase):
    def test_yield_counts_devices_not_measurement_points(self) -> None:
        # 两颗器件，每颗三个频点共 6 条达标测量；旧模型会算出 6/2=300%。
        records = {"U1": complete_unit("U1"), "U2": complete_unit("U2")}
        result = qualify_lot(["U1", "U2"], records)
        self.assertEqual(result.rule_version, RULE_VERSION)
        self.assertEqual(result.total_units, 2)
        self.assertEqual(result.passed, 2)
        self.assertEqual(result.rejected, 0)
        self.assertEqual(result.unknown, 0)
        self.assertEqual(result.yield_rate, 1.0)
        self.assertAlmostEqual(result.yield_rate + result.reject_rate + result.unknown_rate, 1.0)

    def test_any_below_threshold_rejects_unit(self) -> None:
        records = {"U1": complete_unit("U1"), "U2": complete_unit("U2", (0.9, 0.79, 0.85))}
        result = qualify_lot(["U1", "U2"], records)
        self.assertEqual({unit.unit_id: unit.conclusion for unit in result.units}, {"U1": "pass", "U2": "reject"})
        self.assertEqual(result.passed, 1)
        self.assertEqual(result.rejected, 1)
        self.assertEqual(result.units[1].failing_frequencies_hz, (520.0,))
        self.assertEqual(result.reject_rate, 0.5)

    def test_missing_required_frequency_is_unknown(self) -> None:
        partial = [point for point in complete_unit("U2") if point.signal_frequency_hz != 650.0]
        records = {"U1": complete_unit("U1"), "U2": partial}
        result = qualify_lot(["U1", "U2"], records)
        self.assertEqual(result.units[1].conclusion, "unknown")
        self.assertEqual(result.units[1].missing_frequencies_hz, (650.0,))
        self.assertEqual(result.unknown, 1)
        self.assertEqual(result.yield_rate, 0.5)
        self.assertEqual(result.unknown_rate, 0.5)

    def test_repeat_measurement_latest_sequence_wins(self) -> None:
        records_data = complete_unit("U1")
        # 520 Hz 第一次不达标，序列 2 复测达标：最新序列必须胜出 → pass。
        records_data[1] = measurement("U1-520-old", "U1", 520.0, 0.50, sequence_no=1)
        records_data.append(measurement("U1-520-new", "U1", 520.0, 0.95, sequence_no=2))
        result = qualify_lot(["U1"], {"U1": records_data})
        unit = result.units[0]
        self.assertEqual(unit.conclusion, "pass")
        selected = {point.signal_frequency_hz: point for point in unit.selected}
        self.assertEqual(selected[520.0].measurement_id, "U1-520-new")
        self.assertEqual(selected[520.0].status, "selected")
        superseded = [point for point in unit.superseded if point.signal_frequency_hz == 520.0]
        self.assertEqual([point.measurement_id for point in superseded], ["U1-520-old"])
        self.assertIn("取代", superseded[0].note)

    def test_repeat_measurement_latest_sequence_must_not_rescue_old_failure_when_new_fails(self) -> None:
        records_data = complete_unit("U1")
        records_data[1] = measurement("U1-520-good", "U1", 520.0, 0.95, sequence_no=1)
        records_data.append(measurement("U1-520-bad", "U1", 520.0, 0.40, sequence_no=3))
        result = qualify_lot(["U1"], {"U1": records_data})
        self.assertEqual(result.units[0].conclusion, "reject")

    def test_different_instrument_retest_uses_sequence_then_timestamp(self) -> None:
        records_data = [
            measurement("U1-450", "U1", 450.0, 0.9, sequence_no=1),
            measurement("U1-520-a", "U1", 520.0, 0.50, sequence_no=2,
                        measured_at="2026-09-21T09:00:00+00:00", instrument="spectrometer-1"),
            measurement("U1-520-b", "U1", 520.0, 0.93, sequence_no=2,
                        measured_at="2026-09-21T11:00:00+00:00", instrument="spectrometer-2"),
            measurement("U1-650", "U1", 650.0, 0.84, sequence_no=1),
        ]
        result = qualify_lot(["U1"], {"U1": records_data})
        selected = {point.signal_frequency_hz: point.measurement_id for point in result.units[0].selected}
        self.assertEqual(selected[520.0], "U1-520-b")
        self.assertEqual(result.units[0].conclusion, "pass")

    def test_sequence_tie_breaks_by_measurement_id_deterministically(self) -> None:
        records_data = [
            measurement("id-a", "U1", 450.0, 0.9),
            measurement("id-b", "U1", 520.0, 0.9),
            measurement("id-c", "U1", 650.0, 0.9),
            measurement("zzz", "U1", 450.0, 0.50, measured_at="2026-09-21T00:00:00+00:00"),
            measurement("aaa", "U1", 450.0, 0.95, measured_at="2026-09-21T00:00:00+00:00"),
        ]
        first = qualify_lot(["U1"], {"U1": records_data})
        second = qualify_lot(["U1"], {"U1": list(reversed(records_data))})
        self.assertEqual(first.as_dict(), second.as_dict())
        selected = {point.signal_frequency_hz: point.measurement_id for point in first.units[0].selected}
        self.assertEqual(selected[450.0], "aaa")

    def test_revoked_measurement_is_excluded(self) -> None:
        records_data = complete_unit("U1")
        records_data[0] = measurement("U1-450-bad", "U1", 450.0, 0.40, revoked=True)
        result = qualify_lot(["U1"], {"U1": records_data})
        unit = result.units[0]
        # 唯一一条 450 Hz 被撤销 → 该频点视为缺失 → unknown，而不是 reject。
        self.assertEqual(unit.conclusion, "unknown")
        self.assertEqual(unit.missing_frequencies_hz, (450.0,))
        self.assertEqual([point.measurement_id for point in unit.revoked], ["U1-450-bad"])

    def test_revoked_older_reading_lets_newer_count(self) -> None:
        records_data = complete_unit("U1")
        records_data.append(measurement("U1-450-old", "U1", 450.0, 0.40, sequence_no=1, revoked=True))
        result = qualify_lot(["U1"], {"U1": records_data})
        self.assertEqual(result.units[0].conclusion, "pass")

    def test_extra_frequency_is_evidence_but_ignored(self) -> None:
        records_data = complete_unit("U1")
        records_data.append(measurement("U1-700", "U1", 700.0, 0.99))
        result = qualify_lot(["U1"], {"U1": records_data})
        self.assertEqual(result.units[0].conclusion, "pass")
        self.assertEqual([point.measurement_id for point in result.units[0].extra], ["U1-700"])

    def test_unmeasured_registered_unit_is_unknown(self) -> None:
        result = qualify_lot(["U1", "U2"], {"U1": complete_unit("U1"), "U2": []})
        self.assertEqual([u.conclusion for u in result.units], ["pass", "unknown"])
        self.assertEqual(result.total_units, 2)

    def test_empty_ledger_and_duplicate_identities_rejected(self) -> None:
        with self.assertRaises(InvalidLotState):
            qualify_lot([], {})
        with self.assertRaises(InvalidLotState):
            qualify_lot(["U1", "U1"], {"U1": []})

    def test_invalid_samples_are_business_errors(self) -> None:
        with self.assertRaises(InvalidSample):
            validate_measurement("U1", math.nan, 0.9, 0.01, 1, "spec")
        with self.assertRaises(InvalidSample):
            validate_measurement("U1", 450.0, math.inf, 0.01, 1, "spec")
        with self.assertRaises(InvalidSample):
            validate_measurement("", 450.0, 0.9, 0.01, 1, "spec")
        with self.assertRaises(InvalidSample):
            validate_measurement("U1", 450.0, 0.9, 0.01, 0, "spec")
        with self.assertRaises(InvalidSample):
            validate_measurement("U1", 450.0, 0.9, 0.01, True, "spec")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
