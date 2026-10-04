"""冻结存储与 HTTP 审计接口测试。"""
from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path
from urllib.error import HTTPError

import pytest

from app.api import build_server
from app.models import ContractConflictError
from app.parser import parse_payload
from app.storage import AuditStore, canonical_fingerprint

INT = {"kind": "int"}
TEXT = {"kind": "text"}


def base_contract(sender_field=INT, audit_id="AUDIT-FREEZE-1"):
    decl = [{
        "name": "R",
        "type": {"kind": "record", "fields": [
            {"name": "x", "type": sender_field, "required": True}
        ]},
    }]
    return {
        "audit_id": audit_id,
        "sender_types": decl,
        "receiver_types": [
            {"name": "R", "type": {"kind": "record", "fields": [
                {"name": "x", "type": INT, "required": True}
            ]}}
        ],
    }


@pytest.fixture()
def store(tmp_path):
    return AuditStore(tmp_path / "data")


def test_same_contract_resubmit_returns_frozen_conclusion(store):
    payload = parse_payload(base_contract())
    c1, created1 = store.submit(payload)
    assert created1 is True
    assert c1.compatible

    c2, created2 = store.submit(parse_payload(base_contract()))
    assert created2 is False
    assert c2.frozen_at == c1.frozen_at
    assert c2.contract_fingerprint == c1.contract_fingerprint


def test_changed_contract_is_rejected_and_never_rewritten(store):
    # 先冻结一份兼容契约。
    c1, _ = store.submit(parse_payload(base_contract()))
    assert c1.compatible

    # 改变发送端字段类型为不兼容契约：必须拒绝，而不是覆盖成 incompatible。
    changed = base_contract()
    changed["sender_types"][0]["type"]["fields"][0]["type"] = TEXT
    with pytest.raises(ContractConflictError):
        store.submit(parse_payload(changed))

    c2 = store.get("AUDIT-FREEZE-1")
    assert c2.compatible is True
    assert c2.frozen_at == c1.frozen_at


def test_persistence_across_store_instances(tmp_path):
    directory = tmp_path / "data"
    AuditStore(directory).submit(parse_payload(base_contract()))
    again = AuditStore(directory).get("AUDIT-FREEZE-1")
    assert again.compatible is True


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


def test_health(server):
    status, data = _request(f"{server}/health")
    assert status == 200 and data["status"] == "ok"


def test_api_submit_resubmit_conflict_and_reopen(server):
    contract = base_contract(audit_id="AUDIT-API-1")

    status, data = _request(f"{server}/api/audits", "POST", contract)
    assert status == 201 and data["conclusion"]["compatible"] is True

    # 相同载荷重传：200，读取原冻结结论。
    status, data = _request(f"{server}/api/audits", "POST", contract)
    assert status == 200 and data["resubmitted_same_contract"] is True
    frozen_at = data["conclusion"]["frozen_at"]

    # 改契约：409。
    changed = json.loads(json.dumps(contract))
    changed["sender_types"][0]["type"]["fields"][0]["type"] = TEXT
    status, data = _request(f"{server}/api/audits", "POST", changed)
    assert status == 409 and data["error"] == "contract-conflict"

    # 重开：仍是原结论。
    status, data = _request(f"{server}/api/audits/AUDIT-API-1")
    assert status == 200
    assert data["conclusion"]["compatible"] is True
    assert data["conclusion"]["frozen_at"] == frozen_at


def test_api_validation_issues_batched(server):
    bad = {
        "audit_id": "bad id",
        "sender_types": [{"name": "1x", "type": {"kind": "wat"}}],
        "receiver_types": [],
    }
    status, data = _request(f"{server}/api/audits", "POST", bad)
    assert status == 400
    assert len(data["issues"]) >= 2  # 多个问题一次反馈


def test_api_reopen_missing(server):
    status, data = _request(f"{server}/api/audits/NOPE")
    assert status == 404 and data["error"] == "audit-not-found"


def test_api_incompatible_payload_shape(server):
    contract = base_contract(audit_id="AUDIT-API-BAD")
    contract["receiver_types"] = [{
        "name": "R",
        "type": {"kind": "record", "fields": [
            {"name": "x", "type": INT, "required": True},
            {"name": "y", "type": TEXT, "required": True},
        ]},
    }]
    status, data = _request(f"{server}/api/audits", "POST", contract)
    assert status == 201
    c = data["conclusion"]
    assert c["compatible"] is False
    assert c["mismatch"]["code"] == "field-missing"
    assert c["mismatch"]["path"] == "R.y"


def test_api_recursive_contract_marks_recycled_pairs(server):
    list_decl = [
        {"name": "List", "type": {"kind": "variant", "tags": [
            {"label": "Nil", "type": {"kind": "record", "fields": []}},
            {"label": "Cons", "type": {"kind": "record", "fields": [
                {"name": "head", "type": INT, "required": True},
                {"name": "tail", "type": {"kind": "ref", "name": "List"},
                 "required": False},
            ]}},
        ]}}
    ]
    body = {
        "audit_id": "AUDIT-LIST",
        "sender_types": list_decl,
        "receiver_types": list_decl,
        "root_name": "List",
    }
    status, data = _request(f"{server}/api/audits", "POST", body)
    assert status == 201
    assert data["conclusion"]["compatible"] is True
    pairs = [r["pair"] for r in data["conclusion"]["recycled"]]
    assert any("List <= List" in p for p in pairs)


