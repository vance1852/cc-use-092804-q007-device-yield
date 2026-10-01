"""历史算法 point-count/1：原始报告逻辑的冻结复现。

旧实现把每个“达标测量点”计为一颗合格器件，分子是测量点数量而不是器件
数量，因此多频点/复测会算出超过批次数量的良率，或在计数校验处直接失败。

该算法**只**用于复现和追溯历史报告（历史报告保留原算法与输入摘要）。
新分析一律使用 :mod:`component_qualification.rules`（device-yield/2），
不会、也不允许静默回退到本模块改写旧决定。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Sequence

from .analytics import confidence_interval, summarize_signal_profile

LEGACY_ALGORITHM_VERSION = "point-count/1"
LEGACY_RESPONSE_THRESHOLD = 0.8
LEGACY_MIN_MEASUREMENTS = 3


def legacy_point_yield(unit_count: int, responses: Sequence[float]) -> dict[str, float]:
    """逐字复现旧算法：达标测量点数 / 批次器件数。

    与旧实现一致，当达标点数超过批次数量时抛出 ValueError（计数不一致）。
    """

    passed_points = sum(1 for response in responses if response >= LEGACY_RESPONSE_THRESHOLD)
    if unit_count <= 0 or passed_points < 0 or passed_points > unit_count:
        raise ValueError("inconsistent lot counts")
    return {
        "yield": passed_points / unit_count,
        "reject_rate": 0.0,
        "unknown_rate": (unit_count - passed_points) / unit_count,
    }


def legacy_analyze(
    unit_count: int,
    signal_frequencys_hz: Sequence[float],
    responses: Sequence[float],
) -> dict[str, object]:
    """复现 2026-10 之前历史报告的分析输出形状与计算口径。"""

    if len(responses) < LEGACY_MIN_MEASUREMENTS:
        raise ValueError("three measurements are required")
    summary = summarize_signal_profile(signal_frequencys_hz, responses)
    rates = legacy_point_yield(unit_count, responses)
    ci = confidence_interval(responses)
    return {
        "algorithm_version": LEGACY_ALGORITHM_VERSION,
        "signal_profile": asdict(summary),
        "yield": rates,
        "response_ci": ci,
    }
