"""审计隔离与冻结记录修复的回归测试。

背景：递归比较对缓存曾跨审计共享，缓存键只含类型路径——同名、同递归拓扑
但定义不同的两份契约会互相污染裁决：
- 先冻结兼容契约后，第二份同名不兼容契约（接收端在 Data 序号处要求文本）
  被误判为兼容且没有首个违约；
- 反向顺序下，同名兼容契约又会被此前的拒绝结论误拒。

修复后：每次裁决使用全新缓存，审计彼此隔离；已被误判冻结的历史记录在按原
审计标识重开或以完全相同契约重传时，依据冻结的原始契约修复结论，保留审计
标识、契约指纹与冻结时间；不同契约重传仍拒绝且不改写记录。
"""
from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path
from urllib.error import HTTPError

import pytest

from app.api import build_server
from app.models import AuditConclusion, ContractConflictError
from app.parser import parse_payload
from app.storage import AuditStore, canonical_fingerprint

INT = {"kind": "int"}
TEXT = {"kind": "text"}

MISMATCH_PATH = "Cmd.payload[Data].seq"
MISMATCH_CODE = "primitive-mismatch"


def payload_contract(audit_id: str, receiver_seq=TEXT) -> dict:
    """根类型含必需载荷引用；载荷为带 Data 标签的递归类型。

    Data 含序号（发送端始终为整数）和可选的自引用后继；receiver_seq 控制
    接收端在序号处要求的类型：int 兼容，text 不兼容。
    """

    def decls(seq_type):
        return [
            {"name": "Cmd", "type": {"kind": "record", "fields": [
                {"name": "payload",
                 "type": {"kind": "ref", "name": "Payload"},
                 "required": True}]}},
            {"name": "Payload", "type": {"kind": "variant", "tags": [
                {"label": "Data", "type": {"kind": "record", "fields": [
                    {"name": "seq", "type": seq_type, "required": True},
                    {"name": "next",
                     "type": {"kind": "ref", "name": "Payload"},
                     "required": False}]}}]}},
        ]

    return {
        "audit_id": audit_id,
        "root_name": "Cmd",
        "sender_types": decls(INT),
        "receiver_types": decls(receiver_seq),
    }


@pytest.fixture()
def store(tmp_path):
    return AuditStore(tmp_path / "data")


def assert_bad_rejection(conclusion):
    assert conclusion.compatible is False
    assert conclusion.mismatch is not None
    assert conclusion.mismatch.code == MISMATCH_CODE
    assert conclusion.mismatch.path == MISMATCH_PATH


# ---------- 审计隔离 ----------

def test_same_name_contracts_are_isolated(store):
    ok, _ = store.submit(parse_payload(payload_contract("AUDIT-OK", INT)))
    assert ok.compatible is True

    # 全新审计标识、同名同拓扑但接收端要求文本序号：必须稳定拒绝。
    bad, _ = store.submit(parse_payload(payload_contract("AUDIT-BAD", TEXT)))
    assert_bad_rejection(bad)

    # 第一份兼容审计保持原结论不变。
    assert store.get("AUDIT-OK").compatible is True
    # 拒绝结论按标识重开稳定可重放。
    assert_bad_rejection(store.get("AUDIT-BAD"))


def test_reverse_order_no_false_rejection(store):
    bad, _ = store.submit(parse_payload(payload_contract("AUDIT-REV-BAD", TEXT)))
    assert_bad_rejection(bad)

    # 先提交不兼容契约，再提交同名且兼容的独立审计：不得误拒。
    ok, _ = store.submit(parse_payload(payload_contract("AUDIT-REV-OK", INT)))
    assert ok.compatible is True
    assert store.get("AUDIT-REV-OK").compatible is True


def test_rejection_stable_across_resubmit_and_reopen(store):
    body = payload_contract("AUDIT-STABLE", TEXT)
    c1, created1 = store.submit(parse_payload(body))
    assert created1 is True
    assert_bad_rejection(c1)

    # 相同契约重传：读取原冻结结论，冻结时间不变。
    c2, created2 = store.submit(parse_payload(body))
    assert created2 is False
    assert_bad_rejection(c2)
    assert c2.frozen_at == c1.frozen_at
    assert c2.contract_fingerprint == c1.contract_fingerprint

    # 按标识重开：同一拒绝结论。
    reopened = store.get("AUDIT-STABLE")
    assert_bad_rejection(reopened)
    assert reopened.frozen_at == c1.frozen_at


