"""递归协归兼容裁决的核心性质测试。"""
from __future__ import annotations

import pytest

from app.models import TypeExpr as T
from app.models import FieldDecl as F
from app.models import TagDecl as G
from app.models import NamedType as N


def rec(*fields: F) -> T:
    return T(kind="record", fields=list(fields))


def var(*tags: G) -> T:
    return T(kind="variant", tags=list(tags))


def ref(name: str) -> T:
    return T(kind="ref", ref_name=name)


def check(sender, receiver, root="R"):
    from app.subtype import check_compatibility

    return check_compatibility(sender, receiver, root)


def ok_case(sender, receiver, **kw):
    compatible, mismatch, recycled = check(
        [N("R", sender)], [N("R", receiver)]
    )
    assert compatible, f"应当兼容，但报告：{mismatch}"
    return recycled


def bad_case(sender, receiver, code, **kw):
    compatible, mismatch, recycled = check(
        [N("R", sender)], [N("R", receiver)]
    )
    assert not compatible
    assert mismatch is not None
    assert mismatch.code == code
    return mismatch


# ---------- 基本规则 ----------

def test_primitives():
    ok_case(T("int"), T("int"))
    bad_case(T("int"), T("text"), "primitive-mismatch")
    bad_case(T("bool"), T("int"), "primitive-mismatch")


def test_record_width_and_depth_subtyping():
    s = rec(F("a", T("int")), F("b", T("text")))
    r = rec(F("a", T("int")))
    ok_case(s, r)  # 发送端额外字段合法

    s2 = rec(F("a", T("int")))
    r2 = rec(F("a", T("int")), F("b", T("text"), required=False))
    ok_case(s2, r2)  # 接收端可选字段缺失合法

    s3 = rec(F("a", T("int")))
    r3 = rec(F("a", T("int")), F("b", T("text")))
    bad_case(s3, r3, "field-missing")


def test_optional_required_nullability():
    # 接收端必需、发送端可选 => 拒收（运行期可能缺失）
    bad_case(
        rec(F("a", T("int"), required=False)),
        rec(F("a", T("int"), required=True)),
        "optional-required",
    )
    # 接收端可选、发送端必选 => 接收
    ok_case(
        rec(F("a", T("int"), required=True)),
        rec(F("a", T("int"), required=False)),
    )


def test_variant_tags():
    s = var(G("A", T("int")), G("B", T("text")))
    r = var(G("A", T("int")), G("B", T("text")), G("C", T("bool")))
    ok_case(s, r)  # 0. 接收端额外标签合法；1. 发送端超集拒收。

    bad_case(var(G("X", T("int"))), r, "extra-tag")
    bad_case(
        var(G("A", T("text"))),  # 同标签载荷不兼容
        var(G("A", T("int"))),
        "primitive-mismatch",
    )


def test_kind_mismatch():
    bad_case(rec(F("a", T("int"))), var(G("A", T("int"))), "kind-mismatch")
    bad_case(T("int"), rec(F("a", T("int"))), "kind-mismatch")


# ---------- 递归：协归而非固定深度 ----------

def test_recursive_identical_accepted_without_depth_limit():
    # List = variant { Nil: unit-ish, Cons: record{head:int, tail: List} }
    def list_type():
        return var(
            G("Nil", rec()),
            G("Cons", rec(F("head", T("int")), F("tail", ref("List")))),
        )

    s = [N("List", list_type())]
    r = [N("List", list_type())]
    compatible, mismatch, recycled = check(s, r, "List")
    assert compatible
    # 没有实例抽样：至少一次比较对复用标记（Cons.tail -> List <= List）。
    assert any("List <= List" in e.pair for e in recycled)


def test_recursive_deep_mismatch_is_found():
    # 递归列表元素类型不一致：head int vs text，必须在某层裁决出 primitive-mismatch。
    s = [
        N("List",
          var(
              G("Nil", rec()),
              G("Cons", rec(F("head", T("int")), F("tail", ref("List")))),
          ))
    ]
    r = [
        N("List",
          var(
              G("Nil", rec()),
              G("Cons", rec(F("head", T("text")), F("tail", ref("List")))),
          ))
    ]
    compatible, mismatch, _ = check(s, r, "List")
    assert not compatible
    assert mismatch.code == "primitive-mismatch"
    assert "[Cons].head" in mismatch.path


def test_mutual_recursion_accepted():
    # A = record{b: B}; B = record{a: ref A 可选}
    s = [
        N("A", rec(F("b", ref("B")))),
        N("B", rec(F("a", ref("A"), required=False))),
    ]
    r = [
        N("A", rec(F("b", ref("B")))),
        N("B", rec(F("a", ref("A"), required=False))),
    ]
    compatible, mismatch, recycled = check(s, r, "A")
    assert compatible
    assert mismatch is None
    pairs = {e.pair for e in recycled}
    assert "A <= A" in pairs


def test_mutual_recursion_bad_field_deep():
    s = [
        N("A", rec(F("b", ref("B")))),
        N("B", rec(F("x", T("int")), F("a", ref("A"), required=False))),
    ]
    r = [
        N("A", rec(F("b", ref("B")))),
        N("B", rec(F("x", T("text")), F("a", ref("A"), required=False))),
    ]
    compatible, mismatch, _ = check(s, r, "A")
    assert not compatible
    assert mismatch.code == "primitive-mismatch"
    assert mismatch.path == "A.b.x"


def test_coinductive_support_direct_self_reference_through_guard():
    # Tree = record{children: variant{...}} 简化为
    # Tree = record{left: Tree 可选, v:int}；受 record 保护，直接自环合法且兼容。
    tree = rec(F("v", T("int")), F("left", ref("Tree"), required=False))
    s = [N("Tree", tree)]
    r = [N("Tree", rec(F("v", T("int")), F("left", ref("Tree"), required=False)))]
    compatible, _, recycled = check(s, r, "Tree")
    assert compatible
    assert any(e.pair == "Tree <= Tree" for e in recycled)


def test_stable_first_mismatch_field_order():
    # 两个字段同时违约时，稳定选择接收端声明顺序中的第一个：a 先于 b。
    s = rec()
    r = rec(F("a", T("int")), F("b", T("text")))
    m = bad_case(s, r, "field-missing")
    assert m.path == "R.a"

    # 发送端多出两个变体标签时，稳定报告发送端声明顺序第一个：X。
    sv = var(G("X", T("int")), G("Y", T("int")))
    rv = var(G("Z", T("int")))
    m2 = bad_case(sv, rv, "extra-tag")
    assert m2.path == "R[X]"


def test_recycled_pair_not_reported_on_failed_assumption_branch():
    # 深层字段缺失场景中，不应把仅在失败展开里出现的协归假设标为成功复用。
    s = [
        N("R", rec(F("p", ref("Inner")), F("q", T("int"), required=False))),
        N("Inner", var(
            G("Done", rec()),
            G("More", rec(F("child", ref("Inner"), required=False))),
        )),
    ]
    r = [
        N("R", rec(F("p", ref("Inner")), F("q", T("text")))),  # q 必需且类型变
        N("Inner", var(
            G("Done", rec()),
            G("More", rec(F("child", ref("Inner"), required=False))),
        )),
    ]
    compatible, mismatch, recycled = check(s, r, "R")
    assert not compatible
    assert mismatch.path == "R.q"
    assert all("Inner <= Inner" not in e.pair for e in recycled)
