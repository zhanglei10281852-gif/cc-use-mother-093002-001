"""联合培养履约协同的核心领域模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from .hashing import GENESIS_HASH, canonical_json, sha256_text


class TermCategory(str, Enum):
    """条款类别，对应四方最容易口径不一致、必须逐项会签的事项。"""

    ENROLLMENT_QUOTA = "招生名额"
    COURSE_DELIVERY = "课程交付"
    TEACHER_DISPATCH = "师资派出"
    RESOURCE_PROVISION = "资源提供"
    ACHIEVEMENT_OWNERSHIP = "成果归属"


class ObligationStatus(str, Enum):
    """履约承诺状态；OVERDUE 与 DISPUTED 由查询时派生，不落库。"""

    PENDING = "待履行"
    PARTIAL = "部分履行"
    FULFILLED = "已履行"
    LATE_FULFILLED = "迟交履行"
    SUBSTITUTED = "替代履行"
    SUPERSEDED = "已被修订替代"
    OVERDUE = "逾期未履"
    DISPUTED = "争议中"


TERMINAL_STATUSES = frozenset(
    {
        ObligationStatus.FULFILLED,
        ObligationStatus.LATE_FULFILLED,
        ObligationStatus.SUBSTITUTED,
        ObligationStatus.SUPERSEDED,
    }
)

UNFULFILLED_STATUSES = frozenset(
    {
        ObligationStatus.PENDING,
        ObligationStatus.PARTIAL,
        ObligationStatus.OVERDUE,
        ObligationStatus.DISPUTED,
    }
)


@dataclass(frozen=True)
class ObligationSpec:
    """条款中声明的一条履约承诺模板，版本生效时登记为正式承诺。"""

    title: str
    responsible_party: str
    batch_id: str
    due_at: datetime
    quantity_required: int = 1
    category: Optional[TermCategory] = None  # 缺省继承条款类别

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "responsible_party": self.responsible_party,
            "batch_id": self.batch_id,
            "due_at": self.due_at.isoformat(),
            "quantity_required": self.quantity_required,
            "category": self.category.value if self.category else None,
        }

    @staticmethod
    def from_dict(data: dict) -> "ObligationSpec":
        return ObligationSpec(
            title=data["title"],
            responsible_party=data["responsible_party"],
            batch_id=data["batch_id"],
            due_at=datetime.fromisoformat(data["due_at"]),
            quantity_required=data.get("quantity_required", 1),
            category=TermCategory(data["category"]) if data.get("category") else None,
        )


@dataclass(frozen=True)
class Term:
    """协议条款：内容哈希是判定会签是否仍然有效的依据。"""

    term_id: str
    category: TermCategory
    content: str
    content_hash: str
    obligation_specs: tuple
    proposed_by: str

    def to_dict(self) -> dict:
        return {
            "term_id": self.term_id,
            "category": self.category.value,
            "content": self.content,
            "content_hash": self.content_hash,
            "obligation_specs": [s.to_dict() for s in self.obligation_specs],
            "proposed_by": self.proposed_by,
        }

    @staticmethod
    def from_dict(data: dict) -> "Term":
        return Term(
            term_id=data["term_id"],
            category=TermCategory(data["category"]),
            content=data["content"],
            content_hash=data["content_hash"],
            obligation_specs=tuple(ObligationSpec.from_dict(s) for s in data["obligation_specs"]),
            proposed_by=data["proposed_by"],
        )


@dataclass(frozen=True)
class Signature:
    """一次会签：只对特定内容哈希负责。"""

    party_id: str
    content_hash: str
    signed_at: datetime


@dataclass
class Draft:
    """首版协议草案：逐条提出条款、逐项会签。"""

    draft_id: str
    created_by: str
    sign_expires_at: datetime
    terms: dict = field(default_factory=dict)  # term_id -> Term
    signatures: dict = field(default_factory=dict)  # term_id -> [Signature]
    status: str = "open"  # open / activated


@dataclass(frozen=True)
class AmendmentChange:
    """修订案中的一处变更：新增、修改或废止条款。"""

    op: str  # "add" / "modify" / "remove"
    term_id: Optional[str]
    category: Optional[TermCategory]
    content: Optional[str]
    content_hash: Optional[str]
    obligation_specs: tuple = ()

    def to_dict(self) -> dict:
        return {
            "op": self.op,
            "term_id": self.term_id,
            "category": self.category.value if self.category else None,
            "content": self.content,
            "content_hash": self.content_hash,
            "obligation_specs": [s.to_dict() for s in self.obligation_specs],
        }

    @staticmethod
    def from_dict(data: dict) -> "AmendmentChange":
        return AmendmentChange(
            op=data["op"],
            term_id=data.get("term_id"),
            category=TermCategory(data["category"]) if data.get("category") else None,
            content=data.get("content"),
            content_hash=data.get("content_hash"),
            obligation_specs=tuple(ObligationSpec.from_dict(s) for s in data.get("obligation_specs", [])),
        )


@dataclass
class Amendment:
    """修订案：协议生效后唯一的变更通道。"""

    amendment_id: str
    base_rev: int
    changes: tuple
    changes_hash: str
    proposed_by: str
    sign_expires_at: datetime
    signatures: list = field(default_factory=list)  # [Signature]
    status: str = "open"  # open / enacted


@dataclass
class Version:
    """带生效期的协议版本，内容不可变。"""

    version_id: str
    rev: int
    effective_from: datetime
    batch_ids: tuple
    terms: dict  # term_id -> Term
    source: str  # "initial" 或 amendment_id
    activated_at: datetime


@dataclass
class EvidenceItem:
    """证据链上的一环，哈希前后衔接，篡改可被发现。"""

    seq: int
    kind: str
    description: str
    uri: Optional[str]
    recorded_by: str
    recorded_at: datetime
    prev_hash: str
    item_hash: str

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "description": self.description,
            "uri": self.uri,
            "recorded_by": self.recorded_by,
            "recorded_at": self.recorded_at.isoformat(),
            "prev_hash": self.prev_hash,
            "item_hash": self.item_hash,
        }

    @staticmethod
    def from_dict(data: dict) -> "EvidenceItem":
        return EvidenceItem(
            seq=data["seq"],
            kind=data["kind"],
            description=data["description"],
            uri=data.get("uri"),
            recorded_by=data["recorded_by"],
            recorded_at=datetime.fromisoformat(data["recorded_at"]),
            prev_hash=data["prev_hash"],
            item_hash=data["item_hash"],
        )


@dataclass
class Obligation:
    """挂接到具体条款的履约承诺。"""

    obligation_id: str
    version_rev: int
    term_id: str
    category: TermCategory
    party_id: str
    batch_id: str
    title: str
    due_at: datetime
    quantity_required: int
    quantity_fulfilled: int = 0
    status: ObligationStatus = ObligationStatus.PENDING
    dispute_open: bool = False
    dispute_reason: Optional[str] = None
    substitute_note: Optional[str] = None
    superseded_by: Optional[str] = None
    via_amendment: Optional[str] = None
    evidence: list = field(default_factory=list)  # [EvidenceItem]


@dataclass(frozen=True)
class Party:
    """合作方：只能在各自权限内提出条款。"""

    party_id: str
    name: str
    permissions: frozenset  # frozenset[TermCategory]


def term_content_hash(category: TermCategory, content: str, specs) -> str:
    """条款内容哈希：会签只对这个哈希负责，内容一变旧会签即失效。"""
    return sha256_text(
        canonical_json(
            {
                "category": category.value,
                "content": content,
                "obligation_specs": [s.to_dict() for s in specs],
            }
        )
    )


def amendment_changes_hash(changes) -> str:
    return sha256_text(canonical_json([c.to_dict() for c in changes]))


def evidence_item_hash(prev_hash: str, *, seq: int, kind: str, description: str,
                       uri: Optional[str], recorded_by: str, recorded_at: datetime) -> str:
    body = canonical_json(
        {
            "seq": seq,
            "kind": kind,
            "description": description,
            "uri": uri,
            "recorded_by": recorded_by,
            "recorded_at": recorded_at.isoformat(),
        }
    )
    return sha256_text(prev_hash + "\n" + body)


def effective_status(obligation: Obligation, now: datetime) -> ObligationStatus:
    """查询时派生的对外状态：争议覆盖一切，逾期由业务时钟判定。"""
    if obligation.dispute_open:
        return ObligationStatus.DISPUTED
    if obligation.status in TERMINAL_STATUSES:
        return obligation.status
    if now > obligation.due_at:
        return ObligationStatus.OVERDUE
    return obligation.status


def verify_evidence_chain(obligation: Obligation) -> bool:
    """重算证据链哈希，任何一环被改动都会校验失败。"""
    prev = GENESIS_HASH
    for expected_seq, item in enumerate(obligation.evidence, 1):
        if item.seq != expected_seq or item.prev_hash != prev:
            return False
        recomputed = evidence_item_hash(
            item.prev_hash,
            seq=item.seq,
            kind=item.kind,
            description=item.description,
            uri=item.uri,
            recorded_by=item.recorded_by,
            recorded_at=item.recorded_at,
        )
        if recomputed != item.item_hash:
            return False
        prev = item.item_hash
    return True