# ---------- 递归裁决按审计标识隔离（同名拓扑不得互相污染） ----------

def _rec(*fields):
    return {"kind": "record", "fields": list(fields)}


def _field(name, t, required=True):
    return {"name": name, "type": t, "required": required}


def _variant(*tags):
    return {"kind": "variant", "tags": list(tags)}


def _tag(label, t):
    return {"label": label, "type": t}


def _ref(name):
    return {"kind": "ref", "name": name}


def _payload_contract(audit_id, seq_type):
    # Cmd{payload: Payload}; Payload = variant{Data}; Data = record{seq, next: Data?}
    sender = [
        {"name": "Cmd", "type": _rec(_field("payload", _ref("Payload")))},
        {"name": "Payload", "type": _variant(_tag("Data", _ref("Data")))},
        {"name": "Data", "type": _rec(
            _field("seq", INT), _field("next", _ref("Data"), required=False))},
    ]
    receiver = [
        {"name": "Cmd", "type": _rec(_field("payload", _ref("Payload")))},
        {"name": "Payload", "type": _variant(_tag("Data", _ref("Data")))},
        {"name": "Data", "type": _rec(
            _field("seq", seq_type), _field("next", _ref("Data"), required=False))},
    ]
    return {
        "audit_id": audit_id,
        "root_name": "Cmd",
        "sender_types": sender,
        "receiver_types": receiver,
    }


def test_recursive_audits_with_same_topology_are_isolated(store):
    # 先提交一份兼容契约（Data.seq 两边均 int）。
    good, _ = store.submit(parse_payload(_payload_contract("AUDIT-ISO-OK", INT)))
    assert good.compatible is True
    good_frozen_at = good.frozen_at

    # 全新审计标识、同名同递归拓扑，但接收端在 Data.seq 要求 text。
    bad, created = store.submit(parse_payload(_payload_contract("AUDIT-ISO-BAD", TEXT)))
    assert created is True
    assert bad.compatible is False
    assert bad.mismatch is not None
    assert bad.mismatch.code == "primitive-mismatch"
    assert bad.mismatch.path == "Cmd.payload[Data].seq"

    # 第一份兼容审计结论保持不变（重开读取原冻结记录）。
    reopened = store.get("AUDIT-ISO-OK")
    assert reopened.compatible is True
    assert reopened.frozen_at == good_frozen_at
    assert reopened.contract_fingerprint == good.contract_fingerprint


def test_recursive_audits_isolated_reverse_order(store):
    # 相反顺序：先不兼容，再提交同名拓扑的独立兼容审计，不得反向误拒。
    bad, _ = store.submit(parse_payload(_payload_contract("AUDIT-REV-BAD", TEXT)))
    assert bad.compatible is False

    good, created = store.submit(parse_payload(_payload_contract("AUDIT-REV-OK", INT)))
    assert created is True
    assert good.compatible is True
    assert good.mismatch is None


def test_api_two_independent_recursive_audits_second_rejected(server):
    # 通过真实审计 API 连续提交两份独立审计。
    status, data = _request(
        f"{server}/api/audits", "POST", _payload_contract("API-ISO-OK", INT)
    )
    assert status == 201 and data["conclusion"]["compatible"] is True

    status, data = _request(
        f"{server}/api/audits", "POST", _payload_contract("API-ISO-BAD", TEXT)
    )
    assert status == 201
    mismatch = data["conclusion"]["mismatch"]
    assert data["conclusion"]["compatible"] is False
    assert mismatch["code"] == "primitive-mismatch"
    assert mismatch["path"] == "Cmd.payload[Data].seq"

    # 按标识重开：第二份仍拒绝，第一份结论不变。
    status, data = _request(f"{server}/api/audits/API-ISO-BAD")
    assert status == 200 and data["conclusion"]["compatible"] is False
    assert data["conclusion"]["mismatch"]["code"] == "primitive-mismatch"

    status, data = _request(f"{server}/api/audits/API-ISO-OK")
    assert status == 200 and data["conclusion"]["compatible"] is True


# ---------- 已被旧版本错误冻结的记录：按冻结契约恢复正确结论 ----------

def _write_legacy_record(directory: Path, body: dict, conclusion: dict) -> str:
    """写入旧版本（无 conclusion_version）冻结记录，返回契约指纹。"""
    directory.mkdir(parents=True, exist_ok=True)
    payload = parse_payload(body)
    fingerprint = canonical_fingerprint(payload)
    record = {
        "contract_fingerprint": fingerprint,
        "contract": payload.fingerprint_dict(),
        "conclusion": {**conclusion, "contract_fingerprint": fingerprint},
    }
    target = directory / f"{body['audit_id']}.json"
    target.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    return fingerprint


