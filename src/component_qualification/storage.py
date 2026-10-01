"""国产电子部件批次、器件台账、测量记录与质量决定的 SQLite 结构。"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator


# 离线服务以单连接配合线程化 HTTP 服务器；进程内串行化写事务，
# 避免多个请求线程在同一连接上嵌套 BEGIN。
_WRITE_LOCK = threading.RLock()


SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS chip_lots(
 lot_id TEXT PRIMARY KEY, product TEXT NOT NULL, process_rev TEXT NOT NULL,
 unit_count INTEGER NOT NULL CHECK(unit_count > 0), status TEXT NOT NULL,
 owner TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS lot_units(
 lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 unit_id TEXT NOT NULL,
 registered_by TEXT NOT NULL, registered_at TEXT NOT NULL,
 PRIMARY KEY(lot_id, unit_id));

CREATE TABLE IF NOT EXISTS measurements(
 measurement_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 unit_id TEXT NOT NULL,
 signal_frequency_hz REAL NOT NULL, response REAL NOT NULL, noise REAL NOT NULL,
 instrument TEXT NOT NULL, sequence_no INTEGER NOT NULL CHECK(sequence_no > 0),
 operator TEXT NOT NULL, measured_at TEXT NOT NULL,
 revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)),
 revoked_at TEXT, revoked_by TEXT, revoke_reason TEXT,
 UNIQUE(lot_id, unit_id, signal_frequency_hz, sequence_no));

CREATE TABLE IF NOT EXISTS lot_analyses(
 analysis_id INTEGER PRIMARY KEY AUTOINCREMENT,
 lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 rule_version TEXT NOT NULL,
 required_frequencies_json TEXT NOT NULL, response_threshold REAL NOT NULL,
 input_sha256 TEXT NOT NULL,
 result_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(lot_id, rule_version, input_sha256));

CREATE TABLE IF NOT EXISTS lot_decisions(
 decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
 lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 analysis_id INTEGER NOT NULL REFERENCES lot_analyses(analysis_id),
 decision TEXT NOT NULL CHECK(decision IN ('release','hold','reject')),
 reason TEXT NOT NULL, reviewer TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(lot_id, analysis_id));

CREATE TABLE IF NOT EXISTS lot_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT NOT NULL,
 event_type TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str = ":memory:") -> sqlite3.Connection:
    # 离线 HTTP 服务使用线程化服务器；写事务均以 BEGIN IMMEDIATE 串行化，
    # 关闭同线程校验以允许请求工作线程共享连接。
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    db.commit()
    return db


@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    with _WRITE_LOCK:
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
        (lot_id, event_type, actor, json.dumps(payload, ensure_ascii=False, sort_keys=True), utcnow()),
    )
