"""承诺规划：机器人主办、人工直接办理、暂缓。

机器人主办必须同时满足：
  - 设备声明含该任务，且没有未通过的能力复查；
  - 该家庭自己的历史证据达到任务政策最低量与人工复核；
  - 请求环境/物品有本地证据，缺少环境不能默认安全；
  - 按接管风险估计保留合格人员的保障窗口，且该窗口未被其他家庭占用。

同一名人员的保障与服务时间都是独占资源；建议或待确认的替代安排不占用。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable

from .domain import (
    BLOCKED,
    FEASIBLE,
    PENDING,
    PLAN_DEFER,
    PLAN_HUMAN,
    PLAN_ROBOT,
    Interval,
    Option,
    iso,
    merge_intervals,
    parse_ts,
    subtract_interval,
)
from .evidence import (
    RISK_BAND_FACTOR,
    EvidenceSnapshot,
    allowed_windows,
    service_inside_allowed_hours,
)

GRID_MINUTES = 15


@dataclass
class RequestPlan:
    request: str
    family: str
    task: str
    earliest: datetime
    latest: datetime
    minutes: int
    object_name: str | None
    environment: str
    qualification: dict
    options: list[Option] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def feasible_options(self) -> list[Option]:
        return [o for o in self.options if o.feasible]

    def as_dict(self) -> dict:
        return {
            "request": self.request,
            "family": self.family,
            "task": self.task,
            "object": self.object_name,
            "environment": self.environment,
            "earliest": iso(self.earliest),
            "latest": iso(self.latest),
            "minutes": self.minutes,
            "qualification": self.qualification,
            "notes": self.notes,
            "options": [o.as_dict() for o in self.options],
        }


def _minutes(value: int) -> timedelta:
    return timedelta(minutes=value)


def _reservation_rows(ws, *, as_of: datetime | None = None) -> list[dict]:
    rows = ws.active_reservations(as_of)
    result = []
    for row in rows:
        result.append(
            {
                "id": row["id"],
                "request": row["request"],
                "kind": row["kind"],
                "resource": row["resource"],
                "start": parse_ts(row["start"]),
                "end": parse_ts(row["end"]),
                "meta": json.loads(row["meta"] or "{}"),
            }
        )
    return result


def reserved_intervals(
    reservations: Iterable[dict],
    kind: str,
    resource: str,
    *,
    exclude_request: str | None = None,
) -> list[Interval]:
    return [
        Interval(r["start"], r["end"])
        for r in reservations
        if r["kind"] == kind and r["resource"] == resource and r["request"] != exclude_request
    ]


def staff_availability(snapshot: EvidenceSnapshot, staff_id: str, region: str, task: str) -> list[Interval]:
    windows = []
    for row in snapshot.staff:
        if row.get("staff") != staff_id:
            continue
        if region not in (row.get("regions") or []):
            continue
        if task not in (row.get("tasks") or []):
            continue
        start = parse_ts(row["start"])
        end = parse_ts(row["end"])
        if start and end and end > start:
            windows.append(Interval(start, end))
    merged = merge_intervals(windows)
    if not merged:
        return []
    unavailable = []
    for row in snapshot.staff_unavailability:
        if row.get("staff") == staff_id:
            start, end = parse_ts(row["start"]), parse_ts(row["end"])
            if start and end and end > start:
                unavailable.append(Interval(start, end))
    free = []
    for window in merged:
        free.extend(subtract_interval(window, unavailable))
    return free


def staff_candidates(
    snapshot: EvidenceSnapshot,
    region: str,
    task: str,
    interval: Interval,
    reservations: Iterable[dict],
    *,
    exclude_request: str | None = None,
) -> list[str]:
    staff_ids = sorted({row["staff"] for row in snapshot.staff})
    result = []
    for staff_id in staff_ids:
        windows = staff_availability(snapshot, staff_id, region, task)
        if not any(w.start <= interval.start and interval.end <= w.end for w in windows):
            continue
        busy = reserved_intervals(reservations, "staff", staff_id, exclude_request=exclude_request)
        if any(interval.overlaps(b) for b in busy):
            continue
        result.append(staff_id)
    return result


def staff_free_windows(
    snapshot: EvidenceSnapshot,
    staff_id: str,
    reservations: Iterable[dict],
    *,
    region: str | None = None,
    task: str | None = None,
) -> list[Interval]:
    windows: list[Interval] = []
    for row in snapshot.staff:
        if row.get("staff") != staff_id:
            continue
        if region is not None and region not in (row.get("regions") or []):
            continue
        if task is not None and task not in (row.get("tasks") or []):
            continue
        windows.append(Interval(parse_ts(row["start"]), parse_ts(row["end"])))
    merged = merge_intervals(windows)
    unavailable = [
        Interval(parse_ts(row["start"]), parse_ts(row["end"]))
        for row in snapshot.staff_unavailability
        if row.get("staff") == staff_id
    ]
    busy = reserved_intervals(reservations, "staff", staff_id)
    cuts = merge_intervals(unavailable + busy)
    free: list[Interval] = []
    for window in merged:
        free.extend(subtract_interval(window, cuts))
    return free


def assurance_interval(
    risk: dict,
    service: Interval,
    earliest: datetime,
    latest: datetime,
) -> tuple[Interval, str, bool]:
    """按风险上界计算保障窗口。

    返回 (窗口, 说明, 是否可满足)。窗口不得被请求最晚时点悄悄截短：
    若所需保障超出请求窗口，应视为无法保留足够保障，而不是把风险降级。
    """
    band = risk.get("band", "unknown")
    factor = RISK_BAND_FACTOR.get(band)
    if factor is None:
        return Interval(earliest, latest), "风险范围不确定，采用全窗口保障", True
    minutes = int(math.ceil(service.minutes * factor))
    end = service.start + _minutes(minutes)
    note = f"接管风险上界 {risk.get('upper_95')} 对应{factor:g}倍保障：{minutes} 分钟"
    if end > latest:
        return Interval(service.start, end), (
            note + f"；所需保障到 {iso(end)}，超出请求最晚时点 {iso(latest)}，无法完整保留"
        ), False
    return Interval(service.start, end), note, True


def _candidate_starts(earliest: datetime, latest: datetime, minutes: int) -> list[datetime]:
    duration = _minutes(minutes)
    if earliest + duration > latest:
        return []
    starts = []
    cursor = earliest
    step = _minutes(GRID_MINUTES)
    while cursor + duration <= latest:
        starts.append(cursor)
        cursor += step
    # 最晚可开始时刻通常不在 15 分钟网格上；单独补上，避免漏掉合法时段。
    last = latest - duration
    if last >= earliest and last not in starts:
        starts.append(last)
    return starts


def _maintenance_reason(overlaps: list[Interval]) -> str:
    pieces = ", ".join(f"{iso(i.start)}–{iso(i.end)}" for i in overlaps)
    return f"设备维护不可用：{pieces}"


def build_plan(
    snapshot: EvidenceSnapshot,
    request: dict,
    reservations: Iterable[dict],
    *,
    exclude_request: str | None = None,
    at: datetime | None = None,
) -> RequestPlan:
    earliest = parse_ts(request["earliest"])
    latest = parse_ts(request["latest"])
    minutes = int(request["minutes"])
    family = request["family"]
    task = request["task"]
    object_name = request.get("object")
    environment = request.get("environment") or "unknown"
    at = at or snapshot.at

    plan = RequestPlan(
        request=request.get("request"),
        family=family,
        task=task,
        earliest=earliest,
        latest=latest,
        minutes=minutes,
        object_name=object_name,
        environment=environment,
        qualification={},
    )
    family_row = snapshot.families.get(family)
    if family_row is None:
        plan.qualification = {
            "status": BLOCKED,
            "reasons": [f"家庭 {family} 未登记"],
            "risk": None,
        }
        plan.notes.append("家庭资料缺失，无法安排")
        return plan

    qual = snapshot.qualification(family, task, object_name=object_name, environment=environment)
    plan.qualification = qual
    region = family_row.get("region")

    # 服务授权：机器人主办与人工直接办理都需要任务级服务授权
    service_ok = snapshot.service_authorized(family, task)
    service_scope = snapshot.service_scope_status(family, task)
    if not service_ok:
        if service_scope and service_scope.get("status") == "revoked":
            reason = (
                f"家庭已撤回 {task} 的服务授权（{iso(parse_ts(service_scope['effective']))} 生效）；"
                "撤回后不承接新安排，恢复设备健康也不会自动恢复授权"
            )
        else:
            reason = f"缺少 {task} 的服务授权"
        plan.notes.append(reason)

    allowed = allowed_windows(family_row, earliest, latest)
    if not allowed:
        plan.notes.append("请求时段与家庭可接受小时没有交集")

    starts = _candidate_starts(earliest, latest, minutes)
    if not starts:
        plan.notes.append("请求时段不足以容纳预计时长")

    robot_row = snapshot.robot_for(family, task)
    robot = robot_row["robot"] if robot_row else None
    version = robot_row["version"] if robot_row else None
    maintenance = snapshot.maintenance_intervals(robot) if robot else []

    seen_reason_once: set[tuple[str, str]] = set()

    for start in starts:
        service = Interval(start, start + _minutes(minutes))
        if not service_inside_allowed_hours(family_row, service):
            continue
        # 机器人主办
        if robot:
            robot_busy = reserved_intervals(
                reservations, "robot", robot, exclude_request=exclude_request
            )
            maint = [m for m in maintenance if m.overlaps(service)]
            busy = [m for m in robot_busy if m.overlaps(service)]
            robot_free = not maint and not busy
            robot_reasons = []
            if maint:
                robot_reasons.append(_maintenance_reason(maint))
            if busy:
                robot_reasons.append(
                    "设备已被其他已承接请求占用："
                    + ", ".join(f"{iso(m.start)}–{iso(m.end)}" for m in busy)
                )
            if not service_ok:
                robot_reasons.append("没有有效的任务服务授权")
            if qual.get("status") == FEASIBLE and robot_free and service_ok:
                risk = qual.get("risk") or {}
                assurance, assurance_note, assurance_fits = assurance_interval(
                    risk, service, earliest, latest
                )
                candidates = (
                    staff_candidates(
                        snapshot, region, task, assurance, reservations, exclude_request=exclude_request
                    )
                    if assurance_fits
                    else []
                )
                option = Option(
                    label=f"robot:{iso(start)}",
                    start=start,
                    end=service.end,
                    plan=PLAN_ROBOT,
                    feasible=bool(candidates),
                    status=FEASIBLE if candidates else PENDING,
                    reasons=[],
                    robot=robot,
                    staff_ids=tuple(candidates),
                    handoff_risk=risk.get("posterior_mean"),
                    risk_upper=risk.get("upper_95"),
                    risk_band=risk.get("band"),
                    assurances=[
                        {
                            "role": "assurance",
                            "start": assurance.start,
                            "end": assurance.end,
                            "minutes": assurance.minutes,
                            "note": assurance_note,
                        }
                    ],
                    support=qual.get("reasons", []),
                )
                if not assurance_fits:
                    option.reasons.append(assurance_note)
                elif candidates:
                    option.reasons.append(
                        f"合格接管人员 {', '.join(candidates)} 在 {iso(assurance.start)}–{iso(assurance.end)} 可用"
                    )
                else:
                    option.reasons.append(
                        f"没有具备 {region}/{task} 资格的接管人员在保障窗口 "
                        f"{iso(assurance.start)}–{iso(assurance.end)} 完整可用"
                    )
                plan.options.append(option)
            else:
                reasons = list(robot_reasons)
                if qual.get("status") != FEASIBLE:
                    reasons.append("；".join(qual.get("reasons") or ["证据不足"]))
                key = (BLOCKED if any(robot_reasons) or qual.get("status") == BLOCKED else PENDING, "|".join(reasons))
                if key not in seen_reason_once:
                    seen_reason_once.add(key)
                    plan.options.append(
                        Option(
                            label=f"robot:{iso(start)}",
                            start=start,
                            end=service.end,
                            plan=PLAN_ROBOT,
                            feasible=False,
                            status=BLOCKED if key[0] == BLOCKED else PENDING,
                            reasons=reasons,
                            robot=robot,
                            handoff_risk=(qual.get("risk") or {}).get("posterior_mean"),
                            risk_upper=(qual.get("risk") or {}).get("upper_95"),
                            risk_band=(qual.get("risk") or {}).get("band"),
                        )
                    )
        # 人工直接办理：同样要求家庭任务许可与服务授权
        if service_ok and task in (family_row.get("allowed_tasks") or []):
            candidates = staff_candidates(
                snapshot, region, task, service, reservations, exclude_request=exclude_request
            )
            option = Option(
                label=f"human:{iso(start)}",
                start=start,
                end=service.end,
                plan=PLAN_HUMAN,
                feasible=bool(candidates),
                status=FEASIBLE if candidates else PENDING,
                reasons=[],
                staff_ids=tuple(candidates),
                assurances=[],
            )
            if candidates:
                option.reasons.append(
                    f"合格工作人员 {', '.join(candidates)} 可直接办理 {iso(start)}–{iso(service.end)}"
                )
            else:
                option.reasons.append(
                    f"没有具备 {region}/{task} 资格的工作人员在 {iso(start)}–{iso(service.end)} 完整可用"
                )
            plan.options.append(option)

    # 暂缓：不占用资源，也不构成承诺
    plan.options.append(
        Option(
            label="defer",
            start=earliest,
            end=earliest + _minutes(minutes),
            plan=PLAN_DEFER,
            feasible=True,
            status=FEASIBLE,
            reasons=[
                "暂缓不占用设备或人员，也不会向家庭显示为已承接",
                "暂缓期间若证据、授权或人员变化，需重新评估，不能沿用旧结论",
            ],
        )
    )

    order = {PLAN_ROBOT: 0, PLAN_HUMAN: 1, PLAN_DEFER: 2}
    plan.options.sort(key=lambda o: (0 if o.feasible else 1, order.get(o.plan, 3), o.start, o.label))

    if not plan.feasible_options:
        plan.notes.append("当前没有可承诺的安排；只能暂缓或等待补充证据/保障")
    return plan


def find_option(plan: RequestPlan, *, plan_kind: str, start: datetime) -> Option | None:
    for option in plan.options:
        if option.plan == plan_kind and option.start == start:
            return option
    return None
