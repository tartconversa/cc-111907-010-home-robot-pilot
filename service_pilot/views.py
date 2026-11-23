"""分角色视图、统计与回溯预言。

家庭视图：仅本户请求、授权与接手人可见信息。
运营视图：全部请求、覆盖、失约、人工接管负荷、证据薄弱范围。
历史回溯：只在当时可知的证据下复算承诺，并单独列出后来才发现的事实。

统计不计算“综合成功率”。独立/接管/拒绝/未知分列；不同设备版本不合并。
"""
from __future__ import annotations

import json
from datetime import datetime

from .domain import (
    OUTCOME_ZH,
    OUTCOMES,
    REQ_ACCEPTED,
    REQ_CANCELLED,
    REQ_COMPLETED,
    REQ_HELD,
    REQ_PENDING,
    REQ_RECEIVED,
    REQ_REJECTED,
    Interval,
    iso,
    parse_ts,
)
from .incidents import affected_requests
from .planner import build_plan
from .storage import Workspace

STATUS_ZH = {
    REQ_RECEIVED: "已收到，等待运营评估",
    REQ_PENDING: "等待确认（尚未承诺）",
    REQ_ACCEPTED: "已承接",
    REQ_HELD: "受影响/待确认（未完成承诺）",
    REQ_REJECTED: "未承接",
    REQ_CANCELLED: "已取消（承诺已释放）",
    REQ_COMPLETED: "已完成（结果已记录）",
}

PLAN_KIND_ZH = {None: "—", "robot": "机器人主办", "human": "人工直接办理", "defer": "暂缓"}




def family_view(ws: Workspace, family: str, *, now: datetime | None = None, request_id: str | None = None) -> dict:
    """家庭只能看到本户内容；不返回其他家庭或工作人员的排班细节。"""
    at = now or ws.now()
    snapshot = ws.snapshot(at)
    if family not in snapshot.families:
        raise ValueError(f"未登记的家庭: {family}")
    requests = [r for r in ws.requests(at) if r["family"] == family]
    if request_id:
        requests = [r for r in requests if r["request"] == request_id]
        if not requests:
            raise ValueError("该请求不属于本户或不存在")
    scopes = snapshot.active_scopes(family)
    consent = {
        "service_tasks": {
            scope.split("/", 1)[1]: row["status"]
            for scope, row in scopes.items()
            if scope.startswith("service/")
        },
        "analysis": (scopes.get("analysis") or {}).get("status", "missing"),
    }
    incident_by_request = {
        item["request"]: item for item in affected_requests(ws, now=at)
    }
    items = []
    for row in requests:
        # 家庭可见摘要只来自请求事件，不使用运营内部描述
        events = ws.request_events(row["request"])
        visible_events = []
        for event in events:
            detail = json.loads(event["detail"])
            visible_events.append(
                {
                    "at": event["at"],
                    "kind": event["kind"],
                    "label": _FAMILY_EVENT_LABEL.get(event["kind"], event["kind"]),
                    "detail": _family_event_detail(event["kind"], detail),
                }
            )
        incident = incident_by_request.get(row["request"])
        open_proposals = [
            {
                "proposal": p["id"],
                "action": p["action"],
                "plan": p["plan_kind"],
                "start": p["start"],
                "end": p["end"],
                "staff": json.loads(p["staff_ids"] or "[]"),
            }
            for p in ws.proposals(row["request"], open_only=True)
            if parse_ts(p["created_at"]) <= at
        ]
        items.append(
            {
                "request": row["request"],
                "task": row["task"],
                "object": row["object"],
                "environment": row["environment"],
                "earliest": row["earliest"],
                "latest": row["latest"],
                "minutes": row["minutes"],
                "status": row["status"],
                "status_zh": STATUS_ZH.get(row["status"], row["status"]),
                "plan": row["plan_kind"],
                "plan_zh": PLAN_KIND_ZH.get(row["plan_kind"], row["plan_kind"]),
                "plan_start": row["plan_start"],
                "plan_end": row["plan_end"],
                "assigned_staff": (
                    json.loads(row["staff_ids"] or "[]")
                    if row["status"] in (REQ_ACCEPTED, REQ_COMPLETED)
                    else []
                ),
                "pending_proposals": open_proposals,
                "incident": (
                    {
                        "affected": True,
                        "message": "这项安排受到设备维护、能力复查或授权变化影响，运营正在准备改期或转人工方案；原安排在新的安排被您确认前继续保留。",
                        "reasons": incident["reasons"],
                    }
                    if incident
                    else {"affected": False}
                ),
                "events": visible_events,
            }
        )
    return {
        "view": "family",
        "family": family,
        "generated_at": iso(at),
        "region_allowed_hours": {
            "start_hour": snapshot.families[family]["start_hour"],
            "end_hour": snapshot.families[family]["end_hour"],
        },
        "consent": consent,
        "requests": items,
        "explanation": "“已承接”表示设备与保障窗口已经占用；其他状态不是承诺。",
    }


