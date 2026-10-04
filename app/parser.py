"""把页面/API 提交的 JSON 声明解析为领域模型并做全部静态校验。

一次反馈的问题：
- 不合法标识（审计标识、类型名、字段名、变体标签）
- 每套声明内的重复类型名 / 记录内重复字段 / 变体内重复标签
- 未定义引用（发送/接收两套声明分别闭包）
- 无保护别名环（仅由 ref 与类型别名构成、未经过 record/variant 节点的环）
- 每套至多 24 个类型
"""
from __future__ import annotations

import re
from typing import Any

from .models import (
    FieldDecl,
    NamedType,
    Payload,
    TagDecl,
    TypeExpr,
    ValidationError,
)

# 稳定审计标识：字母/数字/下划线/连字符，1..64，且必须含字母或数字。
AUDIT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# 类型名 / 字段名 / 标签：字母或下划线开头，后接字母数字下划线，1..48。
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,47}$")

MAX_TYPES = 24
VALID_KINDS = {"int", "bool", "text", "record", "variant", "ref"}


def _is_ident(value: Any) -> bool:
    return isinstance(value, str) and IDENT_RE.fullmatch(value) is not None


class _Builder:
    def __init__(self, issues: list[str]):
        self.issues = issues

    def build_type(self, raw: Any, where: str) -> TypeExpr | None:
        if not isinstance(raw, dict):
            self.issues.append(f"{where}：类型必须是对象")
            return None
        kind = raw.get("kind")
        if kind not in VALID_KINDS:
            self.issues.append(f"{where}：未知类型 kind={kind!r}")
            return None
        if kind in ("int", "bool", "text"):
            return TypeExpr(kind=kind)
        if kind == "record":
            fields_raw = raw.get("fields")
            if not isinstance(fields_raw, list):
                self.issues.append(f"{where}：record 必须包含 fields 列表")
                return None
            fields: list[FieldDecl] = []
            seen: set[str] = set()
            had_error = False
            for i, fr in enumerate(fields_raw):
                fw = f"{where}.fields[{i}]"
                if not isinstance(fr, dict):
                    self.issues.append(f"{fw}：字段必须是对象")
                    had_error = True
                    continue
                fname = fr.get("name")
                if not _is_ident(fname):
                    self.issues.append(f"{fw}：字段名 {fname!r} 不合法")
                    had_error = True
                elif fname in seen:
                    self.issues.append(f"{fw}：字段名 {fname!r} 重复")
                    had_error = True
                else:
                    seen.add(fname)
                ftype = self.build_type(fr.get("type"), f"{fw}.type")
                if ftype is None:
                    had_error = True
                required = fr.get("required", True)
                if not isinstance(required, bool):
                    self.issues.append(f"{fw}：required 必须是布尔值")
                    required = True
                if ftype is not None and _is_ident(fname):
                    fields.append(FieldDecl(name=fname, type=ftype, required=required))
            if had_error:
                return None
            return TypeExpr(kind="record", fields=fields)  # 零字段记录合法（unit 载荷）
        if kind == "variant":
            tags_raw = raw.get("tags")
            if not isinstance(tags_raw, list) or not tags_raw:
                self.issues.append(f"{where}：variant 必须包含非空 tags 列表")
                return None
            tags: list[TagDecl] = []
            seen_labels: set[str] = set()
            had_error = False
            for i, tr in enumerate(tags_raw):
                tw = f"{where}.tags[{i}]"
                if not isinstance(tr, dict):
                    self.issues.append(f"{tw}：标签必须是对象")
                    had_error = True
                    continue
                label = tr.get("label")
                if not _is_ident(label):
                    self.issues.append(f"{tw}：标签 {label!r} 不合法")
                    had_error = True
                elif label in seen_labels:
                    self.issues.append(f"{tw}：标签 {label!r} 重复")
                    had_error = True
                else:
                    seen_labels.add(label)
                ttype = self.build_type(tr.get("type"), f"{tw}.type")
                if ttype is None:
                    had_error = True
                if ttype is not None and _is_ident(label):
                    tags.append(TagDecl(label=label, type=ttype))
            if had_error:
                return None
            return TypeExpr(kind="variant", tags=tags)
        # ref
        ref_name = raw.get("name")
        if not _is_ident(ref_name):
            self.issues.append(f"{where}：引用名 {ref_name!r} 不合法")
            return None
        return TypeExpr(kind="ref", ref_name=ref_name)

    def build_decl_list(self, raw: Any, side: str) -> list[NamedType] | None:
        where = f"{side} 类型声明"
        if not isinstance(raw, list):
            self.issues.append(f"{where}：必须是列表")
            return None
        if len(raw) > MAX_TYPES:
            self.issues.append(f"{where}：至多 {MAX_TYPES} 个类型，实际 {len(raw)} 个")
        if not raw:
            self.issues.append(f"{where}：至少需要 1 个类型")
            return None
        decls: list[NamedType] = []
        seen: set[str] = set()
        for i, item in enumerate(raw):
            iw = f"{where}[{i}]"
            if not isinstance(item, dict):
                self.issues.append(f"{iw}：声明必须是对象")
                continue
            name = item.get("name")
            if not _is_ident(name):
                self.issues.append(f"{iw}：类型名 {name!r} 不合法")
            elif name in seen:
                self.issues.append(f"{iw}：类型名 {name!r} 重复")
            else:
                seen.add(name)
            texpr = self.build_type(item.get("type"), f"{iw}.type")
            if texpr is not None and _is_ident(name):
                decls.append(NamedType(name=name, type=texpr))
        return decls or None


