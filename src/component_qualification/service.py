"""协调认证、批次台账、测量序列、版本化分析与放行门禁的应用服务。

关键不变量：
* 良率只按器件身份计数，分母是批次登记器件数；
* 分析结果按 (规则版本, 输入摘要) 不可变留存，新测量或撤销产生新版本，
  旧分析与旧决定原样保留，绝不静默改写；
* 无效样本与台账矛盾抛出 :class:`ComponentError` 业务错误。
"""

from __future__ import annotations

import json
import uuid
from typing import Sequence

from . import analytics
from .auth import Auth
from .errors import Conflict, InvalidLotState, InvalidSample, NotFound
from .jsonio import canonical_json, content_digest
from .qualification import (
    DEFAULT_REQUIRED_FREQUENCIES_HZ,
    DEFAULT_RESPONSE_THRESHOLD,
    RULE_VERSION,
    MeasurementRecord,
    qualify_lot,
    validate_measurement,
)
from .storage import connect, event, transaction, utcnow


class ComponentService:
    def __init__(self, database: str = ":memory:"):
        self.db = connect(database)
        self.auth = Auth(self.db)

    def bootstrap_admin(self, user_id: str = "admin", password: str = "component-admin") -> None:
        try:
            self.auth.create_user(user_id, password, "admin")
        except Exception:
            pass

    # ----- 批次与器件台账 -------------------------------------------------

    def create_lot(self, token: str, lot_id: str, product: str, process_rev: str, unit_count: int) -> dict:
        actor = self.auth.require(token, "submit")
        if (
            not isinstance(unit_count, int)
            or isinstance(unit_count, bool)
            or unit_count <= 0
            or not lot_id.strip()
            or not process_rev.strip()
        ):
            raise InvalidSample("批次字段无效：编号、工艺版本和正整数器件数量均为必填")
        now = utcnow()
        try:
            with transaction(self.db):
                self.db.execute(
                    "INSERT INTO chip_lots VALUES(?,?,?,?,?,?,?,?)",
                    (lot_id, product, process_rev, unit_count, "engineering", actor.user_id, now, now),
                )
                event(self.db, lot_id, "created", actor.user_id, {"product": product, "process_rev": process_rev, "unit_count": unit_count})
        except Exception as exc:
            raise Conflict(f"批次已存在: {lot_id}") from exc
        return self.get_lot(token, lot_id)

    def get_lot(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "read")
        return self._lot_row(lot_id)

    def _lot_row(self, lot_id: str) -> dict:
        row = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not row:
            raise NotFound(f"批次不存在: {lot_id}")
        return dict(row)

    def register_units(self, token: str, lot_id: str, unit_ids: Sequence[str]) -> dict:
        """把器件身份登记进台账；登记数量必须与批次声明数量一致。"""

        actor = self.auth.require(token, "submit")
        lot = self._lot_row(lot_id)
        ids = [str(unit_id).strip() for unit_id in unit_ids]
        if not ids or any(not unit_id for unit_id in ids):
            raise InvalidSample("器件身份列表不能为空且不得含空白身份")
        if len(ids) != len(set(ids)):
            raise InvalidSample("器件身份存在重复")
        if len(ids) != lot["unit_count"]:
            raise InvalidLotState(
                f"器件登记数量 {len(ids)} 与批次声明数量 {lot['unit_count']} 不一致，拒绝据此计算良率"
            )
        now = utcnow()
        try:
            with transaction(self.db):
                for unit_id in ids:
                    self.db.execute(
                        "INSERT INTO lot_units(lot_id,unit_id,registered_by,registered_at) VALUES(?,?,?,?)",
                        (lot_id, unit_id, actor.user_id, now),
                    )
                self.db.execute("UPDATE chip_lots SET updated_at=? WHERE lot_id=?", (now, lot_id))
                event(self.db, lot_id, "units.registered", actor.user_id, {"unit_ids": ids})
        except Exception as exc:
            raise Conflict("器件台账已登记或身份冲突，不可重复登记") from exc
        return {"lot_id": lot_id, "unit_ids": ids, "registered": len(ids)}

    def list_units(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        self._lot_row(lot_id)
        rows = self.db.execute(
            "SELECT unit_id,registered_by,registered_at FROM lot_units WHERE lot_id=? ORDER BY unit_id",
            (lot_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ----- 测量序列 -------------------------------------------------------

    def add_measurement(
        self,
        token: str,
        lot_id: str,
        unit_id: str,
        signal_frequency_hz: float,
        response: float,
        noise: float,
        instrument: str,
        sequence_no: int | None = None,
    ) -> dict:
        actor = self.auth.require(token, "measure")
        self._lot_row(lot_id)
        if not self.db.execute(
            "SELECT 1 FROM lot_units WHERE lot_id=? AND unit_id=?", (lot_id, unit_id)
        ).fetchone():
            raise InvalidSample(f"器件未登记，不能挂靠测量: {unit_id}")
        validate_measurement(unit_id, signal_frequency_hz, response, noise, sequence_no or 1, instrument)
        frequency = float(signal_frequency_hz)
        with transaction(self.db):
            if sequence_no is None:
                row = self.db.execute(
                    "SELECT COALESCE(MAX(sequence_no),0)+1 AS next_no FROM measurements "
                    "WHERE lot_id=? AND unit_id=? AND signal_frequency_hz=?",
                    (lot_id, unit_id, frequency),
                ).fetchone()
                sequence_no = int(row["next_no"])
            measurement_id = uuid.uuid4().hex
            measured_at = utcnow()
            try:
                self.db.execute(
                    "INSERT INTO measurements(measurement_id,lot_id,unit_id,signal_frequency_hz,response,noise,"
                    "instrument,sequence_no,operator,measured_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (measurement_id, lot_id, unit_id, frequency, float(response), float(noise),
                     instrument, int(sequence_no), actor.user_id, measured_at),
                )
            except Exception as exc:
                raise Conflict(
                    f"测量序列号冲突: 器件 {unit_id} 频点 {frequency:g} Hz 序列 {sequence_no} 已存在"
                ) from exc
            event(self.db, lot_id, "measurement.recorded", actor.user_id, {
                "measurement_id": measurement_id, "unit_id": unit_id,
                "signal_frequency_hz": frequency, "sequence_no": sequence_no, "instrument": instrument,
            })
        return {"measurement_id": measurement_id, "lot_id": lot_id, "unit_id": unit_id, "sequence_no": sequence_no}

    def revoke_measurement(self, token: str, measurement_id: str, reason: str) -> dict:
        """撤销测量；撤销只追加标记，不删除原始记录，且不可重复撤销。"""

        actor = self.auth.require(token, "measure")
        if not reason.strip():
            raise InvalidSample("撤销原因不能为空")
        row = self.db.execute("SELECT * FROM measurements WHERE measurement_id=?", (measurement_id,)).fetchone()
        if not row:
            raise NotFound(f"测量记录不存在: {measurement_id}")
        if row["revoked"]:
            raise Conflict(f"测量已撤销，不能重复撤销: {measurement_id}")
        now = utcnow()
        with transaction(self.db):
            self.db.execute(
                "UPDATE measurements SET revoked=1,revoked_at=?,revoked_by=?,revoke_reason=? WHERE measurement_id=?",
                (now, actor.user_id, reason, measurement_id),
            )
            event(self.db, row["lot_id"], "measurement.revoked", actor.user_id, {
                "measurement_id": measurement_id, "unit_id": row["unit_id"],
                "signal_frequency_hz": row["signal_frequency_hz"], "sequence_no": row["sequence_no"],
                "reason": reason,
            })
        return {"measurement_id": measurement_id, "status": "revoked"}

    # ----- 分析输入快照 ---------------------------------------------------

    def _analysis_input(self, lot_id: str) -> tuple[list[str], list[dict]]:
        unit_rows = self.db.execute(
            "SELECT unit_id FROM lot_units WHERE lot_id=? ORDER BY unit_id", (lot_id,)
        ).fetchall()
        unit_ids = [row["unit_id"] for row in unit_rows]
        measurement_rows = self.db.execute(
            "SELECT measurement_id,unit_id,signal_frequency_hz,response,noise,instrument,sequence_no,"
            "measured_at,revoked FROM measurements WHERE lot_id=? "
            "ORDER BY unit_id,signal_frequency_hz,sequence_no,measurement_id",
            (lot_id,),
        ).fetchall()
        measurements = [dict(row) for row in measurement_rows]
        return unit_ids, measurements

    def _require_complete_ledger(self, lot: dict, unit_ids: list[str]) -> None:
        if not unit_ids:
            raise InvalidLotState("批次尚未登记器件，无法计算良率")
        if len(unit_ids) != lot["unit_count"]:
            raise InvalidLotState(
                f"器件登记数量 {len(unit_ids)} 与批次声明数量 {lot['unit_count']} 不一致，拒绝计算"
            )

    def _store_analysis(
        self, lot_id: str, rule_version: str, frequencies: Sequence[float], threshold: float,
        input_rows: list[dict], result: dict, actor_id: str,
    ) -> dict:
        input_sha = content_digest(input_rows)
        parameters = {"required_frequencies_hz": sorted(frequencies), "response_threshold": threshold}
        fingerprint = content_digest([parameters, input_rows])
        with transaction(self.db):
            existing = self.db.execute(
                "SELECT analysis_id,result_json,input_sha256 FROM lot_analyses "
                "WHERE lot_id=? AND rule_version=? AND input_sha256=?",
                (lot_id, rule_version, fingerprint),
            ).fetchone()
            if existing:
                analysis_id = existing["analysis_id"]
                stored_result = json.loads(existing["result_json"])
                stored_sha = existing["input_sha256"]
                created = False
            else:
                cursor = self.db.execute(
                    "INSERT INTO lot_analyses(lot_id,rule_version,required_frequencies_json,response_threshold,"
                    "input_sha256,result_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (lot_id, rule_version, canonical_json(sorted(frequencies)), threshold, fingerprint,
                     canonical_json(result), actor_id, utcnow()),
                )
                analysis_id = cursor.lastrowid
                stored_result = result
                stored_sha = fingerprint
                created = True
                event(self.db, lot_id, "analysis.recorded", actor_id, {
                    "analysis_id": analysis_id, "rule_version": rule_version,
                    "input_sha256": fingerprint, "measurements_sha256": input_sha,
                    "passed": result.get("passed"), "rejected": result.get("rejected"),
                    "unknown": result.get("unknown"),
                    "units": [
                        {"unit_id": unit["unit_id"], "conclusion": unit["conclusion"]}
                        for unit in result.get("units", [])
                    ],
                })
        return {
            "analysis_id": analysis_id, "lot_id": lot_id, "rule_version": rule_version,
            "input_sha256": stored_sha, "measurements_sha256": input_sha,
            "reused": not created, "result": stored_result,
        }

    # ----- 新版器件级分析（device-yield/2） --------------------------------

    def analyze(
        self,
        token: str,
        lot_id: str,
        required_frequencies_hz: Sequence[float] = DEFAULT_REQUIRED_FREQUENCIES_HZ,
        response_threshold: float = DEFAULT_RESPONSE_THRESHOLD,
    ) -> dict:
        actor = self.auth.require(token, "analyze")
        lot = self._lot_row(lot_id)
        unit_ids, input_rows = self._analysis_input(lot_id)
        self._require_complete_ledger(lot, unit_ids)
        records = {unit_id: [] for unit_id in unit_ids}
        for row in input_rows:
            records[row["unit_id"]].append(MeasurementRecord(
                measurement_id=row["measurement_id"], unit_id=row["unit_id"],
                signal_frequency_hz=row["signal_frequency_hz"], response=row["response"], noise=row["noise"],
                instrument=row["instrument"], sequence_no=row["sequence_no"], measured_at=row["measured_at"],
                revoked=bool(row["revoked"]),
            ))
        qualification = qualify_lot(
            unit_ids, records,
            required_frequencies_hz=required_frequencies_hz, response_threshold=response_threshold,
        )
        result = qualification.as_dict()
        return self._store_analysis(
            lot_id, RULE_VERSION, required_frequencies_hz, response_threshold, input_rows, result, actor.user_id
        )

    # ----- 旧版测量点计数分析（point-count-yield/1，仅留档对照） -----------

    def analyze_legacy(self, token: str, lot_id: str) -> dict:
        """按历史算法复算并留档：把每个达标测量点计为一颗合格器件。

        ``analytics`` 模块保持原样不改动；当测量点数超出器件数量时旧算法
        自身会以计数矛盾拒绝，这里转译为业务错误，便于报告对照而不崩溃。
        """

        actor = self.auth.require(token, "analyze")
        lot = self._lot_row(lot_id)
        unit_ids, input_rows = self._analysis_input(lot_id)
        self._require_complete_ledger(lot, unit_ids)
        active = [row for row in input_rows if not row["revoked"]]
        if len(active) < 3:
            raise InvalidLotState("旧版算法至少需要三个未撤销测量点")
        legacy_version = "point-count-yield/1"
        summary = analytics.summarize_signal_profile(
            [row["signal_frequency_hz"] for row in active], [row["response"] for row in active]
        ).__dict__
        try:
            rates = analytics.yield_rate(
                lot["unit_count"], sum(1 for row in active if row["response"] >= 0.8), 0
            )
        except ValueError as exc:
            raise InvalidLotState(f"旧版算法计数矛盾（测量点被误当器件）: {exc}") from exc
        ci = analytics.confidence_interval([row["response"] for row in active])
        result = {
            "rule_version": legacy_version,
            "signal_profile": summary,
            "yield": rates,
            "response_ci": ci,
            "note": "历史算法：按达标测量点计数，不区分器件身份；仅用于旧报告对照",
        }
        return self._store_analysis(lot_id, legacy_version, (), 0.8, input_rows, result, actor.user_id)

    # ----- 分析版本查询 ---------------------------------------------------

    def list_analyses(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "read")
        self._lot_row(lot_id)
        rows = self.db.execute(
            "SELECT a.analysis_id,a.rule_version,a.input_sha256,a.created_by,a.created_at,"
            "d.decision,d.reason AS decision_reason FROM lot_analyses a "
            "LEFT JOIN lot_decisions d ON d.analysis_id=a.analysis_id "
            "WHERE a.lot_id=? ORDER BY a.analysis_id",
            (lot_id,),
        ).fetchall()
        return {"lot_id": lot_id, "analyses": [dict(row) for row in rows]}

    def get_analysis(self, token: str, analysis_id: int) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM lot_analyses WHERE analysis_id=?", (analysis_id,)).fetchone()
        if not row:
            raise NotFound(f"分析版本不存在: {analysis_id}")
        decision = self.db.execute(
            "SELECT decision,reason,reviewer,created_at FROM lot_decisions WHERE analysis_id=?",
            (analysis_id,),
        ).fetchone()
        return {
            "analysis_id": row["analysis_id"], "lot_id": row["lot_id"],
            "rule_version": row["rule_version"], "input_sha256": row["input_sha256"],
            "required_frequencies_hz": json.loads(row["required_frequencies_json"]),
            "response_threshold": row["response_threshold"],
            "created_by": row["created_by"], "created_at": row["created_at"],
            "result": json.loads(row["result_json"]),
            "decision": None if decision is None else dict(decision),
        }

    # ----- 质量放行决定（只追加，不改分析） -------------------------------

    def approve(self, token: str, lot_id: str, analysis_id: int, decision: str, reason: str) -> dict:
        actor = self.auth.require(token, "approve")
        if decision not in {"release", "hold", "reject"} or not reason.strip():
            raise InvalidSample("决定必须为 release/hold/reject 且填写原因")
        row = self.db.execute(
            "SELECT analysis_id,rule_version FROM lot_analyses WHERE analysis_id=? AND lot_id=?",
            (analysis_id, lot_id),
        ).fetchone()
        if not row:
            raise NotFound(f"该批次下分析版本不存在: {analysis_id}")
        rule_version = row["rule_version"]
        now = utcnow()
        try:
            with transaction(self.db):
                cursor = self.db.execute(
                    "INSERT INTO lot_decisions(lot_id,analysis_id,decision,reason,reviewer,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (lot_id, analysis_id, decision, reason, actor.user_id, now),
                )
                status = {"release": "released", "hold": "hold", "reject": "rejected"}[decision]
                self.db.execute("UPDATE chip_lots SET status=?,updated_at=? WHERE lot_id=?", (status, now, lot_id))
                event(self.db, lot_id, "decision.recorded", actor.user_id, {
                    "decision_id": cursor.lastrowid, "analysis_id": analysis_id,
                    "rule_version": rule_version,
                    "decision": decision, "reason": reason,
                })
        except Exception as exc:
            raise Conflict("该分析版本已经有放行决定；请基于新分析版本发起决定") from exc
        return self.get_lot(token, lot_id)

    def audit(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        self._lot_row(lot_id)
        return [
            dict(r) | {"payload": json.loads(r["payload"])}
            for r in self.db.execute(
                "SELECT event_id,lot_id,event_type,actor,payload,created_at FROM lot_events "
                "WHERE lot_id=? ORDER BY event_id", (lot_id,)
            ).fetchall()
        ]
