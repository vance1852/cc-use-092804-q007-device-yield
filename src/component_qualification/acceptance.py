"""容器和快照检查使用的冒烟验收命令。

演示修正后的口径：两颗样件（device-1/device-2），每颗做多频点测量，
含跨仪器复测与一次撤销；良率以器件为单位。同时复现历史 point-count/1
报告以证明旧算法与输入摘要被保留、旧决定未被改写。
"""

from __future__ import annotations

import argparse
import json

from .rules import RULE_VERSION
from .service import ComponentService


def run() -> dict:
    service = ComponentService()
    service.bootstrap_admin()
    token = service.auth.login("admin", "component-admin")
    service.create_lot(
        token, "LOT-DEMO", "domestic signal-processor", "P3.2", 2,
        device_ids=["device-1", "device-2"],
    )

    # device-1：三个频点全部达标（520Hz 经历一次撤销后用另一台仪器复测）。
    service.add_measurement(token, "LOT-DEMO", "device-1", 450, .82, .01, "instrument-A")
    bad = service.add_measurement(token, "LOT-DEMO", "device-1", 520, .40, .01, "instrument-A")
    service.add_measurement(token, "LOT-DEMO", "device-1", 650, .85, .01, "instrument-A")
    service.retract_measurement(token, bad["measurement_id"], "读数异常，判定为接触不良")
    service.add_measurement(token, "LOT-DEMO", "device-1", 520, .93, .01, "instrument-B")

    # device-2：缺 650Hz，且 450Hz 重复测量（跨仪器复测，取最新序号）。
    service.add_measurement(token, "LOT-DEMO", "device-2", 450, .71, .01, "instrument-A")
    service.add_measurement(token, "LOT-DEMO", "device-2", 450, .90, .01, "instrument-B")
    service.add_measurement(token, "LOT-DEMO", "device-2", 520, .88, .01, "instrument-A")

    result = service.analyze(token, "LOT-DEMO")
    legacy = service.legacy_report(token, "LOT-DEMO")
    events = service.audit(token, "LOT-DEMO")

    assert result["algorithm_version"] == RULE_VERSION
    assert result["result"]["passed"] == 1
    assert result["result"]["unknown"] == 1
    assert result["traceability"]["passed_device_ids"] == ["device-1"]
    assert result["traceability"]["unknown_device_ids"] == ["device-2"]

    return {
        "status": "ok",
        "lot": result["lot_id"],
        "rule_version": result["rule_version"],
        "rates": result["result"]["rates"],
        "passed": result["traceability"]["passed_device_ids"],
        "unknown": result["traceability"]["unknown_device_ids"],
        "legacy_algorithm": legacy["algorithm_version"],
        "legacy_point_count": legacy["input_summary"]["measurement_point_count"],
        "events": len(events),
    }


def main() -> None:
    argparse.ArgumentParser().parse_args()
    print(json.dumps(run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
