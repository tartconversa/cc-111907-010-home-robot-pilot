"""运营与家庭操作：承接、确认、取消、受影响请求处置、履约记录。

不变量：
- 被接受的请求才占用设备与保障；建议/待确认不占用。
- 两个运营确认重叠安排时，必须重新核对资源并串行写入；
  后者若资源已不可用，必须失败而不是各自显示“已承接”。
- 转人工的替代安排需要家庭确认；原安排在替代安排成立后释放。
- 已开始任务的履约事实只追加，不因授权撤回而删除。
"""
from __future__ import annotations

import json
from datetime import datetime

from .domain import (
    ACTION_CANCEL,
    ACTION_HUMAN,
    ACTION_RESCHEDULE,
    OUTCOME_ZH,
    PLAN_HUMAN,
    PLAN_ROBOT,
    PLAN_ZH,
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
from .planner import (
    build_plan,
    find_option,
    reserved_intervals,
)
from .storage import Workspace


def _next_request_id(ws: Workspace) -> str:
    current = {row["request"] for row in ws.requests()}
    index = 1
    while f"RQ{index}" in current:
        index += 1
    return f"RQ{index}"


def submit_request(
    ws: Workspace,
    *,
    family: str,
    task: str,
    earliest: str,
    latest: str,
    minutes: int,
    object_name: str | None,
    environment: str,
    collection_scope: str | None = None,
    actor: str | None = None,
    now: datetime | None = None,
) -> dict:
    """家庭提交请求：给出可接受时段、任务类别和允许的采集范围。

    提交本身不是承诺。应用只登记请求与家庭声明的采集范围；
    真正的服务授权来自来源记录，不能由请求提交自动产生。
    """
    from .evidence import allowed_windows

    at = _now(ws, now)
    start = parse_ts(earliest)
    end = parse_ts(latest)
    if start is None or end is None or end <= start:
        raise OperationError("可接受时段必须给出起点，且终点晚于起点")
    if end <= at:
        raise OperationError("可接受时段已经结束，无法提交")
    if minutes <= 0:
        raise OperationError("预计时长必须是正整数")
    snapshot = ws.snapshot(at)
    family_row = snapshot.families.get(family)
    if family_row is None:
        raise OperationError(f"家庭未登记: {family}")
    if task not in (family_row.get("allowed_tasks") or []):
        raise OperationError(
            f"任务 {task} 不在家庭允许的任务内（{', '.join(family_row.get('allowed_tasks') or [])}）"
        )
    if not any(window.overlaps(Interval(start, end)) for window in allowed_windows(family_row, start, end)):
        raise OperationError(
            f"请求时段与家庭可接受小时（UTC {family_row['start_hour']}–{family_row['end_hour']}）没有交集"
        )
    with ws.transaction():
        # 在事务内分配编号，避免两个家庭同时提交时得到同一个请求号。
        request_id = _next_request_id(ws)
        ws.conn.execute(
            "INSERT INTO request_state(request, family, task, earliest, latest, minutes, object, environment, "
            "received, status, status_reason, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request_id,
                family,
                task,
                iso(start),
                iso(end),
                int(minutes),
                object_name,
                environment,
                iso(at),
                REQ_RECEIVED,
                "家庭已提交，等待运营确认；提交本身不是承诺",
                iso(at),
            ),
        )
        ws._event(
            request_id,
            at,
            actor or f"family:{family}",
            REQ_RECEIVED,
            {
                "family": family,
                "task": task,
                "collection_scope": collection_scope,
                "submitted_by": "family",
            },
        )
        ws.audit(
            actor or f"family:{family}",
            "submit_request",
            {
                "request": request_id,
                "family": family,
                "task": task,
                "collection_scope": collection_scope,
            },
            at,
        )
    return {"request": request_id, "family": family, "status": REQ_RECEIVED}


def family_cancel(
    ws: Workspace, request_id: str, *, family: str, reason: str, now: datetime | None = None
) -> dict:
    row = ws.request_row(request_id)
    if row is None or row["family"] != family:
        raise OperationError("该请求不属于本户或不存在")
    return cancel_request(ws, request_id, actor=f"family:{family}", reason=reason, now=now)


