"""协调认证、批次、器件身份、测量序列、版本化分析与放行门禁的应用服务。"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import asdict
from typing import Sequence

from . import legacy
from .analytics import summarize_signal_profile
from .errors import InvalidSample, NotFound, ServiceError, ValidationFailed
from .rules import (
    DEFAULT_REQUIRED_FREQUENCIES_HZ,
    RULE_VERSION,
    DeviceConclusion,
    MeasurementObservation,
    DeviceRule,
    batch_yield,
    classify_devices,
    default_rule,
)
from .auth import Auth
from .storage import connect, event, transaction, utcnow


class ComponentService:
    def __init__(self, database: str = ":memory:"):
        # HTTP 服务在多线程工作进程间共享同一连接；写入由 BEGIN IMMEDIATE 串行化。
        self.db = connect(database, check_same_thread=False)
        self.auth = Auth(self.db)

    def bootstrap_admin(self, user_id: str = "admin", password: str = "component-admin") -> None:
        try:
            self.auth.create_user(user_id, password, "admin")
        except Exception:
            pass

    # ------------------------------------------------------------------ 批次

    def create_lot(
        self,
        token: str,
        lot_id: str,
        product: str,
        process_rev: str,
        unit_count: int,
        device_ids: Sequence[str] | None = None,
        required_frequencies_hz: Sequence[float] = DEFAULT_REQUIRED_FREQUENCIES_HZ,
    ) -> dict:
        actor = self.auth.require(token, "submit")
        if unit_count <= 0 or not str(lot_id).strip() or not str(process_rev).strip():
            raise ValidationFailed("lot fields are invalid")
        frequencies = tuple(DEFAULT_REQUIRED_FREQUENCIES_HZ if required_frequencies_hz is None
                            else (float(f) for f in required_frequencies_hz))
        rule = default_rule(frequencies)
        if device_ids is None:
            device_ids = [f"{lot_id}-U{index + 1:03d}" for index in range(unit_count)]
        device_ids = tuple(str(device_id).strip() for device_id in device_ids)
        if len(device_ids) != unit_count or any(not device_id for device_id in device_ids):
            raise ValidationFailed("器件身份数量必须与批次数量一致且编号非空")
        if len(set(device_ids)) != unit_count:
            raise ValidationFailed("器件身份编号不能重复")
        now = utcnow()
        with transaction(self.db):
            self.db.execute(
                "INSERT INTO chip_lots VALUES(?,?,?,?,?,?,?,?,?,?)",
                (lot_id, product, process_rev, unit_count, RULE_VERSION,
                 json.dumps(list(rule.required_frequencies_hz)),
                 "engineering", actor.user_id, now, now),
            )
            for device_id in device_ids:
                self.db.execute(
                    "INSERT INTO chip_devices(lot_id,device_id,created_at) VALUES(?,?,?)",
                    (lot_id, device_id, now),
                )
            event(self.db, lot_id, "created", actor.user_id, {
                "product": product,
                "process_rev": process_rev,
                "unit_count": unit_count,
                "rule_version": RULE_VERSION,
                "required_frequencies_hz": list(rule.required_frequencies_hz),
                "device_ids": list(device_ids),
            })
        return self.get_lot(token, lot_id)

    def get_lot(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not row:
            raise NotFound(lot_id)
        return dict(row)

    def list_devices(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        self._require_lot(lot_id)
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT lot_id,device_id,created_at FROM chip_devices WHERE lot_id=? ORDER BY device_id",
                (lot_id,),
            ).fetchall()
        ]

    def _require_lot(self, lot_id: str) -> dict:
        row = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not row:
            raise NotFound(lot_id)
        return dict(row)

    def _rule_for(self, lot: dict) -> DeviceRule:
        version = lot.get("rule_version") or RULE_VERSION
        if version != RULE_VERSION:
            raise ServiceError(f"批次 {lot['lot_id']} 属于历史规则 {version}，请使用历史报告接口查看")
        stored = lot.get("required_frequencies_hz")
        frequencies = json.loads(stored) if stored else list(DEFAULT_REQUIRED_FREQUENCIES_HZ)
        return default_rule(frequencies)

    # -------------------------------------------------------------- 测量序列

    def add_measurement(
        self,
        token: str,
        lot_id: str,
        device_id: str,
        signal_frequency_hz: float,
        response: float,
        noise: float,
        instrument: str,
        sequence_no: int | None = None,
        measured_at: str | None = None,
    ) -> dict:
        actor = self.auth.require(token, "measure")
        lot = self._require_lot(lot_id)
        rule = self._rule_for(lot)
        try:
            frequency = float(signal_frequency_hz)
            response_value = float(response)
            noise_value = float(noise)
        except (TypeError, ValueError) as exc:
            raise InvalidSample("测量频点、响应和噪声必须是数值") from exc
        if not all(math.isfinite(value) for value in (frequency, response_value, noise_value)):
            raise InvalidSample("测量频点、响应和噪声必须为有限数值")
        if not str(instrument or "").strip():
            raise InvalidSample("仪器编号不能为空")
        if not any(
            math.isclose(frequency, criterion.frequency_hz, abs_tol=rule.tolerance_hz)
            for criterion in rule.criteria
        ):
            raise InvalidSample(
                f"频点 {frequency:g} Hz 不在冻结测量计划内: "
                f"{[f for f in rule.required_frequencies_hz]}"
            )
        with transaction(self.db):
            if not self.db.execute(
                "SELECT 1 FROM chip_devices WHERE lot_id=? AND device_id=?", (lot_id, device_id)
            ).fetchone():
                raise NotFound(f"器件 {device_id} 不属于批次 {lot_id}")
            if sequence_no is None:
                row = self.db.execute(
                    "SELECT COALESCE(MAX(sequence_no),0)+1 FROM measurements WHERE lot_id=? AND device_id=?",
                    (lot_id, device_id),
                ).fetchone()
                sequence_no = int(row[0])
            elif int(sequence_no) <= 0:
                raise InvalidSample("测量序号必须为正整数")
            measured_at = measured_at or utcnow()
            measurement_id = uuid.uuid4().hex
            try:
                self.db.execute(
                    "INSERT INTO measurements(measurement_id,lot_id,device_id,sequence_no,"
                    "signal_frequency_hz,response,noise,instrument,operator,measured_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (measurement_id, lot_id, device_id, int(sequence_no), frequency,
                     response_value, noise_value, str(instrument).strip(), actor.user_id, measured_at),
                )
            except Exception as exc:
                raise InvalidSample("同一器件的测量序号已存在（重复提交请使用下一序号）") from exc
            event(self.db, lot_id, "measurement", actor.user_id, {
                "measurement_id": measurement_id,
                "device_id": device_id,
                "sequence_no": int(sequence_no),
                "signal_frequency_hz": frequency,
                "instrument": str(instrument).strip(),
            })
        return {
            "measurement_id": measurement_id,
            "lot_id": lot_id,
            "device_id": device_id,
            "sequence_no": int(sequence_no),
        }

    def retract_measurement(self, token: str, measurement_id: str, reason: str) -> dict:
        """撤销一条测量：保留记录留痕，但该点不再参与任何器件结论。"""

        actor = self.auth.require(token, "measure")
        if not str(reason or "").strip():
            raise ValidationFailed("撤销原因不能为空")
        with transaction(self.db):
            row = self.db.execute(
                "SELECT lot_id,device_id,retracted FROM measurements WHERE measurement_id=?",
                (measurement_id,),
            ).fetchone()
            if not row:
                raise NotFound(measurement_id)
            if row["retracted"]:
                raise ValidationFailed("该测量已经被撤销")
            self.db.execute(
                "UPDATE measurements SET retracted=1,retraction_reason=? WHERE measurement_id=?",
                (str(reason).strip(), measurement_id),
            )
            event(self.db, row["lot_id"], "measurement.retracted", actor.user_id, {
                "measurement_id": measurement_id,
                "device_id": row["device_id"],
                "reason": str(reason).strip(),
            })
        return {"measurement_id": measurement_id, "retracted": True}

    def list_measurements(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        self._require_lot(lot_id)
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT measurement_id,lot_id,device_id,sequence_no,signal_frequency_hz,"
                "response,noise,instrument,operator,measured_at,retracted,retraction_reason "
                "FROM measurements WHERE lot_id=? "
                "ORDER BY device_id,signal_frequency_hz,sequence_no",
                (lot_id,),
            ).fetchall()
        ]

    # -------------------------------------------------------------- 分析（新）

    def _observations(self, lot_id: str) -> tuple[MeasurementObservation, ...]:
        rows = self.db.execute(
            "SELECT measurement_id,device_id,sequence_no,signal_frequency_hz,response,noise,"
            "instrument,measured_at,retracted,retraction_reason "
            "FROM measurements WHERE lot_id=?",
            (lot_id,),
        ).fetchall()
        return tuple(
            MeasurementObservation(
                measurement_id=row["measurement_id"],
                device_id=row["device_id"],
                sequence_no=row["sequence_no"],
                frequency_hz=row["signal_frequency_hz"],
                response=row["response"],
                noise=row["noise"],
                instrument=row["instrument"],
                measured_at=row["measured_at"],
                retracted=bool(row["retracted"]),
                retraction_reason=row["retraction_reason"],
            )
            for row in rows
        )

    def _input_digest(
        self, lot: dict, rule: DeviceRule, device_ids: Sequence[str], observations: Sequence[MeasurementObservation]
    ) -> str:
        snapshot = {
            "lot_id": lot["lot_id"],
            "rule_version": rule.version,
            "required_frequencies_hz": list(rule.required_frequencies_hz),
            "unit_count": lot["unit_count"],
            "device_ids": list(device_ids),
            "measurements": [
                {
                    "measurement_id": item.measurement_id,
                    "device_id": item.device_id,
                    "sequence_no": item.sequence_no,
                    "frequency_hz": item.frequency_hz,
                    "response": item.response,
                    "noise": item.noise,
                    "instrument": item.instrument,
                    "measured_at": item.measured_at,
                    "retracted": item.retracted,
                    "retraction_reason": item.retraction_reason,
                }
                for item in observations
            ],
        }
        return hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def analyze(self, token: str, lot_id: str) -> dict:
        actor = self.auth.require(token, "analyze")
        lot = self._require_lot(lot_id)
        rule = self._rule_for(lot)
        device_rows = self.db.execute(
            "SELECT device_id FROM chip_devices WHERE lot_id=? ORDER BY device_id", (lot_id,)
        ).fetchall()
        device_ids = tuple(row["device_id"] for row in device_rows)
        if len(device_ids) != lot["unit_count"]:
            raise ValidationFailed(
                f"登记器件数 {len(device_ids)} 与批次数量 {lot['unit_count']} 不一致，拒绝出良率"
            )
        observations = self._observations(lot_id)
        input_digest = self._input_digest(lot, rule, device_ids, observations)

        existing = self.db.execute(
            "SELECT * FROM analyses WHERE lot_id=? AND input_sha256=? AND algorithm_version=?",
            (lot_id, input_digest, RULE_VERSION),
        ).fetchone()
        if existing:
            return self._analysis_payload(existing, device_ids, observations, cached=True)

        conclusions = classify_devices(device_ids, observations, rule)
        try:
            summary = batch_yield(lot["unit_count"], conclusions)
        except ValueError as exc:
            # 计数不一致属于业务数据问题，而不是让底层除零/断言逃逸成 500。
            raise ValidationFailed(str(exc)) from exc
        result = {
            "rule_version": RULE_VERSION,
            "unit_count": summary.unit_count,
            "passed": summary.passed,
            "rejected": summary.rejected,
            "unknown": summary.unknown,
            "rates": {
                "yield_rate": summary.yield_rate,
                "reject_rate": summary.reject_rate,
                "unknown_rate": summary.unknown_rate,
            },
            "device_conclusions": [self._conclusion_dict(item) for item in conclusions],
        }
        now = utcnow()
        with transaction(self.db):
            cursor = self.db.execute(
                "INSERT INTO analyses(lot_id,algorithm_version,rule_version,input_sha256,unit_count,"
                "passed,rejected,unknown,result_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (lot_id, RULE_VERSION, RULE_VERSION, input_digest, summary.unit_count,
                 summary.passed, summary.rejected, summary.unknown,
                 json.dumps(result, ensure_ascii=False, sort_keys=True), actor.user_id, now),
            )
            analysis_id = cursor.lastrowid
            event(self.db, lot_id, "analysis.completed", actor.user_id, {
                "analysis_id": analysis_id,
                "algorithm_version": RULE_VERSION,
                "input_sha256": input_digest,
                "passed": summary.passed,
                "rejected": summary.rejected,
                "unknown": summary.unknown,
            })
        row = self.db.execute("SELECT * FROM analyses WHERE analysis_id=?", (analysis_id,)).fetchone()
        return self._analysis_payload(row, device_ids, observations, cached=False)

    @staticmethod
    def _conclusion_dict(conclusion: DeviceConclusion) -> dict:
        return {
            "device_id": conclusion.device_id,
            "verdict": conclusion.verdict,
            "rule_version": conclusion.rule_version,
            "missing_frequencies_hz": list(conclusion.missing_frequencies_hz),
            "frequencies": [asdict(item) for item in conclusion.frequencies],
        }

    def _analysis_payload(
        self,
        row,
        device_ids: Sequence[str],
        observations: Sequence[MeasurementObservation],
        *,
        cached: bool,
    ) -> dict:
        result = json.loads(row["result_json"])
        return {
            "analysis_id": row["analysis_id"],
            "lot_id": row["lot_id"],
            "algorithm_version": row["algorithm_version"],
            "rule_version": row["rule_version"],
            "input_sha256": row["input_sha256"],
            "created_by": row["created_by"],
            "cached": cached,
            "result": result,
            "traceability": {
                "unit_count": row["unit_count"],
                "passed_device_ids": [
                    item["device_id"] for item in result["device_conclusions"] if item["verdict"] == "pass"
                ],
                "rejected_device_ids": [
                    item["device_id"] for item in result["device_conclusions"] if item["verdict"] == "reject"
                ],
                "unknown_device_ids": [
                    item["device_id"] for item in result["device_conclusions"] if item["verdict"] == "unknown"
                ],
            },
        }

    def get_analysis(self, token: str, lot_id: str, analysis_id: int) -> dict:
        self.auth.require(token, "read")
        self._require_lot(lot_id)
        row = self.db.execute(
            "SELECT * FROM analyses WHERE lot_id=? AND analysis_id=?", (lot_id, analysis_id)
        ).fetchone()
        if not row:
            raise NotFound(f"分析版本 {analysis_id} 不存在")
        # 追溯信息直接取自不可变结果体，不依赖调用时刻的器件/测量现状。
        return self._analysis_payload(row, (), (), cached=True)

    def list_analyses(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        self._require_lot(lot_id)
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT analysis_id,lot_id,algorithm_version,rule_version,input_sha256,unit_count,"
                "passed,rejected,unknown,created_by,created_at FROM analyses "
                "WHERE lot_id=? ORDER BY analysis_id",
                (lot_id,),
            ).fetchall()
        ]

    # -------------------------------------------------------- 历史报告（旧）

    def legacy_report(self, token: str, lot_id: str) -> dict:
        """按冻结的历史 point-count/1 口径复现旧报告，只读、不写、不改旧决定。"""

        self.auth.require(token, "read")
        lot = self._require_lot(lot_id)
        rows = self.db.execute(
            "SELECT signal_frequency_hz,response,measurement_id,device_id,retracted "
            "FROM measurements WHERE lot_id=? ORDER BY signal_frequency_hz,measurement_id",
            (lot_id,),
        ).fetchall()
        # 历史算法没有器件身份与撤销概念，逐字复现其输入口径：全部测量点。
        frequencies = [row["signal_frequency_hz"] for row in rows]
        responses = [row["response"] for row in rows]
        input_summary = {
            "measurement_point_count": len(rows),
            "device_identities_ignored": True,
            "retractions_ignored": True,
            "measurement_ids": [row["measurement_id"] for row in rows],
        }
        if len(responses) < legacy.LEGACY_MIN_MEASUREMENTS:
            reproduced: dict[str, object] = {
                "error": "three measurements are required",
            }
        else:
            try:
                reproduced = legacy.legacy_analyze(lot["unit_count"], frequencies, responses)
            except ValueError as exc:
                # 历史上正是在这里因计数不一致失败；保留该结论，不做任何“修正”。
                reproduced = {"error": str(exc)}
        return {
            "lot_id": lot_id,
            "historical": True,
            "algorithm_version": legacy.LEGACY_ALGORITHM_VERSION,
            "input_summary": input_summary,
            "reproduced": reproduced,
            "note": "历史报告按 point-count/1 原算法复现；放行决定请使用 device-yield/2 新分析。",
        }

    def signal_profile(self, token: str, lot_id: str) -> dict:
        """信号谱摘要（科学计算，与良率口径解耦），使用各频点最新有效值。"""

        self.auth.require(token, "analyze")
        lot = self._require_lot(lot_id)
        rule = self._rule_for(lot)
        observations = self._observations(lot_id)
        effective: dict[float, float] = {}
        for obs in sorted(observations, key=lambda o: (o.frequency_hz, o.sequence_no, o.measured_at)):
            if obs.retracted:
                continue
            if not any(
                math.isclose(obs.frequency_hz, c.frequency_hz, abs_tol=rule.tolerance_hz)
                for c in rule.criteria
            ):
                continue
            effective[obs.frequency_hz] = obs.response
        if len(effective) < 3:
            raise ValidationFailed("至少需要三个计划内频点的有效测量才能生成信号谱摘要")
        frequencies = sorted(effective)
        summary = summarize_signal_profile(frequencies, [effective[f] for f in frequencies])
        return {"rule_version": RULE_VERSION, "signal_profile": summary.__dict__}

    # ------------------------------------------------------------------ 审批

    def approve(self, token: str, lot_id: str, decision: str, reason: str) -> dict:
        actor = self.auth.require(token, "approve")
        if decision not in {"release", "hold", "reject"} or not reason.strip():
            raise ValidationFailed("decision and reason are required")
        with transaction(self.db):
            if not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
                raise NotFound(lot_id)
            self.db.execute("INSERT OR REPLACE INTO approvals VALUES(?,?,?,?,?)", (lot_id, actor.user_id, decision, reason, utcnow()))
            status = {"release": "released", "hold": "hold", "reject": "rejected"}[decision]
            self.db.execute("UPDATE chip_lots SET status=?,updated_at=? WHERE lot_id=?", (status, utcnow(), lot_id))
            event(self.db, lot_id, "approval", actor.user_id, {"decision": decision, "reason": reason})
        return self.get_lot(token, lot_id)

    def audit(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        return [dict(r) for r in self.db.execute("SELECT * FROM lot_events WHERE lot_id=? ORDER BY event_id", (lot_id,)).fetchall()]
