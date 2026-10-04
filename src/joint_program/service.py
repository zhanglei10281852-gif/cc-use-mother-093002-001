"""联合培养履约协同服务。

核心业务规则：
- 合作方只能在各自权限内提出条款，条款须逐项会签；
- 会签只对特定内容哈希负责，过期或内容已变的会签不得污染较新的决定；
- 首个协议版本由草案激活产生，此后任何变更只能通过修订案推进；
- 修订案必须基于当前生效版本，并发修订中落后的一方在生效时被拒绝；
- 履约承诺挂接具体条款，已执行的承诺不被修订覆盖；
- 提醒与升级由注入的业务时钟计算；事件落盘，重启后待确认事项仍在。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .clock import Clock, SystemClock
from .domain import (
    Amendment,
    AmendmentChange,
    Draft,
    EvidenceItem,
    Obligation,
    ObligationSpec,
    ObligationStatus,
    Party,
    Signature,
    Term,
    TermCategory,
    TERMINAL_STATUSES,
    Version,
    amendment_changes_hash,
    effective_status,
    evidence_item_hash,
    term_content_hash,
)
from .errors import (
    IncompleteError,
    NotFoundError,
    PermissionDeniedError,
    SignatureError,
    StaleDecisionError,
    StateError,
    ValidationError,
)
from .events import EventType
from .hashing import GENESIS_HASH
from .store import EventStore


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None:
        raise ValidationError(f"{name}必须携带时区信息")


class State:
    """由事件流折叠出的内存状态，服务命令与历史查询共用同一份折叠逻辑。"""

    def __init__(self) -> None:
        self.parties: dict[str, Party] = {}
        self.drafts: dict[str, Draft] = {}
        self.amendments: dict[str, Amendment] = {}
        self.versions: list[Version] = []
        self.obligations: dict[str, Obligation] = {}
        self.counters: dict[str, int] = {}

    @property
    def current_rev(self) -> int:
        return self.versions[-1].rev if self.versions else 0

    def peek_id(self, kind: str, prefix: str, offset: int = 1) -> str:
        return f"{prefix}-{self.counters.get(kind, 0) + offset}"

    def apply(self, record: dict) -> None:
        handler = getattr(self, "_on_" + record["event_type"].lower())
        handler(record["payload"], record)

    # ---- 事件折叠 ----

    def _on_party_registered(self, payload: dict, record: dict) -> None:
        self.parties[payload["party_id"]] = Party(
            party_id=payload["party_id"],
            name=payload["name"],
            permissions=frozenset(TermCategory(p) for p in payload["permissions"]),
        )

    def _on_draft_created(self, payload: dict, record: dict) -> None:
        self.drafts[payload["draft_id"]] = Draft(
            draft_id=payload["draft_id"],
            created_by=payload["created_by"],
            sign_expires_at=_dt(payload["sign_expires_at"]),
        )
        self.counters["draft"] = self.counters.get("draft", 0) + 1

    def _on_term_proposed(self, payload: dict, record: dict) -> None:
        draft = self.drafts[payload["draft_id"]]
        term = Term.from_dict(payload)
        draft.terms[term.term_id] = term
        draft.signatures.setdefault(term.term_id, [])
        self.counters["term"] = self.counters.get("term", 0) + 1

    def _on_term_revised(self, payload: dict, record: dict) -> None:
        draft = self.drafts[payload["draft_id"]]
        old = draft.terms[payload["term_id"]]
        draft.terms[payload["term_id"]] = Term(
            term_id=old.term_id,
            category=old.category,
            content=payload["content"],
            content_hash=payload["content_hash"],
            obligation_specs=tuple(ObligationSpec.from_dict(s) for s in payload["obligation_specs"]),
            proposed_by=old.proposed_by,
        )

    def _on_term_countersigned(self, payload: dict, record: dict) -> None:
        draft = self.drafts[payload["draft_id"]]
        draft.signatures[payload["term_id"]].append(
            Signature(
                party_id=payload["party_id"],
                content_hash=payload["content_hash"],
                signed_at=_dt(record["occurred_at"]),
            )
        )

    def _on_version_activated(self, payload: dict, record: dict) -> None:
        terms = {t["term_id"]: Term.from_dict(t) for t in payload["terms"]}
        self.versions.append(
            Version(
                version_id=payload["version_id"],
                rev=payload["rev"],
                effective_from=_dt(payload["effective_from"]),
                batch_ids=tuple(payload["batch_ids"]),
                terms=terms,
                source="initial",
                activated_at=_dt(record["occurred_at"]),
            )
        )
        self.drafts[payload["draft_id"]].status = "activated"

    def _on_amendment_proposed(self, payload: dict, record: dict) -> None:
        self.amendments[payload["amendment_id"]] = Amendment(
            amendment_id=payload["amendment_id"],
            base_rev=payload["base_rev"],
            changes=tuple(AmendmentChange.from_dict(c) for c in payload["changes"]),
            changes_hash=payload["changes_hash"],
            proposed_by=payload["proposed_by"],
            sign_expires_at=_dt(payload["sign_expires_at"]),
        )
        self.counters["amendment"] = self.counters.get("amendment", 0) + 1

    def _on_amendment_countersigned(self, payload: dict, record: dict) -> None:
        self.amendments[payload["amendment_id"]].signatures.append(
            Signature(
                party_id=payload["party_id"],
                content_hash=payload["changes_hash"],
                signed_at=_dt(record["occurred_at"]),
            )
        )

    def _on_amendment_enacted(self, payload: dict, record: dict) -> None:
        base_terms = self.versions[-1].terms if self.versions else {}
        terms = {t["term_id"]: Term.from_dict(t) for t in payload["terms"]}
        added = [tid for tid in terms if tid not in base_terms]
        self.versions.append(
            Version(
                version_id=payload["version_id"],
                rev=payload["new_rev"],
                effective_from=_dt(payload["effective_from"]),
                batch_ids=tuple(payload["batch_ids"]),
                terms=terms,
                source=payload["amendment_id"],
                activated_at=_dt(record["occurred_at"]),
            )
        )
        self.amendments[payload["amendment_id"]].status = "enacted"
        self.counters["term"] = self.counters.get("term", 0) + len(added)

    def _on_obligation_registered(self, payload: dict, record: dict) -> None:
        self.obligations[payload["obligation_id"]] = Obligation(
            obligation_id=payload["obligation_id"],
            version_rev=payload["version_rev"],
            term_id=payload["term_id"],
            category=TermCategory(payload["category"]),
            party_id=payload["party_id"],
            batch_id=payload["batch_id"],
            title=payload["title"],
            due_at=_dt(payload["due_at"]),
            quantity_required=payload["quantity_required"],
            via_amendment=payload.get("via_amendment"),
        )
        self.counters["obligation"] = self.counters.get("obligation", 0) + 1

    def _on_obligation_superseded(self, payload: dict, record: dict) -> None:
        obligation = self.obligations[payload["obligation_id"]]
        obligation.status = ObligationStatus.SUPERSEDED
        obligation.superseded_by = payload["by_amendment"]

    def _on_fulfillment_recorded(self, payload: dict, record: dict) -> None:
        obligation = self.obligations[payload["obligation_id"]]
        obligation.quantity_fulfilled = payload["new_total"]
        obligation.status = ObligationStatus(payload["new_status"])
        obligation.evidence.append(EvidenceItem.from_dict(payload["evidence"]))

    def _on_dispute_raised(self, payload: dict, record: dict) -> None:
        obligation = self.obligations[payload["obligation_id"]]
        obligation.dispute_open = True
        obligation.dispute_reason = payload["reason"]
        obligation.evidence.append(EvidenceItem.from_dict(payload["evidence"]))

    def _on_dispute_resolved(self, payload: dict, record: dict) -> None:
        obligation = self.obligations[payload["obligation_id"]]
        obligation.dispute_open = False
        obligation.evidence.append(EvidenceItem.from_dict(payload["evidence"]))

    def _on_substitute_recorded(self, payload: dict, record: dict) -> None:
        obligation = self.obligations[payload["obligation_id"]]
        obligation.status = ObligationStatus.SUBSTITUTED
        obligation.substitute_note = payload["description"]
        obligation.evidence.append(EvidenceItem.from_dict(payload["evidence"]))


@dataclass(frozen=True)
class ReminderPolicy:
    """提醒与升级策略：临期窗口、会签窗口、逾期升级阶梯。"""

    due_soon_lead: timedelta = timedelta(days=7)
    signature_lead: timedelta = timedelta(days=3)
    escalation_steps: tuple = (timedelta(days=3), timedelta(days=7))

    def escalation_level(self, overdue: timedelta) -> int:
        level = 1
        for step in self.escalation_steps:
            if overdue >= step:
                level += 1
        return level


@dataclass(frozen=True)
class Reminder:
    kind: str  # due_soon / overdue_escalation / countersign_pending / countersign_expired
    target_id: str
    message: str
    level: int = 0
    party_id: Optional[str] = None
    due_at: Optional[datetime] = None


class CoordinationService:
    """联合培养履约协同服务：会签、版本、修订、履约与提醒。"""

    def __init__(
        self,
        store: EventStore,
        clock: Optional[Clock] = None,
        coordinator_id: str = "coordinator",
        policy: Optional[ReminderPolicy] = None,
    ) -> None:
        self._store = store
        self._clock = clock or SystemClock()
        self.coordinator_id = coordinator_id
        self.policy = policy or ReminderPolicy()
        self._state = State()
        for record in store.read_all():
            self._state.apply(record)

    # ---- 基础访问 ----

    @property
    def parties(self) -> dict:
        return dict(self._state.parties)

    def get_draft(self, draft_id: str) -> Draft:
        draft = self._state.drafts.get(draft_id)
        if draft is None:
            raise NotFoundError(f"草案不存在：{draft_id}")
        return draft

    def get_amendment(self, amendment_id: str) -> Amendment:
        amendment = self._state.amendments.get(amendment_id)
        if amendment is None:
            raise NotFoundError(f"修订案不存在：{amendment_id}")
        return amendment

    def get_obligation(self, obligation_id: str) -> Obligation:
        obligation = self._state.obligations.get(obligation_id)
        if obligation is None:
            raise NotFoundError(f"履约承诺不存在：{obligation_id}")
        return obligation

    def current_version(self) -> Optional[Version]:
        return self._state.versions[-1] if self._state.versions else None

    def list_obligations(self, batch_id: Optional[str] = None, party_id: Optional[str] = None) -> list:
        return [
            ob
            for ob in self._state.obligations.values()
            if (batch_id is None or ob.batch_id == batch_id)
            and (party_id is None or ob.party_id == party_id)
        ]

    # ---- 内部工具 ----

    def _emit(self, event_type: str, actor: str, payload: dict) -> dict:
        record = self._store.append(
            event_type=event_type, actor=actor, occurred_at=self._clock.now(), payload=payload
        )
        self._state.apply(record)
        return record

    def _require_party(self, party_id: str) -> Party:
        party = self._state.parties.get(party_id)
        if party is None:
            raise NotFoundError(f"合作方未注册：{party_id}")
        return party

    def _require_coordinator(self, actor: str) -> None:
        if actor != self.coordinator_id:
            raise PermissionDeniedError("只有协调方可以执行该操作")

    def _normalize_specs(self, specs) -> tuple:
        normalized = []
        for spec in specs or ():
            item = spec if isinstance(spec, ObligationSpec) else ObligationSpec(**spec)
            if not item.title or not item.batch_id:
                raise ValidationError("履约承诺缺少标题或培养批次")
            if item.responsible_party not in self._state.parties:
                raise NotFoundError(f"责任方未注册：{item.responsible_party}")
            if item.quantity_required < 1:
                raise ValidationError("履约数量必须为正数")
            _require_aware(item.due_at, "履约期限")
            if item.category is not None and not isinstance(item.category, TermCategory):
                item = ObligationSpec(
                    title=item.title,
                    responsible_party=item.responsible_party,
                    batch_id=item.batch_id,
                    due_at=item.due_at,
                    quantity_required=item.quantity_required,
                    category=TermCategory(item.category),
                )
            normalized.append(item)
        return tuple(normalized)

    def _valid_signers(self, signatures, expected_hash: str, expires_at: datetime) -> set:
        """只统计对当前内容哈希负责、且在有效期内签署的会签。"""
        return {
            s.party_id
            for s in signatures
            if s.content_hash == expected_hash and s.signed_at <= expires_at
        }

    def _all_party_ids(self) -> set:
        return set(self._state.parties)

    def _next_evidence(self, obligation: Obligation, kind: str, description: str,
                       uri: Optional[str], recorded_by: str) -> EvidenceItem:
        seq = len(obligation.evidence) + 1
        prev_hash = obligation.evidence[-1].item_hash if obligation.evidence else GENESIS_HASH
        recorded_at = self._clock.now()
        item_hash = evidence_item_hash(
            prev_hash,
            seq=seq,
            kind=kind,
            description=description,
            uri=uri,
            recorded_by=recorded_by,
            recorded_at=recorded_at,
        )
        return EvidenceItem(
            seq=seq,
            kind=kind,
            description=description,
            uri=uri,
            recorded_by=recorded_by,
            recorded_at=recorded_at,
            prev_hash=prev_hash,
            item_hash=item_hash,
        )

    # ---- 合作方 ----

    def register_party(self, party_id: str, name: str, permissions) -> Party:
        if not party_id or not name:
            raise ValidationError("合作方标识与名称不能为空")
        if party_id in self._state.parties:
            raise ValidationError(f"合作方已存在：{party_id}")
        perms = sorted(TermCategory(p).value for p in permissions)
        self._emit(
            EventType.PARTY_REGISTERED,
            actor=self.coordinator_id,
            payload={"party_id": party_id, "name": name, "permissions": perms},
        )
        return self._state.parties[party_id]

    # ---- 草案与会签 ----

    def create_draft(self, actor: str, sign_expires_at: datetime) -> str:
        self._require_party(actor)
        if self._state.current_rev > 0:
            raise StateError("已存在生效协议，后续变更只能通过修订案推进")
        if any(d.status == "open" for d in self._state.drafts.values()):
            raise StateError("已存在未完成的协议草案")
        _require_aware(sign_expires_at, "会签截止时刻")
        if sign_expires_at <= self._clock.now():
            raise ValidationError("会签截止时刻必须晚于当前时刻")
        draft_id = self._state.peek_id("draft", "D")
        self._emit(
            EventType.DRAFT_CREATED,
            actor=actor,
            payload={
                "draft_id": draft_id,
                "created_by": actor,
                "sign_expires_at": sign_expires_at.isoformat(),
            },
        )
        return draft_id

    def propose_term(self, actor: str, draft_id: str, category, content: str, obligation_specs=()) -> str:
        party = self._require_party(actor)
        draft = self.get_draft(draft_id)
        if draft.status != "open":
            raise StateError("草案已关闭，不能再提出条款")
        category = TermCategory(category)
        if category not in party.permissions:
            raise PermissionDeniedError(f"{party.name}无权提出「{category.value}」类条款")
        if not content:
            raise ValidationError("条款内容不能为空")
        specs = self._normalize_specs(obligation_specs)
        term_id = self._state.peek_id("term", "T")
        content_hash = term_content_hash(category, content, specs)
        self._emit(
            EventType.TERM_PROPOSED,
            actor=actor,
            payload={
                "draft_id": draft_id,
                "term_id": term_id,
                "category": category.value,
                "content": content,
                "content_hash": content_hash,
                "obligation_specs": [s.to_dict() for s in specs],
                "proposed_by": actor,
            },
        )
        return term_id

    def revise_term(self, actor: str, draft_id: str, term_id: str, content: str, obligation_specs=()) -> None:
        """修订草案条款：内容哈希改变后，此前针对旧内容的会签自动失效。"""
        draft = self.get_draft(draft_id)
        if draft.status != "open":
            raise StateError("草案已关闭，不能再修订条款")
        term = draft.terms.get(term_id)
        if term is None:
            raise NotFoundError(f"条款不存在：{term_id}")
        if actor != term.proposed_by and actor != self.coordinator_id:
            raise PermissionDeniedError("只有条款提出方或协调方可以修订条款")
        if not content:
            raise ValidationError("条款内容不能为空")
        specs = self._normalize_specs(obligation_specs)
        content_hash = term_content_hash(term.category, content, specs)
        self._emit(
            EventType.TERM_REVISED,
            actor=actor,
            payload={
                "draft_id": draft_id,
                "term_id": term_id,
                "content": content,
                "content_hash": content_hash,
                "obligation_specs": [s.to_dict() for s in specs],
            },
        )

    def countersign_term(self, party_id: str, draft_id: str, term_id: str, content_hash: str) -> None:
        self._require_party(party_id)
        draft = self.get_draft(draft_id)
        if draft.status != "open":
            raise StateError("草案已关闭，不能再会签")
        term = draft.terms.get(term_id)
        if term is None:
            raise NotFoundError(f"条款不存在：{term_id}")
        now = self._clock.now()
        if now > draft.sign_expires_at:
            raise SignatureError("会签已过期，须由提出方确认后重新发起")
        if content_hash != term.content_hash:
            raise SignatureError("会签针对的内容与最新条款不一致，已按过期会签处理")
        valid = self._valid_signers(draft.signatures[term_id], term.content_hash, draft.sign_expires_at)
        if party_id in valid:
            raise SignatureError(f"{party_id}已对当前内容会签，请勿重复")
        self._emit(
            EventType.TERM_COUNTERSIGNED,
            actor=party_id,
            payload={
                "draft_id": draft_id,
                "term_id": term_id,
                "party_id": party_id,
                "content_hash": content_hash,
            },
        )

    def _missing_term_signers(self, draft: Draft) -> dict:
        missing = {}
        for term_id, term in draft.terms.items():
            valid = self._valid_signers(draft.signatures[term_id], term.content_hash, draft.sign_expires_at)
            lack = sorted(self._all_party_ids() - valid)
            if lack:
                missing[term_id] = lack
        return missing

    def activate_draft(self, actor: str, draft_id: str, effective_from: datetime, batch_ids=()) -> str:
        """草案全部条款会签齐全后激活为带生效期的首个协议版本。"""
        self._require_coordinator(actor)
        draft = self.get_draft(draft_id)
        if draft.status != "open":
            raise StateError("草案已关闭")
        if not draft.terms:
            raise IncompleteError("草案没有任何条款，不能生效")
        missing = self._missing_term_signers(draft)
        if missing:
            raise IncompleteError(f"会签不齐，不能生效：{missing}")
        _require_aware(effective_from, "生效时刻")
        rev = self._state.current_rev + 1
        version_id = f"V-{rev}"
        self._emit(
            EventType.VERSION_ACTIVATED,
            actor=actor,
            payload={
                "version_id": version_id,
                "rev": rev,
                "draft_id": draft_id,
                "effective_from": effective_from.isoformat(),
                "batch_ids": list(batch_ids),
                "terms": [t.to_dict() for t in draft.terms.values()],
            },
        )
        for term in draft.terms.values():
            for spec in term.obligation_specs:
                self._register_obligation(rev, term, spec, via_amendment=None)
        return version_id

    def _register_obligation(self, rev: int, term: Term, spec: ObligationSpec, via_amendment: Optional[str]) -> str:
        obligation_id = self._state.peek_id("obligation", "O")
        self._emit(
            EventType.OBLIGATION_REGISTERED,
            actor=self.coordinator_id,
            payload={
                "obligation_id": obligation_id,
                "version_rev": rev,
                "term_id": term.term_id,
                "category": (spec.category or term.category).value,
                "party_id": spec.responsible_party,
                "batch_id": spec.batch_id,
                "title": spec.title,
                "due_at": spec.due_at.isoformat(),
                "quantity_required": spec.quantity_required,
                "via_amendment": via_amendment,
            },
        )
        return obligation_id

    # ---- 修订案 ----

    def propose_amendment(self, actor: str, base_rev: int, changes, sign_expires_at: datetime) -> str:
        party = self._require_party(actor)
        is_coordinator = actor == self.coordinator_id
        if self._state.current_rev < 1:
            raise StateError("尚无生效协议，请先通过草案建立首个版本")
        if base_rev != self._state.current_rev:
            raise StaleDecisionError(
                f"修订案基于 V{base_rev}，但当前生效版本是 V{self._state.current_rev}"
            )
        _require_aware(sign_expires_at, "会签截止时刻")
        if sign_expires_at <= self._clock.now():
            raise ValidationError("会签截止时刻必须晚于当前时刻")
        base = self._state.versions[-1]
        normalized = []
        for change in changes:
            op = change.get("op")
            if op not in ("add", "modify", "remove"):
                raise ValidationError(f"不支持的变更类型：{op}")
            term_id = change.get("term_id")
            if op in ("modify", "remove"):
                if term_id not in base.terms:
                    raise NotFoundError(f"基线版本中不存在条款：{term_id}")
            category = None
            content = None
            content_hash = None
            specs = ()
            if op in ("add", "modify"):
                category = TermCategory(change["category"])
                # 协调方汇总各方已确认的变更，可跨类别提出；合作方仅限自身权限
                if not is_coordinator and category not in party.permissions:
                    raise PermissionDeniedError(f"{party.name}无权变更「{category.value}」类条款")
                content = change.get("content")
                if not content:
                    raise ValidationError("变更后的条款内容不能为空")
                specs = self._normalize_specs(change.get("obligation_specs"))
                content_hash = term_content_hash(category, content, specs)
            else:
                category = base.terms[term_id].category
                if not is_coordinator and category not in party.permissions:
                    raise PermissionDeniedError(f"{party.name}无权废止「{category.value}」类条款")
            normalized.append(
                AmendmentChange(
                    op=op,
                    term_id=term_id if op != "add" else None,
                    category=category,
                    content=content,
                    content_hash=content_hash,
                    obligation_specs=specs,
                )
            )
        if not normalized:
            raise ValidationError("修订案至少要包含一处变更")
        amendment_id = self._state.peek_id("amendment", "A")
        self._emit(
            EventType.AMENDMENT_PROPOSED,
            actor=actor,
            payload={
                "amendment_id": amendment_id,
                "base_rev": base_rev,
                "changes": [c.to_dict() for c in normalized],
                "changes_hash": amendment_changes_hash(normalized),
                "proposed_by": actor,
                "sign_expires_at": sign_expires_at.isoformat(),
            },
        )
        return amendment_id

    def countersign_amendment(self, party_id: str, amendment_id: str, changes_hash: str) -> None:
        self._require_party(party_id)
        amendment = self.get_amendment(amendment_id)
        if amendment.status != "open":
            raise StateError("修订案已关闭，不能再会签")
        if amendment.base_rev != self._state.current_rev:
            raise StaleDecisionError("修订案已落后于当前生效版本，签署无效")
        now = self._clock.now()
        if now > amendment.sign_expires_at:
            raise SignatureError("会签已过期，须重新发起修订案")
        if changes_hash != amendment.changes_hash:
            raise SignatureError("会签针对的变更内容与最新修订案不一致")
        valid = self._valid_signers(amendment.signatures, amendment.changes_hash, amendment.sign_expires_at)
        if party_id in valid:
            raise SignatureError(f"{party_id}已会签该修订案，请勿重复")
        self._emit(
            EventType.AMENDMENT_COUNTERSIGNED,
            actor=party_id,
            payload={
                "amendment_id": amendment_id,
                "party_id": party_id,
                "changes_hash": changes_hash,
            },
        )

    def enact_amendment(self, actor: str, amendment_id: str, effective_from: datetime, batch_ids=None) -> str:
        """修订案会签齐全后生效为新版本；并发修订中落后的一方在此被拒绝。"""
        self._require_coordinator(actor)
        amendment = self.get_amendment(amendment_id)
        if amendment.status != "open":
            raise StateError("修订案已关闭")
        if amendment.base_rev != self._state.current_rev:
            raise StaleDecisionError(
                f"修订案基于 V{amendment.base_rev}，已有更新的 V{self._state.current_rev} 生效，不能覆盖"
            )
        valid = self._valid_signers(amendment.signatures, amendment.changes_hash, amendment.sign_expires_at)
        missing = sorted(self._all_party_ids() - valid)
        if missing:
            raise IncompleteError(f"修订案会签不齐，缺少：{missing}")
        base = self._state.versions[-1]
        _require_aware(effective_from, "生效时刻")
        if effective_from <= base.effective_from:
            raise ValidationError("新版本生效期必须晚于上一版本")
        new_rev = base.rev + 1
        new_terms = dict(base.terms)
        change_terms = []  # [(AmendmentChange, Term)]，供后续登记新承诺
        add_count = 0
        for change in amendment.changes:
            if change.op == "remove":
                del new_terms[change.term_id]
                continue
            if change.op == "modify":
                term_id = change.term_id
            else:  # add
                add_count += 1
                term_id = self._state.peek_id("term", "T", offset=add_count)
            term = Term(
                term_id=term_id,
                category=change.category,
                content=change.content,
                content_hash=change.content_hash,
                obligation_specs=change.obligation_specs,
                proposed_by=amendment.proposed_by,
            )
            new_terms[term_id] = term
            change_terms.append((change, term))
        version_id = f"V-{new_rev}"
        self._emit(
            EventType.AMENDMENT_ENACTED,
            actor=actor,
            payload={
                "amendment_id": amendment_id,
                "version_id": version_id,
                "new_rev": new_rev,
                "effective_from": effective_from.isoformat(),
                "batch_ids": list(batch_ids) if batch_ids is not None else list(base.batch_ids),
                "terms": [t.to_dict() for t in new_terms.values()],
            },
        )
        # 已执行的承诺不被覆盖；只有尚未启动的承诺才被修订替代。
        changed_term_ids = {c.term_id for c in amendment.changes if c.op in ("modify", "remove")}
        for obligation in list(self._state.obligations.values()):
            if (
                obligation.term_id in changed_term_ids
                and obligation.version_rev == base.rev
                and self._is_untouched(obligation)
            ):
                self._emit(
                    EventType.OBLIGATION_SUPERSEDED,
                    actor=actor,
                    payload={"obligation_id": obligation.obligation_id, "by_amendment": amendment_id},
                )
        for change, term in change_terms:
            for spec in change.obligation_specs:
                self._register_obligation(new_rev, term, spec, via_amendment=amendment_id)
        return version_id

    @staticmethod
    def _is_untouched(obligation: Obligation) -> bool:
        return (
            obligation.status == ObligationStatus.PENDING
            and obligation.quantity_fulfilled == 0
            and not obligation.dispute_open
            and not obligation.evidence
        )

    # ---- 履约登记 ----

    def record_fulfillment(self, actor: str, obligation_id: str, quantity: int,
                           evidence_kind: str, description: str, uri: Optional[str] = None) -> None:
        obligation = self.get_obligation(obligation_id)
        if obligation.status not in (ObligationStatus.PENDING, ObligationStatus.PARTIAL):
            raise StateError(f"当前状态（{obligation.status.value}）不可登记履约")
        if actor != obligation.party_id and actor != self.coordinator_id:
            raise PermissionDeniedError("只有责任方或协调方可以登记履约")
        if quantity < 1:
            raise ValidationError("履约数量必须为正数")
        now = self._clock.now()
        new_total = obligation.quantity_fulfilled + quantity
        if new_total >= obligation.quantity_required:
            new_status = (
                ObligationStatus.FULFILLED if now <= obligation.due_at else ObligationStatus.LATE_FULFILLED
            )
        else:
            new_status = ObligationStatus.PARTIAL
        evidence = self._next_evidence(obligation, evidence_kind, description, uri, actor)
        self._emit(
            EventType.FULFILLMENT_RECORDED,
            actor=actor,
            payload={
                "obligation_id": obligation_id,
                "quantity": quantity,
                "new_total": new_total,
                "new_status": new_status.value,
                "evidence": evidence.to_dict(),
            },
        )

    def raise_dispute(self, actor: str, obligation_id: str, reason: str) -> None:
        obligation = self.get_obligation(obligation_id)
        self._require_party(actor)
        if obligation.status == ObligationStatus.SUPERSEDED:
            raise StateError("已被修订替代的承诺不能再发起争议")
        if obligation.dispute_open:
            raise StateError("该承诺已有未决争议")
        if not reason:
            raise ValidationError("争议理由不能为空")
        evidence = self._next_evidence(obligation, "争议", reason, None, actor)
        self._emit(
            EventType.DISPUTE_RAISED,
            actor=actor,
            payload={
                "obligation_id": obligation_id,
                "party_id": actor,
                "reason": reason,
                "evidence": evidence.to_dict(),
            },
        )

    def resolve_dispute(self, actor: str, obligation_id: str, resolution: str) -> None:
        obligation = self.get_obligation(obligation_id)
        if not obligation.dispute_open:
            raise StateError("该承诺没有未决争议")
        if not resolution:
            raise ValidationError("争议结论不能为空")
        evidence = self._next_evidence(obligation, "争议解决", resolution, None, actor)
        self._emit(
            EventType.DISPUTE_RESOLVED,
            actor=actor,
            payload={
                "obligation_id": obligation_id,
                "resolution": resolution,
                "evidence": evidence.to_dict(),
            },
        )

    def record_substitute(self, actor: str, obligation_id: str, description: str,
                          evidence_kind: str = "替代履行", uri: Optional[str] = None) -> None:
        obligation = self.get_obligation(obligation_id)
        if obligation.status not in (ObligationStatus.PENDING, ObligationStatus.PARTIAL):
            raise StateError(f"当前状态（{obligation.status.value}）不可登记替代履行")
        if obligation.dispute_open:
            raise StateError("争议未决，不能登记替代履行")
        if actor != obligation.party_id and actor != self.coordinator_id:
            raise PermissionDeniedError("只有责任方或协调方可以登记替代履行")
        if not description:
            raise ValidationError("替代履行说明不能为空")
        evidence = self._next_evidence(obligation, evidence_kind, description, uri, actor)
        self._emit(
            EventType.SUBSTITUTE_RECORDED,
            actor=actor,
            payload={
                "obligation_id": obligation_id,
                "description": description,
                "evidence": evidence.to_dict(),
            },
        )

    # ---- 提醒与待确认 ----

    def reminders(self, now: Optional[datetime] = None) -> list:
        """由业务时钟计算的临期提醒与逾期升级，不落库、随时重算。"""
        now = now or self._clock.now()
        _require_aware(now, "当前时刻")
        reminders = []
        for obligation in self._state.obligations.values():
            status = effective_status(obligation, now)
            if status in TERMINAL_STATUSES:
                continue
            if status == ObligationStatus.OVERDUE:
                level = self.policy.escalation_level(now - obligation.due_at)
                reminders.append(
                    Reminder(
                        kind="overdue_escalation",
                        target_id=obligation.obligation_id,
                        message=f"「{obligation.title}」已逾期，升级级别 L{level}",
                        level=level,
                        party_id=obligation.party_id,
                        due_at=obligation.due_at,
                    )
                )
            elif obligation.due_at - now <= self.policy.due_soon_lead:
                reminders.append(
                    Reminder(
                        kind="due_soon",
                        target_id=obligation.obligation_id,
                        message=f"「{obligation.title}」临近履约期限",
                        party_id=obligation.party_id,
                        due_at=obligation.due_at,
                    )
                )
        for item in self.pending_confirmations(now):
            if item["stale"]:
                continue
            expires_at = datetime.fromisoformat(item["expires_at"])
            target = item.get("term_id") or item.get("amendment_id")
            if now > expires_at:
                reminders.append(
                    Reminder(
                        kind="countersign_expired",
                        target_id=target,
                        message=f"会签已过期，仍缺：{'、'.join(item['missing_parties'])}",
                        level=2,
                        due_at=expires_at,
                    )
                )
            elif expires_at - now <= self.policy.signature_lead:
                reminders.append(
                    Reminder(
                        kind="countersign_pending",
                        target_id=target,
                        message=f"会签即将截止，仍缺：{'、'.join(item['missing_parties'])}",
                        due_at=expires_at,
                    )
                )
        return reminders

    def pending_confirmations(self, now: Optional[datetime] = None) -> list:
        """待确认事项：重启后依然存在，供各方继续会签。"""
        pending = []
        for draft in self._state.drafts.values():
            if draft.status != "open":
                continue
            for term_id, lack in self._missing_term_signers(draft).items():
                pending.append(
                    {
                        "kind": "term",
                        "draft_id": draft.draft_id,
                        "term_id": term_id,
                        "missing_parties": lack,
                        "expires_at": draft.sign_expires_at.isoformat(),
                        "stale": False,
                    }
                )
        for amendment in self._state.amendments.values():
            if amendment.status != "open":
                continue
            valid = self._valid_signers(
                amendment.signatures, amendment.changes_hash, amendment.sign_expires_at
            )
            lack = sorted(self._all_party_ids() - valid)
            if lack:
                pending.append(
                    {
                        "kind": "amendment",
                        "amendment_id": amendment.amendment_id,
                        "base_rev": amendment.base_rev,
                        "missing_parties": lack,
                        "expires_at": amendment.sign_expires_at.isoformat(),
                        "stale": amendment.base_rev != self._state.current_rev,
                    }
                )
        return pending