def family_confirm_proposal(
    ws: Workspace, proposal_id: int, *, family: str, accept: bool = True, now: datetime | None = None
) -> dict:
    proposal = ws.proposal(proposal_id)
    if proposal is None:
        raise OperationError("替代安排不存在")
    row = ws.request_row(proposal["request"])
    if row is None or row["family"] != family:
        raise OperationError("该替代安排不属于本户或不存在")
    return confirm_proposal(ws, proposal_id, actor=f"family:{family}", now=now, accept=accept)


class OperationError(Exception):
    """面向操作人员的业务错误。"""


def _now(ws: Workspace, now: datetime | None) -> datetime:
    """运行动作使用的时间。显式时间不得早于工作目录已推进到的时点，
    以免用后来的知识重写更早的操作。只读的 replay 不受此限。

    不传 --now 的动作使用并推进演练时钟。
    """
    current = ws.now()
    if now is not None and now < current:
        raise OperationError(
            f"操作时间 {iso(now)} 早于工作目录当前时点 {iso(current)}；历史回看请使用 replay"
        )
    at = now or current
    if at != current:
        ws.set_clock(at)
    return at


def _resource_rows(ws: Workspace, at: datetime | None = None):
    """指定时点仍有效的占用。评估/回看不把后来才发生的占用算进来。"""
    return [
        {
            "id": row["id"],
            "request": row["request"],
            "kind": row["kind"],
            "resource": row["resource"],
            "start": parse_ts(row["start"]),
            "end": parse_ts(row["end"]),
            "meta": json.loads(row["meta"] or "{}"),
        }
        for row in ws.active_reservations(at)
    ]


def _request_payload(ws: Workspace, request_id: str) -> dict:
    row = ws.request_row(request_id)
    if row is None:
        raise OperationError(f"请求不存在: {request_id}")
    if row["status"] in (REQ_CANCELLED, REQ_REJECTED, REQ_COMPLETED):
        raise OperationError(f"请求 {request_id} 已结束（{row['status']}），不能再承接或改派")
    return {
        "request": row["request"],
        "family": row["family"],
        "task": row["task"],
        "earliest": row["earliest"],
        "latest": row["latest"],
        "minutes": row["minutes"],
        "object": row["object"],
        "environment": row["environment"],
    }


def _read_time(ws: Workspace, now: datetime | None) -> datetime:
    """只读查询允许回看；不推进也不改写任何状态。"""
    return now or ws.now()


def assess_request(ws: Workspace, request_id: str, *, now: datetime | None = None) -> dict:
    """只评估，不写资源占用；结果显示给运营与家庭可见摘要。"""
    at = _read_time(ws, now)
    payload = _request_payload(ws, request_id)
    snapshot = ws.snapshot(at)
    plan = build_plan(snapshot, payload, _resource_rows(ws, at), exclude_request=request_id, at=at)
    return plan.as_dict()


def preview_plan(
    ws: Workspace,
    *,
    family: str,
    task: str,
    earliest: str,
    latest: str,
    minutes: int,
    object_name: str | None,
    environment: str,
    request_id: str = "PREVIEW",
    now: datetime | None = None,
) -> dict:
    at = _read_time(ws, now)
    snapshot = ws.snapshot(at)
    payload = {
        "request": request_id,
        "family": family,
        "task": task,
        "earliest": earliest,
        "latest": latest,
        "minutes": minutes,
        "object": object_name,
        "environment": environment,
    }
    plan = build_plan(snapshot, payload, _resource_rows(ws, at), exclude_request=request_id, at=at)
    return plan.as_dict()


def _assert_option_current(
    ws: Workspace,
    request_id: str,
    plan_kind: str,
    start: datetime,
    *,
    at: datetime,
) -> tuple[dict, object]:
    """关键步骤：在事务内重新读取资源并重建候选，防止并发确认重叠。"""
    payload = _request_payload(ws, request_id)
    snapshot = ws.snapshot(at)
    plan = build_plan(snapshot, payload, _resource_rows(ws, at), exclude_request=request_id, at=at)
    option = find_option(plan, plan_kind=plan_kind, start=start)
    if option is None:
        raise OperationError("该时段不在请求的候选范围内，请重新评估")
    if not option.feasible:
        detail = "；".join(option.reasons) if option.reasons else "资源或证据不足"
        raise OperationError(f"该安排当前不可承诺：{detail}")
    return payload, option


