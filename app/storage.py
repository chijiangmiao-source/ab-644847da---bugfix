"""审计结论冻结存储。

规则：
- 相同 audit_id + 完全相同契约（规范化 JSON 的 SHA-256）：返回原冻结结论，
  不重新计算、不改变 frozen_at；
- 相同 audit_id 但契约指纹变化：拒绝（409），绝不改写原结论；
- 结论持久化为 JSON 文件，容器重启后仍可重开。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    AuditConclusion,
    AuditNotFoundError,
    ContractConflictError,
    Payload,
    RecyclePoint,
    Mismatch,
)
from .subtype import ComparisonCache, check_compatibility


def canonical_fingerprint(payload: Payload) -> str:
    """契约的稳定指纹：紧凑、排序键、ensure_ascii=False 不影响字节稳定性。"""
    blob = json.dumps(
        payload.fingerprint_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AuditStore:
    def __init__(self, directory: str | os.PathLike[str]):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._comparison_cache: ComparisonCache = {}

    def _path(self, audit_id: str) -> Path:
        # audit_id 已由 parser 限定为 [A-Za-z0-9_-]，无路径穿越风险。
        return self.dir / f"{audit_id}.json"

    def get(self, audit_id: str) -> AuditConclusion:
        record = self._read_raw(audit_id)
        if record is None:
            raise AuditNotFoundError(audit_id)
        return _conclusion_from_json(record["conclusion"])

    def _read_raw(self, audit_id: str) -> dict | None:
        path = self._path(audit_id)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def submit(self, payload: Payload) -> tuple[AuditConclusion, bool]:
        """提交（或幂等重传）。返回 (结论, 是否本次新建)。"""
        fingerprint = canonical_fingerprint(payload)
        with self._lock:
            existing = self._read_raw(payload.audit_id)
            if existing is not None:
                if existing["contract_fingerprint"] != fingerprint:
                    raise ContractConflictError(payload.audit_id)
                # 相同契约重传：读取原冻结结论，绝不改写。
                return _conclusion_from_json(existing["conclusion"]), False

            ok, mismatch, recycled = check_compatibility(
                payload.sender_types,
                payload.receiver_types,
                payload.root_name or "",
                self._comparison_cache,
            )
            conclusion = AuditConclusion(
                audit_id=payload.audit_id,
                compatible=ok,
                root=payload.root_name or "",
                mismatch=mismatch,
                recycled=recycled,
                contract_fingerprint=fingerprint,
                frozen_at=_utc_now(),
            )
            record = {
                "contract_fingerprint": fingerprint,
                # 冻结原始契约，便于重开页面时回放与审计。
                "contract": payload.fingerprint_dict(),
                "conclusion": conclusion.to_json(),
            }
            tmp = self._path(payload.audit_id).with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(record, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path(payload.audit_id))
            return conclusion, True


def _conclusion_from_json(data: dict) -> AuditConclusion:
    mm = data.get("mismatch")
    mismatch = None
    if mm:
        mismatch = Mismatch(
            path=mm["path"],
            code=mm["code"],
            message=mm["message"],
            detail=mm.get("detail", {}),
        )
    recycled = [
        RecyclePoint(
            path=r["path"], pair=r["pair"], first_seen_at=r["first_seen_at"]
        )
        for r in data.get("recycled", [])
    ]
    return AuditConclusion(
        audit_id=data["audit_id"],
        compatible=data["compatible"],
        root=data["root"],
        mismatch=mismatch,
        recycled=recycled,
        contract_fingerprint=data["contract_fingerprint"],
        frozen_at=data["frozen_at"],
    )