_FAMILY_EVENT_LABEL = {
    REQ_RECEIVED: "请求已收到",
    REQ_PENDING: "运营已受理，等待确认",
    REQ_ACCEPTED: "已承接",
    REQ_HELD: "暂时受影响，等待新的安排",
    REQ_REJECTED: "未承接",
    REQ_CANCELLED: "已取消",
    "proposal": "新的替代安排待您确认",
    "proposal_declined": "替代安排已拒绝",
    "fulfillment": "服务结果已记录",
    REQ_COMPLETED: "服务已完成",
}


def _family_event_detail(kind: str, detail: dict) -> dict:
    if kind == REQ_ACCEPTED:
        return {
            "plan": detail.get("plan"),
            "start": detail.get("start"),
            "end": detail.get("end"),
            "staff": detail.get("staff", []),
        }
    if kind == "proposal":
        return {
            "proposal": detail.get("proposal"),
            "action": detail.get("action"),
            "plan": detail.get("plan"),
            "start": detail.get("start"),
            "end": detail.get("end"),
            "staff": detail.get("staff", []),
        }
    if kind == "fulfillment":
        # 运营登记的内部备注不进入家庭视图。
        return {k: detail.get(k) for k in ("outcome", "started_at", "completed_at")}
    if kind == REQ_CANCELLED:
        return {"reason": detail.get("reason")}
    if kind in (REQ_HELD, REQ_REJECTED):
        # 这两个状态可能包含运营内部判断；家庭只需知道状态，不暴露内部措辞。
        return {}
    return {}


def request_detail(ws: Workspace, request_id: str, *, now: datetime | None = None) -> dict:
    at = now or ws.now()
    row = ws.request_row(request_id)
    if row is None:
        raise ValueError(f"请求不存在: {request_id}")
    if parse_ts(row["received"]) > at:
        raise ValueError(f"请求 {request_id} 在 {iso(at)} 尚未进入系统")
    snapshot = ws.snapshot(at)
    payload = {
        "request": row["request"],
        "family": row["family"],
        "task": row["task"],
        "earliest": row["earliest"],
        "latest": row["latest"],
        "minutes": row["minutes"],
        "object": row["object"],
        "environment": row["environment"],
    }
    reservations = [
        {
            "id": r["id"],
            "request": r["request"],
            "kind": r["kind"],
            "resource": r["resource"],
            "start": parse_ts(r["start"]),
            "end": parse_ts(r["end"]),
            "meta": json.loads(r["meta"] or "{}"),
        }
        for r in ws.active_reservations(at)
    ]
    plan = build_plan(snapshot, payload, reservations, exclude_request=request_id, at=at)
    state = ws.request_state_as_of(request_id, at)
    status = state["status"] if state else row["status"]
    plan_state = state or {
        "plan_kind": row["plan_kind"],
        "plan_start": row["plan_start"],
        "plan_end": row["plan_end"],
        "robot": row["robot"],
        "staff_ids": row["staff_ids"],
        "risk_band": row["risk_band"],
    }
    data = plan.as_dict()
    data["status"] = status
    data["status_zh"] = STATUS_ZH.get(status, status)
    data["status_reason"] = state.get("status_reason") if state else row["status_reason"]
    data["current_plan"] = {
        "kind": plan_state.get("plan_kind"),
        "kind_zh": PLAN_KIND_ZH.get(plan_state.get("plan_kind"), plan_state.get("plan_kind")),
        "start": plan_state.get("plan_start"),
        "end": plan_state.get("plan_end"),
        "robot": plan_state.get("robot"),
        "staff": json.loads(plan_state.get("staff_ids") or "[]"),
        "risk_band": plan_state.get("risk_band"),
        "risk_basis": json.loads(row["risk_basis"] or "{}"),
    }
    data["environment_summary"] = snapshot.environment_summary(row["family"], row["task"])
    data["consent"] = {
        scope: {
            "status": entry.get("status"),
            "effective": entry.get("effective"),
        }
        for scope, entry in snapshot.active_scopes(row["family"]).items()
    }
    data["incident"] = next(
        (item for item in affected_requests(ws, now=at) if item["request"] == request_id),
        None,
    ) if status in (REQ_ACCEPTED, REQ_HELD) else None
    return data