def _write_reservations(ws: Workspace, request_id: str, option, at: datetime, assigned_staff: str) -> None:
    """把已确认安排的独占资源写入占用表。调用方必须已持有事务。

    一名工作人员在同一时段只能被一个请求占用；其余合格候选保持可用。
    """
    if option.plan == PLAN_ROBOT:
        ws.conn.execute(
            "INSERT INTO reservation(request, kind, resource, start, end, status, created_at, meta) "
            "VALUES (?, 'robot', ?, ?, ?, 'held', ?, ?)",
            (request_id, option.robot, iso(option.start), iso(option.end), iso(at), "{}"),
        )
        support = option.assurances[0]
        ws.conn.execute(
            "INSERT INTO reservation(request, kind, resource, start, end, status, created_at, meta) "
            "VALUES (?, 'staff', ?, ?, ?, 'held', ?, ?)",
            (
                request_id,
                assigned_staff,
                iso(support["start"]),
                iso(support["end"]),
                iso(at),
                json.dumps(
                    {
                        "role": "assurance",
                        "service_start": iso(option.start),
                        "service_end": iso(option.end),
                        "alternates": [s for s in option.staff_ids if s != assigned_staff],
                    },
                    ensure_ascii=False,
                ),
            ),
        )
    elif option.plan == PLAN_HUMAN:
        ws.conn.execute(
            "INSERT INTO reservation(request, kind, resource, start, end, status, created_at, meta) "
            "VALUES (?, 'staff', ?, ?, ?, 'held', ?, ?)",
            (request_id, assigned_staff, iso(option.start), iso(option.end), iso(at), "{}"),
        )


