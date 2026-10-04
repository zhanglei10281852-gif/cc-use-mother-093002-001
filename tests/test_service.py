import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from joint_program.domain import ObligationStatus, TermCategory, verify_evidence_chain
from joint_program.errors import (
    IncompleteError,
    PermissionDeniedError,
    SignatureError,
    StaleDecisionError,
    StateError,
    StoreCorruptedError,
)
from joint_program.clock import ManualClock
from joint_program.queries import QueryService
from joint_program.service import CoordinationService
from joint_program.store import EventStore

TZ = timezone.utc
ALL_PARTIES = ("GXU", "VNU", "TU", "KU")


def at(month, day, hour=9):
    return datetime(2026, month, day, hour, tzinfo=TZ)


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store_path = Path(self._tmp.name) / "events.jsonl"
        self.clock = ManualClock(at(9, 1))
        self.service = self._new_service()

    def _new_service(self):
        service = CoordinationService(
            EventStore(self.store_path), self.clock, coordinator_id="GXU"
        )
        return service

    def _register_parties(self, service=None):
        service = service or self.service
        service.register_party(
            "GXU", "广西高校",
            [TermCategory.ENROLLMENT_QUOTA, TermCategory.ACHIEVEMENT_OWNERSHIP, TermCategory.RESOURCE_PROVISION],
        )
        service.register_party("VNU", "越南河内大学", [TermCategory.COURSE_DELIVERY, TermCategory.TEACHER_DISPATCH])
        service.register_party("TU", "泰国清迈大学", [TermCategory.COURSE_DELIVERY])
        service.register_party("KU", "柬埔寨金边大学", [TermCategory.RESOURCE_PROVISION, TermCategory.TEACHER_DISPATCH])
        return service

    def _sign_all(self, service, draft_id, term_ids):
        for term_id in term_ids:
            content_hash = service.get_draft(draft_id).terms[term_id].content_hash
            for party in ALL_PARTIES:
                service.countersign_term(party, draft_id, term_id, content_hash)

    def _activated_service(self):
        """建好四方、激活含两类条款的 V1，返回 (service, term_ids)。"""
        service = self._register_parties()
        draft_id = service.create_draft("GXU", sign_expires_at=at(9, 10))
        t_quota = service.propose_term(
            "GXU", draft_id, TermCategory.ENROLLMENT_QUOTA, "2026-秋批次招生120人",
            obligation_specs=[dict(title="2026-秋招生注册", responsible_party="GXU",
                                   batch_id="2026-秋", due_at=at(9, 10), quantity_required=120)],
        )
        t_teacher = service.propose_term(
            "VNU", draft_id, TermCategory.TEACHER_DISPATCH, "VNU派出2名教师9月8日前到岗",
            obligation_specs=[dict(title="VNU教师到岗", responsible_party="VNU",
                                   batch_id="2026-秋", due_at=at(9, 8), quantity_required=2)],
        )
        self._sign_all(service, draft_id, (t_quota, t_teacher))
        service.activate_draft("GXU", draft_id, effective_from=at(9, 2))
        return service, (t_quota, t_teacher)

    # ---- 会签与版本 ----

    def test_activate_creates_version_and_obligations(self):
        service, (t_quota, t_teacher) = self._activated_service()
        version = service.current_version()
        self.assertEqual(version.rev, 1)
        self.assertEqual(set(version.terms), {t_quota, t_teacher})
        obligations = service.list_obligations(batch_id="2026-秋")
        self.assertEqual(len(obligations), 2)
        by_title = {ob.title: ob for ob in obligations}
        self.assertEqual(by_title["2026-秋招生注册"].term_id, t_quota)
        self.assertEqual(by_title["VNU教师到岗"].party_id, "VNU")
        self.assertEqual(by_title["VNU教师到岗"].status, ObligationStatus.PENDING)

    def test_propose_term_requires_permission(self):
        service = self._register_parties()
        draft_id = service.create_draft("GXU", sign_expires_at=at(9, 10))
        with self.assertRaises(PermissionDeniedError):
            service.propose_term("VNU", draft_id, TermCategory.ENROLLMENT_QUOTA, "越权条款")

    def test_countersign_rejects_mismatched_hash(self):
        service = self._register_parties()
        draft_id = service.create_draft("GXU", sign_expires_at=at(9, 10))
        term_id = service.propose_term("GXU", draft_id, TermCategory.ENROLLMENT_QUOTA, "招生120人")
        with self.assertRaises(SignatureError):
            service.countersign_term("VNU", draft_id, term_id, "0" * 64)

    def test_countersign_rejected_after_expiry(self):
        service = self._register_parties()
        draft_id = service.create_draft("GXU", sign_expires_at=at(9, 3))
        term_id = service.propose_term("GXU", draft_id, TermCategory.ENROLLMENT_QUOTA, "招生120人")
        content_hash = service.get_draft(draft_id).terms[term_id].content_hash
        service.countersign_term("GXU", draft_id, term_id, content_hash)
        self.clock.set(at(9, 4))  # 已过会签截止
        with self.assertRaises(SignatureError):
            service.countersign_term("VNU", draft_id, term_id, content_hash)
        with self.assertRaises(IncompleteError):
            service.activate_draft("GXU", draft_id, effective_from=at(9, 5))

    def test_revised_term_invalidates_previous_signatures(self):
        service = self._register_parties()
        draft_id = service.create_draft("GXU", sign_expires_at=at(9, 10))
        term_id = service.propose_term("GXU", draft_id, TermCategory.ENROLLMENT_QUOTA, "招生120人")
        self._sign_all(service, draft_id, (term_id,))
        service.revise_term("GXU", draft_id, term_id, "招生100人")
        # 旧会签不得污染新决定：激活应失败，按旧哈希再签也应被拒绝
        with self.assertRaises(IncompleteError):
            service.activate_draft("GXU", draft_id, effective_from=at(9, 5))
        old_term_hash = service.get_draft(draft_id).signatures[term_id][0].content_hash
        with self.assertRaises(SignatureError):
            service.countersign_term("GXU", draft_id, term_id, old_term_hash)
        self._sign_all(service, draft_id, (term_id,))
        service.activate_draft("GXU", draft_id, effective_from=at(9, 5))
        self.assertEqual(service.current_version().terms[term_id].content, "招生100人")

    def test_second_draft_rejected_after_activation(self):
        service, _ = self._activated_service()
        with self.assertRaises(StateError):
            service.create_draft("GXU", sign_expires_at=at(9, 20))

    # ---- 修订案 ----

    def _propose_signed_amendment(self, service, actor, changes, base_rev=1):
        amendment_id = service.propose_amendment(actor, base_rev=base_rev, changes=changes,
                                                 sign_expires_at=at(9, 25))
        changes_hash = service.get_amendment(amendment_id).changes_hash
        for party in ALL_PARTIES:
            service.countersign_amendment(party, amendment_id, changes_hash)
        return amendment_id

    def test_amendment_supersedes_only_untouched_obligations(self):
        service, (t_quota, t_teacher) = self._activated_service()
        obligations = {ob.term_id: ob for ob in service.list_obligations()}
        # 招生承诺先部分履行（已执行），师资承诺保持未启动
        service.record_fulfillment("GXU", obligations[t_quota].obligation_id, 80,
                                 "注册记录", "首批80人完成注册")
        amendment_id = self._propose_signed_amendment(service, "GXU", [
            {"op": "modify", "term_id": t_quota, "category": "招生名额", "content": "招生调整为100人",
             "obligation_specs": [dict(title="2026-秋招生补录", responsible_party="GXU",
                                       batch_id="2026-秋", due_at=at(9, 20), quantity_required=20)]},
            {"op": "modify", "term_id": t_teacher, "category": "师资派出", "content": "改为派出1名教师",
             "obligation_specs": [dict(title="VNU教师到岗(调减)", responsible_party="VNU",
                                       batch_id="2026-秋", due_at=at(9, 8), quantity_required=1)]},
        ])
        service.enact_amendment("GXU", amendment_id, effective_from=at(9, 12))
        self.assertEqual(service.current_version().rev, 2)
        # 已部分履行的承诺保留原条款，不被覆盖
        kept = service.get_obligation(obligations[t_quota].obligation_id)
        self.assertEqual(kept.status, ObligationStatus.PARTIAL)
        self.assertEqual(kept.version_rev, 1)
        self.assertEqual(kept.quantity_fulfilled, 80)
        # 未启动的承诺被替代，并按新条款登记新承诺
        replaced = service.get_obligation(obligations[t_teacher].obligation_id)
        self.assertEqual(replaced.status, ObligationStatus.SUPERSEDED)
        self.assertEqual(replaced.superseded_by, amendment_id)
        new_obs = [ob for ob in service.list_obligations() if ob.version_rev == 2]
        self.assertEqual(len(new_obs), 2)
        self.assertTrue(all(ob.via_amendment == amendment_id for ob in new_obs))

    def test_concurrent_amendment_second_enact_rejected(self):
        service, (t_quota, t_teacher) = self._activated_service()
        first = self._propose_signed_amendment(service, "GXU", [
            {"op": "modify", "term_id": t_quota, "category": "招生名额", "content": "招生100人",
             "obligation_specs": []},
        ])
        second = self._propose_signed_amendment(service, "VNU", [
            {"op": "modify", "term_id": t_teacher, "category": "师资派出", "content": "派出1名教师",
             "obligation_specs": []},
        ])
        service.enact_amendment("GXU", first, effective_from=at(9, 12))
        # 并发修订中落后的一方不得覆盖较新的决定
        with self.assertRaises(StaleDecisionError):
            service.enact_amendment("GXU", second, effective_from=at(9, 13))
        with self.assertRaises(StaleDecisionError):
            service.countersign_amendment("KU", second, service.get_amendment(second).changes_hash)
        self.assertEqual(service.current_version().rev, 2)

    def test_amendment_must_target_current_rev(self):
        service, _ = self._activated_service()
        first = self._propose_signed_amendment(service, "GXU", [
            {"op": "add", "category": "成果归属", "content": "联合教研成果归双方共有",
             "obligation_specs": []},
        ])
        service.enact_amendment("GXU", first, effective_from=at(9, 12))
        # 当前已是 V2，仍基于 V1 的提案属于过期决定
        with self.assertRaises(StaleDecisionError):
            service.propose_amendment("GXU", base_rev=1, changes=[
                {"op": "add", "category": "成果归属", "content": "另一处变更", "obligation_specs": []},
            ], sign_expires_at=at(9, 25))

    # ---- 履约状态与证据链 ----

    def test_fulfillment_status_transitions(self):
        service, (t_quota, _) = self._activated_service()
        obligation = next(ob for ob in service.list_obligations() if ob.term_id == t_quota)
        self.clock.set(at(9, 5))
        service.record_fulfillment("GXU", obligation.obligation_id, 80, "注册记录", "首批80人")
        self.assertEqual(service.get_obligation(obligation.obligation_id).status, ObligationStatus.PARTIAL)
        service.record_fulfillment("GXU", obligation.obligation_id, 40, "注册记录", "第二批40人")
        self.assertEqual(service.get_obligation(obligation.obligation_id).status, ObligationStatus.FULFILLED)

    def test_late_fulfillment_marked(self):
        service, (t_quota, _) = self._activated_service()
        obligation = next(ob for ob in service.list_obligations() if ob.term_id == t_quota)
        self.clock.set(at(9, 11))  # 超过 9-10 期限
        service.record_fulfillment("GXU", obligation.obligation_id, 120, "注册记录", "迟到的注册")
        self.assertEqual(service.get_obligation(obligation.obligation_id).status,
                         ObligationStatus.LATE_FULFILLED)

    def test_dispute_flow_and_substitute(self):
        service, (t_quota, t_teacher) = self._activated_service()
        obligations = {ob.term_id: ob for ob in service.list_obligations()}
        quota_ob = obligations[t_quota]
        service.raise_dispute("VNU", quota_ob.obligation_id, "注册名单与约定名额不符")
        self.assertTrue(service.get_obligation(quota_ob.obligation_id).dispute_open)
        with self.assertRaises(StateError):
            service.record_substitute("GXU", quota_ob.obligation_id, "争议未决")
        service.resolve_dispute("GXU", quota_ob.obligation_id, "双方确认以盖章名单为准")
        self.assertFalse(service.get_obligation(quota_ob.obligation_id).dispute_open)
        teacher_ob = obligations[t_teacher]
        service.record_substitute("VNU", teacher_ob.obligation_id, "以在线授课替代1名教师到岗")
        self.assertEqual(service.get_obligation(teacher_ob.obligation_id).status,
                         ObligationStatus.SUBSTITUTED)
        with self.assertRaises(StateError):
            service.record_fulfillment("VNU", teacher_ob.obligation_id, 1, "到岗记录", "补录")

    def test_evidence_chain_is_verifiable(self):
        service, (t_quota, _) = self._activated_service()
        obligation = next(ob for ob in service.list_obligations() if ob.term_id == t_quota)
        service.record_fulfillment("GXU", obligation.obligation_id, 60, "注册记录", "首批60人")
        service.raise_dispute("VNU", obligation.obligation_id, "名单存疑")
        service.resolve_dispute("GXU", obligation.obligation_id, "复核无误")
        stored = service.get_obligation(obligation.obligation_id)
        self.assertTrue(verify_evidence_chain(stored))
        self.assertEqual(len(stored.evidence), 3)
        stored.evidence[1].description = "被篡改"
        self.assertFalse(verify_evidence_chain(stored))

    # ---- 提醒与升级 ----

    def test_reminders_and_escalations_follow_clock(self):
        service, (t_quota, _) = self._activated_service()
        obligation = next(ob for ob in service.list_obligations() if ob.term_id == t_quota)
        self.clock.set(at(9, 5))  # 距期限 5 天，进入 7 天临期窗口
        kinds = {r.kind for r in service.reminders()}
        self.assertIn("due_soon", kinds)
        self.clock.set(at(9, 12))  # 逾期 2 天 → L1
        reminder = next(r for r in service.reminders() if r.target_id == obligation.obligation_id)
        self.assertEqual((reminder.kind, reminder.level), ("overdue_escalation", 1))
        self.clock.set(at(9, 14))  # 逾期 4 天 → L2
        reminder = next(r for r in service.reminders() if r.target_id == obligation.obligation_id)
        self.assertEqual(reminder.level, 2)
        self.clock.set(at(9, 18))  # 逾期 8 天 → L3
        reminder = next(r for r in service.reminders() if r.target_id == obligation.obligation_id)
        self.assertEqual(reminder.level, 3)

    def test_countersign_reminders(self):
        service = self._register_parties()
        draft_id = service.create_draft("GXU", sign_expires_at=at(9, 4))
        service.propose_term("GXU", draft_id, TermCategory.ENROLLMENT_QUOTA, "招生120人")
        self.clock.set(at(9, 2))  # 距截止 2 天，进入 3 天窗口
        self.assertIn("countersign_pending", {r.kind for r in service.reminders()})
        self.clock.set(at(9, 5))  # 已过期
        self.assertIn("countersign_expired", {r.kind for r in service.reminders()})

    # ---- 重启恢复 ----

    def test_restart_preserves_pending_confirmations(self):
        service = self._register_parties()
        draft_id = service.create_draft("GXU", sign_expires_at=at(9, 10))
        term_id = service.propose_term("GXU", draft_id, TermCategory.ENROLLMENT_QUOTA, "招生120人")
        content_hash = service.get_draft(draft_id).terms[term_id].content_hash
        for party in ("GXU", "VNU"):
            service.countersign_term(party, draft_id, term_id, content_hash)
        # 模拟进程重启：同一事件文件重新构建服务
        restarted = self._new_service()
        pending = restarted.pending_confirmations()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["missing_parties"], ["KU", "TU"])
        # 重启后继续会签并生效
        for party in ("TU", "KU"):
            restarted.countersign_term(party, draft_id, term_id, content_hash)
        restarted.activate_draft("GXU", draft_id, effective_from=at(9, 2))
        self.assertEqual(restarted.current_version().rev, 1)

    # ---- 历史时点查询 ----

    def _build_two_versions(self):
        service, (t_quota, t_teacher) = self._activated_service()
        obligations = {ob.term_id: ob for ob in service.list_obligations()}
        self.clock.set(at(9, 5))
        service.record_fulfillment("GXU", obligations[t_quota].obligation_id, 80,
                                   "注册记录", "首批80人")
        amendment_id = self._propose_signed_amendment(service, "GXU", [
            {"op": "modify", "term_id": t_teacher, "category": "师资派出", "content": "改为派出1名教师",
             "obligation_specs": [dict(title="VNU教师到岗(调减)", responsible_party="VNU",
                                       batch_id="2026-秋", due_at=at(9, 8), quantity_required=1)]},
        ])
        self.clock.set(at(9, 11))
        service.enact_amendment("GXU", amendment_id, effective_from=at(9, 12))
        return service, amendment_id, obligations

    def test_agreement_at_historical_time(self):
        self._build_two_versions()
        queries = QueryService(EventStore(self.store_path))
        v1 = queries.agreement_at(at(9, 10), batch_id="2026-秋")
        self.assertEqual(v1["rev"], 1)
        contents = {t["term_id"]: t["content"] for t in v1["terms"]}
        self.assertEqual(contents["T-2"], "VNU派出2名教师9月8日前到岗")
        v2 = queries.agreement_at(at(9, 13), batch_id="2026-秋")
        self.assertEqual(v2["rev"], 2)
        self.assertEqual(v2["source"], "A-1")
        self.assertIsNone(queries.agreement_at(at(8, 31)))

    def test_unfulfilled_at_historical_time(self):
        _, _, obligations = self._build_two_versions()
        queries = QueryService(EventStore(self.store_path))
        early = queries.unfulfilled_at(at(9, 6), batch_id="2026-秋")
        self.assertEqual(set(early), {"GXU", "VNU"})
        gxu_items = early["GXU"]
        self.assertEqual(gxu_items[0]["status"], "部分履行")
        self.assertEqual(gxu_items[0]["quantity_fulfilled"], 80)
        later = queries.unfulfilled_at(at(9, 13), batch_id="2026-秋")
        vnu_items = later["VNU"]
        # 旧的师资承诺已被替代，只剩调减后的新承诺
        self.assertEqual(len(vnu_items), 1)
        self.assertEqual(vnu_items[0]["title"], "VNU教师到岗(调减)")

    def test_amendment_impact_report(self):
        _, amendment_id, obligations = self._build_two_versions()
        queries = QueryService(EventStore(self.store_path))
        impact = queries.amendment_impact(amendment_id)
        self.assertEqual(impact["status"], "enacted")
        self.assertEqual(impact["base_rev"], 1)
        self.assertEqual(impact["new_rev"], 2)
        self.assertEqual(set(impact["signers"]), set(ALL_PARTIES))
        self.assertEqual(len(impact["obligations_superseded"]), 1)
        self.assertEqual(len(impact["obligations_created"]), 1)
        # 已部分履行的招生承诺不在本修订案范围内，不受影响
        self.assertEqual(impact["obligations_preserved"], [])

    def test_decision_records_and_log_verification(self):
        self._build_two_versions()
        queries = QueryService(EventStore(self.store_path))
        records = queries.decision_records(at=at(9, 30), batch_id="2026-秋")
        self.assertGreater(len(records), 10)
        self.assertEqual([r["seq"] for r in records], sorted(r["seq"] for r in records))
        self.assertTrue(all(r["hash"] and r["prev_hash"] for r in records))
        self.assertTrue(queries.verify_log())
        # 只取历史时点之前的记录
        early_records = queries.decision_records(at=at(9, 3))
        self.assertLess(len(early_records), len(records))
        self.assertTrue(all(r["occurred_at"] <= at(9, 3).isoformat() for r in early_records))

    def test_log_tampering_is_detected(self):
        self._build_two_versions()
        queries = QueryService(EventStore(self.store_path))
        self.assertTrue(queries.verify_log())
        lines = self.store_path.read_text(encoding="utf-8").splitlines()
        target = next(i for i, line in enumerate(lines) if "招生" in line)
        lines[target] = lines[target].replace("招生", "篡改", 1)
        self.store_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertFalse(queries.verify_log())
        with self.assertRaises(StoreCorruptedError):
            EventStore(self.store_path)

    def test_batch_filtering_in_queries(self):
        service = self._register_parties()
        draft_id = service.create_draft("GXU", sign_expires_at=at(9, 10))
        term_id = service.propose_term(
            "GXU", draft_id, TermCategory.ENROLLMENT_QUOTA, "两批次招生",
            obligation_specs=[
                dict(title="2026-秋招生", responsible_party="GXU", batch_id="2026-秋",
                     due_at=at(9, 10), quantity_required=120),
                dict(title="2027-春招生", responsible_party="GXU", batch_id="2027-春",
                     due_at=at(9, 10) + timedelta(days=150), quantity_required=80),
            ],
        )
        self._sign_all(service, draft_id, (term_id,))
        service.activate_draft("GXU", draft_id, effective_from=at(9, 2))
        queries = QueryService(EventStore(self.store_path))
        autumn = queries.unfulfilled_at(at(9, 5), batch_id="2026-秋")
        self.assertEqual(len(autumn["GXU"]), 1)
        self.assertEqual(autumn["GXU"][0]["batch_id"], "2026-秋")
        records = queries.decision_records(batch_id="2027-春")
        obligation_events = [r for r in records if "2026-秋招生" in r["summary"]]
        self.assertEqual(obligation_events, [])


if __name__ == "__main__":
    unittest.main()
