"""命令行冒烟:走通联合培养履约协同的关键路径。

演示:四方会签生效 → 活动挂接与部分履行 → 业务时钟提醒/升级 →
修订案(改名额、替代资源条款) → 争议与替代履行 → 进程重启恢复 →
历史时点查询。全程只追加事件,结果以 JSON 打印。
"""
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from joint_program import (
    Activity,
    Clause,
    ClauseCategory,
    ClauseDisposition,
    CollaborationService,
    DisputeResolution,
    Evidence,
    FixedClock,
    Party,
    PartyRole,
)
from joint_program.store import EventStore

AGREEMENT = "A-JP-2026F"
TEACHER_DUE = datetime(2026, 9, 15, 9, 0, 0)


def banner(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> None:
    clock = FixedClock(datetime(2026, 9, 1, 9, 0, 0))
    tmp = Path(tempfile.mkdtemp(prefix="joint_program_"))
    store_path = tmp / "events.jsonl"

    service = CollaborationService(AGREEMENT, EventStore(store_path), clock)

    # 1) 注册四方 + 管理员
    for party in (
        Party("P-GX", "广西高校", PartyRole.COORDINATOR),
        Party("P-A", "东盟甲校", PartyRole.ACADEMIC_PARTNER),
        Party("P-B", "东盟乙校", PartyRole.ACADEMIC_PARTNER),
        Party("P-C", "东盟丙校", PartyRole.ACADEMIC_PARTNER),
        Party("ADMIN", "联合管理办公室", PartyRole.ADMIN),
    ):
        service.register_party(party)

    # 2) 起草 v1 并逐项会签生效
    service.draft_version(None, [
        Clause("C-QUOTA", ClauseCategory.ENROLLMENT_QUOTA,
               "2026秋季联合班招生名额120人,四方各30人", "P-GX"),
        Clause("C-COURSE", ClauseCategory.COURSE_DELIVERY,
               "东盟甲校负责2门核心中文课程线上交付", "P-A"),
        Clause("C-TEACH", ClauseCategory.TEACHER_DISPATCH,
               "东盟乙校派出2名教师,2026-09-15前到岗", "P-B", due_at=TEACHER_DUE),
        Clause("C-RES", ClauseCategory.RESOURCE,
               "东盟丙校提供教学场地与网络资源", "P-C"),
        Clause("C-OWN", ClauseCategory.OUTPUT_OWNERSHIP,
               "联合教研成果四方共有", "P-GX"),
    ], "P-GX")
    service.open_for_signature(1, "P-GX")
    view = service.aggregate.view(1)
    for clause in view.clauses:
        for party_id in sorted(view.signer_party_ids):
            service.sign_clause(1, clause.clause_id, party_id, True)
    service.finalize_version(1)
    banner("v1 生效")
    print(json.dumps({
        "version": service.current_version().version_no,
        "state": service.current_version().state.value,
        "effective_at": service.current_version().effective_at.isoformat(),
    }, ensure_ascii=False))

    # 3) 招生批次与师资活动挂接到具体条款
    service.link_activity(Activity(
        "A-ENROLL", ClauseCategory.ENROLLMENT_QUOTA, "COHORT-2026F",
        "C-QUOTA", "2026秋季批次学籍注册", clock.now(),
    ))
    service.link_activity(Activity(
        "A-TEACH", ClauseCategory.TEACHER_DISPATCH, "COHORT-2026F",
        "C-TEACH", "2026秋季批次师资到岗", clock.now(), due_at=TEACHER_DUE,
    ))

    # 4) 乙校1名教师到岗(部分履行),距到期 2 天触发提醒
    clock.set(datetime(2026, 9, 13, 9, 0, 0))
    service.register_progress(
        "C-TEACH", 0.5, "P-B",
        [Evidence("EV-1", "第一名教师到岗登记表", "P-B", clock.now())],
        due_at=TEACHER_DUE,
    )
    reminders = service.sweep()
    banner("业务时钟推进到 2026-09-13:提醒")
    print(json.dumps([
        {"level": e.payload["level"], "message": e.payload["message"]}
        for e in reminders if e.event_type == "ObligationNotice"
    ], ensure_ascii=False))

    # 5) 修订案 v2:名额调整、资源条款被新条款替代,其余延续
    clock.set(datetime(2026, 9, 14, 9, 0, 0))
    service.draft_version(1, [
        Clause("C-QUOTA", ClauseCategory.ENROLLMENT_QUOTA,
               "2026秋季联合班招生名额100人,四方各25人", "P-GX",
               disposition=ClauseDisposition.MODIFIED),
        Clause("C-COURSE", ClauseCategory.COURSE_DELIVERY,
               "东盟甲校负责2门核心中文课程线上交付", "P-A",
               disposition=ClauseDisposition.CARRIED),
        Clause("C-TEACH", ClauseCategory.TEACHER_DISPATCH,
               "东盟乙校派出2名教师,2026-09-15前到岗", "P-B",
               disposition=ClauseDisposition.CARRIED, due_at=TEACHER_DUE),
        Clause("C-RES-2", ClauseCategory.RESOURCE,
               "东盟丙校提供升级后的智慧教室与网络资源", "P-C",
               disposition=ClauseDisposition.ADDED, supersedes_clause_id="C-RES"),
        Clause("C-OWN", ClauseCategory.OUTPUT_OWNERSHIP,
               "联合教研成果四方共有", "P-GX",
               disposition=ClauseDisposition.CARRIED),
    ], "P-GX")
    service.open_for_signature(2, "P-GX")
    view = service.aggregate.view(2)
    for clause in view.clauses:
        for party_id in sorted(view.signer_party_ids):
            service.sign_clause(2, clause.clause_id, party_id, True)
    service.finalize_version(2)
    banner("v2 修订生效:影响面")
    print(json.dumps(service.amendment_impact(2).as_dict(), ensure_ascii=False))

    # 6) 乙校逾期未补齐第二名教师:升级
    clock.set(datetime(2026, 9, 17, 9, 0, 0))
    escalations = service.sweep()
    banner("业务时钟推进到 2026-09-17:逾期升级")
    print(json.dumps([
        {"level": e.payload["level"], "message": e.payload["message"]}
        for e in escalations if e.event_type == "ObligationNotice"
    ], ensure_ascii=False))

    # 7) 对到岗提出争议,成立后以替代履行结清
    service.raise_dispute(
        "C-TEACH", "P-GX", "登记教师资质不符,到岗不予认可",
        [Evidence("EV-D", "资质核查记录", "P-GX", clock.now())],
    )
    service.resolve_dispute(
        "C-TEACH", DisputeResolution.UPHELD, "资质不符,原到岗不抵扣",
        [Evidence("EV-R", "联合管委会决议", "ADMIN", clock.now())],
    )
    service.draft_version(2, [
        Clause("C-QUOTA", ClauseCategory.ENROLLMENT_QUOTA,
               "2026秋季联合班招生名额100人,四方各25人", "P-GX",
               disposition=ClauseDisposition.CARRIED),
        Clause("C-COURSE", ClauseCategory.COURSE_DELIVERY,
               "东盟甲校负责2门核心中文课程线上交付", "P-A",
               disposition=ClauseDisposition.CARRIED),
        Clause("C-TEACH-ALT", ClauseCategory.TEACHER_DISPATCH,
               "乙校改派2名符合资质教师,采用混合教学到岗", "P-B",
               disposition=ClauseDisposition.ADDED, supersedes_clause_id="C-TEACH"),
        Clause("C-RES-2", ClauseCategory.RESOURCE,
               "东盟丙校提供升级后的智慧教室与网络资源", "P-C",
               disposition=ClauseDisposition.CARRIED),
        Clause("C-OWN", ClauseCategory.OUTPUT_OWNERSHIP,
               "联合教研成果四方共有", "P-GX",
               disposition=ClauseDisposition.CARRIED),
    ], "P-GX")
    service.open_for_signature(3, "P-GX")
    view = service.aggregate.view(3)
    for clause in view.clauses:
        for party_id in sorted(view.signer_party_ids):
            service.sign_clause(3, clause.clause_id, party_id, True)
    service.finalize_version(3)
    service.substitute_performance(
        "C-TEACH", "C-TEACH-ALT", "P-GX",
        [Evidence("EV-S", "替代履行确认书", "P-GX", clock.now())],
        note="双方确认混合教学方案",
    )
    banner("争议成立后替代履行")
    print(json.dumps({
        "C-TEACH": service.aggregate.fulfillments["C-TEACH"].state.value,
        "evidence_count": len(service.aggregate.fulfillments["C-TEACH"].evidences),
    }, ensure_ascii=False))

    # 8) 进程重启:新实例从日志重放,待确认事项仍在
    restarted = CollaborationService(AGREEMENT, EventStore(store_path), clock)
    banner("进程重启后:批次未履约项")
    print(json.dumps([
        o.as_dict() for o in restarted.outstanding_obligations(cohort="COHORT-2026F")
    ], ensure_ascii=False, default=str))

    # 9) 历史时点查询:v2 生效之前与之后
    banner("历史时点查询 2026-09-12(v1 适用,尚无修订案)")
    report = restarted.history_report(datetime(2026, 9, 12, 9, 0, 0))
    print(json.dumps({
        "applicable_version": report["applicable_version"]["version_no"],
        "amendments": report["amendments"],
        "decision_count": len(report["decisions"]),
        "chain_head": report["decisions"][-1]["hash"][:12],
    }, ensure_ascii=False))

    banner("事件日志路径(可独立核验哈希链)")
    print(str(store_path))


if __name__ == "__main__":
    main()
