"""审计结论冻结存储。

规则：
- 相同 audit_id + 完全相同契约（规范化 JSON 的 SHA-256）：返回原冻结结论，
  不重新计算、不改变 frozen_at；
- 相同 audit_id 但契约指纹变化：拒绝（409），绝不改写原结论；
- 结论持久化为 JSON 文件，容器重启后仍可重开。

裁决隔离与冻结修复：
- 每次兼容性裁决都使用全新的递归比较对缓存，一份审计的裁决绝不影响另一份
  稳定审计标识下的结论；
- 在裁决隔离修复之前被污染缓存误判而冻结的历史记录，按原审计标识重开或以
  完全相同契约重传时，依据其冻结的原始契约重新裁决并修复结论，同时保留
  审计标识、契约指纹与冻结时间；不同契约重传仍拒绝且不改写记录。
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
from .parser import parse_payload
from .subtype import check_compatibility


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

    def _path(self, audit_id: str) -> Path:
        # audit_id 已由 parser 限定为 [A-Za-z0-9_-]，无路径穿越风险。
        return self.dir / f"{audit_id}.json"

    def get(self, audit_id: str) -> AuditConclusion:
        with self._lock:
            record = self._read_raw(audit_id)
            if record is None:
                raise AuditNotFoundError(audit_id)
            record = self._repair_if_needed(audit_id, record)
            return _conclusion_from_json(record["conclusion"])

    def _read_raw(self, audit_id: str) -> dict | None:
        path = self._path(audit_id)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def _write_raw(self, audit_id: str, record: dict) -> None:
        tmp = self._path(audit_id).with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path(audit_id))

    def _repair_if_needed(self, audit_id: str, record: dict) -> dict:
        """依据冻结的原始契约重新裁决，修复被污染缓存误判的历史结论。

        只更新结论本身：审计标识、契约指纹、冻结时间与冻结契约保持原样。
        冻结契约无法回放时维持原记录，读取不中断。
        """
        contract = record.get("contract")
        stored = record.get("conclusion")
        if not contract or not stored:
            return record
        try:
            payload = parse_payload({
                "audit_id": contract.get("audit_id"),
                "root_name": contract.get("root_name"),
                "sender_types": contract.get("sender"),
                "receiver_types": contract.get("receiver"),
            })
            ok, mismatch, recycled = check_compatibility(
                payload.sender_types,
                payload.receiver_types,
                payload.root_name or "",
            )
        except Exception:
            return record
        repaired = AuditConclusion(
            audit_id=stored["audit_id"],
            compatible=ok,
            root=stored["root"],
            mismatch=mismatch,
            recycled=recycled,
            contract_fingerprint=stored["contract_fingerprint"],
            frozen_at=stored["frozen_at"],
        ).to_json()
        if repaired == stored:
            return record
        record = {**record, "conclusion": repaired}
        self._write_raw(audit_id, record)
        return record

    def submit(self, payload: Payload) -> tuple[AuditConclusion, bool]:
        """提交（或幂等重传）。返回 (结论, 是否本次新建)。"""
        fingerprint = canonical_fingerprint(payload)
        with self._lock:
            existing = self._read_raw(payload.audit_id)
            if existing is not None:
                if existing["contract_fingerprint"] != fingerprint:
                    raise ContractConflictError(payload.audit_id)
                # 相同契约重传：读取原冻结结论；历史误判按冻结契约修复，
                # 冻结时间不变。
                existing = self._repair_if_needed(payload.audit_id, existing)
                return _conclusion_from_json(existing["conclusion"]), False

            ok, mismatch, recycled = check_compatibility(
                payload.sender_types,
                payload.receiver_types,
                payload.root_name or "",
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
            self._write_raw(payload.audit_id, record)
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