def test_legacy_wrong_compatible_record_heals_on_reopen(store):
    body = _payload_contract("LEGACY-BAD", TEXT)
    fingerprint = _write_legacy_record(
        store.dir, body,
        {"audit_id": "LEGACY-BAD", "compatible": True, "root": "Cmd",
         "mismatch": None, "recycled": [],
         "frozen_at": "2026-01-01T00:00:00Z"},
    )

    healed = store.get("LEGACY-BAD")
    assert healed.compatible is False
    assert healed.mismatch.code == "primitive-mismatch"
    assert healed.mismatch.path == "Cmd.payload[Data].seq"
    # 审计标识、契约指纹、冻结时间逐字保留。
    assert healed.audit_id == "LEGACY-BAD"
    assert healed.contract_fingerprint == fingerprint
    assert healed.frozen_at == "2026-01-01T00:00:00Z"


def test_legacy_record_heals_on_same_contract_resubmit_and_conflicts_on_change(store):
    body = _payload_contract("LEGACY-RESUB", TEXT)
    fingerprint = _write_legacy_record(
        store.dir, body,
        {"audit_id": "LEGACY-RESUB", "compatible": True, "root": "Cmd",
         "mismatch": None, "recycled": [],
         "frozen_at": "2026-01-02T00:00:00Z"},
    )

    # 完全相同契约重传：恢复正确结论，frozen_at 不变。
    healed, created = store.submit(parse_payload(body))
    assert created is False
    assert healed.compatible is False
    assert healed.frozen_at == "2026-01-02T00:00:00Z"
    assert healed.contract_fingerprint == fingerprint

    # 不同契约重传仍须 409，且记录不得被改写。
    with pytest.raises(ContractConflictError):
        store.submit(parse_payload(_payload_contract("LEGACY-RESUB", INT)))
    raw = json.loads((store.dir / "LEGACY-RESUB.json").read_text(encoding="utf-8"))
    assert raw["contract_fingerprint"] == fingerprint
    assert raw["conclusion"]["frozen_at"] == "2026-01-02T00:00:00Z"
    assert raw["conclusion"]["compatible"] is False


def test_legacy_wrong_incompatible_record_heals_reverse(store):
    body = _payload_contract("LEGACY-GOOD", INT)
    _write_legacy_record(
        store.dir, body,
        {"audit_id": "LEGACY-GOOD", "compatible": False, "root": "Cmd",
         "mismatch": {"path": "Cmd.payload[Data].seq", "code": "primitive-mismatch",
                      "message": "stale", "detail": {}},
         "recycled": [], "frozen_at": "2026-02-02T00:00:00Z"},
    )
    healed = store.get("LEGACY-GOOD")
    assert healed.compatible is True
    assert healed.mismatch is None
    assert healed.frozen_at == "2026-02-02T00:00:00Z"


def test_legacy_healing_survives_restart_and_is_idempotent(tmp_path):
    directory = tmp_path / "data"
    body = _payload_contract("LEGACY-RESTART", TEXT)
    _write_legacy_record(
        directory, body,
        {"audit_id": "LEGACY-RESTART", "compatible": True, "root": "Cmd",
         "mismatch": None, "recycled": [],
         "frozen_at": "2026-03-03T00:00:00Z"},
    )

    first = AuditStore(directory).get("LEGACY-RESTART")
    assert first.compatible is False

    # 模拟服务重启：新 store 实例读取已修复记录，结论稳定，不再重写文件。
    record_path = directory / "LEGACY-RESTART.json"
    mtime_before = record_path.stat().st_mtime_ns
    again = AuditStore(directory).get("LEGACY-RESTART")
    assert again.compatible is False
    assert again.mismatch.code == "primitive-mismatch"
    assert again.frozen_at == "2026-03-03T00:00:00Z"
    assert record_path.stat().st_mtime_ns == mtime_before
    raw = json.loads(record_path.read_text(encoding="utf-8"))
    assert raw["conclusion"]["conclusion_version"] >= 2


def test_corrupt_legacy_contract_does_not_crash_get(store):
    # 旧记录缺少冻结契约 / 契约损坏时，原样返回存储的结论，不臆造也不崩溃。
    body = _payload_contract("LEGACY-CORRUPT", TEXT)
    fingerprint = _write_legacy_record(
        store.dir, body,
        {"audit_id": "LEGACY-CORRUPT", "compatible": True, "root": "Cmd",
         "mismatch": None, "recycled": [],
         "frozen_at": "2026-04-04T00:00:00Z"},
    )
    # 手工破坏冻结契约（删除 sender 声明）。
    record_path = store.dir / "LEGACY-CORRUPT.json"
    raw = json.loads(record_path.read_text(encoding="utf-8"))
    del raw["contract"]["sender"]
    record_path.write_text(json.dumps(raw), encoding="utf-8")

    got = store.get("LEGACY-CORRUPT")
    assert got.compatible is True  # 无法重裁时回退为存储结论
    assert got.frozen_at == "2026-04-04T00:00:00Z"
    assert got.contract_fingerprint == fingerprint
