"""应用服务:时钟驱动的命令提交、提醒扫描,以及时间点查询。

CollaborationService 把"可控业务时钟 + 只追加事件存储 + 聚合根"接起来:

- 所有命令以 clock.now() 为业务时间,提醒/升级由 sweep() 显式推进;
- 事件先持久化再作用内存;进程重启后从日志重放,待确认事项原样恢复;
- 查询端点按培养批次(cohort)与历史时点(as_of)回放事件,
  返回当时适用的协议、各方未履约项、修订影响与可核验决策记录。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from .aggregate import JointProgramAggregate
from .domain import (
    Activity,
    AgreementVersionView,
    Clause,
    Clock,
    DisputeResolution,
    Evidence,
    Fulfillment,
    FulfillmentState,
    Party,
    VersionState,
)
from .events import Event
from .store import EventStore


def replay(agreement_id: str, events: list[Event],
           as_of: Optional[datetime] = None) -> JointProgramAggregate:
    """重放事件得到聚合状态;给定 as_of 时只应用不晚于该时点的前缀。"""
    aggregate = JointProgramAggregate(agreement_id)
    for event in events:
        if as_of is not None and event.at > as_of:
            break
        aggregate.apply(event)
    return aggregate


@dataclass
class ObligationView:
    clause_id: str
    category: str
    responsible_party_id: str
    text: str
    state: str
    fulfilled_ratio: float
    due_at: Optional[datetime]
    has_open_dispute: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "clause_id": self.clause_id,
            "category": self.category,
            "responsible_party_id": self.responsible_party_id,
            "text": self.text,
            "state": self.state,
            "fulfilled_ratio": self.fulfilled_ratio,
            "due_at": self.due_at.isoformat() if self.due_at else None,
            "has_open_dispute": self.has_open_dispute,
        }


class CollaborationService:
    def __init__(self, agreement_id: str, store: EventStore, clock: Clock) -> None:
        self.agreement_id = agreement_id
        self.store = store
        self.clock = clock
        self.aggregate = replay(agreement_id, store.load())

    # ------------------------------------------------------------- 内部提交

    def _commit(self, events: Event | list[Event]) -> list[Event]:
        if isinstance(events, Event):
            events = [events]
        if not events:
            return []
        # 先封盖连续序号与哈希链,再落盘(fsync),最后更新内存;
        # 崩溃后由重放补齐。
        events = self.aggregate.seal(events)
        self.store.append(events)
        for event in events:
            self.aggregate.apply(event)
        return events

    @property
    def now(self) -> datetime:
        return self.clock.now()

    # ------------------------------------------------------------- 合作方

    def register_party(self, party: Party) -> Event:
        return self._commit(self.aggregate.register_party(party, self.now))[0]

    # ---------------------------------------------------------------- 版本

    def draft_version(
        self, base_version_no: Optional[int], clauses: list[Clause], proposer_id: str,
    ) -> Event:
        return self._commit(
            self.aggregate.draft_version(proposer_id, self.now, base_version_no, clauses)
        )[0]

    def open_for_signature(self, version_no: int, opener_id: str) -> Event:
        return self._commit(
            self.aggregate.open_for_signature(version_no, opener_id, self.now)
        )[0]

    def sign_clause(self, version_no: int, clause_id: str, signer_id: str,
                    agrees: bool, comment: str = "") -> Event:
        return self._commit(self.aggregate.sign_clause(
            version_no, clause_id, signer_id, agrees, self.now, comment
        ))[0]

    def finalize_version(self, version_no: int) -> list[Event]:
        return self._commit(self.aggregate.finalize_version(version_no, self.now))

    def amendment_impact(self, version_no: int):
        return self.aggregate.amendment_impact(version_no)

    # ---------------------------------------------------------------- 履约

    def register_progress(self, clause_id: str, ratio: float, reporter_id: str,
                          evidences: list[Evidence],
                          due_at: Optional[datetime] = None) -> list[Event]:
        return self._commit(self.aggregate.register_progress(
            clause_id, ratio, reporter_id, self.now, evidences, due_at
        ))

    def raise_dispute(self, clause_id: str, raised_by: str, reason: str,
                      evidences: list[Evidence]) -> Event:
        return self._commit(
            self.aggregate.raise_dispute(clause_id, raised_by, reason, self.now, evidences)
        )[0]

    def resolve_dispute(self, clause_id: str, resolution: DisputeResolution,
                        note: str, evidences: Optional[list[Evidence]] = None) -> Event:
        return self._commit(self.aggregate.resolve_dispute(
            clause_id, resolution, self.now, note, evidences
        ))[0]

    def substitute_performance(self, clause_id: str, substitute_clause_id: str,
                               reporter_id: str, evidences: list[Evidence],
                               note: str = "") -> Event:
        return self._commit(self.aggregate.substitute_performance(
            clause_id, substitute_clause_id, reporter_id, self.now, evidences, note
        ))[0]

    def link_activity(self, activity: Activity) -> Event:
        return self._commit(self.aggregate.link_activity(activity, self.now))[0]

    # ------------------------------------------------------- 提醒/升级扫描

    def sweep(self) -> list[Event]:
        """推进业务时钟上的自动化判定:过期版本终结 + 提醒/升级发出。

        可重复调用;结果幂等(已终结不再终结,已通知不再通知)。
        """
        committed: list[Event] = []
        for no, version in list(self.aggregate.versions.items()):
            if version.state == VersionState.OPEN:
                committed.extend(self.finalize_version(no))
        committed.extend(self._commit(self.aggregate.scan_notifications(self.now)))
        return committed

    # ------------------------------------------------------------- 实时查询

    def current_version(self) -> Optional[AgreementVersionView]:
        return self.aggregate.effective_version_at(self.now)

    def outstanding_obligations(
        self, party_id: Optional[str] = None, cohort: Optional[str] = None,
    ) -> list[ObligationView]:
        return _collect_outstanding(self.aggregate, self.now, party_id, cohort)

    # ---------------------------------------------------------- 历史时点查询

    def history_report(self, as_of: datetime, cohort: Optional[str] = None) -> dict[str, Any]:
        """管理员端点:按培养批次 + 历史时点回放,返回当时完整履约态势。

        包含:
        - applicable_version: as_of 时点适用(生效)的协议版本及条款;
        - outstanding: 当时各方未履约项(可按批次过滤);
        - amendments: 当时已生效的各修订案及其影响;
        - decisions: 截至 as_of 的可核验决策记录(事件哈希链)。
        """
        events = self.store.load()  # 载入即完成全链校验
        snapshot = replay(self.agreement_id, events, as_of)

        effective = snapshot.effective_version_at(as_of)
        clause_ids_for_cohort: Optional[set[str]] = None
        if cohort is not None:
            clause_ids_for_cohort = {
                a.clause_id for a in snapshot.activities.values() if a.cohort == cohort
            }

        outstanding = _collect_outstanding(snapshot, as_of, cohort=cohort)

        amendments = []
        for no in sorted(snapshot.versions):
            version = snapshot.versions[no]
            if version.base_version_no is not None and version.effective_at is not None \
                    and version.effective_at <= as_of:
                amendments.append(snapshot.amendment_impact(no).as_dict())

        decisions = [
            {
                "seq": e.seq,
                "event_type": e.event_type,
                "at": e.at.isoformat(),
                "payload": e.payload,
                "prev_hash": e.prev_hash,
                "hash": e.hash,
                "causation_id": e.causation_id,
            }
            for e in events if e.at <= as_of
        ]

        return {
            "agreement_id": self.agreement_id,
            "as_of": as_of.isoformat(),
            "cohort": cohort,
            "applicable_version": _version_dict(effective),
            "outstanding": [o.as_dict() for o in outstanding],
            "amendments": amendments,
            "decisions": decisions,
        }


def _version_dict(view: Optional[AgreementVersionView]) -> Optional[dict[str, Any]]:
    if view is None:
        return None
    return {
        "agreement_id": view.agreement_id,
        "version_no": view.version_no,
        "state": view.state.value,
        "base_version_no": view.base_version_no,
        "effective_at": view.effective_at.isoformat() if view.effective_at else None,
        "sign_deadline": view.sign_deadline.isoformat() if view.sign_deadline else None,
        "clauses": [
            {
                "clause_id": c.clause_id,
                "category": c.category.value,
                "text": c.text,
                "responsible_party_id": c.responsible_party_id,
                "disposition": c.disposition.value,
                "supersedes_clause_id": c.supersedes_clause_id,
            }
            for c in view.clauses
        ],
        "pending_signatures": view.pending_signatures(),
        "signatures": [
            {
                "clause_id": s.clause_id,
                "party_id": s.party_id,
                "agrees": s.agrees,
                "signed_at": s.signed_at.isoformat(),
                "comment": s.comment,
            }
            for s in view.signatures
        ],
    }


def _collect_outstanding(
    aggregate: JointProgramAggregate, as_of: datetime,
    party_id: Optional[str] = None, cohort: Optional[str] = None,
) -> list[ObligationView]:
    effective = aggregate.effective_version_at(as_of)
    if effective is None:
        return []

    cohort_clauses: Optional[set[str]] = None
    if cohort is not None:
        cohort_clauses = {
            a.clause_id for a in aggregate.activities.values() if a.cohort == cohort
        }

    views: list[ObligationView] = []
    for clause in effective.clauses:
        if party_id is not None and clause.responsible_party_id != party_id:
            continue
        if cohort_clauses is not None and clause.clause_id not in cohort_clauses:
            continue
        ful: Optional[Fulfillment] = aggregate.fulfillments.get(clause.clause_id)
        if ful is not None and not ful.is_outstanding:
            continue
        state = ful.state if ful else FulfillmentState.PENDING
        views.append(ObligationView(
            clause_id=clause.clause_id,
            category=clause.category.value,
            responsible_party_id=clause.responsible_party_id,
            text=clause.text,
            state=state.value,
            fulfilled_ratio=ful.fulfilled_ratio if ful else 0.0,
            due_at=ful.due_at if ful else None,
            has_open_dispute=bool(
                ful and ful.dispute
                and ful.dispute.state == DisputeResolution.PENDING
            ),
        ))
    return sorted(views, key=lambda v: (v.due_at or datetime.max, v.clause_id))