def test_conflicting_contract_rejected_without_rewrite(store):
    store.submit(parse_payload(payload_contract("AUDIT-CONFLICT", TEXT)))
    before = (store.dir / "AUDIT-CONFLICT.json").read_text(encoding="utf-8")

    # 同一标识改变契约（接收端序号改回整数）：拒绝且不改写记录。
    with pytest.raises(ContractConflictError):
        store.submit(parse_payload(payload_contract("AUDIT-CONFLICT", INT)))
    after = (store.dir / "AUDIT-CONFLICT.json").read_text(encoding="utf-8")
    assert after == before
    assert_bad_rejection(store.get("AUDIT-CONFLICT"))


# ---------- 历史污染记录的冻结修复 ----------

CONTAMINATED_FROZEN_AT = "2026-01-01T00:00:00Z"


def freeze_contaminated(directory: Path, body: dict) -> tuple[Payload, str]:
    """按旧缺陷服务的行为冻结一条被污染的记录：契约实际不兼容，
    但结论被误判为兼容且无首个违约。返回 (载荷, 原始记录文本)。"""
    payload = parse_payload(body)
    fingerprint = canonical_fingerprint(payload)
    record = {
        "contract_fingerprint": fingerprint,
        "contract": payload.fingerprint_dict(),
        "conclusion": AuditConclusion(
            audit_id=payload.audit_id,
            compatible=True,  # 污染缓存导致的误判
            root=payload.root_name or "",
            mismatch=None,
            recycled=[],
            contract_fingerprint=fingerprint,
            frozen_at=CONTAMINATED_FROZEN_AT,
        ).to_json(),
    }
    path = Path(directory) / f"{payload.audit_id}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return payload, path.read_text(encoding="utf-8")


def test_contaminated_record_repaired_on_reopen(tmp_path):
    directory = tmp_path / "data"
    directory.mkdir()
    body = payload_contract("AUDIT-LEGACY", TEXT)
    payload, _ = freeze_contaminated(directory, body)

    store = AuditStore(directory)
    repaired = store.get("AUDIT-LEGACY")

    # 依据冻结的原始契约恢复正确结论。
    assert_bad_rejection(repaired)
    # 审计标识、契约指纹、冻结时间保留。
    assert repaired.audit_id == "AUDIT-LEGACY"
    assert repaired.contract_fingerprint == canonical_fingerprint(payload)
    assert repaired.frozen_at == CONTAMINATED_FROZEN_AT

    # 修复已持久化：服务重启（全新存储实例）后读取仍是正确结论。
    again = AuditStore(directory).get("AUDIT-LEGACY")
    assert_bad_rejection(again)
    assert again.frozen_at == CONTAMINATED_FROZEN_AT


def test_contaminated_record_repaired_on_same_contract_resubmit(tmp_path):
    directory = tmp_path / "data"
    directory.mkdir()
    body = payload_contract("AUDIT-LEGACY-RESUB", TEXT)
    payload, _ = freeze_contaminated(directory, body)

    store = AuditStore(directory)
    repaired, created = store.submit(parse_payload(body))

    assert created is False  # 相同契约重传，不是新建
    assert_bad_rejection(repaired)
    assert repaired.contract_fingerprint == canonical_fingerprint(payload)
    assert repaired.frozen_at == CONTAMINATED_FROZEN_AT


def test_conflicting_resubmit_never_rewrites_contaminated_record(tmp_path):
    directory = tmp_path / "data"
    directory.mkdir()
    body = payload_contract("AUDIT-LEGACY-409", TEXT)
    _, original_text = freeze_contaminated(directory, body)

    store = AuditStore(directory)
    # 不同契约重传：拒绝，且不得改写（包括不得触发修复改写）记录。
    with pytest.raises(ContractConflictError):
        store.submit(parse_payload(payload_contract("AUDIT-LEGACY-409", INT)))
    path = directory / "AUDIT-LEGACY-409.json"
    assert path.read_text(encoding="utf-8") == original_text


def test_unaffected_frozen_record_not_rewritten(tmp_path):
    # 未受污染影响的记录：重开不触发任何改写，字节级不变。
    directory = tmp_path / "data"
    store = AuditStore(directory)
    store.submit(parse_payload(payload_contract("AUDIT-PRISTINE", INT)))
    path = directory / "AUDIT-PRISTINE.json"
    before = path.read_text(encoding="utf-8")

    assert store.get("AUDIT-PRISTINE").compatible is True
    assert path.read_text(encoding="utf-8") == before