def accept_request(
    ws: Workspace,
    request_id: str,
    *,
    plan_kind: str,
    start: str,
    actor: str,
    now: datetime | None = None,
    note: str | None = None,
    staff_id: str | None = None,
    supersede_proposal: int | None = None,
) -> dict:
    at = _now(ws, now)
    start_dt = parse_ts(start)
    if plan_kind not in (PLAN_ROBOT, PLAN_HUMAN):
        raise OperationError("承接只能选择机器人主办或人工直接办理；暂缓不是承诺")
    with ws.transaction():
        current = ws.request_row(request_id)
        if current is None:
            raise OperationError(f"请求不存在: {request_id}")
        if current["status"] == REQ_ACCEPTED:
            raise OperationError(
                "该请求已经承接；改期或转人工必须先提出替代安排，并由家庭确认后再释放原安排"
            )
        payload, option = _assert_option_current(ws, request_id, plan_kind, start_dt, at=at)
        if start_dt < at:
            raise OperationError(
                f"不能承接已过去的时段（开始 {iso(start_dt)}，当前 {iso(at)}）；请改期或取消"
            )
        if option.plan == PLAN_ROBOT and not option.staff_ids:
            raise OperationError("机器人主办需要保留合格接管人员，当前没有可用保障")
        if not option.staff_ids:
            raise OperationError("没有可用的合格工作人员")
        if staff_id is not None and staff_id not in option.staff_ids:
            raise OperationError(f"工作人员 {staff_id} 不在该时段的合格候选内")
        assigned_staff = staff_id or option.staff_ids[0]
        # 二次核对：在事务内再确认机器人/人员仍空闲
        rows = _resource_rows(ws, at)
        if option.plan == PLAN_ROBOT:
            busy = reserved_intervals(rows, "robot", option.robot, exclude_request=request_id)
            if any(Interval(option.start, option.end).overlaps(b) for b in busy):
                raise OperationError("设备在此期间已被其他请求占用，请重新评估")
            support = option.assurances[0]
            staff_busy = reserved_intervals(rows, "staff", assigned_staff, exclude_request=request_id)
            if any(Interval(support["start"], support["end"]).overlaps(b) for b in staff_busy):
                raise OperationError("接管保障人员在此期间已被占用，请重新评估")
        else:
            busy = reserved_intervals(rows, "staff", assigned_staff, exclude_request=request_id)
            if any(Interval(option.start, option.end).overlaps(b) for b in busy):
                raise OperationError(f"工作人员 {assigned_staff} 在此期间已被占用，请重新评估")
        # 释放该请求旧的（未开始的）占用
        ws.conn.execute(
            "UPDATE reservation SET status = 'released', released_at = ?, release_reason = 'superseded' "
            "WHERE request = ? AND status = 'held'",
            (iso(at), request_id),
        )
        _write_reservations(ws, request_id, option, at, assigned_staff)
        risk = None
        if plan_kind == PLAN_ROBOT:
            support = option.assurances[0]
            meta = {
                "assurance_start": iso(support["start"]),
                "assurance_end": iso(support["end"]),
                "candidates": list(option.staff_ids),
                "assigned_staff": assigned_staff,
            }
            risk_basis = {
                "basis": (ws.snapshot(at).qualification(
                    payload["family"], payload["task"],
                    object_name=payload.get("object"), environment=payload.get("environment") or "unknown",
                ).get("risk") or {}).get("basis"),
                "assurance_minutes": support["minutes"],
            }
        else:
            meta = {"candidates": list(option.staff_ids), "assigned_staff": assigned_staff}
            risk_basis = {"basis": "人工直接办理，无接管风险估计"}
        ws.conn.execute(
            "UPDATE request_state SET status = ?, status_reason = ?, plan_kind = ?, plan_start = ?, plan_end = ?, "
            "robot = ?, version = ?, staff_required = ?, staff_ids = ?, risk_basis = ?, risk_upper = ?, "
            "risk_band = ?, updated_at = ? WHERE request = ?",
            (
                REQ_ACCEPTED,
                note or f"已按{PLAN_ZH[plan_kind]}承接",
                plan_kind,
                iso(option.start),
                iso(option.end),
                option.robot if plan_kind == PLAN_ROBOT else None,
                (ws.snapshot(at).robot_for(payload["family"], payload["task"]) or {}).get("version")
                if plan_kind == PLAN_ROBOT
                else None,
                1 if plan_kind == PLAN_ROBOT else 0,
                json.dumps([assigned_staff], ensure_ascii=False),
                json.dumps(risk_basis, ensure_ascii=False),
                option.risk_upper,
                option.risk_band,
                iso(at),
                request_id,
            ),
        )
        # 直接承接会取代该请求尚未确认的替代建议，避免家庭看到过期选项。
        ws.conn.execute(
            "UPDATE proposal SET status = 'superseded' "
            "WHERE request = ? AND status = 'awaiting_family'",
            (request_id,),
        )
        if supersede_proposal is not None:
            ws.conn.execute(
                "UPDATE proposal SET status = 'superseded' WHERE id = ? AND request = ?",
                (supersede_proposal, request_id),
            )
        ws._event(
            request_id,
            at,
            actor,
            REQ_ACCEPTED,
            {
                "plan": plan_kind,
                "start": iso(option.start),
                "end": iso(option.end),
                "robot": option.robot,
                "version": (ws.snapshot(at).robot_for(payload["family"], payload["task"]) or {}).get("version")
                if option.robot
                else None,
                "staff": [assigned_staff],
                "candidates": list(option.staff_ids),
                "assurance": [
                    {"start": iso(a["start"]), "end": iso(a["end"]), "minutes": a["minutes"], "note": a["note"]}
                    for a in option.assurances
                ],
                "risk_upper": option.risk_upper,
                "risk_band": option.risk_band,
            },
        )
        ws.audit(actor, "accept", {"request": request_id, "plan": plan_kind, "start": iso(option.start)}, at)
    result = option.as_dict()
    result["assigned_staff"] = assigned_staff
    return {"request": request_id, "status": REQ_ACCEPTED, "plan": result}


