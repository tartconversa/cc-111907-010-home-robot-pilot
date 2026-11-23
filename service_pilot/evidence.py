"""证据、统计与能力资格。

统计规则（与 INPUT_CONTRACT 一致）：
- 尝试、独立完成、接管后完成、拒绝、未知分列；不把拒绝或未知算作成功。
- 设备/任务/版本是不同组合，不合并成功率。
- 停机期间没有尝试，不产生任何“可靠性提高”的推断。
- 风险以 Beta(1,1) 后验均值估计，并以 Wilson 95% 上界表达不确定性；
  上界同时决定接管保障时长。样本过少时采用全窗口保障。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from .domain import (
    BLOCKED,
    FEASIBLE,
    PENDING,
    Interval,
    iso,
    parse_ts,
)

SUPPORTED_ENVIRONMENT_MIN = 3
WILSON_Z = 1.96

# 保障系数：按接管风险上界
RISK_BAND_FACTOR = {
    "low": 1.0,
    "medium": 1.5,
    "high": 2.0,
    "unknown": None,  # 全窗口
}
RISK_BAND_ZH = {
    "low": "低",
    "medium": "中",
    "high": "高",
    "unknown": "不确定（全窗口保障）",
}


def wilson_upper(successes: int, n: int, z: float = WILSON_Z) -> float | None:
    if n <= 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return min(1.0, centre + margin)


def posterior_mean(successes: int, n: int) -> float | None:
    if n < 0:
        return None
    return (successes + 1) / (n + 2)


def risk_band(upper: float | None) -> str:
    if upper is None:
        return "unknown"
    if upper <= 0.20:
        return "low"
    if upper <= 0.40:
        return "medium"
    return "high"


@dataclass
class Stratum:
    family: str
    robot: str
    version: str
    task: str
    object: str | None
    environment: str | None
    observations: list[dict] = field(default_factory=list)

    @property
    def attempts(self) -> int:
        return len(self.observations)

    def count(self, outcome: str) -> int:
        return sum(1 for row in self.observations if row.get("outcome") == outcome)

    @property
    def reviewed(self) -> int:
        return sum(1 for row in self.observations if row.get("reviewed") is True)

    @property
    def handoff_risk(self) -> float | None:
        return posterior_mean(self.count("handoff"), self.attempts)

    @property
    def risk_upper(self) -> float | None:
        return wilson_upper(self.count("handoff"), self.attempts)

    @property
    def band(self) -> str:
        if self.attempts < 5:
            return "unknown"
        return risk_band(self.risk_upper)

    def summary(self) -> dict:
        return {
            "family": self.family,
            "robot": self.robot,
            "version": self.version,
            "task": self.task,
            "object": self.object,
            "environment": self.environment,
            "attempts": self.attempts,
            "independent": self.count("independent"),
            "handoff": self.count("handoff"),
            "refused": self.count("refused"),
            "unknown": self.count("unknown"),
            "reviewed": self.reviewed,
            "unknown_fraction": round(self.count("unknown") / self.attempts, 4) if self.attempts else None,
            "handoff_risk": round(self.handoff_risk, 4) if self.handoff_risk is not None else None,
            "risk_upper": round(self.risk_upper, 4) if self.risk_upper is not None else None,
            "risk_band": self.band,
        }


def _group_key(row: dict) -> tuple:
    return (
        row.get("family"),
        row.get("robot"),
        row.get("version"),
        row.get("task"),
        row.get("object"),
        row.get("environment"),
    )


@dataclass
class EvidenceSnapshot:
    """某一分析时点可见的来源事实。"""

    at: datetime
    families: dict[str, dict]
    robots: list[dict]
    policy: dict[str, dict]
    history: list[dict]
    consents: list[dict]
    staff: list[dict]
    maintenance: list[dict]
    capability_reviews: list[dict] = field(default_factory=list)
    staff_unavailability: list[dict] = field(default_factory=list)
    fulfilments: list[dict] = field(default_factory=list)
    analysis_denied: set[str] = field(default_factory=set)
    withheld_families: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)

    # ---------- 授权 ----------
    def active_scopes(self, family: str) -> dict[str, dict]:
        """截至 snapshot，每个 scope 最新一份授权事件。"""
        events = [
            row
            for row in self.consents
            if row.get("family") == family
            and (parse_ts(row.get("effective")) or datetime.min.replace(tzinfo=timezone.utc)) <= self.at
        ]
        latest: dict[str, dict] = {}
        for row in sorted(
            events,
            key=lambda r: (parse_ts(r["effective"]), iso(parse_ts(r.get("received"))) or ""),
        ):
            latest[str(row.get("scope"))] = row
        return latest

    def service_authorized(self, family: str, task: str) -> bool:
        scopes = self.active_scopes(family)
        row = scopes.get(f"service/{task}")
        if row is None:
            return False
        return row.get("status") == "active"



    def analysis_authorized(self, family: str) -> bool:
        scopes = self.active_scopes(family)
        row = scopes.get("analysis")
        return bool(row and row.get("status") == "active")

    def service_scope_status(self, family: str, task: str) -> dict | None:
        row = self.active_scopes(family).get(f"service/{task}")
        return row

    # ---------- 设备 ----------
    def robots_for(self, family: str) -> list[dict]:
        return [row for row in self.robots if row.get("family") == family]

    def robot_for(self, family: str, task: str | None = None) -> dict | None:
        rows = self.robots_for(family)
        if task is not None:
            declaring = [row for row in rows if task in (row.get("tasks") or [])]
            if declaring:
                return declaring[-1]
        return rows[-1] if rows else None

    def robot_versions(self, robot: str) -> list[dict]:
        return [r for r in self.robots if r.get("robot") == robot]

    def latest_capability_review(self, robot: str, version: str, task: str) -> dict | None:
        rows = [
            r
            for r in self.capability_reviews
            if r.get("robot") == robot
            and r.get("version") == version
            and r.get("task") == task
            and (parse_ts(r.get("effective")) or datetime.min.replace(tzinfo=timezone.utc)) <= self.at
        ]
        if not rows:
            return None
        return sorted(rows, key=lambda r: parse_ts(r["effective"]))[-1]

    # ---------- 维护 ----------
    def maintenance_intervals(self, robot: str) -> list[Interval]:
        return [
            Interval(parse_ts(row["start"]), parse_ts(row["end"]))
            for row in self.maintenance
            if row.get("robot") == robot
        ]

    # ---------- 历史 ----------
    def revoked_analysis_families(self) -> set[str]:
        """曾授权分析、后来撤回的家庭。

        撤回之后不使用其历史估计；撤回前已经发生的履约事实仍可保留
        （见 fulfillments），不会被伪造为“从未发生”。
        """
        revoked = set()
        for family in self.families:
            scopes = self.active_scopes(family)
            row = scopes.get("analysis")
            if row is None or row.get("status") != "active":
                # 只有确实存在过授权记录才算“撤回”，未授权家庭单独处理
                if any(c.get("family") == family and c.get("scope") == "analysis" for c in self.consents):
                    revoked.add(family)
        return revoked

    def usable_history(self) -> list[dict]:
        usable = []
        for row in self.history:
            family = row.get("family")
            if not self.analysis_authorized(family):
                self.withheld_families.add(family)
                continue
            usable.append(row)
        # 运营登记且明确标记为证据的履约结果，可作为后续证据；
        # 未标记的不进入资格估计。授权撤回后不再使用。
        for row in self.fulfilments:
            if not row.get("evidence_used"):
                continue
            if row.get("robot") is None or row.get("version") is None:
                continue
            if not self.analysis_authorized(row.get("family")):
                self.withheld_families.add(row.get("family"))
                continue
            usable.append(row)
        return usable

    def strata(self) -> dict[tuple, Stratum]:
        groups: dict[tuple, Stratum] = {}
        for row in self.usable_history():
            key = _group_key(row)
            stratum = groups.get(key)
            if stratum is None:
                stratum = Stratum(
                    family=row.get("family"),
                    robot=row.get("robot"),
                    version=row.get("version"),
                    task=row.get("task"),
                    object=row.get("object"),
                    environment=row.get("environment"),
                )
                groups[key] = stratum
            stratum.observations.append(row)
        return groups

    def family_task_strata(self, family: str, robot: str, version: str, task: str) -> list[Stratum]:
        return [
            s
            for s in self.strata().values()
            if s.family == family and s.robot == robot and s.version == version and s.task == task
        ]

    def certification_state(self, robot: str, version: str, task: str) -> dict:
        """复查状态。新版本没有 pass 记录时，旧版本结论不能沿用。

        返回 stale=True 表示“有旧版本曾通过、但当前版本尚未通过复查”。
        """
        review = self.latest_capability_review(robot, version, task)
        if review is not None:
            return {
                "state": "pass" if review.get("result") == "pass" else "fail",
                "review": review,
                "stale": False,
            }
        other = [
            row
            for row in self.capability_reviews
            if row.get("robot") == robot
            and row.get("task") == task
            and row.get("version") != version
            and row.get("result") == "pass"
        ]
        return {"state": "missing", "review": None, "stale": bool(other), "previous_versions": other}

    def qualification(
        self,
        family: str,
        task: str,
        *,
        object_name: str | None,
        environment: str,
    ) -> dict:
        """判断机器人主办是否“可考虑”，并给出证据、不确定性与缺口。"""
        robot_row = self.robot_for(family, task)
        result: dict = {
            "family": family,
            "task": task,
            "object": object_name,
            "environment": environment,
            "status": PENDING,
            "reasons": [],
            "policy": self.policy.get(task),
            "robot": None,
            "version": None,
            "counts": None,
            "risk": None,
            "strata": [],
            "coverage": {},
            "selective_demo": False,
            "analysis_authorized": self.analysis_authorized(family),
        }

        family_row = self.families.get(family)
        allowed_tasks = (family_row or {}).get("allowed_tasks") or []
        if task not in allowed_tasks:
            result.update(status=BLOCKED)
            result["reasons"].append(
                f"家庭的任务许可不包含 {task}（允许：{', '.join(allowed_tasks) or '无'}）"
            )
            return result

        if robot_row is None:
            result.update(status=BLOCKED)
            result["reasons"].append("该家庭没有已登记的设备")
            return result

        robot = robot_row["robot"]
        version = robot_row["version"]
        result["robot"] = robot
        result["version"] = version
        gaps: list[str] = []

        def pending(reason: str) -> None:
            result["status"] = PENDING
            result["reasons"].append(reason)
            gaps.append(reason)
        result["status"] = FEASIBLE  # 由下面的证据缺口重新判定

        if task not in (robot_row.get("tasks") or []):
            result.update(status=BLOCKED)
            result["reasons"].append(f"设备 {robot} 的能力声明不含任务 {task}")
            return result

        policy = self.policy.get(task)
        if policy is None:
            result.update(status=BLOCKED)
            result["reasons"].append(f"任务 {task} 缺少任务政策，无法判断最低证据")
            return result

        cert = self.certification_state(robot, version, task)
        result["certification"] = {
            "state": cert["state"],
            "stale": cert.get("stale", False),
            "review_effective": iso(parse_ts(cert["review"]["effective"]))
            if cert.get("review")
            else None,
        }
        if cert["state"] == "fail":
            result.update(status=BLOCKED)
            result["reasons"].append(
                f"能力复查未通过（{iso(parse_ts(cert['review']['effective']))} 记录），"
                "在再次通过复查前不承接机器人主办"
            )
            return result
        if cert["stale"]:
            pending(
                f"能力版本 {version} 尚无复查记录；旧版本的通过结论不能延用到新版本"
            )

        if not self.analysis_authorized(family):
            pending("家庭未授权分析，历史证据不可用于评估；机器人主办暂不能承诺")
            return result

        strata = self.family_task_strata(family, robot, version, task)
        overall_attempts = sum(s.attempts for s in strata)
        overall_reviewed = sum(s.reviewed for s in strata)
        overall_unknown = sum(s.count("unknown") for s in strata)
        unknown_fraction = (overall_unknown / overall_attempts) if overall_attempts else None

        result["counts"] = {
            "attempts": overall_attempts,
            "reviewed": overall_reviewed,
            "unique_reviewed": sum(
                1 for s in strata for row in s.observations if row.get("reviewed") is True
            ),
            "independent": sum(s.count("independent") for s in strata),
            "handoff": sum(s.count("handoff") for s in strata),
            "refused": sum(s.count("refused") for s in strata),
            "unknown": overall_unknown,
            "unknown_fraction": round(unknown_fraction, 4) if unknown_fraction is not None else None,
        }
        result["strata"] = [
            s.summary() for s in sorted(strata, key=lambda s: (s.environment or "", s.object or ""))
        ]
        environments = sorted({s.environment for s in strata if s.environment is not None})
        objects = sorted({s.object for s in strata if s.object is not None})
        result["coverage"] = {
            "environments": environments,
            "objects": objects,
            "requested_environment_attempts": sum(s.attempts for s in strata if s.environment == environment),
            "requested_object_attempts": sum(s.attempts for s in strata if s.object == object_name),
        }

        if overall_attempts and environments:
            top_share = max(
                sum(s.attempts for s in strata if s.environment == env) / overall_attempts
                for env in environments
            )
            env_attempts = result["coverage"]["requested_environment_attempts"]
            if top_share >= 0.75 and env_attempts < overall_attempts * 0.25:
                result["selective_demo"] = True
                pending(
                    f"历史演示高度集中于 {max(environments, key=lambda e: sum(s.attempts for s in strata if s.environment == e))}，"
                    "不能直接外推到本请求环境"
                )

        # 环境证据
        env_strata = [s for s in strata if s.environment == environment]
        env_attempts = sum(s.attempts for s in env_strata)
        env_reviewed = sum(s.reviewed for s in env_strata)
        if env_attempts == 0:
            pending(f"缺少环境 {environment} 的历史尝试，不能默认安全")
        elif env_attempts < SUPPORTED_ENVIRONMENT_MIN:
            pending(
                f"环境 {environment} 仅有 {env_attempts} 次尝试，低于 {SUPPORTED_ENVIRONMENT_MIN} 次本地证据门槛"
            )
        if policy.get("review_required") is True and env_reviewed == 0:
            pending(f"环境 {environment} 的历史尝试尚未经人工复核")

        if object_name is not None and result["coverage"]["requested_object_attempts"] == 0:
            pending(f"缺少物品 {object_name} 的历史尝试，不能默认安全")

        # 政策最低量
        if overall_attempts < policy.get("min_observations", 0):
            pending(
                f"版本 {version}/任务 {task} 仅有 {overall_attempts} 次尝试，低于政策最低 {policy['min_observations']} 次"
            )
        if overall_reviewed < policy.get("min_reviewed", 0):
            pending(f"已人工复核 {overall_reviewed} 次，低于政策最低 {policy['min_reviewed']} 次")
        if unknown_fraction is not None and unknown_fraction > policy.get("max_unknown_fraction", 1):
            pending(
                f"未知结果占比 {unknown_fraction:.0%} 超过政策上限 {policy['max_unknown_fraction']:.0%}"
            )

        # 风险：优先使用请求环境；不足时披露组合整体并采用全窗口
        env_handoff = sum(s.count("handoff") for s in env_strata)
        env_upper = wilson_upper(env_handoff, env_attempts)
        overall_handoff = sum(s.count("handoff") for s in strata)
        overall_upper = wilson_upper(overall_handoff, overall_attempts)
        if env_attempts >= 5:
            risk_upper = env_upper
            risk_basis = f"环境 {environment}"
            risk_attempts = env_attempts
            risk_handoff = env_handoff
        else:
            risk_upper = overall_upper
            risk_basis = "版本/任务整体（请求环境样本不足）"
            risk_attempts = overall_attempts
            risk_handoff = overall_handoff
        band = risk_band(risk_upper)
        if risk_attempts < 5:
            band = "unknown"
        result["risk"] = {
            "basis": risk_basis,
            "attempts": risk_attempts,
            "handoff": risk_handoff,
            "posterior_mean": round(posterior_mean(risk_handoff, risk_attempts), 4)
            if risk_attempts
            else None,
            "upper_95": round(risk_upper, 4) if risk_upper is not None else None,
            "band": band,
            "band_zh": RISK_BAND_ZH[band],
            "guarantee_factor": RISK_BAND_FACTOR[band],
        }

        # 样本量本身不足以支撑任何个体风险披露时，机器人主办仍须等待
        if overall_attempts < 5:
            pending(
                f"版本 {version}/任务 {task} 仅有 {overall_attempts} 次尝试，"
                "样本过少，无法估计接管风险，需采用全窗口保障并补充证据"
            )
        if result["status"] == FEASIBLE and not result["reasons"]:
            result["reasons"].append("满足任务政策最低证据与人工复核条件，按风险估计保留接管保障")
        elif result["status"] == PENDING and not result["reasons"]:
            pending("证据条件尚未满足，机器人主办暂不能承诺")
        return result

    def environment_summary(self, family: str, task: str) -> dict:
        """某家庭/任务的环境覆盖和不确定性。"""
        robot_row = self.robot_for(family, task)
        if robot_row is None:
            return {"family": family, "task": task, "status": "no_robot", "environments": []}
        strata = self.family_task_strata(family, robot_row["robot"], robot_row["version"], task)
        envs = sorted({s.environment for s in strata if s.environment is not None})
        return {
            "family": family,
            "task": task,
            "robot": robot_row["robot"],
            "version": robot_row["version"],
            "analysis_authorized": self.analysis_authorized(family),
            "version_task_counts": {
                "attempts": sum(s.attempts for s in strata),
                "reviewed": sum(s.reviewed for s in strata),
            },
            "environments": [
                {
                    "environment": env,
                    **{
                        key: value
                        for key, value in _aggregate_env([s for s in strata if s.environment == env]).items()
                    },
                }
                for env in envs
            ],
            "objects": sorted({s.object for s in strata if s.object is not None}),
        }


def _aggregate_env(strata: list[Stratum]) -> dict:
    attempts = sum(s.attempts for s in strata)
    reviewed = sum(s.reviewed for s in strata)
    handoff = sum(s.count("handoff") for s in strata)
    unknown = sum(s.count("unknown") for s in strata)
    upper = wilson_upper(handoff, attempts)
    band = "unknown" if attempts < 5 else risk_band(upper)
    return {
        "attempts": attempts,
        "independent": sum(s.count("independent") for s in strata),
        "handoff": handoff,
        "refused": sum(s.count("refused") for s in strata),
        "unknown": unknown,
        "reviewed": reviewed,
        "unknown_fraction": round(unknown / attempts, 4) if attempts else None,
        "handoff_risk": round(posterior_mean(handoff, attempts), 4) if attempts else None,
        "risk_upper": round(upper, 4) if upper is not None else None,
        "risk_band": band,
        "object_coverage": sorted({s.object for s in strata if s.object is not None}),
    }


def allowed_windows(family_row: dict, start: datetime, end: datetime) -> list[Interval]:
    """按家庭 UTC 可接受小时，展开 [start,end) 内的每日允许窗口。

    若 end_hour <= start_hour，视为跨午夜窗口（例如 22:00–06:00）。
    """
    start_hour = int(family_row["start_hour"])
    end_hour = int(family_row["end_hour"])
    first_day = start.date() - timedelta(days=1)
    last_day = (end - timedelta(microseconds=1)).date()
    windows: list[Interval] = []
    day = first_day
    while day <= last_day:
        base = datetime.combine(day, time(0, 0), tzinfo=timezone.utc)
        day_start = base + timedelta(hours=start_hour)
        if end_hour > start_hour:
            day_end = base + timedelta(hours=end_hour)
        else:
            day_end = base + timedelta(days=1, hours=end_hour)
        windows.append(Interval(day_start, day_end))
        day += timedelta(days=1)
    return windows


def service_inside_allowed_hours(family_row: dict, interval: Interval) -> bool:
    for window in allowed_windows(family_row, interval.start, interval.end):
        if window.start <= interval.start and interval.end <= window.end:
            return True
    return False
