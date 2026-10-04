#!/usr/bin/env python3
"""预置旧版本（跨审计缓存污染期）冻结的错误结论记录。

在 Compose 验收中、真实 API 流量开始前对 audit-data 卷运行一次，
制造“修复部署前已经受此问题影响而冻结”的记录：

- LEGACY-SEED-BAD：契约本身不兼容（接收端在 Data.seq 要求 text，
  发送端产生 int），却被旧版本错误冻结为 compatible=true；
- LEGACY-SEED-GOOD：契约本身兼容，却被旧版本反向污染冻结为
  compatible=false（携带旧的 primitive-mismatch 违约）。

记录格式与旧版本逐字一致（结论不含 conclusion_version），
audit_id / 契约指纹 / 冻结时间固定，便于验收核对修复后这些字段保留。
幂等：目标记录已存在时跳过，绝不覆盖已修复的记录。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.models import AuditConclusion, Mismatch  # noqa: E402
from app.parser import parse_payload  # noqa: E402
from app.storage import canonical_fingerprint  # noqa: E402

INT = {"kind": "int"}
TEXT = {"kind": "text"}
DATA_DIR = Path(os.environ.get("AUDIT_DATA_DIR", "/data"))

LEGACY_BAD = "LEGACY-SEED-BAD"
LEGACY_GOOD = "LEGACY-SEED-GOOD"
LEGACY_BAD_FROZEN_AT = "2026-01-01T00:00:00Z"
LEGACY_GOOD_FROZEN_AT = "2026-02-02T00:00:00Z"


def rec(*fields):
    return {"kind": "record", "fields": list(fields)}


def field(name, t, required=True):
    return {"name": name, "type": t, "required": required}


def variant(*tags):
    return {"kind": "variant", "tags": list(tags)}


def tag(label, t):
    return {"label": label, "type": t}


def ref(name):
    return {"kind": "ref", "name": name}


def payload_contract(audit_id, seq_type):
    sender = [
        {"name": "Cmd", "type": rec(field("payload", ref("Payload")))},
        {"name": "Payload", "type": variant(tag("Data", ref("Data")))},
        {"name": "Data", "type": rec(
            field("seq", INT), field("next", ref("Data"), required=False))},
    ]
    receiver = [
        {"name": "Cmd", "type": rec(field("payload", ref("Payload")))},
        {"name": "Payload", "type": variant(tag("Data", ref("Data")))},
        {"name": "Data", "type": rec(
            field("seq", seq_type), field("next", ref("Data"), required=False))},
    ]
    return {
        "audit_id": audit_id,
        "root_name": "Cmd",
        "sender_types": sender,
        "receiver_types": receiver,
    }


def _legacy_record(body, conclusion: AuditConclusion) -> dict:
    payload = parse_payload(body)
    fingerprint = canonical_fingerprint(payload)
    # 旧版本落盘形态：结论直接是 AuditConclusion.to_json()（无版本标记）。
    conclusion_json = conclusion.to_json()
    conclusion_json["contract_fingerprint"] = fingerprint
    return {
        "contract_fingerprint": fingerprint,
        "contract": payload.fingerprint_dict(),
        "conclusion": conclusion_json,
    }


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    bad_conclusion = AuditConclusion(
        audit_id=LEGACY_BAD,
        compatible=True,  # 旧版本误判：实际不兼容
        root="Cmd",
        mismatch=None,
        recycled=[],
        contract_fingerprint="",
        frozen_at=LEGACY_BAD_FROZEN_AT,
    )
    good_conclusion = AuditConclusion(
        audit_id=LEGACY_GOOD,
        compatible=False,  # 旧版本反向误拒：实际兼容
        root="Cmd",
        mismatch=Mismatch(
            path="Cmd.payload[Data].seq",
            code="primitive-mismatch",
            message="类型不兼容：发送端 int，接收端 text（旧版本污染结论）",
            detail={"sender_kind": "int", "receiver_kind": "text"},
        ),
        recycled=[],
        contract_fingerprint="",
        frozen_at=LEGACY_GOOD_FROZEN_AT,
    )

    records = [
        (LEGACY_BAD, payload_contract(LEGACY_BAD, TEXT), bad_conclusion),
        (LEGACY_GOOD, payload_contract(LEGACY_GOOD, INT), good_conclusion),
    ]
    for audit_id, body, conclusion in records:
        target = DATA_DIR / f"{audit_id}.json"
        if target.exists():
            print(f"seed: {audit_id} 已存在，跳过（不覆盖）")
            continue
        record = _legacy_record(body, conclusion)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)
        print(f"seed: 已写入旧版本冻结记录 {audit_id} -> {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
