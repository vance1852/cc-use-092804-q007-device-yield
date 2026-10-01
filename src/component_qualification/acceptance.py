"""容器和快照检查使用的冒烟验收命令。

场景：一批只有两颗国产信号处理芯片样件，各做多频点响应测量。
- U1：三个必测频点齐全，520 Hz 在另一台仪器上复测，最新序列胜出，达标 → pass；
- U2：只测了两个频点，缺 650 Hz → unknown（不许按部分频点放行）。
旧版把达标测量点当器件计数，在该数据下直接构成计数矛盾，以业务错误留档；
新版按器件身份归并，良率分母恒为 2。
"""

from __future__ import annotations

import argparse
import json

from .errors import ComponentError
from .service import ComponentService


REQUIRED_FREQUENCIES = (450.0, 520.0, 650.0)


def run() -> dict:
    service = ComponentService()
    service.bootstrap_admin()
    token = service.auth.login("admin", "component-admin")
    service.create_lot(token, "LOT-DEMO", "CMOS image sensor", "P3.2", 2)
    service.register_units(token, "LOT-DEMO", ["U1", "U2"])

    # U1：频点齐全；520 Hz 先在 spectrometer-1 测量，再在 spectrometer-2 复测。
    for frequency, response, instrument, sequence in (
        (450.0, 0.90, "spectrometer-1", 1),
        (520.0, 0.93, "spectrometer-1", 1),
        (650.0, 0.84, "spectrometer-1", 1),
        (520.0, 0.95, "spectrometer-2", 2),
    ):
        service.add_measurement(
            token, "LOT-DEMO", "U1", frequency, response, 0.01, instrument, sequence
        )
    # U2：缺 650 Hz。
    for frequency, response in ((450.0, 0.71), (520.0, 0.93)):
        service.add_measurement(token, "LOT-DEMO", "U2", frequency, response, 0.01, "spectrometer-1", 1)

    result = service.analyze(token, "LOT-DEMO", REQUIRED_FREQUENCIES)
    qualification = result["result"]

    legacy: dict
    try:
        service.analyze_legacy(token, "LOT-DEMO")
        legacy = {"status": "unexpected"}
    except ComponentError as exc:
        legacy = {"status": "rejected", "code": exc.code, "message": str(exc)}

    service.approve(token, "LOT-DEMO", result["analysis_id"], "hold", "U2 缺少 650 Hz 频点，结论未知，暂缓放行")
    events = service.audit(token, "LOT-DEMO")
    return {
        "status": "ok",
        "lot": "LOT-DEMO",
        "rule_version": qualification["rule_version"],
        "total_units": qualification["total_units"],
        "passed": qualification["passed"],
        "rejected": qualification["rejected"],
        "unknown": qualification["unknown"],
        "rates": qualification["rates"],
        "units": [
            {"unit_id": unit["unit_id"], "conclusion": unit["conclusion"]}
            for unit in qualification["units"]
        ],
        "legacy_point_count": legacy,
        "analysis_id": result["analysis_id"],
        "events": len(events),
    }


def main() -> None:
    argparse.ArgumentParser().parse_args()
    print(json.dumps(run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