# ---------- HTTP 端到端（真实审计 API） ----------

@pytest.fixture()
def server(tmp_path):
    store = AuditStore(tmp_path / "data")
    srv = build_server("127.0.0.1", 0, store)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    host, port = srv.server_address
    yield f"http://{host}:{port}"
    srv.shutdown()
    srv.server_close()


def _request(url, method="GET", body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_api_same_name_audits_are_isolated(server):
    status, data = _request(
        f"{server}/api/audits", "POST", payload_contract("API-OK", INT))
    assert status == 201 and data["conclusion"]["compatible"] is True

    status, data = _request(
        f"{server}/api/audits", "POST", payload_contract("API-BAD", TEXT))
    assert status == 201
    conclusion = data["conclusion"]
    assert conclusion["compatible"] is False
    assert conclusion["mismatch"]["code"] == MISMATCH_CODE
    assert conclusion["mismatch"]["path"] == MISMATCH_PATH
    frozen_at = conclusion["frozen_at"]

    # 相同契约重传：幂等读取同一拒绝结论。
    status, data = _request(
        f"{server}/api/audits", "POST", payload_contract("API-BAD", TEXT))
    assert status == 200 and data["resubmitted_same_contract"] is True
    assert data["conclusion"]["compatible"] is False
    assert data["conclusion"]["frozen_at"] == frozen_at

    # 按标识重开：拒绝结论从冻结记录中稳定返回。
    status, data = _request(f"{server}/api/audits/API-BAD")
    assert status == 200
    assert data["conclusion"]["compatible"] is False
    assert data["conclusion"]["mismatch"]["path"] == MISMATCH_PATH
    assert data["conclusion"]["frozen_at"] == frozen_at

    # 第一份兼容审计结论不变。
    status, data = _request(f"{server}/api/audits/API-OK")
    assert status == 200 and data["conclusion"]["compatible"] is True


def test_api_reopens_contaminated_record_with_repaired_conclusion(tmp_path):
    # 旧缺陷服务冻结的污染记录：契约实际不兼容，结论却被误判为兼容。
    directory = tmp_path / "data"
    directory.mkdir()
    body = payload_contract("API-LEGACY", TEXT)
    payload, _ = freeze_contaminated(directory, body)

    srv = build_server("127.0.0.1", 0, AuditStore(directory))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    host, port = srv.server_address
    base = f"http://{host}:{port}"
    try:
        # 按原审计标识重开：依据冻结的原始契约恢复正确结论。
        status, data = _request(f"{base}/api/audits/API-LEGACY")
        assert status == 200
        conclusion = data["conclusion"]
        assert conclusion["compatible"] is False
        assert conclusion["mismatch"]["code"] == MISMATCH_CODE
        assert conclusion["mismatch"]["path"] == MISMATCH_PATH
        # 审计标识、契约指纹、冻结时间保留。
        assert conclusion["audit_id"] == "API-LEGACY"
        assert conclusion["contract_fingerprint"] == canonical_fingerprint(payload)
        assert conclusion["frozen_at"] == CONTAMINATED_FROZEN_AT

        # 完全相同契约重传：返回修复后的结论，冻结时间不变。
        status, data = _request(f"{base}/api/audits", "POST", body)
        assert status == 200 and data["resubmitted_same_contract"] is True
        assert data["conclusion"]["compatible"] is False
        assert data["conclusion"]["frozen_at"] == CONTAMINATED_FROZEN_AT

        # 不同契约重传：409，拒绝改写。
        status, data = _request(
            f"{base}/api/audits", "POST", payload_contract("API-LEGACY", INT))
        assert status == 409 and data["error"] == "contract-conflict"
    finally:
        srv.shutdown()
        srv.server_close()

    # 修复已持久化：服务重启后读取仍是正确结论。
    srv = build_server("127.0.0.1", 0, AuditStore(directory))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    host, port = srv.server_address
    try:
        status, data = _request(f"http://{host}:{port}/api/audits/API-LEGACY")
        assert status == 200
        assert data["conclusion"]["compatible"] is False
        assert data["conclusion"]["frozen_at"] == CONTAMINATED_FROZEN_AT
    finally:
        srv.shutdown()
        srv.server_close()