def operations_view(ws: Workspace, *, now: datetime | None = None) -> dict:
    """运营总览：承接情况、保障负荷、失约/取消、证据薄弱范围。"""
    at = now or ws.now()
    snapshot = ws.snapshot(at)
    rows = ws.requests(at)
    counts = {status: 0 for status in STATUS_ZH}
    items = []
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        items.append(
            {
                "request": row["request"],
                "family": row["family"],
                "task": row["task"],
                "environment": row["environment"],
                "status": row["status"],
                "status_zh": STATUS_ZH.get(row["status"], row["status"]),
                "plan_zh": PLAN_KIND_ZH.get(row["plan_kind"], row["plan_kind"]),
                "plan_start": row["plan_start"],
                "plan_end": row["plan_end"],
                "robot": row["robot"],
                "staff": json.loads(row["staff_ids"] or "[]"),
                "risk_band": row["risk_band"],
                "status_reason": row["status_reason"],
            }
        )

    # 人工接管负荷：按人员统计已占用保障窗口
    staff_load: dict[str, list[dict]] = {}
    for r in ws.active_reservations(at):
        if r["kind"] != "staff":
            continue
        meta = json.loads(r["meta"] or "{}")
        staff_load.setdefault(r["resource"], []).append(
            {
                "request": r["request"],
                "start": r["start"],
                "end": r["end"],
                "role": meta.get("role", "direct"),
                "status": r["status"],
            }
        )
    for staff_id, load in staff_load.items():
        load.sort(key=lambda x: x["start"])
    busy_staff = [
        {
            "staff": staff_id,
            "assignments": load,
            "occupied_minutes": _union_minutes(
                [Interval(parse_ts(x["start"]), parse_ts(x["end"])) for x in load]
            ),
        }
        for staff_id, load in sorted(staff_load.items())
    ]

    # 证据薄弱范围：按家庭/版本/任务/环境与物品
    coverage = []
    for family, family_row in sorted(snapshot.families.items()):
        robots = snapshot.robots_for(family)
        if not robots:
            continue
        declared = sorted({task for robot in robots for task in (robot.get("tasks") or [])})
        for task in declared:
            robot = snapshot.robot_for(family, task) or robots[-1]
            env_summary = snapshot.environment_summary(family, task)
            strata = snapshot.family_task_strata(family, robot["robot"], robot["version"], task)
            total = sum(s.attempts for s in strata)
            environments = env_summary.get("environments", [])
            objects = [s for s in strata if s.object is not None]
            if objects and len(environments) > 1:
                top = max(environments, key=lambda e: e.get("attempts") or 0)
                if total and (top.get("attempts") or 0) / total >= 0.75:
                    per_obj: dict[str, dict] = {}
                    for stratum in strata:
                        entry = per_obj.setdefault(
                            stratum.object or "未标注",
                            {"attempts": 0, "reviewed": 0, "handoff": 0, "unknown": 0, "envs": set()},
                        )
                        entry["attempts"] += stratum.attempts
                        entry["reviewed"] += stratum.reviewed
                        entry["handoff"] += stratum.count("handoff")
                        entry["unknown"] += stratum.count("unknown")
                        entry["envs"].add(stratum.environment)
                    for object_name, obj_counts in sorted(per_obj.items()):
                        coverage.append(
                            {
                                "family": family,
                                "robot": robot["robot"],
                                "version": robot["version"],
                                "task": task,
                                "environment": str(top.get("environment")),
                                "object": object_name,
                                "attempts": obj_counts["attempts"],
                                "reviewed": obj_counts["reviewed"],
                                "unknown_fraction": round(
                                    obj_counts["unknown"] / obj_counts["attempts"], 4
                                )
                                if obj_counts["attempts"]
                                else None,
                                "risk_upper": top.get("risk_upper"),
                                "risk_band": top.get("risk_band"),
                                "selective_demo": True,
                                "weak": True,
                                "recommendation": "暂停该环境的机器人主办，改由人工办理或先补足本地证据",
                                "note": (
                                    f"选择性演示：{round((top.get('attempts') or 0) / total * 100)}% 的尝试集中在 "
                                    f"{top.get('environment')}，不能外推到其他环境"
                                ),
                            }
                        )
                    continue
            for env in environments:
                coverage.append(
                    {
                        "family": family,
                        "robot": env_summary.get("robot"),
                        "version": env_summary.get("version"),
                        "task": task,
                        "environment": env.get("environment"),
                        "object": ", ".join(env.get("object_coverage") or []),
                        "attempts": env.get("attempts"),
                        "reviewed": env.get("reviewed"),
                        "unknown_fraction": env.get("unknown_fraction"),
                        "risk_band": env.get("risk_band"),
                        "risk_upper": env.get("risk_upper"),
                        "selective_demo": False,
                        "weak": (
                            env.get("attempts", 0) < 5
                            or env.get("reviewed", 0) == 0
                            or env.get("risk_band") == "high"
                        ),
                        "recommendation": _coverage_recommendation(env),
                    }
                )

    # 失约/取消：从请求事件统计
    fulfilment = list(ws.fulfillments(as_of=at))
    outcomes: dict[str, int] = {key: 0 for key in OUTCOMES}
    per_family: dict[str, dict[str, int]] = {}
    per_request: dict[str, str] = {}
    for row in fulfilment:
        outcomes[row["outcome"]] = outcomes.get(row["outcome"], 0) + 1
        fam = per_family.setdefault(row["family"], {key: 0 for key in OUTCOMES})
        fam[row["outcome"]] += 1
        per_request[row["request"]] = row["outcome"]

    failed_promises = [
        dict(item, outcome=OUTCOME_ZH[per_request[item["request"]]])
        for item in items
        if item["request"] in per_request
        and per_request[item["request"]] in ("refused", "unknown", "handoff")
    ]
    cancelled = [item for item in items if item["status"] == REQ_CANCELLED]
    incident_list = affected_requests(ws, now=at)
    open_proposals = [
        {
            "proposal": p["id"],
            "request": p["request"],
            "action": p["action"],
            "plan": p["plan_kind"],
            "start": p["start"],
            "end": p["end"],
            "staff": json.loads(p["staff_ids"] or "[]"),
            "created_at": p["created_at"],
        }
        for p in ws.proposals(open_only=True)
        if parse_ts(p["created_at"]) <= at
    ]
    # 待确认/建议不是承诺：单独列出，避免被总数误读
    not_committed = [
        item for item in items if item["status"] in (REQ_RECEIVED, REQ_PENDING, REQ_HELD)
    ]

    return {
        "view": "operations",
        "generated_at": iso(at),
        "status_counts": {STATUS_ZH.get(k, k): v for k, v in counts.items() if v},
        "requests": items,
        "staff_load": busy_staff,
        "evidence_coverage": coverage,
        "fulfilment": {
            "total": len(fulfilment),
            "by_outcome": {OUTCOME_ZH[k]: v for k, v in outcomes.items()},
            "by_family": per_family,
            "promises_not_met": failed_promises,
            "cancelled": cancelled,
        },
        "open_proposals": open_proposals,
        "incidents": incident_list,
        "not_committed": not_committed,
        "weak_evidence": [c for c in coverage if c["weak"]],
    }


