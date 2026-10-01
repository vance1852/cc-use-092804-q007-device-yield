"""器件级合格判定与批次良率的冻结规则（device-yield/2）。

与旧版 point-count-yield/1（见 ``analytics.py``，把每个达标测量点计为一颗
合格器件）不同，本模块严格区分三层身份：

1. 器件身份（unit_id）：良率的唯一计数对象，分母恒为批次登记器件数；
2. 测量序列（sequence_no / measured_at）：同一器件同一频点可被重复测量；
3. 批次数量（unit_count）：只用于核对台账，不参与测量点计数。

规则一经冻结不得静默修改；规则变更必须提升 RULE_VERSION 并保留旧分析。

v2 冻结规则
-----------
* 被撤销（revoked）的测量不参与任何判定，只在证据中留痕；
* 同一器件同一频点存在重复测量时，按
  ``sequence_no 最大 → measured_at 最新 → measurement_id 最小``
  的确定性顺序取唯一有效值；较早记录标记为 superseded。不同仪器复测同等
  对待，仪器标识随证据保留，最新序列即仲裁结果；
* 必测频点缺失任一：器件结论为 unknown，不得以部分频点推断合格；
* 必测频点齐全：所有有效响应 ``response >= threshold`` 为 pass，
  任一低于阈值为 reject；
* 批次良率分母为已登记器件数，分子为器件级结论计数，
  pass + reject + unknown 恒等于分母，比例之和恒为 1。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .errors import InvalidLotState, InvalidSample


RULE_VERSION = "device-yield/2"
DEFAULT_REQUIRED_FREQUENCIES_HZ = (450.0, 520.0, 650.0)
DEFAULT_RESPONSE_THRESHOLD = 0.8


@dataclass(frozen=True)
class MeasurementRecord:
    """一条原始测量；revoked=True 表示已被质量人员撤销。"""

    measurement_id: str
    unit_id: str
    signal_frequency_hz: float
    response: float
    noise: float
    instrument: str
    sequence_no: int
    measured_at: str
    revoked: bool = False


@dataclass(frozen=True)
class PointEvidence:
    measurement_id: str
    signal_frequency_hz: float
    response: float
    noise: float
    instrument: str
    sequence_no: int
    measured_at: str
    status: str  # selected | superseded | revoked | extra
    note: str = ""


@dataclass(frozen=True)
class UnitConclusion:
    unit_id: str
    conclusion: str  # pass | reject | unknown
    selected: tuple[PointEvidence, ...]
    missing_frequencies_hz: tuple[float, ...]
    failing_frequencies_hz: tuple[float, ...]
    superseded: tuple[PointEvidence, ...]
    revoked: tuple[PointEvidence, ...]
    extra: tuple[PointEvidence, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class LotQualificationResult:
    rule_version: str
    required_frequencies_hz: tuple[float, ...]
    response_threshold: float
    total_units: int
    passed: int
    rejected: int
    unknown: int
    yield_rate: float
    reject_rate: float
    unknown_rate: float
    units: tuple[UnitConclusion, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "rule_version": self.rule_version,
            "required_frequencies_hz": list(self.required_frequencies_hz),
            "response_threshold": self.response_threshold,
            "total_units": self.total_units,
            "passed": self.passed,
            "rejected": self.rejected,
            "unknown": self.unknown,
            "rates": {
                "yield": self.yield_rate,
                "reject": self.reject_rate,
                "unknown": self.unknown_rate,
            },
            "units": [
                {
                    "unit_id": unit.unit_id,
                    "conclusion": unit.conclusion,
                    "reasons": list(unit.reasons),
                    "missing_frequencies_hz": list(unit.missing_frequencies_hz),
                    "failing_frequencies_hz": list(unit.failing_frequencies_hz),
                    "selected": [_point_dict(point) for point in unit.selected],
                    "superseded": [_point_dict(point) for point in unit.superseded],
                    "revoked": [_point_dict(point) for point in unit.revoked],
                    "extra": [_point_dict(point) for point in unit.extra],
                }
                for unit in self.units
            ],
        }


def _point_dict(point: PointEvidence) -> dict:
    return {
        "measurement_id": point.measurement_id,
        "signal_frequency_hz": point.signal_frequency_hz,
        "response": point.response,
        "noise": point.noise,
        "instrument": point.instrument,
        "sequence_no": point.sequence_no,
        "measured_at": point.measured_at,
        "status": point.status,
        "note": point.note,
    }


def validate_measurement(
    unit_id: str,
    signal_frequency_hz: float,
    response: float,
    noise: float,
    sequence_no: int,
    instrument: str,
) -> None:
    """在入库前把坏样本拦截为业务错误，而不是留到计算阶段抛底层异常。"""

    if not isinstance(unit_id, str) or not unit_id.strip():
        raise InvalidSample("器件身份不能为空")
    if not isinstance(instrument, str) or not instrument.strip():
        raise InvalidSample("仪器标识不能为空")
    if not isinstance(sequence_no, int) or isinstance(sequence_no, bool) or sequence_no <= 0:
        raise InvalidSample("测量序列号必须为正整数")
    values = (signal_frequency_hz, response, noise)
    if any(not isinstance(value, (int, float)) or isinstance(value, bool) for value in values):
        raise InvalidSample("频点、响应和噪声必须为数值")
    if any(not math.isfinite(float(value)) for value in values):
        raise InvalidSample("测量数值必须有限，不接受 NaN/Inf")
    if not math.isfinite(float(signal_frequency_hz)) or float(signal_frequency_hz) <= 0:
        raise InvalidSample("信号频点必须为正数")


def _select_winner(points: Sequence[MeasurementRecord]) -> MeasurementRecord:
    """重复测量仲裁：序列号最大优先，其次测量时间最新；完全并列时取最小记录ID。"""

    newest = max(points, key=lambda record: (record.sequence_no, record.measured_at))
    tied = [
        record
        for record in points
        if (record.sequence_no, record.measured_at) == (newest.sequence_no, newest.measured_at)
    ]
    return min(tied, key=lambda record: record.measurement_id)


def _conclude_unit(
    unit_id: str,
    records: Sequence[MeasurementRecord],
    required_frequencies: Sequence[float],
    threshold: float,
) -> UnitConclusion:
    revoked = [
        PointEvidence(
            record.measurement_id, record.signal_frequency_hz, record.response, record.noise,
            record.instrument, record.sequence_no, record.measured_at, "revoked",
            "已撤销，不参与判定",
        )
        for record in records
        if record.revoked
    ]
    active = [record for record in records if not record.revoked]

    by_frequency: dict[float, list[MeasurementRecord]] = {}
    extra_records: list[MeasurementRecord] = []
    for record in active:
        if record.signal_frequency_hz in required_frequencies:
            by_frequency.setdefault(record.signal_frequency_hz, []).append(record)
        else:
            extra_records.append(record)

    selected: list[PointEvidence] = []
    superseded: list[PointEvidence] = []
    missing: list[float] = []
    failing: list[float] = []
    reasons: list[str] = []

    for frequency in required_frequencies:
        candidates = by_frequency.get(frequency, [])
        if not candidates:
            missing.append(frequency)
            reasons.append(f"缺失必测频点 {frequency:g} Hz")
            continue
        winner = _select_winner(candidates)
        for record in sorted(candidates, key=lambda item: item.measurement_id):
            point = PointEvidence(
                record.measurement_id, record.signal_frequency_hz, record.response, record.noise,
                record.instrument, record.sequence_no, record.measured_at,
                "selected" if record.measurement_id == winner.measurement_id else "superseded",
                "" if record.measurement_id == winner.measurement_id
                else f"被同频点更新序列 {winner.sequence_no}（{winner.measurement_id}）取代",
            )
            (selected if point.status == "selected" else superseded).append(point)
        if winner.response < threshold:
            failing.append(frequency)
            reasons.append(f"频点 {frequency:g} Hz 响应 {winner.response:g} 低于阈值 {threshold:g}")

    extra = [
        PointEvidence(
            record.measurement_id, record.signal_frequency_hz, record.response, record.noise,
            record.instrument, record.sequence_no, record.measured_at, "extra",
            "非必测频点，不参与判定",
        )
        for record in sorted(extra_records, key=lambda item: (item.signal_frequency_hz, item.measurement_id))
    ]

    if missing:
        conclusion = "unknown"
    elif failing:
        conclusion = "reject"
    else:
        conclusion = "pass"

    return UnitConclusion(
        unit_id=unit_id,
        conclusion=conclusion,
        selected=tuple(sorted(selected, key=lambda point: point.signal_frequency_hz)),
        missing_frequencies_hz=tuple(missing),
        failing_frequencies_hz=tuple(failing),
        superseded=tuple(sorted(superseded, key=lambda point: point.signal_frequency_hz)),
        revoked=tuple(sorted(revoked, key=lambda point: (point.signal_frequency_hz, point.measurement_id))),
        extra=tuple(extra),
        reasons=tuple(reasons),
    )


def qualify_lot(
    unit_ids: Sequence[str],
    measurements: Mapping[str, Sequence[MeasurementRecord]],
    *,
    required_frequencies_hz: Sequence[float] = DEFAULT_REQUIRED_FREQUENCIES_HZ,
    response_threshold: float = DEFAULT_RESPONSE_THRESHOLD,
) -> LotQualificationResult:
    """按冻结 v2 规则，把器件台账与测量序列归并为批次结论。"""

    required = tuple(sorted(float(frequency) for frequency in required_frequencies_hz))
    if not required:
        raise InvalidLotState("必测频点集合不能为空")
    if len(set(required)) != len(required):
        raise InvalidLotState("必测频点存在重复")
    if not math.isfinite(response_threshold):
        raise InvalidLotState("响应阈值必须为有限数值")

    ordered_units = tuple(unit_ids)
    if not ordered_units:
        raise InvalidLotState("批次尚未登记任何器件，无法计算良率")
    if len(set(ordered_units)) != len(ordered_units):
        raise InvalidLotState("器件台账存在重复身份")

    units = tuple(
        _conclude_unit(unit_id, tuple(measurements.get(unit_id, ())), required, response_threshold)
        for unit_id in ordered_units
    )
    passed = sum(unit.conclusion == "pass" for unit in units)
    rejected = sum(unit.conclusion == "reject" for unit in units)
    unknown = sum(unit.conclusion == "unknown" for unit in units)
    total = len(units)
    if passed + rejected + unknown != total:  # 防御性不变量，防止计数漂移
        raise InvalidLotState("器件结论计数与台账数量不一致")

    return LotQualificationResult(
        rule_version=RULE_VERSION,
        required_frequencies_hz=required,
        response_threshold=response_threshold,
        total_units=total,
        passed=passed,
        rejected=rejected,
        unknown=unknown,
        yield_rate=passed / total,
        reject_rate=rejected / total,
        unknown_rate=unknown / total,
        units=units,
    )
