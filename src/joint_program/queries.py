"""管理员查询端点：按培养批次与历史时点折叠事件流，还原当时适用的状态。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from .domain import (
    UNFULFILLED_STATUSES,
    Obligation,
    TermCategory,
    Version,
    effective_status,
    verify_evidence_chain,
)
from .events import EventType
from .service import State
from .store import EventStore


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _obligation_view(obligation: Obligation, at: datetime) -> dict:
    return {
        "obligation_id": obligation.obligation_id,
        "title": obligation.title,
        "category": obligation.category.value,
        "party_id": obligation.party_id,
        "batch_id": obligation.batch_id,
        "term_id": obligation.term_id,
        "version_rev": obligation.version_rev,
        "due_at": obligation.due_at.isoformat(),
        "status": effective_status(obligation, at).value,
        "quantity_fulfilled": obligation.quantity_fulfilled,
        "quantity_required": obligation.quantity_required,
        "dispute_reason": obligation.dispute_reason if obligation.dispute_open else None,
        "via_amendment": obligation.via_amendment,
        "superseded_by": obligation.superseded_by,
    }


def _version_view(version: Version) -> dict:
    return {
        "version_id": version.version_id,
        "rev": version.rev,
        "effective_from": version.effective_from.isoformat(),
        "batch_ids": list(version.batch_ids),
        "source": version.source,
        "terms": [
            {
                "term_id": t.term_id,
                "category": t.category.value,
                "content": t.content,
                "content_hash": t.content_hash,
            }
            for t in version.terms.values()
        ],
    }


class QueryService:
    """只读查询端点：不修改任何状态，可任意重放。"""

    def __init__(self, store: EventStore) -> None:
        self._store = store

    def _state_at(self, at: datetime) -> State:
        """折叠截至某历史时点的全部事件，得到当时已知的状态。"""
        state = State()
        for record in self._store.read_all():
            if _dt(record["occurred_at"]) <= at:
                state.apply(record)
        return state

    def agreement_at(self, at: datetime, batch_id: Optional[str] = None) -> Optional[dict]:
        """当时适用的协议版本：生效期已过且面向该培养批次的最新版本。"""
        state = self._state_at(at)
        candidates = [
            v
            for v in state.versions
            if v.effective_from <= at and (not batch_id or not v.batch_ids or batch_id in v.batch_ids)
        ]
        if not candidates:
            return None
        applicable = max(candidates, key=lambda v: (v.effective_from, v.rev))
        return _version_view(applicable)

    def unfulfilled_at(
        self,
        at: datetime,
        party_id: Optional[str] = None,
        batch_id: Optional[str] = None,
    ) -> dict:
        """各方在该时点的未履约项（待履行、部分履行、逾期、争议中）。"""
        state = self._state_at(at)
        result: dict[str, list] = {}
        for obligation in state.obligations.values():
            if party_id and obligation.party_id != party_id:
                continue
            if batch_id and obligation.batch_id != batch_id:
                continue
            if effective_status(obligation, at) not in UNFULFILLED_STATUSES:
                continue
            result.setdefault(obligation.party_id, []).append(_obligation_view(obligation, at))
        for items in result.values():
            items.sort(key=lambda item: item["due_at"])
        return result

    def amendment_impact(self, amendment_id: str) -> dict:
        """修订影响：变更了哪些条款、替代了哪些承诺、保留了哪些已执行承诺。"""
        events = self._store.read_all()
        proposed = next(
            (e for e in events if e["event_type"] == EventType.AMENDMENT_PROPOSED
             and e["payload"]["amendment_id"] == amendment_id),
            None,
        )
        if proposed is None:
            from .errors import NotFoundError

            raise NotFoundError(f"修订案不存在：{amendment_id}")
        enacted = next(
            (e for e in events if e["event_type"] == EventType.AMENDMENT_ENACTED
             and e["payload"]["amendment_id"] == amendment_id),
            None,
        )
        changes = proposed["payload"]["changes"]
        changed_term_ids = {c["term_id"] for c in changes if c["op"] in ("modify", "remove")}
        superseded = [
            e["payload"]["obligation_id"]
            for e in events
            if e["event_type"] == EventType.OBLIGATION_SUPERSEDED
            and e["payload"]["by_amendment"] == amendment_id
        ]
        created = [
            e["payload"]["obligation_id"]
            for e in events
            if e["event_type"] == EventType.OBLIGATION_REGISTERED
            and e["payload"].get("via_amendment") == amendment_id
        ]
        state = State()
        for record in events:
            state.apply(record)
        base_rev = proposed["payload"]["base_rev"]
        preserved = [
            obligation.obligation_id
            for obligation in state.obligations.values()
            if obligation.term_id in changed_term_ids
            and obligation.version_rev == base_rev
            and obligation.obligation_id not in superseded
        ]
        return {
            "amendment_id": amendment_id,
            "status": "enacted" if enacted else "open",
            "base_rev": base_rev,
            "new_rev": enacted["payload"]["new_rev"] if enacted else None,
            "effective_from": enacted["payload"]["effective_from"] if enacted else None,
            "proposed_by": proposed["payload"]["proposed_by"],
            "signers": [
                e["payload"]["party_id"]
                for e in events
                if e["event_type"] == EventType.AMENDMENT_COUNTERSIGNED
                and e["payload"]["amendment_id"] == amendment_id
            ],
            "changes": changes,
            "obligations_superseded": superseded,
            "obligations_created": created,
            "obligations_preserved": preserved,
        }

    def decision_records(self, at: Optional[datetime] = None, batch_id: Optional[str] = None) -> list:
        """可核验的决策记录：带哈希链的事件流水，可按时点与培养批次过滤。"""
        records = []
        state = State()
        for record in self._store.read_all():
            state.apply(record)
            occurred_at = _dt(record["occurred_at"])
            if at is not None and occurred_at > at:
                continue
            if batch_id and not self._concerns_batch(record, state, batch_id):
                continue
            records.append(
                {
                    "seq": record["seq"],
                    "hash": record["hash"],
                    "prev_hash": record["prev_hash"],
                    "event_type": record["event_type"],
                    "actor": record["actor"],
                    "occurred_at": record["occurred_at"],
                    "summary": _summarize(record),
                }
            )
        return records

    @staticmethod
    def _concerns_batch(record: dict, state: State, batch_id: str) -> bool:
        event_type = record["event_type"]
        payload = record["payload"]
        if event_type == EventType.OBLIGATION_REGISTERED:
            return payload["batch_id"] == batch_id
        if event_type in (
            EventType.OBLIGATION_SUPERSEDED,
            EventType.FULFILLMENT_RECORDED,
            EventType.DISPUTE_RAISED,
            EventType.DISPUTE_RESOLVED,
            EventType.SUBSTITUTE_RECORDED,
        ):
            obligation = state.obligations.get(payload["obligation_id"])
            return obligation is not None and obligation.batch_id == batch_id
        if event_type in (EventType.VERSION_ACTIVATED, EventType.AMENDMENT_ENACTED):
            return not payload["batch_ids"] or batch_id in payload["batch_ids"]
        return True  # 治理类事件面向整个项目

    def obligation_evidence(self, obligation_id: str) -> dict:
        """某条承诺的证据链及链上校验结果。"""
        state = State()
        for record in self._store.read_all():
            state.apply(record)
        obligation = state.obligations.get(obligation_id)
        if obligation is None:
            from .errors import NotFoundError

            raise NotFoundError(f"履约承诺不存在：{obligation_id}")
        return {
            "obligation_id": obligation_id,
            "chain_valid": verify_evidence_chain(obligation),
            "evidence": [item.to_dict() for item in obligation.evidence],
        }

    def verify_log(self) -> bool:
        """重新读取磁盘上的事件文件并校验整条哈希链。"""
        return self._store.verify()


def _summarize(record: dict) -> str:
    event_type = record["event_type"]
    payload = record["payload"]
    if event_type == EventType.PARTY_REGISTERED:
        return f"注册合作方 {payload['name']}（{payload['party_id']}）"
    if event_type == EventType.DRAFT_CREATED:
        return f"创建协议草案 {payload['draft_id']}"
    if event_type == EventType.TERM_PROPOSED:
        return f"提出条款 {payload['term_id']}（{payload['category']}）"
    if event_type == EventType.TERM_REVISED:
        return f"修订草案条款 {payload['term_id']}，旧会签失效"
    if event_type == EventType.TERM_COUNTERSIGNED:
        return f"{payload['party_id']} 会签条款 {payload['term_id']}"
    if event_type == EventType.VERSION_ACTIVATED:
        return f"协议版本 {payload['version_id']} 生效（{payload['effective_from']}）"
    if event_type == EventType.AMENDMENT_PROPOSED:
        return f"提出修订案 {payload['amendment_id']}（基于 V{payload['base_rev']}）"
    if event_type == EventType.AMENDMENT_COUNTERSIGNED:
        return f"{payload['party_id']} 会签修订案 {payload['amendment_id']}"
    if event_type == EventType.AMENDMENT_ENACTED:
        return f"修订案 {payload['amendment_id']} 生效，形成 {payload['version_id']}"
    if event_type == EventType.OBLIGATION_REGISTERED:
        return f"登记履约承诺 {payload['obligation_id']}（{payload['title']}）"
    if event_type == EventType.OBLIGATION_SUPERSEDED:
        return f"承诺 {payload['obligation_id']} 被修订案 {payload['by_amendment']} 替代"
    if event_type == EventType.FULFILLMENT_RECORDED:
        return f"承诺 {payload['obligation_id']} 登记履约 {payload['quantity']} 项（{payload['new_status']}）"
    if event_type == EventType.DISPUTE_RAISED:
        return f"承诺 {payload['obligation_id']} 发生争议：{payload['reason']}"
    if event_type == EventType.DISPUTE_RESOLVED:
        return f"承诺 {payload['obligation_id']} 争议解决：{payload['resolution']}"
    if event_type == EventType.SUBSTITUTE_RECORDED:
        return f"承诺 {payload['obligation_id']} 替代履行：{payload['description']}"
    return event_type