def _collect_refs(t: TypeExpr, out: set[str]) -> None:
    if t.kind == "ref":
        out.add(t.ref_name or "")
    for f in t.fields or []:
        _collect_refs(f.type, out)
    for tg in t.tags or []:
        _collect_refs(tg.type, out)


def _check_undefined(decls: list[NamedType], side: str, issues: list[str]) -> None:
    defined = {d.name for d in decls}
    for d in decls:
        refs: set[str] = set()
        _collect_refs(d.type, refs)
        for r in sorted(refs):
            if r not in defined:
                issues.append(f"{side} 类型 {d.name}：引用了未定义类型 {r!r}")


def _unguarded_alias_cycles(decls: list[NamedType], side: str, issues: list[str]) -> None:
    """检测无保护别名环。

    “保护”节点是 record / variant：穿过它们即脱离纯别名展开。
    因此在别名依赖图（name -> 其顶层未被 record/variant 阻隔的直接/嵌套 ref）
    中出现的环即无保护别名环。基本类型是图的汇点。
    """
    by_name = {d.name: d.type for d in decls}

    def alias_targets(t: TypeExpr, acc: set[str]) -> None:
        if t.kind == "ref":
            acc.add(t.ref_name or "")
        elif t.kind in ("record", "variant"):
            return  # 保护节点：阻隔
        else:
            for f in t.fields or []:
                alias_targets(f.type, acc)
            for tg in t.tags or []:
                alias_targets(tg.type, acc)

    graph: dict[str, set[str]] = {}
    for name, t in by_name.items():
        targets: set[str] = set()
        alias_targets(t, targets)
        graph[name] = {x for x in targets if x in by_name}

    # Tarjan 强连通分量，报告非平凡分量（含自环）。
    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    cyclic: list[list[str]] = []

    def strongconnect(v: str) -> None:
        indices[v] = index_counter[0]
        lowlink[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in sorted(graph.get(v, ())):
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], indices[w])
        if lowlink[v] == indices[v]:
            comp: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1 or v in graph.get(v, ()):
                cyclic.append(sorted(comp))

    for n in sorted(graph):
        if n not in indices:
            strongconnect(n)

    for comp in cyclic:
                issues.append(
            f"{side} 类型声明存在无保护别名环：{' -> '.join(comp + [comp[0]])}"
            "（环必须经过 record 或 variant 受保护节点）"
        )


def parse_payload(raw: Any) -> Payload:
    issues: list[str] = []
    if not isinstance(raw, dict):
        raise ValidationError(["请求体必须是 JSON 对象"])

    audit_id = raw.get("audit_id")
    if not isinstance(audit_id, str) or not AUDIT_ID_RE.fullmatch(audit_id):
        issues.append(
            "audit_id 不合法：需为 1-64 位字母、数字、下划线或连字符"
        )

    builder = _Builder(issues)
    sender = builder.build_decl_list(raw.get("sender_types"), "发送端")
    receiver = builder.build_decl_list(raw.get("receiver_types"), "接收端")

    root_name = raw.get("root_name")
    if root_name is not None and not _is_ident(root_name):
        issues.append(f"root_name {root_name!r} 不合法")

    if issues:
        raise ValidationError(issues)

    assert sender is not None and receiver is not None and isinstance(audit_id, str)

    # 结构性校验依赖解析成功的声明。
    _check_undefined(sender, "发送端", issues)
    _check_undefined(receiver, "接收端", issues)

    sender_names = {d.name for d in sender}
    receiver_names = {d.name for d in receiver}
    effective_root = root_name
    if effective_root is None:
        # 默认根：若发送端只有一个类型则用它，否则要求显式指定。
        effective_root = sender[0].name if len(sender) == 1 else None
        if effective_root is None:
            issues.append("root_name 缺失：发送端声明了多个类型时必须显式指定根类型")
    else:
        if effective_root not in sender_names:
            issues.append(f"root_name {effective_root!r} 在发送端声明中不存在")
        if effective_root not in receiver_names:
            issues.append(f"root_name {effective_root!r} 在接收端声明中不存在")

    _unguarded_alias_cycles(sender, "发送端", issues)
    _unguarded_alias_cycles(receiver, "接收端", issues)

    if issues:
        raise ValidationError(issues)

    return Payload(
        audit_id=audit_id,
        sender_types=sender,
        receiver_types=receiver,
        root_name=effective_root,
    )