def reject_request(ws: Workspace, request_id: str, *, actor: str, reason: str, now: datetime | None = None) -> dict:
    at = _now(ws, now)
    with ws.transaction():
        row = ws.request_row(request_id)
        if row is None:
            raise OperationError(f"请求不存在: {request_id}")
        if row["status"] in (REQ_ACCEPTED, REQ_COMPLETED):
            raise OperationError("已承接或已完成的请求不能直接拒绝；请先改期、转人工或由家庭取消")
        released = [
            (r["kind"], r["resource"], r["start"], r["end"]) for r in ws.reservations_for(request_id)
        ]
        ws.conn.execute(
            "UPDATE reservation SET status = 'released', released_at = ?, release_reason = 'rejected' "
            "WHERE request = ? AND status = 'held'",
            (iso(at), request_id),
        )
        ws.conn.execute(
            "UPDATE request_state SET status = ?, status_reason = ?, updated_at = ? WHERE request = ?",
            (REQ_REJECTED, reason, iso(at), request_id),
        )
        ws._event(request_id, at, actor, REQ_REJECTED, {"reason": reason, "released": released})
        ws.audit(actor, "reject", {"request": request_id, "reason": reason, "released": released}, at)
    return {"request": request_id, "status": REQ_REJECTED, "reason": reason}


def cancel_request(
    ws: Workspace,
    request_id: str,
    *,
    actor: str,
    reason: str,
    now: datetime | None = None,
) -> dict:
    at = _now(ws, now)
    with ws.transaction():
        row = ws.request_row(request_id)
        if row is None:
            raise OperationError(f"请求不存在: {request_id}")
        if row["status"] == REQ_CANCELLED:
            raise OperationError("请求已取消")
        if row["status"] == REQ_COMPLETED:
            raise OperationError("请求已完成，履约事实不能取消；如需更正请登记新的说明")
        if row["status"] == REQ_ACCEPTED:
            _assert_not_started(row, at, "取消")
        releases = [
            (r["kind"], r["resource"], r["start"], r["end"])
            for r in ws.reservations_for(request_id)
        ]
        ws.conn.execute(
            "UPDATE reservation SET status = 'released', released_at = ?, release_reason = 'family_cancelled' "
            "WHERE request = ? AND status = 'held'",
            (iso(at), request_id),
        )
        ws.conn.execute(
            "UPDATE proposal SET status = 'cancelled' WHERE request = ? AND status = 'awaiting_family'",
            (request_id,),
        )
        ws.conn.execute(
            "UPDATE request_state SET status = ?, status_reason = ?, updated_at = ? WHERE request = ?",
            (REQ_CANCELLED, reason, iso(at), request_id),
        )
        ws._event(request_id, at, actor, REQ_CANCELLED, {"reason": reason, "released": releases})
        ws.audit(actor, "cancel", {"request": request_id, "reason": reason, "released": releases}, at)
    return {"request": request_id, "status": REQ_CANCELLED, "released": releases}


def acknowledge_request(ws: Workspace, request_id: str, *, actor: str, now: datetime | None = None) -> dict:
    at = _now(ws, now)
    with ws.transaction():
        row = ws.request_row(request_id)
        if row is None:
            raise OperationError(f"请求不存在: {request_id}")
        if row["status"] != REQ_RECEIVED:
            return {"request": request_id, "status": row["status"], "changed": False}
        ws.conn.execute(
            "UPDATE request_state SET status = ?, status_reason = ?, updated_at = ? WHERE request = ?",
            (REQ_PENDING, "运营已受理，等待证据或保障确认", iso(at), request_id),
        )
        ws._event(request_id, at, actor, REQ_PENDING, {"reason": "运营已受理"})
        ws.audit(actor, "acknowledge", {"request": request_id}, at)
    return {"request": request_id, "status": REQ_PENDING, "changed": True}


def _assert_not_started(row, at: datetime, action: str) -> None:
    """已开始的服务保留实际事实，不能被改派或释放其保障。"""
    plan_start = parse_ts(row["plan_start"]) if row["plan_start"] else None
    if plan_start is not None and plan_start <= at:
        raise OperationError(
            f"服务已经开始（{iso(plan_start)}），不能{action}；已发生的接管与完成事实必须保留"
        )


