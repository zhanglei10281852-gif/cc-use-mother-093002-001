"""联合培养履约协同的领域模型。

合作协议采用条款化、逐项会签、版本生效的结构：

- 合作方（Party）在各自角色权限范围内提出条款；
- 协议版本（AgreementVersionView）带生效期，由各方逐项会签后生效；
- 生效后的任何变更只能通过修订案（amendment）推进，已履行条款只能延续；
- 招生批次、课程交付、师资派出、资源提供作为履约活动挂接到具体条款。

本模块只包含纯领域类型与规则，不依赖存储与时钟实现。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from .contracts import AgreementVersion, ObligationRecord  # 兼容:保留基础契约再导出

__all__ = [
    "AgreementVersion",
    "ObligationRecord",
    "PartyRole",
    "ClauseCategory",
    "VersionState",
    "FulfillmentState",
    "DisputeResolution",
    "ClauseDisposition",
    "ROLE_PERMISSIONS",
    "Party",
    "Clause",
    "Signature",
    "Fulfillment",
    "Evidence",
    "Activity",
    "AmendmentImpact",
    "Clock",
    "SystemClock",
    "FixedClock",
]


class PartyRole(str, Enum):
    COORDINATOR = "COORDINATOR"          # 主办高校(广西)
    ACADEMIC_PARTNER = "ACADEMIC_PARTNER"  # 东盟学术伙伴
    ADMIN = "ADMIN"                      # 管理员(只读/查询,不参与会签)


class ClauseCategory(str, Enum):
    ENROLLMENT_QUOTA = "ENROLLMENT_QUOTA"  # 招生名额
    COURSE_DELIVERY = "COURSE_DELIVERY"    # 课程责任/交付
    TEACHER_DISPATCH = "TEACHER_DISPATCH"  # 师资派出(到岗日期)
    RESOURCE = "RESOURCE"                  # 资源提供
    OUTPUT_OWNERSHIP = "OUTPUT_OWNERSHIP"  # 成果归属


class VersionState(str, Enum):
    DRAFT = "DRAFT"
    OPEN = "OPEN"                 # 会签中
    EFFECTIVE = "EFFECTIVE"       # 已生效
    EXPIRED = "EXPIRED"           # 会签窗口过期,未齐签
    DECLINED = "DECLINED"         # 有条款被拒签,版本不成立


class FulfillmentState(str, Enum):
    PENDING = "PENDING"
    PARTIAL = "PARTIAL"
    FULFILLED = "FULFILLED"
    LATE_PARTIAL = "LATE_PARTIAL"
    LATE_FULFILLED = "LATE_FULFILLED"
    DISPUTED = "DISPUTED"
    SUBSTITUTED = "SUBSTITUTED"


class DisputeResolution(str, Enum):
    PENDING = "PENDING"
    UPHELD = "UPHELD"          # 争议成立:原履行不抵扣,需替代/补正
    REJECTED = "REJECTED"      # 争议驳回:按原进度继续履行


class ClauseDisposition(str, Enum):
    """修订案中条款相对于基础版本的处置方式。"""
    CARRIED = "CARRIED"        # 原文延续(承诺不变,已执行部分不受影响)
    MODIFIED = "MODIFIED"      # 条款修改(仅向将来生效)
    REPLACED = "REPLACED"      # 被新条款替代
    ADDED = "ADDED"            # 新增条款


# 角色可提出的条款类别。成果归属为共同关切,任何合作方均可提案;
# 管理员不参与提案与会签。
ROLE_PERMISSIONS: dict[PartyRole, frozenset[ClauseCategory]] = {
    PartyRole.COORDINATOR: frozenset(
        {
            ClauseCategory.ENROLLMENT_QUOTA,
            ClauseCategory.COURSE_DELIVERY,
            ClauseCategory.TEACHER_DISPATCH,
            ClauseCategory.RESOURCE,
            ClauseCategory.OUTPUT_OWNERSHIP,
        }
    ),
    PartyRole.ACADEMIC_PARTNER: frozenset(
        {
            ClauseCategory.ENROLLMENT_QUOTA,
            ClauseCategory.COURSE_DELIVERY,
            ClauseCategory.TEACHER_DISPATCH,
            ClauseCategory.RESOURCE,
            ClauseCategory.OUTPUT_OWNERSHIP,
        }
    ),
    PartyRole.ADMIN: frozenset(),
}

# 修订时不允许直接修改/删除的条款类别:只要该条款已有履行进度,
# 承诺已经进入执行,修订只能延续(CARRIED)或做面向将来的替代安排。
_EXECUTED_DISPOSITION_BLOCKED = {ClauseDisposition.MODIFIED, ClauseDisposition.REPLACED}


@dataclass(frozen=True)
class Party:
    party_id: str
    display_name: str
    role: PartyRole

    def __post_init__(self) -> None:
        if not self.party_id or not self.display_name:
            raise ValueError("合作方信息不完整")
        if not isinstance(self.role, PartyRole):
            raise ValueError("合作方角色不合法")

    @property
    def can_propose(self) -> bool:
        return bool(ROLE_PERMISSIONS[self.role])

    def can_propose_category(self, category: ClauseCategory) -> bool:
        return category in ROLE_PERMISSIONS[self.role]


@dataclass(frozen=True)
class Evidence:
    """证据链条目:文件、登记截图、认证回执等都以不可变形式留存。"""
    evidence_ref: str
    title: str
    submitted_by: str
    submitted_at: datetime
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.evidence_ref or not self.title or not self.submitted_by:
            raise ValueError("证据信息不完整")


@dataclass
class Clause:
    clause_id: str
    category: ClauseCategory
    text: str
    responsible_party_id: str
    version_no: int = 0
    disposition: ClauseDisposition = ClauseDisposition.ADDED
    supersedes_clause_id: Optional[str] = None
    due_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if not self.clause_id or not self.text or not self.responsible_party_id:
            raise ValueError("条款信息不完整")
        if not isinstance(self.category, ClauseCategory):
            raise ValueError("条款类别不合法")
        if self.version_no < 0:
            raise ValueError("条款版本号不合法")

    @property
    def is_live_change(self) -> bool:
        """是否为对已存在条款的面向将来的变更。"""
        return self.disposition in (ClauseDisposition.MODIFIED, ClauseDisposition.REPLACED)


@dataclass(frozen=True)
class Signature:
    clause_id: str
    party_id: str
    signed_at: datetime
    agrees: bool
    comment: str = ""


@dataclass
class Fulfillment:
    """条款的履约状态机。所有状态迁移都要求携带证据。"""
    clause_id: str
    state: FulfillmentState = FulfillmentState.PENDING
    fulfilled_ratio: float = 0.0
    due_at: Optional[datetime] = None
    last_progress_at: Optional[datetime] = None
    evidences: list[Evidence] = field(default_factory=list)
    dispute: Optional["Dispute"] = None

    @property
    def has_progress(self) -> bool:
        return self.fulfilled_ratio > 0.0 or self.state != FulfillmentState.PENDING

    @property
    def is_outstanding(self) -> bool:
        return self.state in (
            FulfillmentState.PENDING,
            FulfillmentState.PARTIAL,
            FulfillmentState.LATE_PARTIAL,
            FulfillmentState.DISPUTED,
        )


@dataclass
class Dispute:
    raised_by: str
    raised_at: datetime
    reason: str
    state: DisputeResolution = DisputeResolution.PENDING
    resolved_at: Optional[datetime] = None
    resolution_note: str = ""
    evidences: list[Evidence] = field(default_factory=list)


@dataclass(frozen=True)
class Activity:
    """挂接到具体条款的履约活动:招生批次/课程交付/师资派出/资源提供。"""
    activity_id: str
    kind: ClauseCategory
    cohort: str
    clause_id: str
    title: str
    created_at: datetime
    due_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if not self.activity_id or not self.cohort or not self.clause_id:
            raise ValueError("履约活动信息不完整")
        if self.kind not in (
            ClauseCategory.ENROLLMENT_QUOTA,
            ClauseCategory.COURSE_DELIVERY,
            ClauseCategory.TEACHER_DISPATCH,
            ClauseCategory.RESOURCE,
        ):
            raise ValueError("履约活动类别不合法")


@dataclass(frozen=True)
class AgreementVersionView:
    """某一时点上的协议版本视图(值对象)。"""
    agreement_id: str
    version_no: int
    state: VersionState
    clauses: tuple[Clause, ...]
    signatures: tuple[Signature, ...]
    opened_at: Optional[datetime]
    effective_at: Optional[datetime]
    sign_deadline: Optional[datetime]
    base_version_no: Optional[int] = None
    sequence: int = 0

    @property
    def signer_party_ids(self) -> frozenset[str]:
        return frozenset(c.responsible_party_id for c in self.clauses)

    def clause(self, clause_id: str) -> Optional[Clause]:
        return next((c for c in self.clauses if c.clause_id == clause_id), None)

    def agreeing_signature_ids(self) -> set[tuple[str, str]]:
        return {(s.clause_id, s.party_id) for s in self.signatures if s.agrees}

    def pending_signatures(self) -> list[dict]:
        """尚缺的会签:返回 [{clause_id, party_id}],含未签与拒签。"""
        decided = {(s.clause_id, s.party_id): s.agrees for s in self.signatures}
        pending: list[dict] = []
        for clause in self.clauses:
            for party_id in sorted(self.signer_party_ids):
                if decided.get((clause.clause_id, party_id), False) is not True:
                    pending.append({"clause_id": clause.clause_id, "party_id": party_id})
        return pending

    def is_fully_signed(self) -> bool:
        agreed = self.agreeing_signature_ids()
        return all(
            (clause.clause_id, party_id) in agreed
            for clause in self.clauses
            for party_id in self.signer_party_ids
        )

    def is_expired_at(self, now: datetime) -> bool:
        if self.state == VersionState.EFFECTIVE:
            return False
        return self.sign_deadline is not None and now > self.sign_deadline


@dataclass(frozen=True)
class AmendmentImpact:
    """一次修订对已执行承诺的影响说明。"""
    new_version_no: int
    base_version_no: int
    carried: tuple[str, ...]
    modified: tuple[str, ...]
    replaced: tuple[str, ...]
    added: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "new_version_no": self.new_version_no,
            "base_version_no": self.base_version_no,
            "carried": list(self.carried),
            "modified": list(self.modified),
            "replaced": list(self.replaced),
            "added": list(self.added),
        }


class Clock:
    """可控业务时钟。提醒、升级、过期判定全部以它为准,而非系统墙钟。"""

    def now(self) -> datetime:  # pragma: no cover - 接口
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.utcnow()


class FixedClock(Clock):
    """测试/演练用固定时钟,可显式拨快。"""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        self._now = self._now + delta
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value


class DomainError(ValueError):
    """领域规则被违反(值语义,便于与输入校验异常统一捕获)。"""


class ConcurrencyError(DomainError):
    """并发修订冲突:所基于的版本已不是最新。"""


def ensure_executed_clause_change_allowed(
    disposition: ClauseDisposition, fulfillment: Optional[Fulfillment]
) -> None:
    """已执行承诺保护:已有履行进度的条款不得被修订覆盖。"""
    if (
        fulfillment is not None
        and fulfillment.has_progress
        and disposition in _EXECUTED_DISPOSITION_BLOCKED
    ):
        raise DomainError(
            f"条款 {fulfillment.clause_id} 已进入履行,修订只能延续(CARRIED)或新增替代条款,不得覆盖已执行承诺"
        )
