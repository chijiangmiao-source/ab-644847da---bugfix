"""契约解析与静态校验测试：问题一次反馈。"""
from __future__ import annotations

import pytest

from app.parser import parse_payload
from app.models import ValidationError


def _payload(sender, receiver=None, audit_id="AUDIT-1", root=None):
    return {
        "audit_id": audit_id,
        "sender_types": sender,
        "receiver_types": receiver if receiver is not None else sender,
        **({"root_name": root} if root else {}),
    }


INT = {"kind": "int"}
BOOL = {"kind": "bool"}
TEXT = {"kind": "text"}


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


def issues_of(raw):
    with pytest.raises(ValidationError) as ei:
        parse_payload(raw)
    return ei.value.issues


def test_minimal_valid_single_type():
    p = parse_payload(_payload([{"name": "R", "type": INT}]))
    assert p.root_name == "R"
    assert p.audit_id == "AUDIT-1"


def test_invalid_identifiers_reported_once_together():
    raw = _payload(
        [{"name": "1bad", "type": INT}],
        [{"name": "ok", "type": INT}],
        audit_id="bad id!",
    )
    issues = issues_of(raw)
    assert any("audit_id" in i for i in issues)
    assert any("1bad" in i for i in issues)
    assert len(issues) >= 2  # 一次反馈，而不是首个即止


def test_duplicate_type_and_field_names():
    raw = _payload([
        {"name": "R", "type": rec(field("a", INT), field("a", BOOL))},
        {"name": "R", "type": INT},
    ])
    issues = issues_of(raw)
    assert any("重复" in i and "R" in i for i in issues)
    assert any("重复" in i and "a" in i for i in issues)


def test_duplicate_variant_labels():
    raw = _payload([{
        "name": "R",
        "type": variant(tag("A", INT), tag("A", TEXT)),
    }])
    issues = issues_of(raw)
    assert any("标签 'A' 重复" in i for i in issues)


def test_undefined_reference():
    raw = _payload([
        {"name": "R", "type": rec(field("x", ref("Ghost")))},
    ])
    issues = issues_of(raw)
    assert any("未定义类型 'Ghost'" in i for i in issues)


def test_unguarded_alias_cycle_direct():
    # A = ref B; B = ref A => 无保护别名环。
    raw = _payload([
        {"name": "A", "type": ref("B")},
        {"name": "B", "type": ref("A")},
    ], root="A")
    issues = issues_of(raw)
    assert any("无保护别名环" in i for i in issues)


def test_unguarded_self_alias():
    raw = _payload([{"name": "A", "type": ref("A")}], root="A")
    issues = issues_of(raw)
    assert any("无保护别名环" in i for i in issues)


def test_guarded_recursion_is_legal():
    # A = record{next: ref A}：受 record 保护，合法。
    p = parse_payload(_payload([
        {"name": "A", "type": rec(field("next", ref("A"), False))},
    ]))
    assert p.root_name == "A"


def test_alias_chain_ending_in_guarded_type_is_legal():
    # A = ref B; B = record{x:int}：别名链终止于保护节点，合法。
    p = parse_payload(_payload([
        {"name": "A", "type": ref("B")},
        {"name": "B", "type": rec(field("x", INT))},
    ], root="A"))
    assert p.root_name == "A"


def test_too_many_types():
    decls = [{"name": f"T{i}", "type": INT} for i in range(25)]
    issues = issues_of(_payload(decls))
    assert any("至多 24" in i for i in issues)


def test_root_must_exist_both_sides():
    raw = _payload(
        [{"name": "A", "type": INT}],
        [{"name": "B", "type": INT}],
        root="A",
    )
    issues = issues_of(raw)
    assert any("接收端声明中不存在" in i for i in issues)


def test_explicit_root_required_with_multiple_types():
    raw = _payload([
        {"name": "A", "type": INT},
        {"name": "B", "type": INT},
    ])
    issues = issues_of(raw)
    assert any("root_name 缺失" in i for i in issues)


def test_empty_record_as_unit_tag_payload_is_legal():
    # 变体标签携带零字段记录（类似 unit 载荷）应合法。
    p = parse_payload(_payload([{
        "name": "R",
        "type": variant(tag("Nil", rec()), tag("Yes", INT)),
    }]))
    tags = p.sender_types[0].type.tags
    assert tags is not None and tags[0].type.kind == "record"
    assert tags[0].type.fields == []
