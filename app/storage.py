"""审计结论冻结存储。

规则：
- 相同 audit_id + 完全相同契约（规范化 JSON 的 SHA-256）：返回原冻结结论，
  不重新计算、不改变 frozen_at；
- 相同 audit_id 但契约指纹变化：拒绝（409），绝不改写原结论；
- 结论持久化为 JSON 文件，容器重启后仍可重开。

历史修复（递归裁决按审计隔离）：
旧版本曾让协归“已定论比较对”缓存在同一进程的所有审计间共享，而比较对键
只含类型路径（如 Data <= Data），不含审计标识。这会让一份审计的递归裁决
污染另一份同名拓扑的独立审计（兼容误判 / 反向误拒），错误结论随即被冻结。
新记录写入 conclusion_version 标记；读取旧记录时，以其**冻结的原始契约**
重新裁决，并用原 audit_id / 指纹 / frozen_at 回写修正（同时补版本号）。
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
    ValidationError,
)
from .parser import parse_payload
from .subtype import check_compatibility

# 当前结论版本号：结论结构或裁决语义修复后递增，用于识别需按冻结契约修正的旧记录。
CONCLUSION_VERSION = 2


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


def _conclusion_to_json(conclusion: AuditConclusion) -> dict:
    data = conclusion.to_json()
    data["conclusion_version"] = CONCLUSION_VERSION
    return data


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


def _derive_conclusion(
    payload: Payload, fingerprint: str, frozen_at: str
) -> AuditConclusion:
    """按给定契约裁决生成结论；指纹与冻结时间沿用冻结记录的值。"""
    ok, mismatch, recycled = check_compatibility(
        payload.sender_types,
        payload.receiver_types,
        payload.root_name or "",
    )
    return AuditConclusion(
        audit_id=payload.audit_id,
        compatible=ok,
        root=payload.root_name or "",
        mismatch=mismatch,
        recycled=recycled,
        contract_fingerprint=fingerprint,
        frozen_at=frozen_at,
    )


class AuditStore:
    def __init__(self, directory: str | os.PathLike[str]):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, audit_id: str) -> Path:
        # audit_id 已由 parser 限定为 [A-Za-z0-9_-]，无路径穿越风险。
        return self.dir / f"{audit_id}.json"

    def _read_raw(self, audit_id: str) -> dict | None:
        path = self._path(audit_id)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def get(self, audit_id: str) -> AuditConclusion:
        with self._lock:
            record = self._read_raw(audit_id)
            if record is None:
                raise AuditNotFoundError(audit_id)
            conclusion = self._heal_if_stale(record)
            return conclusion

    def submit(self, payload: Payload) -> tuple[AuditConclusion, bool]:
        """提交（或幂等重传）。返回 (结论, 是否本次新建)。"""
        fingerprint = canonical_fingerprint(payload)
        with self._lock:
            existing = self._read_raw(payload.audit_id)
            if existing is not None:
                if existing["contract_fingerprint"] != fingerprint:
                    raise ContractConflictError(payload.audit_id)
                # 相同契约重传：以冻结契约回放结论（并顺带修复旧版本误判），
                # audit_id / 契约指纹 / frozen_at 保持原值不变。
                return self._heal_if_stale(existing), False

            # 每份审计独立裁决：协归缓存为本次调用私有，不与任何其他审计共享。
            conclusion = _derive_conclusion(payload, fingerprint, _utc_now())
            record = {
                "contract_fingerprint": fingerprint,
                # 冻结原始契约，便于重开页面时回放、审计与旧结论修复。
                "contract": payload.fingerprint_dict(),
                "conclusion": _conclusion_to_json(conclusion),
            }
            self._atomic_write(payload.audit_id, record)
            return conclusion, True

    # ---------- 旧版本冻结记录的按契约修复 ----------

    def _heal_if_stale(self, record: dict) -> AuditConclusion:
        """旧版本（跨审计缓存污染期）冻结的记录按其原始契约恢复正确结论。

        新记录（带当前 conclusion_version）直接返回，不重新计算；
        旧记录以冻结契约重裁，并保留 audit_id / 指纹 / frozen_at 原子回写，
        同时补上当前版本号，使后续读取不再重裁。
        """
        stored = _conclusion_from_json(record["conclusion"])
        if record["conclusion"].get("conclusion_version") == CONCLUSION_VERSION:
            return stored

        contract = record.get("contract")
        if contract is None:
            # 理论上不会发生：旧版本同样冻结契约；缺失时不臆造，原样返回。
            return stored

        # 冻结契约以 fingerprint_dict() 形态保存（sender/receiver 键），
        # 还原为提交体形态后再解析重裁。记录损坏时不臆造结论，原样返回。
        try:
            payload = parse_payload(
                {
                    "audit_id": contract["audit_id"],
                    "root_name": contract.get("root_name"),
                    "sender_types": contract["sender"],
                    "receiver_types": contract["receiver"],
                }
            )
        except (ValidationError, KeyError, TypeError):
            return stored
        # 以冻结契约重算；audit_id 取自契约，指纹与 frozen_at 逐字沿用。
        derived = _derive_conclusion(
            payload, record["contract_fingerprint"], stored.frozen_at
        )

        # 结论修复（或仅补当前版本号）后原子回写；下次读取即为新版本，不再重裁。
        healed_record = {
            "contract_fingerprint": record["contract_fingerprint"],
            "contract": contract,
            "conclusion": _conclusion_to_json(derived),
        }
        self._atomic_write(stored.audit_id, healed_record)
        return derived

    def _atomic_write(self, audit_id: str, record: dict) -> None:
        tmp = self._path(audit_id).with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path(audit_id))