def create_proposal(
    ws: Workspace,
    request_id: str,
    *,
    action: str,
    plan_kind: str,
    start: str,
    actor: str,
    now: datetime | None = None,
    staff_id: str | None = None,
    note: str | None = None,
) -> dict:
    """为受影响（或需改派）的请求准备替代安排。替代安排在被接受前不占用资源。"""
    at = _now(ws, now)
    start_dt = parse_ts(start)
    if action not in (ACTION_RESCHEDULE, ACTION_HUMAN, ACTION_CANCEL):
        raise OperationError("未知处置动作")
    if action == ACTION_CANCEL:
        raise OperationError("取消请直接调用取消操作")
    with ws.transaction():
        payload = _request_payload(ws, request_id)
        row = ws.request_row(request_id)
        if row["status"] == REQ_ACCEPTED:
            _assert_not_started(row, at, "改期或转人工")
        snapshot = ws.snapshot(at)
        plan = build_plan(snapshot, payload, _resource_rows(ws, at), exclude_request=request_id, at=at)
        option = find_option(plan, plan_kind=plan_kind, start=start_dt)
        if option is None or not option.feasible:
            raise OperationError("替代安排当前不可承诺：" + ("；".join(option.reasons) if option else "时段不在范围内"))
        if staff_id and staff_id not in option.staff_ids:
            raise OperationError(f"工作人员 {staff_id} 不在该时段的合格候选内")
        if not option.staff_ids:
            raise OperationError("替代安排没有可用的合格工作人员")
        proposed_staff = staff_id or option.staff_ids[0]
        support = option.assurances[0] if option.assurances else None
        # 同一请求同一时刻只保留一个待家庭确认的替代方案，避免家庭看到互相冲突的建议。
        ws.conn.execute(
            "UPDATE proposal SET status = 'superseded' "
            "WHERE request = ? AND status = 'awaiting_family'",
            (request_id,),
        )
        ws.conn.execute(
            "INSERT INTO proposal(request, action, plan_kind, start, end, robot, staff_ids, coverage_start, "
            "coverage_end, risk_basis, risk_upper, risk_band, status, created_at, created_by, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_family', ?, ?, ?)",
            (
                request_id,
                action,
                plan_kind,
                iso(option.start),
                iso(option.end),
                option.robot,
                json.dumps([proposed_staff], ensure_ascii=False),
                iso(support["start"]) if support else None,
                iso(support["end"]) if support else None,
                json.dumps(option.support, ensure_ascii=False),
                option.risk_upper,
                option.risk_band,
                iso(at),
                actor,
                json.dumps({"note": note, "reasons": option.reasons}, ensure_ascii=False),
            ),
        )
        proposal_id = ws.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        ws.conn.execute(
            "UPDATE proposal SET detail = ? WHERE id = ?",
            (json.dumps({"note": note, "reasons": option.reasons}, ensure_ascii=False), proposal_id),
        )
        current = ws.request_row(request_id)
        # 已承接请求继续显示“已承接”，另标注替代安排待家庭确认；
        # 原安排在新安排被家庭接受前仍然有效。
        status_value = current["status"] if current["status"] == REQ_ACCEPTED else REQ_HELD
        reason = f"已提出{ACTION_ZH_FOR(action)}（建议 #{proposal_id}），等待家庭确认"
        if status_value == REQ_ACCEPTED:
            reason += "；原安排在新安排被接受前继续有效"
        ws.conn.execute(
            "UPDATE request_state SET status = ?, status_reason = ?, updated_at = ? WHERE request = ?",
            (status_value, reason, iso(at), request_id),
        )
        ws._event(
            request_id,
            at,
            actor,
            "proposal",
            {
                "proposal": proposal_id,
                "action": action,
                "plan": plan_kind,
                "start": iso(option.start),
                "end": iso(option.end),
                "staff": [proposed_staff],
            },
        )
        ws.audit(actor, "propose", {"request": request_id, "proposal": proposal_id, "action": action}, at)
    return {"proposal": proposal_id, "request": request_id, "status": "awaiting_family"}


def ACTION_ZH_FOR(action: str) -> str:
    from .domain import ACTION_ZH

    return ACTION_ZH[action]