def _coverage_recommendation(env: dict) -> str:
    if (env.get("attempts") or 0) < 5:
        return "证据不足：暂停机器人主办，补充人工或先积累并复核尝试"
    if (env.get("reviewed") or 0) == 0:
        return "尚无人工复核：暂停机器人主办直至完成复查"
    if env.get("risk_band") == "high":
        return "接管风险高：优先补充人工保障，必要时暂停机器人主办"
    return "可继续，但需按风险上界保留人工保障"


def _union_minutes(intervals: list[Interval]) -> int:
    if not intervals:
        return 0
    intervals = sorted(intervals)
    total = 0
    cur = intervals[0]
    for item in intervals[1:]:
        if item.start <= cur.end:
            cur = Interval(cur.start, max(cur.end, item.end))
        else:
            total += cur.minutes
            cur = item
    return total + cur.minutes


# ---------------- 回溯 ----------------

def replay_request(ws: Workspace, request_id: str, *, as_of: datetime) -> dict:
    """在当时可知证据下重建承诺，并对照后来才发现的事实。"""
    row = ws.request_row(request_id)
    if row is None:
        raise ValueError(f"请求不存在: {request_id}")
    if parse_ts(row["received"]) > as_of:
        return {
            "request": request_id,
            "as_of": iso(as_of),
            "status_then": None,
            "options_then": None,
            "plan_then": None,
            "later_facts": [],
            "note": "该请求在 as_of 时点尚未进入系统；不能据此声称当时的承诺。",
        }
    snapshot = ws.snapshot(as_of)
    payload = {
        "request": row["request"],
        "family": row["family"],
        "task": row["task"],
        "earliest": row["earliest"],
        "latest": row["latest"],
        "minutes": row["minutes"],
        "object": row["object"],
        "environment": row["environment"],
    }
    reservations = [
        {
            "id": r["id"],
            "request": r["request"],
            "kind": r["kind"],
            "resource": r["resource"],
            "start": parse_ts(r["start"]),
            "end": parse_ts(r["end"]),
            "meta": json.loads(r["meta"] or "{}"),
        }
        for r in ws.active_reservations(as_of)
    ]
    plan = build_plan(snapshot, payload, reservations, exclude_request=request_id, at=as_of)
    status_event = ws.request_state_as_of(request_id, as_of)
    facts_at = {fact["id"] for fact in ws.source_facts(as_of)}
    later_facts = []
    for fact in ws.source_facts(None):
        if fact["id"] in facts_at:
            continue
        if fact["table_name"] in ("maintenance", "capability_review", "consent", "staff_unavailability"):
            later_facts.append(
                {
                    "table": fact["table_name"],
                    "known_at": fact["known_at"],
                    "payload": json.loads(fact["payload"]),
                }
            )
    return {
        "request": request_id,
        "as_of": iso(as_of),
        "status_then": status_event['status'] if status_event else None,
        "status_detail_then": status_event,
        "options_then": plan.as_dict(),
        "plan_then": _committed_plan_at(ws, request_id, as_of),
        "later_facts": later_facts,
        "note": "回溯只使用 as_of 之前已知的事实；later_facts 不会改变当时的判断或风险披露。",
    }


def _committed_plan_at(ws: Workspace, request_id: str, as_of: datetime) -> dict | None:
    events = ws.request_events(request_id, as_of)
    committed = None
    for event in events:
        kind = event["kind"]
        if kind == REQ_ACCEPTED:
            committed = json.loads(event["detail"])
        elif kind in (REQ_CANCELLED, REQ_REJECTED):
            # 取消/拒绝会让当时承诺失效。
            committed = None
    return committed
