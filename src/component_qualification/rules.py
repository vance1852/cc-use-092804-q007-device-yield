"""冻结的器件判定规则与批次良率归并（规则版本 device-yield/2）。

关键修正：良率的分子分母都以**器件**为单位，而不是测量点。一颗器件在多个
频点、多次复测下只产生一个器件结论；批次比例以批次登记的器件数量为分母。

历史规则 point-count/1（把每个达标测量点计为一颗合格器件）只保留在
``legacy`` 模块供旧报告复现，新分析永远不会静默使用它。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Sequence

# 当前冻结规则版本。修改任何判定语义都必须提升版本号。
RULE_VERSION = "device-yield/2"
# 历史算法版本，仅用于复现历史报告。
LEGACY_RULE_VERSION = "point-count/1"

# 冻结的多频点响应计划与单频点达标门限（与历史算法使用的 0.8 门限一致，
# 区别仅在于归并单位从“测量点”改为“器件”）。
DEFAULT_REQUIRED_FREQUENCIES_HZ: tuple[float, ...] = (450.0, 520.0, 650.0)
DEFAULT_RESPONSE_THRESHOLD = 0.8

PASS = "pass"
REJECT = "reject"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class FrequencyCriterion:
    frequency_hz: float
    min_response: float


@dataclass(frozen=True)
class DeviceRule:
    """一份冻结的器件级判定规则。"""

    version: str
    criteria: tuple[FrequencyCriterion, ...]
    tolerance_hz: float = 1e-9

    @property
    def required_frequencies_hz(self) -> tuple[float, ...]:
        return tuple(item.frequency_hz for item in self.criteria)


def default_rule(
    required_frequencies_hz: Sequence[float] = DEFAULT_REQUIRED_FREQUENCIES_HZ,
    response_threshold: float = DEFAULT_RESPONSE_THRESHOLD,
) -> DeviceRule:
    if not required_frequencies_hz or response_threshold != response_threshold:
        raise ValueError("规则参数无效")
    criteria = tuple(
        FrequencyCriterion(frequency_hz=float(f), min_response=float(response_threshold))
        for f in required_frequencies_hz
    )
    return DeviceRule(version=RULE_VERSION, criteria=criteria)


@dataclass(frozen=True)
class MeasurementObservation:
    """一次测量尝试（测量序列中的一个点），而非器件本身。"""

    measurement_id: str
    device_id: str
    sequence_no: int
    frequency_hz: float
    response: float
    noise: float
    instrument: str
    measured_at: str
    retracted: bool = False
    retraction_reason: str | None = None


@dataclass(frozen=True)
class FrequencyOutcome:
    frequency_hz: float
    min_response: float
    response: float | None
    passed: bool
    measurement_id: str | None
    instrument: str | None
    sequence_no: int | None
    retracted_count: int = 0
    repeat_count: int = 0


@dataclass(frozen=True)
class DeviceConclusion:
    device_id: str
    verdict: str
    rule_version: str
    frequencies: tuple[FrequencyOutcome, ...] = field(default_factory=tuple)

    @property
    def missing_frequencies_hz(self) -> tuple[float, ...]:
        return tuple(item.frequency_hz for item in self.frequencies if item.response is None)


def _frequency_key(rule: DeviceRule, frequency_hz: float) -> float:
    for criterion in rule.criteria:
        if math.isclose(frequency_hz, criterion.frequency_hz, abs_tol=rule.tolerance_hz):
            return criterion.frequency_hz
    raise KeyError(frequency_hz)


def consolidate_measurements(
    observations: Sequence[MeasurementObservation], rule: DeviceRule
) -> dict[str, dict[float, MeasurementObservation]]:
    """把测量序列归并为“器件 × 频点”的唯一有效值。

    确定性规则：

    * 被撤销（retracted）的测量一律不参与，只计数留痕；
    * 同一器件同一频点的重复测量（含不同仪器复测）按测量序号 ``sequence_no``
      归并，序号最大的有效测量获胜；序号相同则按测量时间、再按测量编号兜底，
      保证多进程/重放结果一致；
    * 计划外频点不进入任何器件结论（调用方应作为业务错误拒绝，这里防御性忽略）。
    """

    chosen: dict[str, dict[float, MeasurementObservation]] = {}
    for obs in sorted(
        observations,
        key=lambda o: (o.device_id, o.frequency_hz, o.sequence_no, o.measured_at, o.measurement_id),
    ):
        if obs.retracted:
            continue
        try:
            key = _frequency_key(rule, obs.frequency_hz)
        except KeyError:
            continue
        device_map = chosen.setdefault(obs.device_id, {})
        previous = device_map.get(key)
        if previous is None or (obs.sequence_no, obs.measured_at, obs.measurement_id) > (
            previous.sequence_no,
            previous.measured_at,
            previous.measurement_id,
        ):
            device_map[key] = obs
    return chosen


def _retracted_counts(
    observations: Sequence[MeasurementObservation], rule: DeviceRule
) -> dict[tuple[str, float], int]:
    counts: dict[tuple[str, float], int] = {}
    for obs in observations:
        if not obs.retracted:
            continue
        try:
            key_f = _frequency_key(rule, obs.frequency_hz)
        except KeyError:
            continue
        counts[(obs.device_id, key_f)] = counts.get((obs.device_id, key_f), 0) + 1
    return counts


def _repeat_counts(
    observations: Sequence[MeasurementObservation], rule: DeviceRule
) -> dict[tuple[str, float], int]:
    counts: dict[tuple[str, float], int] = {}
    for obs in observations:
        if obs.retracted:
            continue
        try:
            key_f = _frequency_key(rule, obs.frequency_hz)
        except KeyError:
            continue
        key = (obs.device_id, key_f)
        counts[key] = counts.get(key, 0) + 1
    return counts


def classify_devices(
    device_ids: Sequence[str],
    observations: Sequence[MeasurementObservation],
    rule: DeviceRule,
) -> tuple[DeviceConclusion, ...]:
    """先按冻结规则归并单颗器件结论，再供批次层汇总。

    * pass：计划内所有频点都有有效值，且每个频点响应达标；
    * reject：计划内所有频点都有有效值，但至少一个频点不达标；
    * unknown：至少一个频点没有有效值（缺失、全部撤销或未测）。
    """

    effective = consolidate_measurements(observations, rule)
    retracted_counts = _retracted_counts(observations, rule)
    repeat_counts = _repeat_counts(observations, rule)
    conclusions: list[DeviceConclusion] = []
    for device_id in dict.fromkeys(device_ids):
        device_map = effective.get(device_id, {})
        outcomes: list[FrequencyOutcome] = []
        for criterion in rule.criteria:
            chosen = device_map.get(criterion.frequency_hz)
            retracted = retracted_counts.get((device_id, criterion.frequency_hz), 0)
            repeats = repeat_counts.get((device_id, criterion.frequency_hz), 0)
            outcomes.append(
                FrequencyOutcome(
                    frequency_hz=criterion.frequency_hz,
                    min_response=criterion.min_response,
                    response=None if chosen is None else chosen.response,
                    passed=False if chosen is None else chosen.response >= criterion.min_response,
                    measurement_id=None if chosen is None else chosen.measurement_id,
                    instrument=None if chosen is None else chosen.instrument,
                    sequence_no=None if chosen is None else chosen.sequence_no,
                    retracted_count=retracted,
                    repeat_count=repeats,
                )
            )
        if any(item.response is None for item in outcomes):
            verdict = UNKNOWN
        elif all(item.passed for item in outcomes):
            verdict = PASS
        else:
            verdict = REJECT
        conclusions.append(
            DeviceConclusion(
                device_id=device_id, verdict=verdict, rule_version=rule.version, frequencies=tuple(outcomes)
            )
        )
    return tuple(conclusions)


@dataclass(frozen=True)
class BatchYield:
    unit_count: int
    passed: int
    rejected: int
    unknown: int
    yield_rate: float
    reject_rate: float
    unknown_rate: float


def batch_yield(unit_count: int, conclusions: Sequence[DeviceConclusion]) -> BatchYield:
    """以批次登记器件数量为分母计算通过/拒绝/未知比例。"""

    if unit_count <= 0:
        raise ValueError("批次器件数量必须为正整数")
    if len(conclusions) != unit_count:
        raise ValueError(
            f"器件结论数量 {len(conclusions)} 与批次数量 {unit_count} 不一致"
        )
    verdicts: Mapping[str, int] = {
        verdict: sum(1 for c in conclusions if c.verdict == verdict)
        for verdict in (PASS, REJECT, UNKNOWN)
    }
    passed, rejected, unknown = verdicts[PASS], verdicts[REJECT], verdicts[UNKNOWN]
    if passed + rejected + unknown != unit_count:
        raise ValueError("器件结论归并出现未知判定")
    return BatchYield(
        unit_count=unit_count,
        passed=passed,
        rejected=rejected,
        unknown=unknown,
        yield_rate=passed / unit_count,
        reject_rate=rejected / unit_count,
        unknown_rate=unknown / unit_count,
    )
