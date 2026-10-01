"""确定性 JSON 序列化与输入摘要，供分析留存与审计追溯使用。"""

from __future__ import annotations

import hashlib
import json
from typing import Iterable


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def content_digest(values: Iterable[object]) -> str:
    """按输入顺序对规范化 JSON 逐条计算 SHA-256。"""

    digest = hashlib.sha256()
    for value in values:
        digest.update(canonical_json(value).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()
