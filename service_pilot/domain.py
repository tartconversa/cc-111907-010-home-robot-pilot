"""领域常量与通用值对象。

时间一律使用带时区的 UTC datetime；区间左闭右开。
缺失值使用 None（绝不当作 0 或“正常”）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

OUTCOMES = ("independent", "handoff", "refused", "unknown")
OUTCOME_ZH = {
    "independent": "机器人独立完成",
    "handoff": "人工接管后完成",
    "refused": "住户拒绝",
    "unknown": "结果未知",
}

# 可行性判定状态
FEASIBLE = "feasible"
PENDING = "pending"          # 资料缺失或存在待确认事项，不能算已承诺
BLOCKED = "blocked"          # 明确不可行
INCIDENT = "incident"        # 已受影响，需要处置选择

# 请求生命周期
REQ_RECEIVED = "received"    # 尚未评估
REQ_PENDING = "pending"      # 已评估但尚不能承诺（等待确认/补资料）
REQ_ACCEPTED = "accepted"    # 已承接：占用设备与保障
REQ_HELD = "held"            # 因维护/复查/授权受影响，等待处置选择
REQ_REJECTED = "rejected"    # 未承接（明确不可行或运营暂缓）
REQ_COMPLETED = "completed"  # 已记录履约结果
REQ_CANCELLED = "cancelled"  # 家庭取消，承诺释放

# 安排方式
PLAN_ROBOT = "robot"
PLAN_HUMAN = "human"
PLAN_DEFER = "defer"

PLAN_ZH = {PLAN_ROBOT: "机器人主办", PLAN_HUMAN: "人工直接办理", PLAN_DEFER: "暂缓"}

# 请求处置动作（受影响请求）
ACTION_RESCHEDULE = "reschedule"
ACTION_HUMAN = "human"
ACTION_CANCEL = "cancel"
ACTION_ZH = {
    ACTION_RESCHEDULE: "改期（重选时段）",
    ACTION_HUMAN: "转人工（需家庭确认）",
    ACTION_CANCEL: "取消",
}

UTC = timezone.utc


def parse_ts(value: Any) -> datetime | None:
    """把 ISO 时间解析为 UTC datetime；null 保持 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"时间缺少时区: {value!r}")
    return dt.astimezone(UTC)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")



@dataclass(frozen=True, order=True)
class Interval:
    """左闭右开时间区间。"""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("区间必须带时区")
        if self.end < self.start:
            raise ValueError("区间结束早于开始")

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    def overlaps(self, other: "Interval") -> bool:
        return self.start < other.end and other.start < self.end

    def intersection(self, other: "Interval") -> "Interval | None":
        start = max(self.start, other.start)
        end = min(self.end, other.end)
        if start >= end:
            return None
        return Interval(start, end)

    def merge(self, other: "Interval") -> "Interval":
        return Interval(min(self.start, other.start), max(self.end, other.end))

    def shifted(self, minutes: int) -> "Interval":
        delta = timedelta(minutes=minutes)
        return Interval(self.start + delta, self.end + delta)


def merge_intervals(items: Iterable[Interval]) -> list[Interval]:
    ordered = sorted(items)
    result: list[Interval] = []
    for item in ordered:
        if result and item.start <= result[-1].end:
            result[-1] = result[-1].merge(item)
        else:
            result.append(item)
    return result


def subtract_interval(base: Interval, cuts: Iterable[Interval]) -> list[Interval]:
    """base 减去若干不可用区间，返回剩余可用片段。"""
    pieces = [base]
    for cut in merge_intervals(c for c in cuts if c.overlaps(base)):
        nxt: list[Interval] = []
        for piece in pieces:
            if not piece.overlaps(cut):
                nxt.append(piece)
                continue
            if piece.start < cut.start:
                nxt.append(Interval(piece.start, min(piece.end, cut.start)))
            if cut.end < piece.end:
                nxt.append(Interval(max(piece.start, cut.end), piece.end))
        pieces = nxt
    return [p for p in pieces if p.end > p.start]





@dataclass
class Option:
    """某请求在某个时段下的候选安排。"""

    label: str                   # 唯一标签，如 plan/start/end/staff
    start: datetime
    end: datetime
    plan: str                    # robot / human / defer
    feasible: bool
    status: str                  # feasible / pending / blocked
    reasons: list[str] = field(default_factory=list)
    assurances: list[dict] = field(default_factory=list)
    robot: str | None = None
    staff_ids: tuple[str, ...] = ()
    handoff_risk: float | None = None
    risk_upper: float | None = None
    risk_band: str | None = None
    support: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "start": iso(self.start),
            "end": iso(self.end),
            "plan": self.plan,
            "plan_zh": PLAN_ZH.get(self.plan, self.plan),
            "feasible": self.feasible,
            "status": self.status,
            "reasons": list(self.reasons),
            "assurances": [dict(a, start=iso(a["start"]) if isinstance(a.get("start"), datetime) else a.get("start"),
                                end=iso(a["end"]) if isinstance(a.get("end"), datetime) else a.get("end"))
                           for a in self.assurances],
            "robot": self.robot,
            "staff": list(self.staff_ids),
            "handoff_risk": self.handoff_risk,
            "risk_upper": self.risk_upper,
            "risk_band": self.risk_band,
            "support": self.support,
        }
