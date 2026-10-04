"""协议聚合根:所有业务规则的唯一守门人。

JointProgramAggregate 是纯函数式的状态机:它持有从事件日志重放得到的
内存状态,每次命令要么被拒绝(抛领域错误),要么产生一条或多条事件。
它不负责持久化,也不直接读系统时钟——时钟以参数注入,保证:

- 过期会签、并发修订等判定完全由可控业务时钟决定;
- 同一份事件日志在任何时刻重放,得到相同状态。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from .domain import (
    Activity,
    AgreementVersionView,
    AmendmentImpact,
    Clause,
    ClauseCategory,
    ClauseDisposition,
    Clock,
    ConcurrencyError,
    Dispute,
    DisputeResolution,
    DomainError,
    Evidence,
    Fulfillment,
    FulfillmentState,
    Party,
    PartyRole,
    Signature,
    VersionState,
    ensure_executed_clause_change_allowed,
)
from .events import GENESIS_HASH, Event, compute_hash

# 默认会签窗口与履约提醒/升级节奏,均以业务时钟度量。
DEFAULT_SIGN_WINDOW = timedelta(days=7)
DEFAULT_REMINDER_BEFORE_DUE = timedelta(days=3)
DEFAULT_ESCALATION_AFTER_DUE = timedelta(days=1)


@dataclass
class _Version:
    state: VersionState = VersionState.DRAFT
    clauses: dict[str, Clause] = field(default_factory=dict)
    signatures: list[Signature] = field(default_factory=list)
    opened_at: Optional[datetime] = None
    effective_at: Optional[datetime] = None
    sign_deadline: Optional[datetime] = None
    base_version_no: Optional[int] = None

    def view(self, agreement_id: str, version_no: int) -> AgreementVersionView:
        return AgreementVersionView(
            agreement_id=agreement_id,
            version_no=version_no,
            state=self.state,
            clauses=tuple(self.clauses.values()),
            signatures=tuple(self.signatures),
            opened_at=self.opened_at,
            effective_at=self.effective_at,
            sign_deadline=self.sign_deadline,
            base_version_no=self.base_version_no,
        )


def _dump_dt(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _load_dt(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def _dump_evidence(ev: Evidence) -> dict:
    return {
        "evidence_ref": ev.evidence_ref,
        "title": ev.title,
        "submitted_by": ev.submitted_by,
        "submitted_at": ev.submitted_at.isoformat(),
        "detail": ev.detail,
    }


def _load_evidence(data: dict) -> Evidence:
    return Evidence(
        evidence_ref=data["evidence_ref"],
        title=data["title"],
        submitted_by=data["submitted_by"],
        submitted_at=datetime.fromisoformat(data["submitted_at"]),
        detail=data.get("detail", ""),
    )


class JointProgramAggregate:
    def __init__(self, agreement_id: str) -> None:
        self.agreement_id = agreement_id
        self.parties: dict[str, Party] = {}
        self.versions: dict[int, _Version] = {}
        self.clause_versions: dict[str, list[int]] = {}   # clause_id -> 含该条款的版本号(顺序)
        self.activities: dict[str, Activity] = {}
        self.fulfillments: dict[str, Fulfillment] = {}    # clause_id -> 最新条款的履约状态
        self.notified_keys: set[str] = set()             # 已发送提醒/升级的去重键(含级别)
        self.sequence = 0
        self.latest_version_no = 0
        self._last_hash: Optional[str] = None

    # ------------------------------------------------------------------ 基础

    # ------------------------------------------------------------------ 基础

    @property
    def signer_party_ids(self) -> frozenset[str]:
        return frozenset(
            p.party_id for p in self.parties.values() if p.can_propose
        )

    def require_party(self, party_id: str) -> Party:
        party = self.parties.get(party_id)
        if party is None:
            raise DomainError(f"合作方不存在: {party_id}")
        return party

    def require_version(self, version_no: int) -> _Version:
        version = self.versions.get(version_no)
        if version is None:
            raise DomainError(f"协议版本不存在: v{version_no}")
        return version

    def latest_version(self) -> _Version:
        if self.latest_version_no == 0:
            raise DomainError("协议尚无任何版本")
        return self.versions[self.latest_version_no]

    def latest_effective_version(self) -> Optional[tuple[int, "_Version"]]:
        """编号最大的已生效版本(未成立的修订案不影响现行协议)。"""
        candidate: Optional[int] = None
        for no, version in self.versions.items():
            if version.state == VersionState.EFFECTIVE and (
                candidate is None or no > candidate
            ):
                candidate = no
        return (candidate, self.versions[candidate]) if candidate is not None else None

    def effective_version_at(self, as_of: datetime) -> Optional[AgreementVersionView]:
        """返回 as_of 时点适用(已生效)的最新版本。"""
        candidate: Optional[int] = None
        for no, version in self.versions.items():
            if version.effective_at is not None and version.effective_at <= as_of:
                if candidate is None or no > candidate:
                    candidate = no
        if candidate is None:
            return None
        return self.versions[candidate].view(self.agreement_id, candidate)

    # ------------------------------------------------------------------ 事件

    def make_event(self, event_type: str, payload: dict, at: datetime,
                   causation_id: Optional[str] = None) -> Event:
        """构造一条"未封口"事件。序号与哈希链在提交(seal)时统一分配,

        保证同一命令产生的多条事件连续且彼此链接。
        """
        return Event(0, event_type, at, payload, "", "", causation_id)

    def seal(self, events: list[Event]) -> list[Event]:
        """为一批未封口事件分配连续序号与前向哈希链。"""
        seq = self.sequence
        prev = self._last_hash if self._last_hash is not None else GENESIS_HASH
        sealed: list[Event] = []
        for event in events:
            if event.seq != 0 or event.hash != "":
                raise DomainError("事件已封口,不能重复封盖")
            seq += 1
            digest = compute_hash(seq, event.event_type, event.at, event.payload, prev)
            sealed.append(Event(
                seq, event.event_type, event.at, event.payload, prev, digest,
                event.causation_id,
            ))
            prev = digest
        return sealed

    def apply(self, event: Event) -> None:
        """将一条已持久化的事件作用到内存状态(重放与提交共用)。"""
        expected_seq = self.sequence + 1
        expected_prev = self._last_hash if self._last_hash is not None else GENESIS_HASH
        if event.seq != expected_seq:
            raise DomainError(f"事件序号不连续: 期望 {expected_seq}, 实际 {event.seq}")
        if event.prev_hash != expected_prev:
            raise DomainError("事件哈希链断裂,日志可能被篡改或丢失")
        digest = compute_hash(
            event.seq, event.event_type, event.at, event.payload, event.prev_hash
        )
        if digest != event.hash:
            raise DomainError(f"事件内容校验失败: seq={event.seq}")
        handler = getattr(self, f"_apply_{event.event_type}")
        handler(event)
        self.sequence = event.seq
        self._last_hash = event.hash

    # ------------------------------------------------------------- 命令:合作方

    def register_party(self, party: Party, now: datetime) -> Event:
        if party.party_id in self.parties:
            raise DomainError(f"合作方已注册: {party.party_id}")
        return self.make_event("PartyRegistered", {
            "party_id": party.party_id,
            "display_name": party.display_name,
            "role": party.role.value,
        }, now)

    def _apply_PartyRegistered(self, event: Event) -> None:
        p = event.payload
        self.parties[p["party_id"]] = Party(
            party_id=p["party_id"],
            display_name=p["display_name"],
            role=PartyRole(p["role"]),
        )

    # --------------------------------------------------------- 命令:起草与提案

    def draft_version(
        self,
        proposer_id: str,
        now: datetime,
        base_version_no: Optional[int] = None,
        clauses: Optional[list[Clause]] = None,
    ) -> Event:
        """起草新版本。base_version_no 为空表示首版;否则为修订案。

        修订案必须携带对基础版本每一条款的处置说明。
        """
        proposer = self.require_party(proposer_id)
        if not proposer.can_propose:
            raise DomainError("管理员不能提出协议条款")

        new_no = self.latest_version_no + 1
        if self.latest_version_no == 0:
            if base_version_no is not None:
                raise DomainError("协议尚无版本,首版不能声明基础版本")
            base: Optional[_Version] = None
        else:
            latest = self.latest_version()
            if latest.state in (VersionState.EXPIRED, VersionState.DECLINED):
                # 过期/拒签版本不污染较新决定:新草案回到它所基于的版本重新起草。
                if base_version_no != latest.base_version_no:
                    predecessor = latest.base_version_no
                    raise DomainError(
                        f"最近版本 v{self.latest_version_no} 未成立"
                        f"({'尚无基础版本' if predecessor is None else f'基础版本为 v{predecessor}'}),"
                        "请基于其基础版本重新起草"
                    )
                base = self.versions[base_version_no] if base_version_no else None
            elif base_version_no is None:
                raise DomainError("协议已有版本,后续变更必须显式基于最新版本")
            elif base_version_no != self.latest_version_no:
                raise ConcurrencyError(
                    f"并发修订冲突: 基础版本 v{base_version_no} 已不是最新(v{self.latest_version_no})"
                )
            else:
                base = self.require_version(base_version_no)
                if base.state != VersionState.EFFECTIVE:
                    raise DomainError("只能对已生效版本提交修订案")

        clause_items = clauses or []
        if not clause_items:
            raise DomainError("协议版本至少需要一个条款")

        clause_payload = []
        seen: set[str] = set()
        for clause in clause_items:
            if clause.clause_id in seen:
                raise DomainError(f"条款重复: {clause.clause_id}")
            seen.add(clause.clause_id)
            self.require_party(clause.responsible_party_id)
            if not proposer.can_propose_category(clause.category):
                raise DomainError(f"角色无权提出 {clause.category.value} 类条款")
            if base is not None:
                self._validate_revision_clause(base, clause, new_no)
            else:
                clause.version_no = new_no
                if clause.disposition != ClauseDisposition.ADDED:
                    raise DomainError("首版条款只能以新增(ADDED)方式提出")
            clause_payload.append(self._dump_clause(clause))

        if base is not None:
            self._validate_revision_coverage(base, clause_items)

        return self.make_event("VersionDrafted", {
            "version_no": new_no,
            "proposer_id": proposer_id,
            "base_version_no": base_version_no,
            "clauses": clause_payload,
        }, now)

    def _validate_revision_clause(self, base: _Version, clause: Clause, new_no: int) -> None:
        disposition = clause.disposition
        clause.version_no = new_no
        if disposition == ClauseDisposition.ADDED:
            if clause.clause_id in base.clauses:
                raise DomainError(
                    f"条款 {clause.clause_id} 已存在,应为 CARRIED/MODIFIED,或改用新编号"
                )
            if clause.supersedes_clause_id is not None:
                superseded = base.clauses.get(clause.supersedes_clause_id)
                if superseded is None:
                    raise DomainError(
                        f"替代目标 {clause.supersedes_clause_id} 不在基础版本中"
                    )
                # 替代旧条款同样不得覆盖已执行承诺。
                ensure_executed_clause_change_allowed(
                    ClauseDisposition.REPLACED,
                    self.fulfillments.get(clause.supersedes_clause_id),
                )
        elif disposition == ClauseDisposition.CARRIED:
            original = base.clauses.get(clause.clause_id)
            if original is None:
                raise DomainError(f"CARRIED 条款 {clause.clause_id} 在基础版本中不存在")
            # 延续条款文本与到期日必须与原文一致,承诺不被悄然改写。
            if clause.text != original.text:
                raise DomainError(f"CARRIED 条款 {clause.clause_id} 文本不得变更")
            if clause.due_at != original.due_at:
                raise DomainError(f"CARRIED 条款 {clause.clause_id} 到期日不得变更")
            if clause.supersedes_clause_id is not None:
                raise DomainError("CARRIED 条款不能再替代其他条款")
        elif disposition == ClauseDisposition.MODIFIED:
            original = base.clauses.get(clause.clause_id)
            if original is None:
                raise DomainError(f"MODIFIED 条款 {clause.clause_id} 在基础版本中不存在")
            if clause.text == original.text:
                raise DomainError(
                    f"MODIFIED 条款 {clause.clause_id} 文本无变化,应标记为 CARRIED"
                )
            ensure_executed_clause_change_allowed(
                ClauseDisposition.MODIFIED, self.fulfillments.get(clause.clause_id)
            )
            if clause.supersedes_clause_id is not None:
                raise DomainError("MODIFIED 条款沿用原编号,不能声明替代目标")
        else:  # pragma: no cover - REPLACED 不由输入直接使用
            raise DomainError(
                "替代旧条款请以新增条款(ADDED)并填写 supersedes_clause_id 的方式提出"
            )

    def _validate_revision_coverage(self, base: _Version, items: list[Clause]) -> None:
        """基础版本的每一条款都必须被显式处置:延续、修改或被替代。"""
        by_id = {c.clause_id: c for c in items}
        superseded_ids = {
            c.supersedes_clause_id
            for c in items
            if c.disposition == ClauseDisposition.ADDED and c.supersedes_clause_id
        }
        if len(superseded_ids) != len([
            c for c in items
            if c.disposition == ClauseDisposition.ADDED and c.supersedes_clause_id
        ]):
            raise DomainError("同一旧条款不能被多个新条款同时替代")
        for base_clause in base.clauses.values():
            cid = base_clause.clause_id
            if cid in by_id:
                if by_id[cid].disposition not in (
                    ClauseDisposition.CARRIED, ClauseDisposition.MODIFIED
                ):
                    raise DomainError(
                        f"基础版本条款 {cid} 只能标记为 CARRIED 或 MODIFIED"
                    )
            elif cid in superseded_ids:
                continue  # 由新增的替代条款承载,原编号退出新版本
            else:
                raise DomainError(
                    f"修订案遗漏基础版本条款 {cid},必须显式 CARRIED/MODIFIED 或用新条款替代"
                )

    def _dump_clause(self, clause: Clause) -> dict:
        return {
            "clause_id": clause.clause_id,
            "category": clause.category.value,
            "text": clause.text,
            "responsible_party_id": clause.responsible_party_id,
            "version_no": clause.version_no,
            "disposition": clause.disposition.value,
            "supersedes_clause_id": clause.supersedes_clause_id,
            "due_at": _dump_dt(clause.due_at),
        }

    def _load_clause(self, data: dict) -> Clause:
        return Clause(
            clause_id=data["clause_id"],
            category=ClauseCategory(data["category"]),
            text=data["text"],
            responsible_party_id=data["responsible_party_id"],
            version_no=data["version_no"],
            disposition=ClauseDisposition(data.get("disposition", ClauseDisposition.ADDED.value)),
            supersedes_clause_id=data.get("supersedes_clause_id"),
            due_at=_load_dt(data.get("due_at")),
        )

    def _apply_VersionDrafted(self, event: Event) -> None:
        p = event.payload
        no = p["version_no"]
        version = _Version(
            state=VersionState.DRAFT,
            base_version_no=p.get("base_version_no"),
        )
        for data in p["clauses"]:
            clause = self._load_clause(data)
            version.clauses[clause.clause_id] = clause
            self.clause_versions.setdefault(clause.clause_id, []).append(no)
        self.versions[no] = version
        self.latest_version_no = no

    # ------------------------------------------------------------- 命令:会签

    def open_for_signature(
        self, version_no: int, opener_id: str, now: datetime,
        window: timedelta = DEFAULT_SIGN_WINDOW,
    ) -> Event:
        self.require_party(opener_id)
        version = self.require_version(version_no)
        if version.state != VersionState.DRAFT:
            raise DomainError(f"v{version_no} 已开启会签或已终结")
        # 会签方为全部有提案权的合作方,对本版本逐项确认。
        if not self.signer_party_ids:
            raise DomainError("尚无合作方,无法开启会签")
        if not version.clauses:
            raise DomainError("空版本不能开启会签")
        deadline = now + window
        return self.make_event("VersionOpened", {
            "version_no": version_no,
            "opener_id": opener_id,
            "deadline": deadline.isoformat(),
        }, now)

    def _apply_VersionOpened(self, event: Event) -> None:
        p = event.payload
        version = self.versions[p["version_no"]]
        version.state = VersionState.OPEN
        version.opened_at = event.at
        version.sign_deadline = datetime.fromisoformat(p["deadline"])

    def sign_clause(
        self, version_no: int, clause_id: str, signer_id: str,
        agrees: bool, now: datetime, comment: str = "",
    ) -> Event:
        signer = self.require_party(signer_id)
        if not signer.can_propose:
            raise DomainError("管理员不参与会签")
        version = self.require_version(version_no)
        if version.state != VersionState.OPEN:
            raise DomainError(f"v{version_no} 不在会签中(当前 {version.state.value})")
        if now > version.sign_deadline:  # type: ignore[operator]
            raise DomainError("会签窗口已过期,过期会签不予接受")
        if clause_id not in version.clauses:
            raise DomainError(f"条款不在 v{version_no} 中: {clause_id}")
        existing = next(
            (s for s in version.signatures
             if s.clause_id == clause_id and s.party_id == signer_id),
            None,
        )
        if existing is not None:
            raise DomainError("同一合作方对同一条款只能会签一次,不得覆盖已确认意见")
        return self.make_event("ClauseSigned", {
            "version_no": version_no,
            "clause_id": clause_id,
            "signer_id": signer_id,
            "agrees": agrees,
            "comment": comment,
        }, now)

    def _apply_ClauseSigned(self, event: Event) -> None:
        p = event.payload
        version = self.versions[p["version_no"]]
        version.signatures.append(Signature(
            clause_id=p["clause_id"],
            party_id=p["signer_id"],
            signed_at=event.at,
            agrees=p["agrees"],
            comment=p.get("comment", ""),
        ))

    def finalize_version(self, version_no: int, now: datetime) -> list[Event]:
        """根据当前时间与会签情况判定版本:齐签生效、拒签终结、过期终结。"""
        version = self.require_version(version_no)
        events: list[Event] = []
        if version.state == VersionState.DRAFT:
            raise DomainError(f"v{version_no} 尚未开启会签")
        if version.state in (VersionState.EFFECTIVE, VersionState.EXPIRED, VersionState.DECLINED):
            return events

        view = version.view(self.agreement_id, version_no)
        if any(not s.agrees for s in version.signatures):
            events.append(self.make_event("VersionDeclined", {
                "version_no": version_no,
                "reason": "存在拒签条款",
            }, now))
            return events
        if now > version.sign_deadline:  # type: ignore[operator]
            events.append(self.make_event("VersionExpired", {
                "version_no": version_no,
                "deadline": version.sign_deadline.isoformat(),  # type: ignore[union-attr]
            }, now))
            return events
        if view.is_fully_signed():
            events.append(self.make_event("VersionEffective", {
                "version_no": version_no,
            }, now))
            # 生效时为每条条款建立履约跟踪;延续/修改条款沿用历史状态,
            # 仅同步修改后的到期日。
            for clause in version.clauses.values():
                existing = self.fulfillments.get(clause.clause_id)
                if existing is None:
                    events.append(self.make_event("FulfillmentOpened", {
                        "version_no": version_no,
                        "clause_id": clause.clause_id,
                        "due_at": _dump_dt(clause.due_at),
                    }, now))
                elif clause.due_at is not None and clause.due_at != existing.due_at:
                    events.append(self.make_event("FulfillmentDueAdjusted", {
                        "version_no": version_no,
                        "clause_id": clause.clause_id,
                        "due_at": _dump_dt(clause.due_at),
                    }, now))
        return events

    def _apply_VersionEffective(self, event: Event) -> None:
        version = self.versions[event.payload["version_no"]]
        version.state = VersionState.EFFECTIVE
        version.effective_at = event.at

    def _apply_VersionExpired(self, event: Event) -> None:
        self.versions[event.payload["version_no"]].state = VersionState.EXPIRED

    def _apply_VersionDeclined(self, event: Event) -> None:
        self.versions[event.payload["version_no"]].state = VersionState.DECLINED

    # ------------------------------------------------------------- 命令:履约

    def _require_live_clause(self, clause_id: str) -> tuple[Clause, int]:
        live = self.latest_effective_version()
        if live is None:
            raise DomainError("协议尚未生效,不能登记履约")
        version_no, version = live
        clause = version.clauses.get(clause_id)
        if clause is None:
            raise DomainError(f"条款 {clause_id} 不在当前生效版本中")
        return clause, version_no

    def _evidence_payloads(self, evidences: list[Evidence]) -> list[dict]:
        if not evidences:
            raise DomainError("履约状态迁移必须携带证据")
        return [_dump_evidence(e) for e in evidences]

    def register_progress(
        self, clause_id: str, ratio: float, reporter_id: str, now: datetime,
        evidences: list[Evidence], due_at: Optional[datetime] = None,
    ) -> list[Event]:
        """登记部分/全部履行。ratio==1 为履行完毕,小于1为部分履行;
        超过截止日的履行自动进入 LATE* 状态。"""
        self.require_party(reporter_id)
        clause, version_no = self._require_live_clause(clause_id)
        if not 0.0 < ratio <= 1.0:
            raise DomainError("履行比例必须在 (0, 1] 区间")
        current = self.fulfillments.get(clause_id)
        if current is not None and current.state in (
            FulfillmentState.FULFILLED, FulfillmentState.SUBSTITUTED
        ):
            raise DomainError(f"条款 {clause_id} 已履行终结,不能重复登记")
        if current is not None and ratio < current.fulfilled_ratio:
            raise DomainError("履行进度只能前进,不能回退覆盖")

        events = [self.make_event("FulfillmentProgressed", {
            "version_no": version_no,
            "clause_id": clause_id,
            "ratio": ratio,
            "reporter_id": reporter_id,
            "due_at": _dump_dt(due_at),
            "evidences": self._evidence_payloads(evidences),
        }, now)]
        return events

    def _apply_FulfillmentOpened(self, event: Event) -> None:
        clause_id = event.payload["clause_id"]
        self.fulfillments[clause_id] = Fulfillment(
            clause_id=clause_id,
            due_at=_load_dt(event.payload.get("due_at")),
        )

    def _apply_FulfillmentDueAdjusted(self, event: Event) -> None:
        ful = self.fulfillments[event.payload["clause_id"]]
        ful.due_at = _load_dt(event.payload.get("due_at"))

    def _apply_FulfillmentProgressed(self, event: Event) -> None:
        p = event.payload
        clause_id = p["clause_id"]
        ful = self.fulfillments.setdefault(clause_id, Fulfillment(clause_id=clause_id))
        ratio = p["ratio"]
        due = _load_dt(p.get("due_at")) or ful.due_at
        complete = ratio >= 1.0
        late = due is not None and event.at > due
        if complete:
            ful.state = FulfillmentState.LATE_FULFILLED if late else FulfillmentState.FULFILLED
        else:
            ful.state = FulfillmentState.LATE_PARTIAL if late else FulfillmentState.PARTIAL
        ful.fulfilled_ratio = ratio
        ful.due_at = due
        ful.last_progress_at = event.at
        ful.evidences.extend(_load_evidence(e) for e in p["evidences"])

    def raise_dispute(
        self, clause_id: str, raised_by: str, reason: str, now: datetime,
        evidences: list[Evidence],
    ) -> Event:
        self.require_party(raised_by)
        self._require_live_clause(clause_id)
        current = self.fulfillments.get(clause_id)
        if current is None:
            raise DomainError("条款尚无履约记录,无可争议事项")
        if current.state == FulfillmentState.FULFILLED:
            raise DomainError("已完全履行的条款不能再提起争议")
        return self.make_event("DisputeRaised", {
            "clause_id": clause_id,
            "raised_by": raised_by,
            "reason": reason,
            "evidences": self._evidence_payloads(evidences),
        }, now)

    def _apply_DisputeRaised(self, event: Event) -> None:
        p = event.payload
        ful = self.fulfillments[p["clause_id"]]
        ful.state = FulfillmentState.DISPUTED
        ful.dispute = Dispute(
            raised_by=p["raised_by"],
            raised_at=event.at,
            reason=p["reason"],
            evidences=[_load_evidence(e) for e in p["evidences"]],
        )

    def resolve_dispute(
        self, clause_id: str, resolution: DisputeResolution, now: datetime,
        note: str, evidences: Optional[list[Evidence]] = None,
    ) -> Event:
        if resolution == DisputeResolution.PENDING:
            raise DomainError("争议解决结果不能是 PENDING")
        ful = self.fulfillments.get(clause_id)
        if ful is None or ful.dispute is None:
            raise DomainError(f"条款 {clause_id} 无未决争议")
        if ful.dispute.state != DisputeResolution.PENDING:
            raise DomainError("争议已裁决,不能重复处理")
        return self.make_event("DisputeResolved", {
            "clause_id": clause_id,
            "resolution": resolution.value,
            "note": note,
            "evidences": [_dump_evidence(e) for e in (evidences or [])],
        }, now)

    def _apply_DisputeResolved(self, event: Event) -> None:
        p = event.payload
        ful = self.fulfillments[p["clause_id"]]
        dispute = ful.dispute
        assert dispute is not None
        resolution = DisputeResolution(p["resolution"])
        dispute.state = resolution
        dispute.resolved_at = event.at
        dispute.resolution_note = p["note"]
        dispute.evidences.extend(_load_evidence(e) for e in p.get("evidences", []))
        if resolution == DisputeResolution.REJECTED:
            # 争议驳回:回到与既有进度相符的状态。
            ful.state = (
                FulfillmentState.FULFILLED
                if ful.fulfilled_ratio >= 1.0
                else (FulfillmentState.LATE_PARTIAL
                      if ful.due_at and event.at > ful.due_at
                      else FulfillmentState.PARTIAL)
            )
        else:
            # 争议成立:已报进度不抵扣,回到待履行,等待补正或替代履行。
            # 原证据仍保留在证据链中,可供核验。
            ful.fulfilled_ratio = 0.0
            ful.state = FulfillmentState.PENDING

    def substitute_performance(
        self, clause_id: str, substitute_clause_id: str, reporter_id: str,
        now: datetime, evidences: list[Evidence], note: str = "",
    ) -> Event:
        """替代履行:原条款不按原方式履行,以双方确认的替代安排结清。

        原条款可能已被修订替代而退出当前版本,此时按替代条款的
        supersedes_clause_id 反向定位,旧承诺与替代安排一并结清。
        """
        self.require_party(reporter_id)
        live = self.latest_effective_version()
        if live is None:
            raise DomainError("协议尚未生效,不能登记替代履行")
        live_no, latest = live

        old_id: Optional[str] = None
        if clause_id in latest.clauses:
            old_id = clause_id
        else:
            replaced_by = next(
                (c.clause_id for c in latest.clauses.values()
                 if c.supersedes_clause_id == clause_id),
                None,
            )
            if replaced_by is not None:
                old_id = clause_id

        if old_id is None:
            raise DomainError(
                f"条款 {clause_id} 既不在当前生效版本中,也未被任何条款替代"
            )
        if substitute_clause_id not in latest.clauses:
            raise DomainError("替代安排条款不在当前生效版本中")
        substitute = latest.clauses[substitute_clause_id]
        if substitute.supersedes_clause_id not in (None, old_id):
            raise DomainError(
                f"替代条款 {substitute_clause_id} 针对的是其他旧条款"
            )

        ful = self.fulfillments.get(old_id)
        if ful is None:
            raise DomainError("条款尚无履约记录,不能替代履行")
        if ful.state == FulfillmentState.SUBSTITUTED:
            raise DomainError("条款已替代履行终结")
        return self.make_event("PerformanceSubstituted", {
            "clause_id": old_id,
            "substitute_clause_id": substitute_clause_id,
            "reporter_id": reporter_id,
            "note": note,
            "evidences": self._evidence_payloads(evidences),
        }, now)

    def _apply_PerformanceSubstituted(self, event: Event) -> None:
        p = event.payload
        old_id = p["clause_id"]
        new_id = p["substitute_clause_id"]
        ful = self.fulfillments[old_id]
        ful.state = FulfillmentState.SUBSTITUTED
        ful.fulfilled_ratio = 1.0
        ful.last_progress_at = event.at
        ful.evidences.extend(_load_evidence(e) for e in p["evidences"])
        # 替代安排本身视为已履行完毕,证据同源。
        new_ful = self.fulfillments.setdefault(new_id, Fulfillment(clause_id=new_id))
        if new_ful.state != FulfillmentState.SUBSTITUTED:
            new_ful.state = FulfillmentState.FULFILLED
            new_ful.fulfilled_ratio = 1.0
            new_ful.last_progress_at = event.at
            new_ful.evidences.extend(_load_evidence(e) for e in p["evidences"])

    # --------------------------------------------------------- 命令:活动挂接

    def link_activity(self, activity: Activity, now: datetime) -> Event:
        self._require_live_clause(activity.clause_id)
        if activity.activity_id in self.activities:
            raise DomainError(f"履约活动已存在: {activity.activity_id}")
        return self.make_event("ActivityLinked", {
            "activity_id": activity.activity_id,
            "kind": activity.kind.value,
            "cohort": activity.cohort,
            "clause_id": activity.clause_id,
            "title": activity.title,
            "created_at": activity.created_at.isoformat(),
            "due_at": _dump_dt(activity.due_at),
        }, now)

    def _apply_ActivityLinked(self, event: Event) -> None:
        p = event.payload
        self.activities[p["activity_id"]] = Activity(
            activity_id=p["activity_id"],
            kind=ClauseCategory(p["kind"]),
            cohort=p["cohort"],
            clause_id=p["clause_id"],
            title=p["title"],
            created_at=datetime.fromisoformat(p["created_at"]),
            due_at=_load_dt(p.get("due_at")),
        )

    # --------------------------------------------------------- 提醒与升级

    def scan_notifications(self, now: datetime) -> list[Event]:
        """按业务时钟扫描应触发的提醒(REMINDER)与升级(ESCALATION)。

        重复扫描是幂等的:每个 (对象, 级别) 只产生一次通知;
        状态终结(生效/履行/替代/争议裁决)后不再通知。
        """
        events: list[Event] = []

        # 会签窗口临近
        for no, version in self.versions.items():
            if version.state != VersionState.OPEN or version.sign_deadline is None:
                continue
            remaining = version.sign_deadline - now
            if timedelta(0) < remaining <= DEFAULT_REMINDER_BEFORE_DUE:
                events.append(self._notice(
                    f"sign:v{no}", no, None, "SIGNATURE", "REMINDER",
                    f"协议 v{no} 会签即将于 {version.sign_deadline.isoformat()} 截止",
                    now, version.sign_deadline,
                ))

        # 条款履约:临期提醒、逾期升级、争议升级(只扫描当前生效版本)
        live = self.latest_effective_version()
        if live is not None:
            live_no, live_version = live
            for clause in live_version.clauses.values():
                ful = self.fulfillments.get(clause.clause_id)
                if ful is None:
                    continue
                if ful.state in (
                    FulfillmentState.FULFILLED,
                    FulfillmentState.LATE_FULFILLED,
                    FulfillmentState.SUBSTITUTED,
                ):
                    continue
                if ful.state == FulfillmentState.DISPUTED:
                    events.append(self._notice(
                        f"dispute:{clause.clause_id}", live_no,
                        clause.clause_id, "DISPUTE", "ESCALATION",
                        f"条款 {clause.clause_id} 存在未裁决争议,需升级处理",
                        now, None,
                    ))
                    continue
                due = ful.due_at
                if due is None:
                    continue
                if now <= due and due - now <= DEFAULT_REMINDER_BEFORE_DUE:
                    events.append(self._notice(
                        f"due:{clause.clause_id}", live_no,
                        clause.clause_id, "PERFORMANCE", "REMINDER",
                        f"条款 {clause.clause_id} 将于 {due.isoformat()} 到期",
                        now, due,
                    ))
                elif now > due and now - due >= DEFAULT_ESCALATION_AFTER_DUE:
                    events.append(self._notice(
                        f"due:{clause.clause_id}", live_no,
                        clause.clause_id, "PERFORMANCE", "ESCALATION",
                        f"条款 {clause.clause_id} 已逾期未结清(截止 {due.isoformat()})",
                        now, due,
                    ))
        return [e for e in events if e is not None]

    def _notice(self, dedup_key: str, version_no: int, clause_id: Optional[str],
                notice_type: str, level: str, message: str,
                now: datetime, deadline: Optional[datetime]) -> Optional[Event]:
        full_key = f"{dedup_key}:{level}"
        if full_key in self.notified_keys:
            return None
        return self.make_event("ObligationNotice", {
            "dedup_key": full_key,
            "version_no": version_no,
            "clause_id": clause_id,
            "notice_type": notice_type,
            "level": level,
            "message": message,
            "deadline": _dump_dt(deadline),
        }, now, causation_id=full_key)

    def _apply_ObligationNotice(self, event: Event) -> None:
        self.notified_keys.add(event.payload["dedup_key"])

    # ------------------------------------------------------------------ 视图

    def view(self, version_no: int) -> AgreementVersionView:
        return self.require_version(version_no).view(self.agreement_id, version_no)

    def clause_genealogy(self, clause_id: str) -> list[dict]:
        """返回条款跨版本谱系:该编号出现的每一版,以及替代它/它替代的条款。"""
        result: list[dict] = []
        for no in self.clause_versions.get(clause_id, []):
            version = self.versions[no]
            clause = version.clauses.get(clause_id)
            result.append({
                "version_no": no,
                "state": version.state.value,
                "disposition": clause.disposition.value if clause else None,
                "supersedes_clause_id": clause.supersedes_clause_id if clause else None,
            })
        # 反向找出"替代了本条款"的新条款
        for no, version in self.versions.items():
            for clause in version.clauses.values():
                if clause.supersedes_clause_id == clause_id:
                    result.append({
                        "version_no": no,
                        "state": version.state.value,
                        "replaced_by": clause.clause_id,
                    })
        result.sort(key=lambda item: item["version_no"])
        return result

    def amendment_impact(self, version_no: int) -> AmendmentImpact:
        version = self.require_version(version_no)
        if version.base_version_no is None:
            raise DomainError(f"v{version_no} 是首版,不是修订案")
        carried, modified, added = [], [], []
        replaced: list[str] = []
        for clause in version.clauses.values():
            if clause.disposition == ClauseDisposition.CARRIED:
                carried.append(clause.clause_id)
            elif clause.disposition == ClauseDisposition.MODIFIED:
                modified.append(clause.clause_id)
            else:
                added.append(clause.clause_id)
                if clause.supersedes_clause_id:
                    replaced.append(clause.supersedes_clause_id)
        return AmendmentImpact(
            new_version_no=version_no,
            base_version_no=version.base_version_no,
            carried=tuple(carried),
            modified=tuple(modified),
            replaced=tuple(sorted(replaced)),
            added=tuple(added),
        )
