"""命令行入口：本地持续运营、可保存进度的演练应用。

不连接真实机器人，不下发物理动作。所有状态写入指定工作目录的 SQLite。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from . import sources
from .domain import ACTION_ZH, OUTCOME_ZH, PLAN_ZH, REQ_CANCELLED, iso, parse_ts
from .evidence import RISK_BAND_ZH
from .incidents import affected_requests
from .operations import (
    OperationError,
    accept_request,
    acknowledge_request,
    cancel_request,
    confirm_proposal,
    create_proposal,
    family_cancel,
    family_confirm_proposal,
    record_fulfillment,
    reject_request,
    submit_request,
)
from .storage import Workspace
from .views import (
    family_view,
    operations_view,
    replay_request,
    request_detail,
)


def _at(value: str | None) -> datetime | None:
    return parse_ts(value) if value else None


def _print_json(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False))


def _fmt_option(option: dict, index: int | None = None) -> str:
    prefix = f"[{index}] " if index is not None else ""
    plan = PLAN_ZH.get(option.get("plan"), option.get("plan"))
    state = "可承接" if option.get("feasible") else "不可承接"
    if option.get("plan") == "defer":
        state = "不占用资源"
    head = f"{prefix}{plan} {option.get('start')}–{option.get('end')}  {state}"
    lines = [head]
    if option.get("risk_upper") is not None:
        lines.append(
            f"    接管风险估计：均值 {option.get('handoff_risk')}，"
            f"上界 {option.get('risk_upper')}（{RISK_BAND_ZH.get(option.get('risk_band'), option.get('risk_band'))}）"
        )
    if option.get("staff"):
        lines.append(f"    候选工作人员：{', '.join(option['staff'])}")
    for assurance in option.get("assurances") or []:
        lines.append(f"    保障占用：{assurance['start']}–{assurance['end']}（{assurance['minutes']} 分钟）")
    for reason in option.get("reasons") or []:
        lines.append(f"    说明：{reason}")
    return "\n".join(lines)


def _print_plan(data: dict, limit: int | None = None) -> None:
    qual = data.get("qualification") or {}
    print(f"请求 {data['request']}  家庭 {data['family']}  {data['task']} / {data.get('object')} / {data.get('environment')}")
    print(f"期望时段 {data['earliest']}–{data['latest']}，预计 {data['minutes']} 分钟")
    status = qual.get("status")
    status_zh = {"feasible": "满足机器人主办条件", "pending": "尚不能承诺机器人主办", "blocked": "明确不可承接"}.get(status, status)
    print(f"机器人主办资格：{status_zh}")
    for reason in qual.get("reasons") or []:
        print(f"  - {reason}")
    risk = qual.get("risk")
    if risk:
        print(
            f"  风险依据：{risk['basis']}，{risk['attempts']} 次尝试中 {risk['handoff']} 次接管；"
            f"上界 {risk['upper_95']}（{risk['band_zh']}）"
        )
    counts = qual.get("counts")
    if counts:
        print(
            f"  统计（版本 {qual.get('version')}/{data['task']}）：尝试 {counts['attempts']}，"
            f"独立 {counts['independent']}，接管 {counts['handoff']}，拒绝 {counts['refused']}，"
            f"未知 {counts['unknown']}；已复核 {counts['reviewed']}"
        )
    for note in data.get("notes") or []:
        print(f"注意：{note}")
    options = data.get("options") or []
    print("候选安排：")
    for index, option in enumerate(options[:limit] if limit else options):
        print(_fmt_option(option, index))
    if limit and len(options) > limit:
        print(f"……其余 {len(options) - limit} 个候选已省略（加 --all 查看）")


def _print_ops(view: dict) -> None:
    print(f"运营总览（{view['generated_at']}）")
    print("状态计数：" + "，".join(f"{k} {v}" for k, v in view["status_counts"].items()))
    print("\n请求：")
    for item in view["requests"]:
        print(
            f"  {item['request']}  {item['family']}  {item['task']}/{item['environment']}  "
            f"{item['status_zh']}  安排={item['plan_zh']} {item['plan_start'] or ''}"
        )
        if item.get("status_reason"):
            print(f"      {item['status_reason']}")
    if view.get("incidents"):
        print("\n受影响的未开始服务（需处置）：")
        for item in view["incidents"]:
            print(
                f"  {item['request']} {item['family']} 原安排 {item['plan_start']}–{item['plan_end']}："
                + "；".join(item["reasons"])
            )
    print("\n人工保障负荷：")
    if not view["staff_load"]:
        print("  当前没有已占用的保障窗口")
    for load in view["staff_load"]:
        print(f"  {load['staff']}：已占用 {load['occupied_minutes']} 分钟")
        for assignment in load["assignments"]:
            print(
                f"     {assignment['request']} {assignment['start']}–{assignment['end']}"
                f"（{'接管保障' if assignment['role'] == 'assurance' else '直接办理'}）"
            )
    print("\n证据薄弱范围：")
    if not view["weak_evidence"]:
        print("  没有识别到薄弱范围")
    for item in view["weak_evidence"]:
        print(
            f"  {item['family']} {item['robot']}/{item['version']} {item['task']} "
            f"物品 {item.get('object')} 环境 {item['environment']}："
            f"尝试 {item['attempts']}，已复核 {item['reviewed']}，"
            f"未知占比 {item['unknown_fraction']}，风险上界 {item['risk_upper']}（{item['risk_band']}）"
        )
        if item.get("note"):
            print(f"    {item['note']}")
        if item.get("recommendation"):
            print(f"    建议：{item['recommendation']}")
        # 该范围若有请求正等待承接，提示运营补齐人工或暂停该类服务
        waiting = [
            i["request"]
            for i in view["not_committed"]
            if i["family"] == item["family"] and i["task"] == item["task"]
        ]
        if waiting:
            print(f"    正在等待且依赖该范围：{', '.join(waiting)}")
    if view.get("not_committed"):
        print("\n尚未承诺（等待资料、确认或处置）：")
        for item in view["not_committed"]:
            print(f"  {item['request']} {item['family']} {item['status_zh']}")
    if view.get("open_proposals"):
        print("\n等待家庭确认的替代安排（尚未占用资源）：")
        for item in view["open_proposals"]:
            print(
                f"  #{item['proposal']} {item['request']} {item['action']} "
                f"{item['start']}–{item['end']}，拟接手 {', '.join(item['staff']) or '—'}"
            )
    print("\n履约结果（仅已记录事实）：")
    print("  " + "，".join(f"{k} {v}" for k, v in view["fulfilment"]["by_outcome"].items()))
    if view["fulfilment"]["promises_not_met"]:
        print("  未达承诺（接管/拒绝/未知）：")
        for item in view["fulfilment"]["promises_not_met"]:
            print(f"    {item['request']} {item['family']}：{item.get('outcome')}")
    if view["fulfilment"]["cancelled"]:
        print("  已取消（承诺已释放）：" + ", ".join(i["request"] for i in view["fulfilment"]["cancelled"]))


def _print_family(view: dict) -> None:
    print(f"家庭 {view['family']} 的服务请求（{view['generated_at']}）")
    consent = view["consent"]
    print("服务授权：" + (", ".join(f"{k}={v}" for k, v in consent["service_tasks"].items()) or "无"))
    print("分析授权：" + consent["analysis"])
    for item in view["requests"]:
        print(
            f"\n  {item['request']}  {item['task']}/{item['object']}/{item['environment']}  "
            f"{item['status_zh']}"
        )
        print(f"    期望 {item['earliest']}–{item['latest']}")
        if item["status"] in ("accepted", "completed"):
            staff = ", ".join(item["assigned_staff"]) or "—"
            print(f"    已承接安排：{item['plan_zh']} {item['plan_start']}–{item['plan_end']}，接手人 {staff}")
        incident = item.get("incident") or {}
        if incident.get("affected"):
            print(f"    注意：{incident['message']}")
            for reason in incident.get("reasons", []):
                print(f"      影响原因：{reason}")
        for proposal in item["pending_proposals"]:
            print(
                f"    待确认：{ACTION_ZH.get(proposal['action'], proposal['action'])} "
                f"{proposal['start']}–{proposal['end']}，接手人 {', '.join(proposal['staff'])}"
            )
        for event in item["events"]:
            detail = event.get("detail") or {}
            extra = ""
            if event["kind"] == "fulfillment" and detail.get("outcome"):
                extra = f"：{OUTCOME_ZH.get(detail['outcome'], detail['outcome'])}"
            elif event["kind"] == REQ_CANCELLED and detail.get("reason"):
                extra = f"：{detail['reason']}"
            print(f"    记录 {event['at']} {event['label']}{extra}")
    print("\n" + view["explanation"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="service_pilot",
        description="家庭服务机器人试点的任务承诺与安全复查（本地合成资料演练，不连接真实机器人）",
    )
    parser.add_argument("-w", "--workspace", default="work", help="工作目录（保存 SQLite 状态）")
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="以 JSON 输出结果（既可放在子命令前，也可放在具体命令后）",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="用只读来源资料初始化工作目录", parents=[common])
    init.add_argument("--source", default="inputs", help="来源资料目录")
    init.add_argument("--now", help="演练时钟起始时间（默认当前 UTC）")
    init.add_argument("--no-manifest", action="store_true", help="跳过清单哈希校验（仅用于新资料）")

    ops = sub.add_parser("ops", help="运营操作", parents=[common])
    ops_sub = ops.add_subparsers(dest="action", required=True)

    overview = ops_sub.add_parser("overview", help="运营总览", parents=[common])
    overview.add_argument("--now", help="按该时点查看")

    request_list = ops_sub.add_parser("requests", help="列出请求", parents=[common])
    request_list.add_argument("--now", help="按该时点查看")

    assess = ops_sub.add_parser("assess", help="评估一个请求的候选安排（不占用资源）", parents=[common])
    assess.add_argument("request")
    assess.add_argument("--now")
    assess.add_argument("--all", action="store_true", help="显示全部候选")

    detail = ops_sub.add_parser("detail", help="请求详情与证据", parents=[common])
    detail.add_argument("request")
    detail.add_argument("--now")

    preview = ops_sub.add_parser("preview", help="在提交前试算某个时段与任务", parents=[common])
    preview.add_argument("--family", required=True)
    preview.add_argument("--task", required=True)
    preview.add_argument("--earliest", required=True)
    preview.add_argument("--latest", required=True)
    preview.add_argument("--minutes", type=int, required=True)
    preview.add_argument("--object", default=None)
    preview.add_argument("--environment", required=True)
    preview.add_argument("--now")
    preview.add_argument("--all", action="store_true")

    accept = ops_sub.add_parser("accept", help="承接请求（占用设备与保障窗口）", parents=[common])
    accept.add_argument("request")
    accept.add_argument("--plan", required=True, choices=["robot", "human"])
    accept.add_argument("--start", required=True)
    accept.add_argument("--actor", required=True)
    accept.add_argument("--staff", help="指定接管/办理人员（默认第一位合格候选）")
    accept.add_argument("--now")
    accept.add_argument("--note")

    acknowledge = ops_sub.add_parser("acknowledge", help="标记运营已受理（不是承诺）", parents=[common])
    acknowledge.add_argument("request")
    acknowledge.add_argument("--actor", required=True)
    acknowledge.add_argument("--now")

    reject = ops_sub.add_parser("reject", help="未承接请求", parents=[common])
    reject.add_argument("request")
    reject.add_argument("--actor", required=True)
    reject.add_argument("--reason", required=True)
    reject.add_argument("--now")

    cancel = ops_sub.add_parser("cancel", help="家庭取消请求，释放承诺", parents=[common])
    cancel.add_argument("request")
    cancel.add_argument("--actor", required=True)
    cancel.add_argument("--reason", required=True)
    cancel.add_argument("--now")

    propose = ops_sub.add_parser("propose", help="提出替代安排（等待家庭确认，尚未占用）", parents=[common])
    propose.add_argument("request")
    propose.add_argument("--proposal-action", dest="proposal_action", required=True, choices=["reschedule", "human"])
    propose.add_argument("--plan", required=True, choices=["robot", "human"])
    propose.add_argument("--start", required=True)
    propose.add_argument("--actor", required=True)
    propose.add_argument("--staff")
    propose.add_argument("--note")
    propose.add_argument("--now")

    confirm = ops_sub.add_parser("confirm-proposal", help="家庭确认替代安排", parents=[common])
    confirm.add_argument("proposal", type=int)
    confirm.add_argument("--actor", required=True)
    confirm.add_argument("--now")

    decline = ops_sub.add_parser("decline-proposal", help="家庭拒绝替代安排", parents=[common])
    decline.add_argument("proposal", type=int)
    decline.add_argument("--actor", required=True)
    decline.add_argument("--now")

    readiness = ops_sub.add_parser("readiness", help="列出可承接/待确认/受影响/未承接的请求及原因", parents=[common])
    readiness.add_argument("--now")

    incidents = ops_sub.add_parser("incidents", help="识别受维护/复查/授权影响的未开始服务", parents=[common])
    incidents.add_argument("--now")

    fulfil = ops_sub.add_parser("fulfil", help="记录实际履约结果", parents=[common])
    fulfil.add_argument("request")
    fulfil.add_argument("--outcome", required=True, choices=["independent", "handoff", "refused", "unknown"])
    fulfil.add_argument("--actor", required=True)
    fulfil.add_argument("--started-at")
    fulfil.add_argument("--completed-at")
    fulfil.add_argument("--note")
    fulfil.add_argument("--evidence-used", action="store_true", help="该结果会作为后续证据")
    fulfil.add_argument("--force", action="store_true", help="允许对非已承接请求记录例外事实")
    fulfil.add_argument("--now")

    replay = ops_sub.add_parser("replay", help="按当时可知证据回溯请求", parents=[common])
    replay.add_argument("request")
    replay.add_argument("--as-of", required=True)

    import_cmd = ops_sub.add_parser("import", help="导入相同契约的新记录", parents=[common])
    import_cmd.add_argument("file")
    import_cmd.add_argument("--table", help="数组文件对应的资料表")
    import_cmd.add_argument("--actor", default="ops")
    import_cmd.add_argument("--now")

    clock = ops_sub.add_parser("clock", help="查看或设置演练时钟（只能前进）", parents=[common])
    clock.add_argument("--set")

    family = sub.add_parser("family", help="家庭视图（仅本户）", parents=[common])
    family_sub = family.add_subparsers(dest="action", required=True)
    family_view_cmd = family_sub.add_parser("view", help="查看本户请求与授权", parents=[common])
    family_view_cmd.add_argument("family")
    family_view_cmd.add_argument("--request")
    family_view_cmd.add_argument("--now")

    family_submit = family_sub.add_parser(
        "submit", help="本户提交服务请求（给出时段、任务与采集范围）", parents=[common]
    )
    family_submit.add_argument("family")
    family_submit.add_argument("--task", required=True)
    family_submit.add_argument("--earliest", required=True)
    family_submit.add_argument("--latest", required=True)
    family_submit.add_argument("--minutes", type=int, required=True)
    family_submit.add_argument("--object", default=None)
    family_submit.add_argument("--environment", required=True)
    family_submit.add_argument(
        "--collection-scope",
        default="service-only",
        help="家庭允许的采集范围，例如 service-only 或 service+analysis",
    )
    family_submit.add_argument("--now")

    family_cancel_cmd = family_sub.add_parser(
        "cancel", help="本户取消请求，释放承诺", parents=[common]
    )
    family_cancel_cmd.add_argument("family")
    family_cancel_cmd.add_argument("request")
    family_cancel_cmd.add_argument("--reason", required=True)
    family_cancel_cmd.add_argument("--now")

    family_confirm_cmd = family_sub.add_parser(
        "confirm", help="本户确认替代安排", parents=[common]
    )
    family_confirm_cmd.add_argument("family")
    family_confirm_cmd.add_argument("proposal", type=int)
    family_confirm_cmd.add_argument("--now")

    family_decline_cmd = family_sub.add_parser(
        "decline", help="本户拒绝替代安排", parents=[common]
    )
    family_decline_cmd.add_argument("family")
    family_decline_cmd.add_argument("proposal", type=int)
    family_decline_cmd.add_argument("--now")

    explain = sub.add_parser("explain", help="解释一个请求当前为何可/不可承接", parents=[common])
    explain.add_argument("request")
    explain.add_argument("--now")
    explain.add_argument("--all", action="store_true")

    stats = sub.add_parser("stats", help="按家庭/任务/环境查看统计与不确定性", parents=[common])
    stats.add_argument("--family")
    stats.add_argument("--now")

    issues = sub.add_parser("source-issues", help="查看来源冲突", parents=[common])

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if not hasattr(args, "json"):
            args.json = False
        if args.command == "init":
            if Path(args.workspace, "state.db").exists():
                parser.error(f"工作目录已有状态库: {args.workspace}")
            ws = Workspace.initialize(
                args.workspace,
                args.source,
                now=_at(args.now),
                check_manifest=not args.no_manifest,
            )
            if args.json:
                _print_json({"workspace": args.workspace, "source": args.source, "initialized": True})
            else:
                print(f"已初始化工作目录 {args.workspace}（来源 {args.source}）")
            ws.close()
            return 0

        ws = Workspace.open(args.workspace)
        try:
            return _dispatch(ws, args)
        finally:
            ws.close()
    except (OperationError, sources.SourceError) as exc:
        print(f"操作失败：{exc}", file=sys.stderr)
        return 2
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0


def _dispatch(ws: Workspace, args) -> int:
    now = _at(getattr(args, "now", None))
    if args.command == "ops":
        return _ops(ws, args, now)
    if args.command == "family":
        if args.action == "view":
            view = family_view(ws, args.family, now=now, request_id=getattr(args, "request", None))
            _print_json(view) if args.json else _print_family(view)
            return 0
        if args.action == "submit":
            result = submit_request(
                ws,
                family=args.family,
                task=args.task,
                earliest=args.earliest,
                latest=args.latest,
                minutes=args.minutes,
                object_name=args.object,
                environment=args.environment,
                collection_scope=args.collection_scope,
                now=now,
            )
            if args.json:
                _print_json(result)
            else:
                print(
                    f"已提交请求 {result['request']}（家庭 {result['family']}）。"
                    "这是请求，不是承诺；运营确认前不会占用设备或保障。"
                )
            return 0
        if args.action == "cancel":
            result = family_cancel(
                ws, args.request, family=args.family, reason=args.reason, now=now
            )
            if args.json:
                _print_json(result)
            else:
                print(f"{result['request']} 已取消，释放 {len(result['released'])} 项占用")
            return 0
        if args.action in ("confirm", "decline"):
            result = family_confirm_proposal(
                ws,
                args.proposal,
                family=args.family,
                accept=(args.action == "confirm"),
                now=now,
            )
            if args.json:
                _print_json(result)
            else:
                print(
                    f"替代安排 #{result['proposal']} 已{'确认' if args.action == 'confirm' else '拒绝'}"
                )
            return 0
        raise ValueError("未知家庭操作")
    if args.command == "explain":
        data = request_detail(ws, args.request, now=now)
        if args.json:
            _print_json(data)
        else:
            _print_plan(data, None if args.all else 8)
        return 0
    if args.command == "stats":
        data = _stats(ws, family=args.family, now=now)
        if args.json:
            _print_json(data)
        else:
            _print_stats(data)
        return 0
    if args.command == "source-issues":
        rows = ws.source_issues()
        payload = [dict(row) for row in rows]
        if args.json:
            _print_json(payload)
        else:
            if not rows:
                print("没有来源身份冲突")
            for row in rows:
                print(f"{row['at']} {row['table_name']} {row['source_key']}: {row['detail']}")
        return 0
    raise ValueError("未知命令")


def _ops(ws: Workspace, args, now) -> int:
    action = args.action
    if action == "overview":
        view = operations_view(ws, now=now)
        _print_json(view) if args.json else _print_ops(view)
        return 0
    if action == "requests":
        view = operations_view(ws, now=now)
        if args.json:
            _print_json(view["requests"])
        else:
            for item in view["requests"]:
                print(
                    f"{item['request']}  {item['family']}  {item['task']}/{item['environment']}  "
                    f"{item['status_zh']}  {item['plan_zh']} {item['plan_start'] or ''}"
                )
        return 0
    if action == "assess":
        from .operations import assess_request

        data = assess_request(ws, args.request, now=now)
        _print_json(data) if args.json else _print_plan(data, None if args.all else 8)
        return 0
    if action == "detail":
        data = request_detail(ws, args.request, now=now)
        _print_json(data) if args.json else _print_plan(data, 8)
        return 0
    if action == "preview":
        from .operations import preview_plan

        data = preview_plan(
            ws,
            family=args.family,
            task=args.task,
            earliest=args.earliest,
            latest=args.latest,
            minutes=args.minutes,
            object_name=args.object,
            environment=args.environment,
            now=now,
        )
        _print_json(data) if args.json else _print_plan(data, None if args.all else 8)
        return 0
    if action == "accept":
        result = accept_request(
            ws,
            args.request,
            plan_kind=args.plan,
            start=args.start,
            actor=args.actor,
            now=now,
            note=args.note,
            staff_id=args.staff,
        )
        if args.json:
            _print_json(result)
        else:
            print(
                f"已承接 {result['request']}：{PLAN_ZH[result['plan']['plan']]} "
                f"{result['plan']['start']}–{result['plan']['end']}；"
                f"接手人员 {result['plan'].get('assigned_staff') or '—'}"
                f"（合格候选 {', '.join(result['plan'].get('staff') or [])}）"
            )
        return 0
    if action == "acknowledge":
        result = acknowledge_request(ws, args.request, actor=args.actor, now=now)
        if args.json:
            _print_json(result)
        else:
            print(f"{result['request']} 状态：{result['status']}")
        return 0
    if action == "reject":
        result = reject_request(ws, args.request, actor=args.actor, reason=args.reason, now=now)
        if args.json:
            _print_json(result)
        else:
            print(f"{result['request']} 未承接：{result['reason']}")
        return 0
    if action == "cancel":
        result = cancel_request(ws, args.request, actor=args.actor, reason=args.reason, now=now)
        if args.json:
            _print_json(result)
        else:
            print(f"{result['request']} 已取消，释放 {len(result['released'])} 项占用")
        return 0
    if action == "propose":
        result = create_proposal(
            ws,
            args.request,
            action=args.proposal_action,
            plan_kind=args.plan,
            start=args.start,
            actor=args.actor,
            now=now,
            staff_id=args.staff,
            note=args.note,
        )
        if args.json:
            _print_json(result)
        else:
            print(f"已提出替代安排 #{result['proposal']}，等待家庭确认（尚未占用资源）")
        return 0
    if action in ("confirm-proposal", "decline-proposal"):
        result = confirm_proposal(
            ws, args.proposal, actor=args.actor, now=now, accept=(action == "confirm-proposal")
        )
        if args.json:
            _print_json(result)
        elif action == "confirm-proposal":
            print(f"家庭已确认替代安排 #{result['proposal']}；原安排已释放")
        else:
            print(f"家庭已拒绝替代安排 #{result['proposal']}")
        return 0
    if action == "readiness":
        rows = []
        current = now or ws.now()
        for row in ws.requests(current):
            detail = request_detail(ws, row["request"], now=current)
            feasible = [o for o in detail["options"] if o["feasible"] and o["plan"] != "defer"]
            rows.append(
                {
                    "request": row["request"],
                    "family": row["family"],
                    "task": row["task"],
                    "status": row["status"],
                    "status_zh": detail["status_zh"],
                    "qualification": detail["qualification"]["status"],
                    "feasible_robots": sum(1 for o in feasible if o["plan"] == "robot"),
                    "feasible_humans": sum(1 for o in feasible if o["plan"] == "human"),
                    "actionable": row["status"] in ("received", "pending", "held"),
                    "reasons": detail["qualification"].get("reasons") or detail.get("notes") or [],
                }
            )
        if args.json:
            _print_json(rows)
        else:
            for item in rows:
                if item["actionable"]:
                    print(
                        f"{item['request']}  {item['family']}  {item['task']}  {item['status_zh']}  "
                        f"可承接：机器人 {item['feasible_robots']} 个时段，人工 {item['feasible_humans']} 个时段"
                    )
                else:
                    print(
                        f"{item['request']}  {item['family']}  {item['task']}  {item['status_zh']}  "
                        "（已结束或已承诺；改期/转人工请用 propose）"
                    )
                if item["actionable"]:
                    for reason in item["reasons"]:
                        print(f"    {reason}")
        return 0
    if action == "incidents":
        findings = affected_requests(ws, now=now)
        if args.json:
            _print_json(findings)
        else:
            if not findings:
                print("没有受影响的未开始服务")
            for item in findings:
                print(f"{item['request']} 家庭 {item['family']} 原安排 {item['plan_start']}–{item['plan_end']}")
                for reason in item["reasons"]:
                    print(f"  影响：{reason}")
                for option in item["options"]:
                    print(
                        f"  选择：{option.get('action_zh')} "
                        f"{option.get('start') or ''} {option.get('end') or ''} "
                        f"{('人员 ' + ', '.join(option.get('staff') or [])) if option.get('staff') else ''}"
                    )
                    for reason in option.get("reasons") or []:
                        print(f"      {reason}")
        return 0
    if action == "fulfil":
        result = record_fulfillment(
            ws,
            args.request,
            outcome=args.outcome,
            actor=args.actor,
            now=now,
            started_at=args.started_at,
            completed_at=args.completed_at,
            note=args.note,
            evidence_used=args.evidence_used,
            force=args.force,
        )
        if args.json:
            _print_json(result)
        else:
            print(f"{result['request']} 已记录：{OUTCOME_ZH[result['outcome']]}")
        return 0
    if action == "replay":
        data = replay_request(ws, args.request, as_of=parse_ts(args.as_of))
        if args.json:
            _print_json(data)
        else:
            then = data["status_then"]
            if then:
                print(f"请求 {args.request} 在 {data['as_of']} 的可知状态：{then}")
            else:
                print(f"请求 {args.request} 在 {data['as_of']} 尚未进入系统")
            committed = data.get("plan_then")
            if committed:
                print(f"  当时承诺：{PLAN_ZH.get(committed.get('plan'), committed.get('plan'))} {committed.get('start')}–{committed.get('end')}")
            else:
                print("  当时没有已承接安排")
            _print_plan(data["options_then"], 6)
            if data["later_facts"]:
                print("\n后来才发现的事实（不改变当时判断）：")
                for fact in data["later_facts"]:
                    print(f"  {fact['known_at']} {fact['table']}: {json.dumps(fact['payload'], ensure_ascii=False)}")
        return 0
    if action == "import":
        path = Path(args.file)
        data = json.loads(path.read_text(encoding="utf-8"))
        report = ws.import_payload(data, actor=args.actor, now=now, table_hint=args.table)
        if args.json:
            _print_json(report)
        else:
            for table, counts in report.items():
                print(
                    f"{table}: 新增 {counts['imported']}，重复 {counts['duplicate']}，"
                    f"冲突 {counts['conflict']}，拒绝 {counts['rejected']}"
                )
                for error in counts["errors"]:
                    print(f"  拒绝：{error}")
        return 0
    if action == "clock":
        if args.set:
            current = ws.now()
            target = parse_ts(args.set)
            if target < current:
                raise ValueError(f"演练时钟不能倒退（当前 {iso(current)}）")
            ws.set_clock(target)
            if args.json:
                _print_json({"clock": iso(ws.now())})
            else:
                print(f"演练时钟已设为 {iso(ws.now())}")
        else:
            if args.json:
                _print_json({"clock": iso(ws.now())})
            else:
                print(f"演练时钟：{iso(ws.now())}")
        return 0
    raise ValueError("未知运营操作")


def _stats(ws: Workspace, *, family: str | None, now) -> dict:
    at = now or ws.now()
    snapshot = ws.snapshot(at)
    families = [family] if family else sorted(snapshot.families)
    result = {"as_of": iso(at), "families": []}
    for fam in families:
        if fam not in snapshot.families:
            raise ValueError(f"未登记的家庭: {fam}")
        robots = snapshot.robots_for(fam)
        if not robots:
            result["families"].append({"family": fam, "status": "no_robot"})
            continue
        tasks = []
        declared = sorted({task for robot in robots for task in (robot.get("tasks") or [])})
        for task in declared:
            tasks.append(snapshot.environment_summary(fam, task))
        result["families"].append(
            {
                "family": fam,
                "robot": ", ".join(robot["robot"] for robot in robots),
                "versions": {robot["robot"]: robot["version"] for robot in robots},
                "analysis_authorized": snapshot.analysis_authorized(fam),
                "tasks": tasks,
            }
        )
    return result


def _print_stats(data: dict) -> None:
    print(f"证据统计（截至 {data['as_of']}；拒绝与未知不计入完成，不同版本不合并）")
    for fam in data["families"]:
        if fam.get("status") == "no_robot":
            print(f"\n{fam['family']}：没有设备")
            continue
        versions = ", ".join(f"{r}/{v}" for r, v in fam["versions"].items())
        print(f"\n{fam['family']}  设备 {versions}  分析授权={fam['analysis_authorized']}")
        for task in fam["tasks"]:
            counts = task["version_task_counts"]
            if not task.get("environments"):
                print(f"  任务 {task['task']}：没有可用历史（可能未授权分析或缺少设备证据）")
                continue
            print(f"  任务 {task['task']}：尝试 {counts['attempts']}，已复核 {counts['reviewed']}")
            for env in task["environments"]:
                print(
                    f"    环境 {env['environment']}：尝试 {env['attempts']}，"
                    f"独立 {env['independent']}，接管 {env['handoff']}，拒绝 {env['refused']}，未知 {env['unknown']}"
                    f"（未知占比 {env['unknown_fraction']}）；风险上界 {env['risk_upper']}"
                    f"（{env['risk_band']}）；物品 {', '.join(env['object_coverage']) or '—'}"
                )


if __name__ == "__main__":
    raise SystemExit(main())
