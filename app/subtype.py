"""协归（coinductive）结构子类型判定。

裁决方向：发送端（生产者）<= 接收端（消费者）。

规则（对任意协归展开都成立，构成最大不动点）：
- int/bool/text 仅与自身相容；
- record：接收端声明的每个必需字段必须由发送端以必需字段提供且类型相容；
  接收端可选字段缺失合法，存在则类型相容；发送端额外字段合法；
- variant：发送端声明的每个标签必须存在于接收端，载荷类型相容；
  接收端额外标签合法；
- ref：展开到具名声明的根。引用上的环以“比较对复用”裁决，
  不展开到固定深度，也不依赖实例抽样。

实现要点：
- 栈上比较对（_assumed）命中时按协归假设直接成立；若其外层最终违约，
  该假设被回滚，不会作为“已复用比较对”上报——保证标注与结论一致。
- 已定论比较对（_settled，含不相容结论及其首个违约）被再次进入时复用，
  这才是页面上“在递归处标出的已复用比较对”。
- 字段按接收端声明顺序、标签按发送端声明顺序遍历，首个违约稳定可重放。
- 审计隔离：比较对缓存的键只是类型路径，同名类型在不同契约中定义可能
  完全不同，因此缓存只在单次裁决内部有效——每次 check_compatibility 调用
  都使用全新的缓存，绝不跨审计、跨契约共享，否则一份审计的递归裁决会污染
  另一份稳定审计标识下的结论。
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Mismatch, NamedType, RecyclePoint, TypeExpr

ComparisonCache = dict[tuple[str, str], tuple[bool, Mismatch | None]]


@dataclass(frozen=True)
class _Node:
    """展开后的类型节点：具名根下一条稳定类型路径。"""

    root: str
    path: str  # 以具名根开头，如 Cmd 或 Cmd.payload[Data].x
    expr: TypeExpr


class _Checker:
    """单次裁决的判定器：比较对缓存为实例私有，不跨审计共享。"""

    def __init__(
        self,
        sender: dict[str, TypeExpr],
        receiver: dict[str, TypeExpr],
    ):
        self.sender = sender
        self.receiver = receiver
        self._assumed: set[tuple[str, str]] = set()
        self._settled: ComparisonCache = {}
        self._first_seen: dict[tuple[str, str], str] = {}
        self.events: list[RecyclePoint] = []

    def _resolve(self, side: dict[str, TypeExpr], node: _Node) -> _Node:
        while node.expr.kind == "ref":
            target = node.expr.ref_name or ""
            node = _Node(target, target, side[target])
        return node

    def compatible(self, s_root: str, r_root: str) -> tuple[bool, Mismatch | None]:
        s = _Node(s_root, s_root, self.sender[s_root])
        r = _Node(r_root, r_root, self.receiver[r_root])
        return self._check(s, r, s_root)[:2]

    def _recycle_event(self, key: tuple[str, str], path: str) -> RecyclePoint:
        sp, rp = key
        return RecyclePoint(
            path=path, pair=f"{sp} <= {rp}", first_seen_at=self._first_seen[key]
        )

    def _check(
        self, s_node: _Node, r_node: _Node, path: str
    ) -> tuple[bool, Mismatch | None]:
        s_node = self._resolve(self.sender, s_node)
        r_node = self._resolve(self.receiver, r_node)
        key = (s_node.path, r_node.path)

        cached = self._settled.get(key)
        if cached is not None:
            # 已定论比较对复用（含不相容时的原违约）。
            self._first_seen.setdefault(key, path)
            self.events.append(self._recycle_event(key, path))
            return cached

        if key in self._assumed:
            # 协归假设：递归比较对先假定成立，由外层展开的一致性兜底。
            self._first_seen.setdefault(key, path)
            self.events.append(self._recycle_event(key, path))
            return True, None

        self._assumed.add(key)
        self._first_seen.setdefault(key, path)
        marker = len(self.events)
        try:
            ok, mismatch = self._unfold(s_node, r_node, path)
        finally:
            self._assumed.discard(key)
        if not ok:
            # 本帧失败：撤回仅建立在协归假设上的复用标注。
            del self.events[marker:]
        self._settled[key] = (ok, mismatch)
        return ok, mismatch

    def _fail(self, path: str, code: str, message: str, **detail: object):
        return False, Mismatch(path=path, code=code, message=message, detail=detail)

    def _unfold(self, s: _Node, r: _Node, path: str):
        sk, rk = s.expr.kind, r.expr.kind
        primitives = {"int", "bool", "text"}

        if sk in primitives or rk in primitives:
            if sk != rk:
                code = "primitive-mismatch" if {sk, rk} <= primitives else "kind-mismatch"
                return self._fail(
                    path,
                    code,
                    f"类型不兼容：发送端 {sk}，接收端 {rk}",
                    sender_kind=sk,
                    receiver_kind=rk,
                )
            return True, None

        if sk != rk:
            return self._fail(
                path,
                "kind-mismatch",
                f"结构类型不兼容：发送端 {sk}，接收端 {rk}",
                sender_kind=sk,
                receiver_kind=rk,
            )

        if sk == "record":
            return self._check_record(s, r, path)
        return self._check_variant(s, r, path)

    def _check_record(self, s: _Node, r: _Node, path: str):
        s_fields = {f.name: f for f in (s.expr.fields or [])}

        # 第一轮：接收端字段要求（缺失 / 必需-可选可空性变化），稳定按接收端顺序。
        for rf in r.expr.fields or []:
            fpath = f"{path}.{rf.name}"
            sf = s_fields.get(rf.name)
            if sf is None:
                if rf.required:
                    return self._fail(
                        fpath,
                        "field-missing",
                        f"接收端必需字段 {rf.name} 在发送端记录中缺失",
                        field=rf.name,
                    )
                continue
            if rf.required and not sf.required:
                return self._fail(
                    fpath,
                    "optional-required",
                    f"接收端必需字段 {rf.name} 在发送端被声明为可选，运行期可能缺失",
                    field=rf.name,
                )

        # 第二轮：共有字段值类型相容性。
        for rf in r.expr.fields or []:
            sf = s_fields.get(rf.name)
            if sf is None:
                continue
            fpath = f"{path}.{rf.name}"
            s_child = _Node(s.root, f"{s.path}.{rf.name}", sf.type)
            r_child = _Node(r.root, f"{r.path}.{rf.name}", rf.type)
            ok, mismatch = self._check(s_child, r_child, fpath)
            if not ok:
                return False, mismatch
        return True, None

    def _check_variant(self, s: _Node, r: _Node, path: str):
        r_tags = {t.label: t for t in (r.expr.tags or [])}
        # 发送端可能发出的标签逐个裁决，稳定按发送端声明顺序。
        for st in s.expr.tags or []:
            tpath = f"{path}[{st.label}]"
            rt = r_tags.get(st.label)
            if rt is None:
                return self._fail(
                    tpath,
                    "extra-tag",
                    f"发送端标签 {st.label} 超出接收端允许的标签集合",
                    tag=st.label,
                    receiver_tags=sorted(r_tags),
                )
            s_child = _Node(s.root, f"{s.path}[{st.label}]", st.type)
            r_child = _Node(r.root, f"{r.path}[{st.label}]", rt.type)
            ok, mismatch = self._check(s_child, r_child, tpath)
            if not ok:
                return False, mismatch
        return True, None


def check_compatibility(
    sender_types: list[NamedType],
    receiver_types: list[NamedType],
    root_name: str,
) -> tuple[bool, Mismatch | None, list[RecyclePoint]]:
    """裁决一次审计。每次调用都是独立判定：递归比较对缓存不跨调用共享。"""
    sender = {d.name: d.type for d in sender_types}
    receiver = {d.name: d.type for d in receiver_types}
    checker = _Checker(sender, receiver)
    ok, mismatch = checker.compatible(root_name, root_name)
    return ok, mismatch, checker.events