def confirm_proposal(
    ws: Workspace,
    proposal_id: int,
    *,
    actor: str,
    now: datetime | None = None,
    accept: bool = True,
) -> dict:
    at = _now(ws, now)
    with ws.transaction():
        proposal = ws.proposal(proposal_id)
        if proposal is None:
            raise OperationError("替代安排不存在")
        if proposal["status"] != "awaiting_family":
            raise OperationError(f"替代安排当前状态为 {proposal['status']}，不能确认")
        request_id = proposal["request"]
        current_row = ws.request_row(request_id)
        if current_row is None:
            raise OperationError("请求不存在")
        if current_row["status"] == REQ_ACCEPTED:
            _assert_not_started(current_row, at, "确认改期或转人工")
        if not accept:
            ws.conn.execute("UPDATE proposal SET status = 'declined' WHERE id = ?", (proposal_id,))
            ws._event(request_id, at, actor, "proposal_declined", {"proposal": proposal_id})
            ws.audit(actor, "proposal_declined", {"proposal": proposal_id}, at)
            return {"proposal": proposal_id, "status": "declined"}

        # 家庭确认时再次核对资源：确认前不占用，确认时才落位
        payload, option = _assert_option_current(
            ws, request_id, proposal["plan_kind"], parse_ts(proposal["start"]), at=at
        )
        support = option.assurances[0] if option.assurances else None
        if proposal["coverage_start"] and support:
            if iso(support["start"]) != proposal["coverage_start"] or iso(support["end"]) != proposal["coverage_end"]:
                raise OperationError("保障窗口已经变化，请运营重新提出替代安排")
        proposed_staff = (json.loads(proposal["staff_ids"] or "[]") or [None])[0]
        if proposed_staff not in option.staff_ids:
            raise OperationError("替代安排中的人员已不可用，请运营重新提出安排")

        # 先释放原安排，再建立替代安排；两步在同一事务内完成，
        # 外部观察不到“替代未成立而原安排已丢”的中间状态。
        ws.conn.execute(
            "UPDATE reservation SET status = 'released', released_at = ?, release_reason = 'replaced' "
            "WHERE request = ? AND status = 'held'",
            (iso(at), request_id),
        )
        _write_reservations(ws, request_id, option, at, proposed_staff)
        ws.conn.execute(
            "UPDATE proposal SET status = 'accepted' WHERE id = ?", (proposal_id,)
        )
        ws.conn.execute(
            "UPDATE proposal SET status = 'superseded' "
            "WHERE request = ? AND id != ? AND status = 'awaiting_family'",
            (request_id, proposal_id),
        )
        snapshot = ws.snapshot(at)
        version = None
        risk_basis = {"basis": "人工直接办理，无接管风险估计"}
        if proposal["plan_kind"] == PLAN_ROBOT:
            version = (snapshot.robot_for(payload["family"], payload["task"]) or {}).get("version")
            qual_risk = (option.risk_upper is not None)
            risk_basis = {
                "basis": "家庭确认的替代安排" + ("，按风险上界保留保障" if qual_risk else ""),
                "assurance_minutes": support["minutes"] if support else None,
            }
        ws.conn.execute(
            "UPDATE request_state SET status = ?, status_reason = ?, plan_kind = ?, plan_start = ?, plan_end = ?, "
            "robot = ?, version = ?, staff_required = ?, staff_ids = ?, risk_basis = ?, risk_upper = ?, "
            "risk_band = ?, updated_at = ? WHERE request = ?",
            (
                REQ_ACCEPTED,
                f"家庭已确认{ACTION_ZH_FOR(proposal['action'])}，原安排已释放",
                proposal["plan_kind"],
                iso(option.start),
                iso(option.end),
                option.robot,
                version,
                1 if proposal["plan_kind"] == PLAN_ROBOT else 0,
                json.dumps([proposed_staff], ensure_ascii=False),
                json.dumps(risk_basis, ensure_ascii=False),
                option.risk_upper,
                option.risk_band,
                iso(at),
                request_id,
            ),
        )
        ws._event(
            request_id,
            at,
            actor,
            REQ_ACCEPTED,
            {
                "plan": proposal["plan_kind"],
                "start": iso(option.start),
                "end": iso(option.end),
                "robot": option.robot,
                "version": version,
                "staff": [proposed_staff],
                "proposal": proposal_id,
                "replaced_by_family_confirmation": True,
            },
        )
        ws.audit(actor, "confirm_proposal", {"proposal": proposal_id, "request": request_id}, at)
    return {"proposal": proposal_id, "request": request_id, "status": REQ_ACCEPTED}


