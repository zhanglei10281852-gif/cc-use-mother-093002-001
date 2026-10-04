"""联合培养履约协同服务命令行冒烟演示。

模拟开课前两周发现四方合作文件口径不一致后的完整处置流程：
建草案、逐项会签、激活带生效期的版本、登记履约（部分/迟交/争议/替代）、
修订案推进变更、并发修订拦截、重启恢复待确认事项、管理员历史查询。
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from joint_program import (
    CoordinationService,
    EventStore,
    ManualClock,
    QueryService,
    StaleDecisionError,
    TermCategory,
)

TZ = timezone.utc
DATA_FILE = Path(__file__).parent / "data" / "demo-events.jsonl"
PARTIES = ("GXU", "VNU", "TU", "KU")


def at(month, day, hour=9):
    return datetime(2026, month, day, hour, tzinfo=TZ)


def show(step, data):
    print(json.dumps({"step": step, **data}, ensure_ascii=False, default=str))


def sign_all_terms(service, draft_id):
    for term_id, term in service.get_draft(draft_id).terms.items():
        for party in PARTIES:
            service.countersign_term(party, draft_id, term_id, term.content_hash)


def sign_all_amendment(service, amendment_id):
    changes_hash = service.get_amendment(amendment_id).changes_hash
    for party in PARTIES:
        service.countersign_amendment(party, amendment_id, changes_hash)


def build_service(clock):
    service = CoordinationService(EventStore(DATA_FILE), clock, coordinator_id="GXU")
    return service


def main():
    if DATA_FILE.exists():
        DATA_FILE.unlink()
    clock = ManualClock(at(9, 1))  # 开课前两周
    service = build_service(clock)

    # 1. 注册四方：广西高校 + 三所东盟院校，各自限定可提出的条款类别
    service.register_party("GXU", "广西高校", [TermCategory.ENROLLMENT_QUOTA,
                                               TermCategory.ACHIEVEMENT_OWNERSHIP,
                                               TermCategory.RESOURCE_PROVISION])
    service.register_party("VNU", "越南河内大学", [TermCategory.COURSE_DELIVERY, TermCategory.TEACHER_DISPATCH])
    service.register_party("TU", "泰国清迈大学", [TermCategory.COURSE_DELIVERY])
    service.register_party("KU", "柬埔寨金边大学", [TermCategory.RESOURCE_PROVISION, TermCategory.TEACHER_DISPATCH])

    # 2. 各方在权限内提出条款，统一成一个草案
    draft = service.create_draft("GXU", sign_expires_at=at(9, 10))
    service.propose_term("GXU", draft, TermCategory.ENROLLMENT_QUOTA, "2026-秋批次招生120人",
                         obligation_specs=[dict(title="2026-秋招生注册", responsible_party="GXU",
                                                batch_id="2026-秋", due_at=at(9, 10), quantity_required=120)])
    service.propose_term("VNU", draft, TermCategory.COURSE_DELIVERY, "VNU交付汉语精读32课时",
                         obligation_specs=[dict(title="汉语精读课程交付", responsible_party="VNU",
                                                batch_id="2026-秋", due_at=at(9, 15), quantity_required=32)])
    service.propose_term("VNU", draft, TermCategory.TEACHER_DISPATCH, "VNU派2名教师9月8日前到岗",
                         obligation_specs=[dict(title="VNU教师到岗", responsible_party="VNU",
                                                batch_id="2026-秋", due_at=at(9, 8), quantity_required=2)])
    service.propose_term("KU", draft, TermCategory.RESOURCE_PROVISION, "KU提供多媒体教室3间",
                         obligation_specs=[dict(title="多媒体教室到位", responsible_party="KU",
                                                batch_id="2026-秋", due_at=at(9, 20), quantity_required=3)])
    service.propose_term("GXU", draft, TermCategory.ACHIEVEMENT_OWNERSHIP, "联合教研成果四方共有",
                         obligation_specs=[dict(title="成果归属登记", responsible_party="GXU",
                                                batch_id="2026-秋", due_at=at(12, 31), quantity_required=1)])

    # 3. 逐项会签并激活为带生效期的 V1
    sign_all_terms(service, draft)
    service.activate_draft("GXU", draft, effective_from=at(9, 2))
    show("激活协议", {"版本": service.current_version().version_id,
                     "承诺数": len(service.list_obligations())})

    # 4. 履约登记：部分履行、迟交、争议、替代履行
    obs = {ob.title: ob for ob in service.list_obligations()}
    clock.set(at(9, 5))
    service.record_fulfillment("GXU", obs["2026-秋招生注册"].obligation_id, 80, "注册记录", "首批80人完成注册")
    clock.set(at(9, 9))
    service.record_fulfillment("VNU", obs["VNU教师到岗"].obligation_id, 2, "到岗签到", "2名教师到岗（迟1天）")
    service.raise_dispute("TU", obs["汉语精读课程交付"].obligation_id, "教材版本与约定不一致")
    clock.set(at(9, 12))
    service.resolve_dispute("GXU", obs["汉语精读课程交付"].obligation_id, "四方确认使用第二版教材")
    clock.set(at(9, 16))
    escalations = [r for r in service.reminders() if r.kind == "overdue_escalation"]
    show("履约状态", {ob.title: ob.status.value for ob in service.list_obligations()})
    show("逾期升级", {"事项": [{"承诺": r.target_id, "级别": f"L{r.level}"} for r in escalations]})

    # 5. 修订案：并发两个修订案，先生效者胜，落后者被拒绝
    amendment_a = service.propose_amendment("GXU", base_rev=1, sign_expires_at=at(9, 25), changes=[
        {"op": "modify", "term_id": "T-4", "category": "资源提供", "content": "KU提供多媒体教室2间",
         "obligation_specs": [dict(title="多媒体教室到位(调减)", responsible_party="KU",
                                   batch_id="2026-秋", due_at=at(9, 20), quantity_required=2)]},
    ])
    amendment_b = service.propose_amendment("VNU", base_rev=1, sign_expires_at=at(9, 25), changes=[
        {"op": "modify", "term_id": "T-2", "category": "课程交付", "content": "汉语精读调整为28课时",
         "obligation_specs": []},
    ])
    sign_all_amendment(service, amendment_a)
    sign_all_amendment(service, amendment_b)
    service.enact_amendment("GXU", amendment_a, effective_from=at(9, 17))
    try:
        service.enact_amendment("GXU", amendment_b, effective_from=at(9, 18))
    except StaleDecisionError as exc:
        show("并发修订拦截", {"修订案": amendment_b, "原因": str(exc)})

    # 6. 进程重启：待确认事项仍在，可继续会签
    amendment_c = service.propose_amendment("VNU", base_rev=2, sign_expires_at=at(9, 30), changes=[
        {"op": "modify", "term_id": "T-2", "category": "课程交付", "content": "汉语精读调整为28课时",
         "obligation_specs": []},
    ])
    hash_c = service.get_amendment(amendment_c).changes_hash
    service.countersign_amendment("VNU", amendment_c, hash_c)
    restarted = build_service(clock)  # 模拟重启
    pending = restarted.pending_confirmations()
    show("重启后待确认", {"事项": [{"对象": p.get("amendment_id") or p.get("term_id"),
                                   "待会签": p["missing_parties"]} for p in pending]})
    for party in ("GXU", "TU", "KU"):
        restarted.countersign_amendment(party, amendment_c, hash_c)
    clock.set(at(9, 22))
    restarted.enact_amendment("GXU", amendment_c, effective_from=at(9, 23))

    # 7. 管理员按培养批次与历史时点查询
    queries = QueryService(EventStore(DATA_FILE))
    show("历史协议(9-10)", {"版本": queries.agreement_at(at(9, 10), batch_id="2026-秋")["rev"]})
    show("当前协议(9-24)", {"版本": queries.agreement_at(at(9, 24), batch_id="2026-秋")["rev"]})
    unfulfilled = queries.unfulfilled_at(at(9, 24), batch_id="2026-秋")
    show("各方未履约项", {p: [f"{i['title']}({i['status']})" for i in items] for p, items in unfulfilled.items()})
    impact = queries.amendment_impact(amendment_a)
    show("修订影响", {"被替代": impact["obligations_superseded"], "新登记": impact["obligations_created"]})
    records = queries.decision_records(at=at(9, 24), batch_id="2026-秋")
    show("决策记录", {"条数": len(records), "链校验通过": queries.verify_log()})


if __name__ == "__main__":
    main()
