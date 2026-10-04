"""冻结存储与 HTTP 审计接口测试。"""
from __future__ import annotations

import json
import threading
import urllib.request
from urllib.error import HTTPError

import pytest

from app.api import build_server
from app.models import ContractConflictError
from app.parser import parse_payload
from app.storage import AuditStore

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
