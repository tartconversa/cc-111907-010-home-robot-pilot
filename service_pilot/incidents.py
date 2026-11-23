"""维护延期、能力复查失败、授权撤回对未开始服务的影响识别。

发现影响只做识别与挂起，不替运营或家庭做处置。替代安排成立后才释放原占用；
已经开始的履约事实不因授权撤回被删除。
"""
from __future__ import annotations

import json
from datetime import datetime

from .domain import (
    ACTION_CANCEL,
    ACTION_HUMAN,
    ACTION_RESCHEDULE,
    REQ_ACCEPTED,
    REQ_HELD,
    Interval,
    iso,
    parse_ts,
)
from .storage import Workspace




def affected_requests(ws: Workspace, *, now: datetime | None = None) -> list[dict]:
    """扫描未开始的已承接请求，返回受影响的处理建议。

    只报告仍处于 accepted（或已挂起但仍有占用）且服务尚未开始的请求。
    """
    at = now or ws.now()
    snapshot = ws.snapshot(at)
    findings: list[dict] = []
    for row in ws.requests(at):
        if row["status"] not in (REQ_ACCEPTED, REQ_HELD):
            continue
        # 已记录履约的任务保留实际接管与完成事实，不再作为“未开始服务”改写
        start = parse_ts(row["plan_start"]) if row["plan_start"] else None
        if start is None or start <= at:
            continue  # 已开始（含起点到达）或尚无安排：不改写实际事实
        reasons: list[str] = []
        # 1) 维护与设备
        robot = row["robot"]
        if robot:
            for maint in snapshot.maintenance_intervals(robot):
                if Interval(start, parse_ts(row["plan_end"])).overlaps(maint):
                    reasons.append(
                        f"设备维护/延期覆盖 {iso(maint.start)}–{iso(maint.end)}"
                    )
        # 2) 能力复查：必须以“已承接时选定的版本”为准，不能拿最新版本顶替
        version = row["version"]
        if robot and version:
            cert = snapshot.certification_state(robot, version, row["task"])
            # 只有明确的复查失败会推翻已承接的机器人主办。
            # 从未有过复查记录属于“评估时证据缺口”，不追认为事后影响。
            if cert["state"] == "fail":
                review = cert["review"]
                reasons.append(
                    f"能力复查未通过（{iso(parse_ts(review['effective']))} 记录）"
                )
        # 3) 服务授权撤回
        if not snapshot.service_authorized(row["family"], row["task"]):
            reasons.append("家庭撤回了该任务的服务授权")
        if not reasons:
            continue
        options = _remediation_options(ws, row, at, reasons)
        findings.append(
            {
                "request": row["request"],
                "family": row["family"],
                "task": row["task"],
                "status": row["status"],
                "plan_start": row["plan_start"],
                "plan_end": row["plan_end"],
                "reasons": reasons,
                "options": options,
            }
        )
    return findings


def _remediation_options(ws: Workspace, row, at: datetime, reasons: list[str]) -> list[dict]:
    from .planner import build_plan

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
    snapshot = ws.snapshot(at)
    plan = build_plan(snapshot, payload, reservations, exclude_request=row["request"], at=at)
    options = []
    for option in plan.feasible_options:
        if option.plan == "defer":
            continue
        options.append(
            {
                "action": ACTION_RESCHEDULE,
                "plan": option.plan,
                "start": iso(option.start),
                "end": iso(option.end),
                "staff": list(option.staff_ids),
                "reasons": option.reasons,
            }
        )
    # 同一时段同时给改期与转人工的选择
    human = [o for o in options if o["plan"] == "human"]
    robot = [o for o in options if o["plan"] == "robot"]
    labelled = []
    for o in robot:
        labelled.append(dict(o, action=ACTION_RESCHEDULE, action_zh="改期后仍由机器人主办"))
    for o in human:
        labelled.append(dict(o, action=ACTION_HUMAN, action_zh="转人工（需家庭确认）"))
    labelled.append(
        {
            "action": ACTION_CANCEL,
            "action_zh": "取消并释放承诺",
            "plan": None,
            "start": None,
            "end": None,
            "staff": [],
            "reasons": ["取消后释放设备和保障窗口"],
        }
    )
    return labelled
