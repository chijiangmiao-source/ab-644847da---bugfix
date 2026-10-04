"""领域模型：类型声明、错误与审计结论。

类型的 JSON 形态（页面与 API 共用）::

    {"kind": "int"} | {"kind": "bool"} | {"kind": "text"}
    {"kind": "record", "fields": [{"name": str, "type": Type, "required": bool}]}
    {"kind": "variant", "tags": [{"label": str, "type": Type}]}
    {"kind": "ref", "name": str}

每套类型声明是一个有序列表：{"name": str, "type": Type}。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

TypeKind = Literal["int", "bool", "text", "record", "variant", "ref"]

PRIMITIVES = ("int", "bool", "text")


@dataclass
class FieldDecl:
    name: str
    type: "TypeExpr"
    required: bool = True

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type.to_json(), "required": self.required}


@dataclass
class TagDecl:
    label: str
    type: "TypeExpr"

    def to_json(self) -> dict[str, Any]:
        return {"label": self.label, "type": self.type.to_json()}


@dataclass
class TypeExpr:
    kind: TypeKind
    fields: list[FieldDecl] | None = None
    tags: list[TagDecl] | None = None
    ref_name: str | None = None

    def to_json(self) -> dict[str, Any]:
        if self.kind in PRIMITIVES:
            return {"kind": self.kind}
        if self.kind == "record":
            return {"kind": "record", "fields": [f.to_json() for f in self.fields or []]}
        if self.kind == "variant":
            return {"kind": "variant", "tags": [t.to_json() for t in self.tags or []]}
        return {"kind": "ref", "name": self.ref_name}


@dataclass
class NamedType:
    name: str
    type: TypeExpr

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type.to_json()}


@dataclass
class Payload:
    """一次提交的原始契约载荷（将被冻结）。"""

    audit_id: str
    sender_types: list[NamedType]
    receiver_types: list[NamedType]
    root_name: str | None = None

    def fingerprint_dict(self) -> dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "root_name": self.root_name,
            "sender": [t.to_json() for t in self.sender_types],
            "receiver": [t.to_json() for t in self.receiver_types],
        }


@dataclass
class Mismatch:
    """首个按类型路径稳定选定的违约。"""

    path: str
    code: str  # field-missing | extra-tag | primitive-mismatch | optional-required
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class RecyclePoint:
    """在递归处复用已有协归比较对的位置。"""

    path: str
    pair: str  # "sender-type-path <= receiver-type-path"
    first_seen_at: str


@dataclass
class AuditConclusion:
    audit_id: str
    compatible: bool
    root: str
    mismatch: Mismatch | None
    recycled: list[RecyclePoint]
    contract_fingerprint: str
    frozen_at: str

    def to_json(self) -> dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "compatible": self.compatible,
            "root": self.root,
            "mismatch": None
            if self.mismatch is None
            else {
                "path": self.mismatch.path,
                "code": self.mismatch.code,
                "message": self.mismatch.message,
                "detail": self.mismatch.detail,
            },
            "recycled": [
                {"path": r.path, "pair": r.pair, "first_seen_at": r.first_seen_at}
                for r in self.recycled
            ],
            "contract_fingerprint": self.contract_fingerprint,
            "frozen_at": self.frozen_at,
        }


class ValidationError(Exception):
    """契约本身不合法：一次收集全部问题。"""

    def __init__(self, issues: list[str]):
        super().__init__("; ".join(issues))
        self.issues = issues


class ContractConflictError(Exception):
    """相同审计标识重传但契约发生变化。"""


class AuditNotFoundError(Exception):
    """结论不存在。"""