def _inserted_count(option) -> int:
    if option.plan == PLAN_ROBOT:
        return 2
    if option.plan == PLAN_HUMAN:
        return len(option.staff_ids)
    return 0


def hold_request(ws: Workspace, request_id: str, *, actor: str, reason: str, now: datetime | None = None) -> dict:
    at = _now(ws, now)
    with ws.transaction():
        row = ws.request_row(request_id)
        if row is None:
            raise OperationError(f"请求不存在: {request_id}")
        if row["status"] not in (REQ_ACCEPTED, REQ_PENDING, REQ_RECEIVED, REQ_HELD):
            raise OperationError(f"当前状态 {row['status']} 不能挂起")
        ws.conn.execute(
            "UPDATE request_state SET status = ?, status_reason = ?, updated_at = ? WHERE request = ?",
            (REQ_HELD, reason, iso(at), request_id),
        )
        ws._event(request_id, at, actor, REQ_HELD, {"reason": reason})
        ws.audit(actor, "hold", {"request": request_id, "reason": reason}, at)
    return {"request": request_id, "status": REQ_HELD}


def release_reservations_for_reschedule(ws: Workspace, request_id: str, *, reason: str, at: datetime) -> None:
    ws.conn.execute(
        "UPDATE reservation SET status = 'released', released_at = ?, release_reason = ? "
        "WHERE request = ? AND status = 'held'",
        (iso(at), reason, request_id),
    )


def record_fulfillment(
    ws: Workspace,
    request_id: str,
    *,
    outcome: str,
    actor: str,
    now: datetime | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
    note: str | None = None,
    evidence_used: bool = False,
    force: bool = False,
) -> dict:
    at = _now(ws, now)
    if outcome not in ("independent", "handoff", "refused", "unknown"):
        raise OperationError("结果必须是 independent/handoff/refused/unknown 之一")
    with ws.transaction():
        row = ws.request_row(request_id)
        if row is None:
            raise OperationError(f"请求不存在: {request_id}")
        if not force and row["status"] not in (REQ_ACCEPTED, REQ_HELD):
            raise OperationError("只有已承接的请求可以记录履约结果；如属例外请显式确认")
        existing = ws.conn.execute(
            "SELECT 1 FROM fulfillment WHERE request = ?", (request_id,)
        ).fetchone()
        if existing and not force:
            raise OperationError("该请求已记录过履约结果；如确需更正，请显式确认")
        start = parse_ts(started_at) if started_at else None
        complete = parse_ts(completed_at) if completed_at else None
        ws.conn.execute(
            "INSERT INTO fulfillment(request, family, task, robot, version, outcome, started_at, completed_at, "
            "recorded_at, note, evidence_used) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request_id,
                row["family"],
                row["task"],
                row["robot"],
                row["version"],
                outcome,
                iso(start),
                iso(complete),
                iso(at),
                note,
                1 if evidence_used else 0,
            ),
        )
        # 服务已经发生：设备与保障窗口不再属于“未来承诺”，但履约事实保留。
        ws.conn.execute(
            "UPDATE reservation SET status = 'released', released_at = ?, release_reason = 'completed' "
            "WHERE request = ? AND status = 'held'",
            (iso(at), request_id),
        )
        # 已记录的履约事实终结未确认的替代建议，避免家庭看到过期选项。
        ws.conn.execute(
            "UPDATE proposal SET status = 'superseded' "
            "WHERE request = ? AND status = 'awaiting_family'",
            (request_id,),
        )
        ws.conn.execute(
            "UPDATE request_state SET status = ?, status_reason = ?, updated_at = ? WHERE request = ?",
            (REQ_COMPLETED, f"已记录结果：{OUTCOME_ZH[outcome]}", iso(at), request_id),
        )
        ws._event(
            request_id,
            at,
            actor,
            "fulfillment",
            {"outcome": outcome, "note": note, "started_at": iso(start), "completed_at": iso(complete)},
        )
        ws.audit(actor, "fulfillment", {"request": request_id, "outcome": outcome}, at)
    return {"request": request_id, "outcome": outcome}
