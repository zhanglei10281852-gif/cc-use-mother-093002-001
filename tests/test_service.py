"""联合培养履约协同:全流程领域与服务测试。"""
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from joint_program import (
    Activity,
    Clause,
    ClauseCategory,
    ClauseDisposition,
    CollaborationService,
    DisputeResolution,
    Evidence,
    FixedClock,
    FulfillmentState,
    Party,
    PartyRole,
    VersionState,
)
from joint_program.aggregate import ConcurrencyError, DomainError
from joint_program.domain import AgreementVersionView
from joint_program.events import compute_hash
from joint_program.http_api import build_handler
from joint_program.store import EventStore

T0 = datetime(2026, 9, 1, 9, 0, 0)
TEACHER_DUE = datetime(2026, 9, 15, 9, 0, 0)


def make_service(clock: FixedClock, path: str) -> CollaborationService:
    service = CollaborationService("A-JP-001", EventStore(path), clock)
    service.register_party(Party("P-GX", "广西高校", PartyRole.COORDINATOR))
    service.register_party(Party("P-A", "东盟甲校", PartyRole.ACADEMIC_PARTNER))
    service.register_party(Party("P-B", "东盟乙校", PartyRole.ACADEMIC_PARTNER))
    service.register_party(Party("P-C", "东盟丙校", PartyRole.ACADEMIC_PARTNER))
    service.register_party(Party("ADMIN", "管理员", PartyRole.ADMIN))
    return service


def base_clauses():
    return [
        Clause("C-QUOTA", ClauseCategory.ENROLLMENT_QUOTA,
               "2026秋季联合班招生名额120人,四方各30人", "P-GX"),
        Clause("C-COURSE", ClauseCategory.COURSE_DELIVERY,
               "东盟甲校负责2门核心中文课程线上交付", "P-A"),
        Clause("C-TEACH", ClauseCategory.TEACHER_DISPATCH,
               "东盟乙校派出2名教师,2026-09-15前到岗", "P-B",
               version_no=1, due_at=TEACHER_DUE),
        Clause("C-RES", ClauseCategory.RESOURCE,
               "东盟丙校提供教学场地与网络资源", "P-C"),
        Clause("C-OWN", ClauseCategory.OUTPUT_OWNERSHIP,
               "联合教研成果四方共有", "P-GX"),
    ]


def sign_all(service: CollaborationService, version_no: int, agrees=True) -> None:
    view = service.aggregate.view(version_no)
    for clause in view.clauses:
        for party_id in sorted(view.signer_party_ids):
            service.sign_clause(version_no, clause.clause_id, party_id, agrees)


class SigningAndVersioningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock(T0)
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "events.jsonl")
        self.service = make_service(self.clock, self.path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_full_lifecycle_first_version_becomes_effective(self) -> None:
        service = self.service
        service.draft_version(None, base_clauses(), "P-GX")
        service.open_for_signature(1, "P-GX")
        self.assertEqual(service.aggregate.view(1).state, VersionState.OPEN)

        sign_all(service, 1)
        service.finalize_version(1)
        view = service.aggregate.view(1)
        self.assertEqual(view.state, VersionState.EFFECTIVE)
        self.assertEqual(view.effective_at, T0)
        # 生效后每条条款都有履约跟踪,且到岗日期已带入。
        self.assertEqual(service.aggregate.fulfillments["C-TEACH"].due_at, TEACHER_DUE)
        self.assertEqual(service.current_version().version_no, 1)

    def test_admin_can_neither_propose_nor_sign(self) -> None:
        service = self.service
        with self.assertRaises(DomainError):
            service.draft_version(None, base_clauses(), "ADMIN")
        service.draft_version(None, base_clauses(), "P-GX")
        service.open_for_signature(1, "P-GX")
        with self.assertRaises(DomainError):
            service.sign_clause(1, "C-QUOTA", "ADMIN", True)

    def test_late_signature_is_rejected_and_version_expires(self) -> None:
        service = self.service
        service.draft_version(None, base_clauses(), "P-GX")
        service.open_for_signature(1, "P-GX")
        # 只有部分会签
        service.sign_clause(1, "C-QUOTA", "P-GX", True)
        self.clock.advance(timedelta(days=8))  # 超过7天会签窗口
        with self.assertRaises(DomainError):
            service.sign_clause(1, "C-COURSE", "P-A", True)
        service.sweep()
        self.assertEqual(service.aggregate.view(1).state, VersionState.EXPIRED)

    def test_declined_version_does_not_block_fresh_draft(self) -> None:
        service = self.service
        service.draft_version(None, base_clauses(), "P-GX")
        service.open_for_signature(1, "P-GX")
        service.sign_clause(1, "C-QUOTA", "P-A", False, comment="名额分配有异议")
        service.finalize_version(1)
        self.assertEqual(service.aggregate.view(1).state, VersionState.DECLINED)
        # 拒签版本不污染新决定:重新起草首版(base=None,而非基于 v1)
        service.draft_version(None, base_clauses(), "P-GX")
        self.assertEqual(service.aggregate.latest_version_no, 2)
        self.assertIsNone(service.aggregate.versions[2].base_version_no)

    def test_duplicate_signature_cannot_overwrite(self) -> None:
        service = self.service
        service.draft_version(None, base_clauses(), "P-GX")
        service.open_for_signature(1, "P-GX")
        service.sign_clause(1, "C-QUOTA", "P-A", True)
        with self.assertRaises(DomainError):
            service.sign_clause(1, "C-QUOTA", "P-A", False)


class AmendmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock(T0)
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "events.jsonl")
        self.service = make_service(self.clock, self.path)
        self.service.draft_version(None, base_clauses(), "P-GX")
        self.service.open_for_signature(1, "P-GX")
        sign_all(self.service, 1)
        self.service.finalize_version(1)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _amendment_clauses(self):
        # v2:名额条款修改、课程条款延续、教师条款延续、资源条款被新条款替代、成果归属新增补充。
        return [
            Clause("C-QUOTA", ClauseCategory.ENROLLMENT_QUOTA,
                   "2026秋季联合班招生名额100人,四方各25人", "P-GX",
                   version_no=2, disposition=ClauseDisposition.MODIFIED),
            Clause("C-COURSE", ClauseCategory.COURSE_DELIVERY,
                   "东盟甲校负责2门核心中文课程线上交付", "P-A",
                   version_no=2, disposition=ClauseDisposition.CARRIED),
            Clause("C-TEACH", ClauseCategory.TEACHER_DISPATCH,
                   "东盟乙校派出2名教师,2026-09-15前到岗", "P-B",
                   version_no=2, disposition=ClauseDisposition.CARRIED, due_at=TEACHER_DUE),
            Clause("C-RES-2", ClauseCategory.RESOURCE,
                   "东盟丙校提供升级后的智慧教室与网络资源", "P-C",
                   version_no=2, disposition=ClauseDisposition.ADDED,
                   supersedes_clause_id="C-RES"),
            Clause("C-OWN", ClauseCategory.OUTPUT_OWNERSHIP,
                   "联合教研成果四方共有", "P-GX",
                   version_no=2, disposition=ClauseDisposition.CARRIED),
        ]

    def test_amendment_requires_full_coverage(self) -> None:
        service = self.service
        partial = self._amendment_clauses()[:3]  # 漏掉 C-RES 与 C-OWN
        with self.assertRaises(DomainError):
            service.draft_version(1, partial, "P-GX")

    def test_concurrent_amendment_is_rejected(self) -> None:
        service = self.service
        service.draft_version(1, self._amendment_clauses(), "P-GX")
        service.open_for_signature(2, "P-GX")
        sign_all(service, 2)
        service.finalize_version(2)
        # 此时最新已生效版本是 v2,仍基于 v1 的修订必须被拒绝。
        with self.assertRaises(ConcurrencyError):
            service.draft_version(1, self._amendment_clauses(), "P-A")

    def test_expired_amendment_does_not_pollute_newer_decision(self) -> None:
        service = self.service
        # v2 修订案开启会签但过期未齐签
        service.draft_version(1, self._amendment_clauses(), "P-GX")
        service.open_for_signature(2, "P-GX")
        self.clock.advance(timedelta(days=8))
        service.sweep()
        self.assertEqual(service.aggregate.view(2).state, VersionState.EXPIRED)
        # v2 未成立:新修订案必须基于 v1,不被 v2 条款污染
        service.draft_version(1, self._amendment_clauses(), "P-A")
        v3 = service.aggregate.view(3)
        self.assertEqual(v3.base_version_no, 1)
        self.assertEqual(v3.state, VersionState.DRAFT)
        # v2 历史原样保留,仍可核验
        self.assertEqual(service.aggregate.view(2).state, VersionState.EXPIRED)

    def test_fulfillment_continues_against_last_effective_after_amendment_expires(self) -> None:
        service = self.service
        service.draft_version(1, self._amendment_clauses(), "P-GX")
        service.open_for_signature(2, "P-GX")
        self.clock.advance(timedelta(days=8))
        service.sweep()
        self.assertEqual(service.aggregate.view(2).state, VersionState.EXPIRED)
        # v2 未成立,现行协议仍是 v1:履约登记与活动挂接应照常工作
        service.register_progress(
            "C-TEACH", 0.5, "P-B",
            [Evidence("EV-X", "到岗登记", "P-B", self.clock.now())],
            due_at=TEACHER_DUE,
        )
        self.assertEqual(
            service.aggregate.fulfillments["C-TEACH"].state, FulfillmentState.PARTIAL
        )
        self.assertEqual(service.current_version().version_no, 1)

    def test_executed_promise_cannot_be_overwritten(self) -> None:
        service = self.service
        # 教师条款已有部分履行(乙校1名教师到岗)
        service.register_progress(
            "C-TEACH", 0.5, "P-B",
            [Evidence("EV-1", "到岗登记表", "P-B", self.clock.now())],
            due_at=TEACHER_DUE,
        )
        # 试图修改已部分履行的教师条款 -> 拒绝
        changed = [
            Clause("C-QUOTA", ClauseCategory.ENROLLMENT_QUOTA,
                   "名额100人", "P-GX", version_no=2,
                   disposition=ClauseDisposition.MODIFIED),
            Clause("C-COURSE", ClauseCategory.COURSE_DELIVERY,
                   "东盟甲校负责2门核心中文课程线上交付", "P-A",
                   version_no=2, disposition=ClauseDisposition.CARRIED),
            Clause("C-TEACH", ClauseCategory.TEACHER_DISPATCH,
                   "东盟乙校仅派1名教师", "P-B", version_no=2,
                   disposition=ClauseDisposition.MODIFIED),
            Clause("C-RES-2", ClauseCategory.RESOURCE,
                   "智慧教室", "P-C", version_no=2,
                   disposition=ClauseDisposition.ADDED, supersedes_clause_id="C-RES"),
            Clause("C-OWN", ClauseCategory.OUTPUT_OWNERSHIP,
                   "联合教研成果四方共有", "P-GX", version_no=2,
                   disposition=ClauseDisposition.CARRIED),
        ]
        with self.assertRaises(DomainError):
            service.draft_version(1, changed, "P-GX")

    def test_amendment_effective_keeps_history_and_reports_impact(self) -> None:
        service = self.service
        service.draft_version(1, self._amendment_clauses(), "P-GX")
        service.open_for_signature(2, "P-GX")
        sign_all(service, 2)
        service.finalize_version(2)

        impact = service.amendment_impact(2)
        self.assertEqual(impact.carried, ("C-COURSE", "C-TEACH", "C-OWN"))
        self.assertEqual(impact.modified, ("C-QUOTA",))
        self.assertEqual(impact.replaced, ("C-RES",))
        self.assertIn("C-RES-2", impact.added)

        # v1 历史仍在且不可变
        v1 = service.aggregate.view(1)
        self.assertEqual(v1.clause("C-QUOTA").text, "2026秋季联合班招生名额120人,四方各30人")
        # 当前生效版本是 v2,旧条款 C-RES 已退出,新条款 C-RES-2 生效
        current = service.current_version()
        self.assertEqual(current.version_no, 2)
        self.assertIsNone(current.clause("C-RES"))
        self.assertIsNotNone(current.clause("C-RES-2"))


class FulfillmentAndNotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock(T0)
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "events.jsonl")
        self.service = make_service(self.clock, self.path)
        self.service.draft_version(None, base_clauses(), "P-GX")
        self.service.open_for_signature(1, "P-GX")
        sign_all(self.service, 1)
        self.service.finalize_version(1)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_progress_requires_evidence_and_tracks_late(self) -> None:
        service = self.service
        with self.assertRaises(DomainError):
            service.register_progress("C-TEACH", 0.5, "P-B", [], due_at=TEACHER_DUE)
        service.register_progress(
            "C-TEACH", 0.5, "P-B",
            [Evidence("EV-1", "到岗登记", "P-B", self.clock.now())],
            due_at=TEACHER_DUE,
        )
        self.assertEqual(
            service.aggregate.fulfillments["C-TEACH"].state, FulfillmentState.PARTIAL
        )
        # 逾期后补报完成 -> LATE_FULFILLED
        self.clock.set(datetime(2026, 9, 20, 9, 0, 0))
        service.register_progress(
            "C-TEACH", 1.0, "P-B",
            [Evidence("EV-2", "第二名教师到岗", "P-B", self.clock.now())],
            due_at=TEACHER_DUE,
        )
        self.assertEqual(
            service.aggregate.fulfillments["C-TEACH"].state,
            FulfillmentState.LATE_FULFILLED,
        )

    def test_progress_cannot_go_backwards(self) -> None:
        service = self.service
        service.register_progress(
            "C-TEACH", 0.5, "P-B",
            [Evidence("EV-1", "到岗登记", "P-B", self.clock.now())],
            due_at=TEACHER_DUE,
        )
        with self.assertRaises(DomainError):
            service.register_progress(
                "C-TEACH", 0.3, "P-B",
                [Evidence("EV-2", "回退", "P-B", self.clock.now())],
                due_at=TEACHER_DUE,
            )

    def test_dispute_upheld_resets_progress_then_substitution_closes(self) -> None:
        service = self.service
        service.register_progress(
            "C-TEACH", 0.5, "P-B",
            [Evidence("EV-1", "到岗登记", "P-B", self.clock.now())],
            due_at=TEACHER_DUE,
        )
        service.raise_dispute(
            "C-TEACH", "P-GX", "登记教师资质不符,到岗不予认可",
            [Evidence("EV-D", "资质核查记录", "P-GX", self.clock.now())],
        )
        ful = service.aggregate.fulfillments["C-TEACH"]
        self.assertEqual(ful.state, FulfillmentState.DISPUTED)
        service.resolve_dispute(
            "C-TEACH", DisputeResolution.UPHELD, "资质不符,原到岗不抵扣",
            [Evidence("EV-R", "院务会决议", "ADMIN", self.clock.now())],
        )
        ful = service.aggregate.fulfillments["C-TEACH"]
        self.assertEqual(ful.state, FulfillmentState.PENDING)
        self.assertEqual(ful.fulfilled_ratio, 0.0)
        # 通过修订新增替代安排条款并替代履行
        alt = [
            Clause("C-QUOTA", ClauseCategory.ENROLLMENT_QUOTA,
                   "2026秋季联合班招生名额120人,四方各30人", "P-GX",
                   version_no=2, disposition=ClauseDisposition.CARRIED),
            Clause("C-COURSE", ClauseCategory.COURSE_DELIVERY,
                   "东盟甲校负责2门核心中文课程线上交付", "P-A",
                   version_no=2, disposition=ClauseDisposition.CARRIED),
            Clause("C-TEACH-ALT", ClauseCategory.TEACHER_DISPATCH,
                   "乙校改派2名符合资质教师,采用混合教学到岗", "P-B",
                   version_no=2, disposition=ClauseDisposition.ADDED,
                   supersedes_clause_id="C-TEACH"),
            Clause("C-RES", ClauseCategory.RESOURCE,
                   "东盟丙校提供教学场地与网络资源", "P-C",
                   version_no=2, disposition=ClauseDisposition.CARRIED),
            Clause("C-OWN", ClauseCategory.OUTPUT_OWNERSHIP,
                   "联合教研成果四方共有", "P-GX",
                   version_no=2, disposition=ClauseDisposition.CARRIED),
        ]
        service.draft_version(1, alt, "P-GX")
        service.open_for_signature(2, "P-GX")
        sign_all(service, 2)
        service.finalize_version(2)
        service.substitute_performance(
            "C-TEACH", "C-TEACH-ALT", "P-GX",
            [Evidence("EV-S", "替代履行确认书", "P-GX", self.clock.now())],
            note="双方确认混合教学方案",
        )
        # 旧条款以替代履行结清
        self.assertEqual(
            service.aggregate.fulfillments["C-TEACH"].state,
            FulfillmentState.SUBSTITUTED,
        )

    def test_reminder_and_escalation_are_clock_driven_and_idempotent(self) -> None:
        service = self.service
        # 距到岗日 3 天内 -> 提醒
        self.clock.set(datetime(2026, 9, 13, 9, 0, 0))
        first = service.sweep()
        notices = [e for e in first if e.event_type == "ObligationNotice"]
        self.assertTrue(any(e.payload["level"] == "REMINDER" for e in notices))
        # 重复扫描不产生重复提醒
        second = service.sweep()
        self.assertFalse(any(e.event_type == "ObligationNotice" for e in second))
        # 逾期 1 天以上 -> 升级
        self.clock.set(datetime(2026, 9, 17, 9, 0, 0))
        third = service.sweep()
        esc = [e for e in third if e.event_type == "ObligationNotice"]
        self.assertTrue(any(e.payload["level"] == "ESCALATION" for e in esc))

    def test_activity_linking_and_outstanding_filter_by_cohort(self) -> None:
        service = self.service
        service.link_activity(Activity(
            "A-ENROLL-1", ClauseCategory.ENROLLMENT_QUOTA, "COHORT-2026F",
            "C-QUOTA", "2026秋季批次学籍注册", self.clock.now(),
        ))
        service.link_activity(Activity(
            "A-TEACH-1", ClauseCategory.TEACHER_DISPATCH, "COHORT-2026F",
            "C-TEACH", "2026秋季批次师资到岗", self.clock.now(), due_at=TEACHER_DUE,
        ))
        items = service.outstanding_obligations(cohort="COHORT-2026F")
        ids = {o.clause_id for o in items}
        self.assertEqual(ids, {"C-QUOTA", "C-TEACH"})
        # 按责任方过滤
        b_only = service.outstanding_obligations(party_id="P-B", cohort="COHORT-2026F")
        self.assertEqual({o.clause_id for o in b_only}, {"C-TEACH"})


class PersistenceAndHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock(T0)
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "events.jsonl")
        self.service = make_service(self.clock, self.path)
        self.service.draft_version(None, base_clauses(), "P-GX")
        self.service.open_for_signature(1, "P-GX")
        sign_all(self.service, 1)
        self.service.finalize_version(1)
        self.service.link_activity(Activity(
            "A-1", ClauseCategory.ENROLLMENT_QUOTA, "COHORT-2026F",
            "C-QUOTA", "秋季批次注册", self.clock.now(),
        ))
        self.service.register_progress(
            "C-TEACH", 0.5, "P-B",
            [Evidence("EV-1", "到岗登记", "P-B", self.clock.now())],
            due_at=TEACHER_DUE,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_state_survives_restart_with_pending_items(self) -> None:
        # 模拟进程重启:新建服务实例,从日志重放。
        restarted = CollaborationService("A-JP-001", EventStore(self.path), self.clock)
        self.assertEqual(restarted.current_version().version_no, 1)
        pending = {o.clause_id for o in restarted.outstanding_obligations()}
        # C-TEACH 部分履行仍为未结清
        self.assertIn("C-TEACH", pending)
        self.assertEqual(
            restarted.aggregate.fulfillments["C-TEACH"].state, FulfillmentState.PARTIAL
        )

    def test_tampered_event_log_is_detected(self) -> None:
        raw = Path(self.path).read_text(encoding="utf-8").splitlines()
        tampered = dict(json.loads(raw[0]))
        tampered["payload"] = dict(tampered["payload"])
        tampered["payload"]["display_name"] = "被篡改名称"
        lines = [json.dumps(tampered, ensure_ascii=False, sort_keys=True)] + raw[1:]
        Path(self.path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            EventStore(self.path).load()

    def test_history_report_as_of_earlier_version(self) -> None:
        service = self.service
        # 9月2日:v1 生效,教师仍未到岗
        report = service.history_report(datetime(2026, 9, 2, 12, 0, 0))
        self.assertEqual(report["applicable_version"]["version_no"], 1)
        outstanding_ids = {o["clause_id"] for o in report["outstanding"]}
        self.assertIn("C-TEACH", outstanding_ids)
        # 当时还没有修订案
        self.assertEqual(report["amendments"], [])
        # 决策记录含哈希链
        self.assertTrue(all("hash" in d and "prev_hash" in d for d in report["decisions"]))
        first = report["decisions"][0]
        self.assertEqual(first["prev_hash"], "0" * 64)

    def test_history_report_shows_amendment_impact_after_v2(self) -> None:
        service = self.service
        alt = [
            Clause("C-QUOTA", ClauseCategory.ENROLLMENT_QUOTA,
                   "名额100人,四方各25人", "P-GX", version_no=2,
                   disposition=ClauseDisposition.MODIFIED),
            Clause("C-COURSE", ClauseCategory.COURSE_DELIVERY,
                   "东盟甲校负责2门核心中文课程线上交付", "P-A",
                   version_no=2, disposition=ClauseDisposition.CARRIED),
            Clause("C-TEACH", ClauseCategory.TEACHER_DISPATCH,
                   "东盟乙校派出2名教师,2026-09-15前到岗", "P-B",
                   version_no=2, disposition=ClauseDisposition.CARRIED, due_at=TEACHER_DUE),
            Clause("C-RES", ClauseCategory.RESOURCE,
                   "东盟丙校提供教学场地与网络资源", "P-C",
                   version_no=2, disposition=ClauseDisposition.CARRIED),
            Clause("C-OWN", ClauseCategory.OUTPUT_OWNERSHIP,
                   "联合教研成果四方共有", "P-GX",
                   version_no=2, disposition=ClauseDisposition.CARRIED),
        ]
        self.clock.set(datetime(2026, 9, 10, 9, 0, 0))
        service.draft_version(1, alt, "P-GX")
        service.open_for_signature(2, "P-GX")
        sign_all(service, 2)
        service.finalize_version(2)

        report = service.history_report(datetime(2026, 9, 11, 9, 0, 0), "COHORT-2026F")
        self.assertEqual(report["applicable_version"]["version_no"], 2)
        self.assertEqual(len(report["amendments"]), 1)
        self.assertEqual(report["amendments"][0]["modified"], ["C-QUOTA"])

        # v2 生效之前的时点看不到修订案
        before = service.history_report(datetime(2026, 9, 9, 9, 0, 0))
        self.assertEqual(before["applicable_version"]["version_no"], 1)
        self.assertEqual(before["amendments"], [])


class HttpApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock(T0)
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "events.jsonl")
        self.service = make_service(self.clock, self.path)
        self.service.draft_version(None, base_clauses(), "P-GX")
        self.service.open_for_signature(1, "P-GX")
        sign_all(self.service, 1)
        self.service.finalize_version(1)

        handler = build_handler(self.path, "A-JP-001", self.clock)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def _get(self, url: str) -> dict:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{url}") as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_endpoints_current_obligations_history_decisions(self) -> None:
        health = self._get("/health")
        self.assertEqual(health["status"], "ok")

        current = self._get("/current")
        self.assertEqual(current["version"]["version_no"], 1)
        self.assertEqual(len(current["version"]["clauses"]), 5)

        obligations = self._get("/obligations?party=P-B")
        self.assertTrue(
            any(o["clause_id"] == "C-TEACH" for o in obligations["outstanding"])
        )

        history = self._get("/history?as_of=2026-09-02T12:00:00")
        self.assertEqual(history["applicable_version"]["version_no"], 1)
        self.assertTrue(history["decisions"])

        decisions = self._get("/decisions")
        self.assertEqual(decisions["count"], len(history["decisions"]) + 0)
        # 哈希链可在响应中逐跳核验
        prev = "0" * 64
        for item in decisions["decisions"]:
            self.assertEqual(item["prev_hash"], prev)
            prev = item["hash"]

    def test_history_requires_as_of(self) -> None:
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/history")
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
