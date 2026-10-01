"""国产电子部件批次、器件身份、测量序列与分析版本的 SQLite 结构。

建模上明确区分三个层次，修正“把测量点当器件”的根因：

* ``chip_lots``：批次，登记真实器件数量 ``unit_count`` 与冻结规则版本；
* ``chip_devices``：器件身份（device_id），一颗器件一行；
* ``measurements``：测量序列，每次测量一行，带序号、仪器和撤销状态。

分析结果（``analyses``）一旦写入即不可变，重跑只产生新版本或命中同一
内容摘要；历史报告保留原算法版本与输入摘要，永不被静默改写。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS chip_lots(
 lot_id TEXT PRIMARY KEY, product TEXT NOT NULL, process_rev TEXT NOT NULL,
 unit_count INTEGER NOT NULL, rule_version TEXT NOT NULL,
 required_frequencies_hz TEXT NOT NULL,
 status TEXT NOT NULL, owner TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chip_devices(
 lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 device_id TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(lot_id,device_id));
CREATE TABLE IF NOT EXISTS measurements(
 measurement_id TEXT PRIMARY KEY,
 lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 device_id TEXT NOT NULL,
 sequence_no INTEGER NOT NULL,
 signal_frequency_hz REAL NOT NULL, response REAL NOT NULL, noise REAL NOT NULL,
 instrument TEXT NOT NULL, operator TEXT NOT NULL, measured_at TEXT NOT NULL,
 retracted INTEGER NOT NULL DEFAULT 0, retraction_reason TEXT,
 UNIQUE(lot_id,device_id,sequence_no),
 UNIQUE(lot_id,measurement_id),
 FOREIGN KEY(lot_id,device_id) REFERENCES chip_devices(lot_id,device_id));
CREATE TABLE IF NOT EXISTS lot_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT NOT NULL,
 event_type TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals(
 lot_id TEXT NOT NULL, reviewer TEXT NOT NULL, decision TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(lot_id,reviewer));
CREATE TABLE IF NOT EXISTS analyses(
 analysis_id INTEGER PRIMARY KEY AUTOINCREMENT,
 lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 algorithm_version TEXT NOT NULL,
 rule_version TEXT NOT NULL,
 input_sha256 TEXT NOT NULL,
 unit_count INTEGER NOT NULL,
 passed INTEGER NOT NULL, rejected INTEGER NOT NULL, unknown INTEGER NOT NULL,
 result_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(lot_id,input_sha256,algorithm_version));
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str = ":memory:", check_same_thread: bool = True) -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=check_same_thread)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    _migrate_legacy(db)
    db.commit()
    return db


def _migrate_legacy(db: sqlite3.Connection) -> None:
    """把升级前的库补齐新列；旧批次标记为历史规则，绝不重算其旧决定。"""

    lot_columns = {row[1] for row in db.execute("PRAGMA table_info(chip_lots)").fetchall()}
    if "rule_version" not in lot_columns:
        db.execute("ALTER TABLE chip_lots ADD COLUMN rule_version TEXT")
        # 升级前建立的批次沿用历史算法口径；新批次默认使用冻结新规则。
        db.execute("UPDATE chip_lots SET rule_version='point-count/1' WHERE rule_version IS NULL")
    if "required_frequencies_hz" not in lot_columns:
        db.execute(
            "ALTER TABLE chip_lots ADD COLUMN required_frequencies_hz TEXT NOT NULL DEFAULT '[]'"
        )
        db.execute(
            "UPDATE chip_lots SET required_frequencies_hz=? WHERE required_frequencies_hz='[]'",
            (json.dumps([450.0, 520.0, 650.0]),),
        )
    measurement_columns = {row[1] for row in db.execute("PRAGMA table_info(measurements)").fetchall()}
    if "device_id" not in measurement_columns:
        # 旧测量无器件身份，挂到保留的“未知器件”占位身份上，仅用于历史留痕。
        db.execute("ALTER TABLE measurements ADD COLUMN device_id TEXT NOT NULL DEFAULT '__legacy_unknown__'")
        db.execute("ALTER TABLE measurements ADD COLUMN sequence_no INTEGER NOT NULL DEFAULT 1")
        db.execute("ALTER TABLE measurements ADD COLUMN retracted INTEGER NOT NULL DEFAULT 0")
        db.execute("ALTER TABLE measurements ADD COLUMN retraction_reason TEXT")


@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def event(db: sqlite3.Connection, lot_id: str, event_type: str, actor: str, payload: dict) -> None:
    db.execute(
        "INSERT INTO lot_events(lot_id,event_type,actor,payload,created_at) VALUES(?,?,?,?,?)",
        (lot_id, event_type, actor, json.dumps(payload, sort_keys=True, ensure_ascii=False), utcnow()),
    )
