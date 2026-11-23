"""业务测试：合成资料上的承诺、统计、接手、失败与隐私行为。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service_pilot.domain import iso, parse_ts
from service_pilot.evidence import RISK_BAND_FACTOR, wilson_upper
from service_pilot.incidents import affected_requests
from service_pilot.operations import (
    OperationError,
    accept_request,
    cancel_request,
    confirm_proposal,
    create_proposal,
    preview_plan,
    record_fulfillment,
    reject_request,
)
from service_pilot.planner import assurance_interval
from service_pilot.storage import Workspace
from service_pilot.views import family_view, operations_view, replay_request, request_detail

T = lambda value: parse_ts(value)


def make_ws(tmp: str, now: str = "2026-11-19T08:00:00Z") -> Workspace:
    return Workspace.initialize(str(Path(tmp) / "work"), ROOT / "inputs", now=T(now))


class StatisticsTests(unittest.TestCase):
    def test_outcomes_are_separated_and_versions_not_merged(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            snap = ws.snapshot()
            summary = snap.environment_summary("F1", "drink")
            self.assertEqual(summary["version_task_counts"]["attempts"], 40)
            clear = next(e for e in summary["environments"] if e["environment"] == "clear")
            crowded = next(e for e in summary["environments"] if e["environment"] == "crowded")
            # 拒绝和未知不算完成：独立 12/30，接管 6/30，拒绝 6，未知 6
            self.assertEqual(
                (clear["independent"], clear["handoff"], clear["refused"], clear["unknown"]),
                (12, 6, 6, 6),
            )
            self.assertEqual((crowded["independent"], crowded["handoff"]), (4, 2))
            # 不同家庭/版本彼此独立
            q2 = snap.qualification("F2", "retrieve", object_name="box", environment="clear")
            self.assertEqual(q2["version"], "v1")
            self.assertNotEqual(q2["counts"]["attempts"], 0)

    def test_risk_uses_upper_bound_and_discloses_uncertainty(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            snap = ws.snapshot()
            qual = snap.qualification("F1", "drink", object_name="cup", environment="clear")
            risk = qual["risk"]
            self.assertAlmostEqual(risk["upper_95"], 0.3731, places=3)
            self.assertAlmostEqual(risk["posterior_mean"], 0.2188, places=3)
            self.assertEqual(risk["band"], "medium")
            service = type("S", (), {"minutes": 30, "start": T("2026-11-19T10:00:00Z")})()
            cover, note, fits = assurance_interval(
                risk, service, T("2026-11-19T10:00:00Z"), T("2026-11-19T11:00:00Z")
            )
            self.assertEqual(cover.minutes, 45)
            self.assertTrue(fits)
            self.assertIn("1.5", note)
            # 所需保障超出请求窗口时不能被悄悄截短
            late_service = type("S", (), {"minutes": 30, "start": T("2026-11-19T10:45:00Z")})()
            cover2, note2, fits2 = assurance_interval(
                risk, late_service, T("2026-11-19T10:00:00Z"), T("2026-11-19T11:00:00Z")
            )
            self.assertFalse(fits2)
            self.assertEqual(cover2.minutes, 45)
            self.assertIn("无法完整保留", note2)

    def test_wilson_upper_is_monotone_and_bounded(self):
        self.assertLess(wilson_upper(1, 10), wilson_upper(3, 10))
        self.assertAlmostEqual(wilson_upper(0, 10), 0.2775, places=3)
        self.assertLessEqual(wilson_upper(10, 10), 1.0)


class QualificationTests(unittest.TestCase):
    def test_unreviewed_crowded_environment_blocks_robot_offer(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            snap = ws.snapshot()
            qual = snap.qualification("F3", "drink", object_name="cup", environment="crowded")
            self.assertEqual(qual["status"], "pending")
            self.assertTrue(any("复核" in reason for reason in qual["reasons"]))
            # 有历史但未复核，风险被如实披露为高
            self.assertEqual(qual["risk"]["band"], "high")

    def test_missing_environment_cannot_default_to_safe(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            data = preview_plan(
                ws,
                family="F1",
                task="drink",
                earliest="2026-11-19T10:00:00Z",
                latest="2026-11-19T11:00:00Z",
                minutes=30,
                object_name="cup",
                environment="outdoor",
                now=T("2026-11-19T08:00:00Z"),
            )
            self.assertEqual(data["qualification"]["status"], "pending")
            self.assertFalse(any(o["plan"] == "robot" and o["feasible"] for o in data["options"]))
            self.assertTrue(any("缺少环境 outdoor" in r for r in data["qualification"]["reasons"]))

    def test_maintenance_window_is_not_treated_as_reliability_gain(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp, now="2026-11-19T08:00:00Z") as ws:
            snap = ws.snapshot()
            qual = snap.qualification("F1", "drink", object_name="cup", environment="clear")
            # 维护期间没有尝试，不改变任何统计量
            self.assertEqual(qual["counts"]["attempts"], 40)
            self.assertEqual(qual["risk"]["upper_95"], 0.3731)

    def test_capability_review_failure_blocks_robot(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            ws.import_payload(
                {
                    "table": "capability_review",
                    "rows": [
                        {
                            "id": "CR1",
                            "robot": "R1",
                            "version": "v2",
                            "task": "drink",
                            "result": "fail",
                            "effective": "2026-11-19T09:00:00Z",
                            "received": "2026-11-19T09:00:00Z",
                            "note": "抓取失败",
                        }
                    ],
                },
                now=T("2026-11-19T09:05:00Z"),
            )
            snap = ws.snapshot(T("2026-11-19T09:10:00Z"))
            qual = snap.qualification("F1", "drink", object_name="cup", environment="clear")
            self.assertEqual(qual["status"], "blocked")
            self.assertTrue(any("能力复查未通过" in r for r in qual["reasons"]))
            # 恢复设备健康不会自动解除安全复查：无 pass 记录前仍 blocked
            ws.import_payload(
                {
                    "table": "capability_review",
                    "rows": [
                        {
                            "id": "CR2",
                            "robot": "R1",
                            "version": "v2",
                            "task": "drink",
                            "result": "pass",
                            "effective": "2026-11-19T09:30:00Z",
                            "received": "2026-11-19T09:30:00Z",
                            "note": "复查通过",
                        }
                    ],
                },
                now=T("2026-11-19T09:35:00Z"),
            )
            snap = ws.snapshot(T("2026-11-19T09:40:00Z"))
            self.assertEqual(
                snap.qualification("F1", "drink", object_name="cup", environment="clear")["status"],
                "feasible",
            )


class CommitmentTests(unittest.TestCase):
    def test_assessment_is_not_a_commitment_and_holds_no_resources(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            data = request_detail(ws, "Q1", now=T("2026-11-19T08:05:00Z"))
            self.assertEqual(data["status"], "received")
            self.assertEqual(ws.active_reservations(), [])
            self.assertTrue(data["options"])
            view = family_view(ws, "F1", now=T("2026-11-19T08:05:00Z"))
            self.assertEqual(view["requests"][0]["status_zh"], "已收到，等待运营评估")

    def test_accept_holds_robot_and_staff_exclusively(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            result = accept_request(
                ws,
                "Q1",
                plan_kind="robot",
                start="2026-11-19T10:00:00Z",
                actor="ops-A",
                now=T("2026-11-19T08:10:00Z"),
            )
            self.assertEqual(result["status"], "accepted")
            reservations = ws.reservations_for("Q1")
            kinds = sorted((r["kind"], r["resource"]) for r in reservations)
            self.assertEqual(kinds, [("robot", "R1"), ("staff", "S1")])
            staff = next(r for r in reservations if r["kind"] == "staff")
            self.assertEqual(staff["start"], "2026-11-19T10:00:00Z")
            self.assertEqual(staff["end"], "2026-11-19T10:45:00Z")

    def test_two_operators_cannot_spend_same_assurance_twice(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            accept_request(
                ws, "Q1", plan_kind="robot", start="2026-11-19T10:00:00Z", actor="ops-A",
                staff_id="S1", now=T("2026-11-19T08:10:00Z"),
            )
            # Q3 同为 north/retrieve，唯一合格人员 S1 已被 Q1 保障占用
            with self.assertRaises(OperationError):
                accept_request(
                    ws, "Q3", plan_kind="robot", start="2026-11-19T10:15:00Z", actor="ops-B",
                    now=T("2026-11-19T08:15:00Z"),
                )
            # 两个运营不能各自向家庭显示已承接：第二户请求仍未承接
            self.assertEqual(ws.request_row("Q3")["status"], "received")
            # S2 只具备 drink 资格，不能兜底 retrieve
            self.assertEqual(ws.request_row("Q1")["status"], "accepted")

    def test_robot_offer_requires_assurance_even_when_robot_free(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            # 把 S1 的班次从 retrieve 资格中移除后，Q1 的机器人主办不再可行
            ws.import_payload(
                {
                    "table": "staff_unavailability",
                    "rows": [
                        {
                            "id": "U1",
                            "staff": "S1",
                            "start": "2026-11-19T10:00:00Z",
                            "end": "2026-11-19T10:45:00Z",
                            "received": "2026-11-19T08:05:00Z",
                        }
                    ],
                },
                now=T("2026-11-19T08:05:00Z"),
            )
            data = request_detail(ws, "Q3", now=T("2026-11-19T08:10:00Z"))
            robot_at_start = [
                o for o in data["options"]
                if o["plan"] == "robot" and o["start"].startswith("2026-11-19T10:00")
            ]
            # S2 只有 drink 资格，S1 在保障窗口不可用 -> 10:00 的机器人主办不能承诺
            self.assertTrue(robot_at_start)
            self.assertFalse(any(o["feasible"] for o in robot_at_start))
            self.assertTrue(any("保障" in " ".join(o["reasons"]) for o in robot_at_start))

    def test_accepted_request_cannot_be_replaced_without_family_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            accept_request(
                ws, "Q1", plan_kind="human", start="2026-11-19T10:00:00Z", actor="ops",
                staff_id="S1", now=T("2026-11-19T08:10:00Z"),
            )
            with self.assertRaises(OperationError):
                accept_request(
                    ws, "Q1", plan_kind="human", start="2026-11-19T10:30:00Z", actor="ops2",
                    now=T("2026-11-19T08:15:00Z"),
                )
            self.assertEqual(ws.request_row("Q1")["plan_start"], "2026-11-19T10:00:00Z")

    def test_cancel_releases_and_allows_another_family(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            accept_request(
                ws, "Q1", plan_kind="robot", start="2026-11-19T10:00:00Z", actor="ops",
                staff_id="S1", now=T("2026-11-19T08:10:00Z"),
            )
            cancel_request(ws, "Q1", actor="family:F1", reason="家庭临时有事", now=T("2026-11-19T09:00:00Z"))
            self.assertEqual(ws.active_reservations(), [])
            result = accept_request(
                ws, "Q3", plan_kind="human", start="2026-11-19T10:00:00Z", actor="ops",
                now=T("2026-11-19T09:05:00Z"),
            )
            self.assertEqual(result["status"], "accepted")


class IncidentTests(unittest.TestCase):
    def test_maintenance_extension_identifies_affected_services(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            accept_request(
                ws, "Q1", plan_kind="robot", start="2026-11-19T10:00:00Z", actor="ops",
                staff_id="S1", now=T("2026-11-19T08:10:00Z"),
            )
            ws.import_payload(
                {
                    "table": "maintenance",
                    "rows": [
                        {
                            "id": "N2",
                            "robot": "R1",
                            "start": "2026-11-19T10:15:00Z",
                            "end": "2026-11-19T13:00:00Z",
                            "received": "2026-11-19T09:00:00Z",
                        }
                    ],
                },
                now=T("2026-11-19T09:05:00Z"),
            )
            findings = affected_requests(ws, now=T("2026-11-19T09:10:00Z"))
            self.assertEqual([f["request"] for f in findings], ["Q1"])
            actions = {o["action"] for o in findings[0]["options"]}
            # 维护覆盖 Q1 请求窗口内的所有机器人时段，因此只提供转人工与取消
            self.assertEqual(actions, {"human", "cancel"})
            # 识别不等于处置：原承诺仍在
            self.assertEqual(ws.request_row("Q1")["status"], "accepted")

    def test_revoked_service_consent_blocks_new_commitments_but_keeps_facts(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            accept_request(
                ws, "Q1", plan_kind="robot", start="2026-11-19T10:00:00Z", actor="ops",
                now=T("2026-11-19T08:10:00Z"),
            )
            record_fulfillment(
                ws, "Q1", outcome="handoff", actor="ops", started_at="2026-11-19T10:00:00Z",
                completed_at="2026-11-19T10:30:00Z", now=T("2026-11-19T10:35:00Z"),
            )
            ws.import_payload(
                {
                    "table": "consent",
                    "rows": [
                        {
                            "id": "C99",
                            "family": "F1",
                            "scope": "service/drink",
                            "effective": "2026-11-19T10:40:00Z",
                            "status": "revoked",
                        }
                    ],
                },
                now=T("2026-11-19T10:41:00Z"),
            )
            # 已开始的履约事实保留
            self.assertEqual(len(ws.fulfillments("F1")), 1)
            data = preview_plan(
                ws, family="F1", task="drink", earliest="2026-11-20T08:00:00Z",
                latest="2026-11-20T09:00:00Z", minutes=30, object_name="cup",
                environment="clear", now=T("2026-11-19T10:45:00Z"),
            )
            self.assertFalse(any(o["plan"] != "defer" and o["feasible"] for o in data["options"]))
            self.assertTrue(any("撤回" in n for n in data["notes"]))

    def test_revoked_task_consent_creates_incident_with_cancel_option(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            accept_request(
                ws, "Q1", plan_kind="human", start="2026-11-19T10:00:00Z", actor="ops",
                staff_id="S1", now=T("2026-11-19T08:10:00Z"),
            )
            ws.import_payload(
                {
                    "table": "consent",
                    "rows": [
                        {
                            "id": "C91",
                            "family": "F1",
                            "scope": "service/drink",
                            "effective": "2026-11-19T09:30:00Z",
                            "status": "revoked",
                        }
                    ],
                },
                now=T("2026-11-19T09:30:00Z"),
            )
            findings = affected_requests(ws, now=T("2026-11-19T09:35:00Z"))
            self.assertEqual([f["request"] for f in findings], ["Q1"])
            self.assertIn("cancel", {o["action"] for o in findings[0]["options"]})
            # 已承接的旧安排仍在，直到选择取消或替代方案被接受
            self.assertEqual(ws.request_row("Q1")["status"], "accepted")
            cancel_request(
                ws, "Q1", actor="ops", reason="家庭撤回任务授权", now=T("2026-11-19T09:40:00Z")
            )
            self.assertEqual(ws.active_reservations(), [])

    def test_analysis_revocation_stops_history_use(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            ws.import_payload(
                {
                    "table": "consent",
                    "rows": [
                        {
                            "id": "C98",
                            "family": "F1",
                            "scope": "analysis",
                            "effective": "2026-11-19T08:30:00Z",
                            "status": "revoked",
                        }
                    ],
                },
                now=T("2026-11-19T08:35:00Z"),
            )
            before = ws.snapshot(T("2026-11-19T08:20:00Z"))
            after = ws.snapshot(T("2026-11-19T08:40:00Z"))
            self.assertEqual(len(before.usable_history()), 120)
            self.assertEqual(len(after.usable_history()), 80)
            self.assertIn("F1", after.revoked_analysis_families())
            self.assertFalse(after.analysis_authorized("F1"))
            qual = after.qualification("F1", "drink", object_name="cup", environment="clear")
            self.assertEqual(qual["status"], "pending")
            self.assertTrue(any("未授权分析" in r for r in qual["reasons"]))


class ProposalTests(unittest.TestCase):
    def test_replacement_needs_family_confirmation_and_releases_original_after(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            accept_request(
                ws, "Q1", plan_kind="robot", start="2026-11-19T10:00:00Z", actor="ops",
                staff_id="S1", now=T("2026-11-19T08:10:00Z"),
            )
            before = {r["kind"]: r["start"] for r in ws.reservations_for("Q1")}
            proposal = create_proposal(
                ws, "Q1", action="human", plan_kind="human", start="2026-11-19T10:30:00Z",
                actor="ops", now=T("2026-11-19T08:20:00Z"),
            )
            # 家庭确认前，原占用仍在，新安排不占用
            after_proposal = {r["kind"]: r["start"] for r in ws.reservations_for("Q1")}
            self.assertEqual(before, after_proposal)
            self.assertEqual(ws.request_row("Q1")["status"], "accepted")
            confirm_proposal(ws, proposal["proposal"], actor="family:F1", now=T("2026-11-19T08:30:00Z"))
            self.assertEqual(ws.request_row("Q1")["plan_kind"], "human")
            self.assertEqual(ws.request_row("Q1")["plan_start"], "2026-11-19T10:30:00Z")
            active = [(r["kind"], r["resource"]) for r in ws.reservations_for("Q1")]
            self.assertEqual(active, [("staff", "S1")])
            released = [(r["resource"], r["release_reason"]) for r in ws.reservations_for("Q1", active_only=False) if r["status"] == "released"]
            self.assertIn(("R1", "replaced"), released)

    def test_declined_proposal_leaves_original_intact(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            accept_request(
                ws, "Q1", plan_kind="human", start="2026-11-19T10:00:00Z", actor="ops",
                staff_id="S1", now=T("2026-11-19T08:10:00Z"),
            )
            proposal = create_proposal(
                ws, "Q1", action="reschedule", plan_kind="human", start="2026-11-19T10:30:00Z",
                actor="ops", now=T("2026-11-19T08:20:00Z"),
            )
            confirm_proposal(ws, proposal["proposal"], actor="family:F1", accept=False, now=T("2026-11-19T08:25:00Z"))
            row = ws.request_row("Q1")
            self.assertEqual((row["status"], row["plan_start"]), ("accepted", "2026-11-19T10:00:00Z"))


class ReviewRegressionTests(unittest.TestCase):
    def test_cannot_replace_started_service(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            accept_request(
                ws, "Q1", plan_kind="robot", start="2026-11-19T10:00:00Z", actor="ops",
                staff_id="S1", now=T("2026-11-19T08:10:00Z"),
            )
            with self.assertRaises(OperationError):
                create_proposal(
                    ws, "Q1", action="reschedule", plan_kind="human",
                    start="2026-11-19T10:30:00Z", actor="ops", now=T("2026-11-19T10:06:00Z"),
                )
            self.assertEqual(ws.request_row("Q1")["plan_start"], "2026-11-19T10:00:00Z")

    def test_confirmed_robot_replacement_records_version_and_risk(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            accept_request(
                ws, "Q3", plan_kind="human", start="2026-11-19T10:00:00Z", actor="ops",
                staff_id="S1", now=T("2026-11-19T08:05:00Z"),
            )
            proposal = create_proposal(
                ws, "Q3", action="reschedule", plan_kind="robot",
                start="2026-11-19T10:15:00Z", actor="ops", now=T("2026-11-19T08:10:00Z"),
            )
            confirm_proposal(ws, proposal["proposal"], actor="family:F2", now=T("2026-11-19T08:15:00Z"))
            row = ws.request_row("Q3")
            self.assertEqual(row["plan_kind"], "robot")
            self.assertEqual(row["version"], "v1")
            self.assertEqual(row["risk_band"], "medium")
            self.assertIsNotNone(row["risk_upper"])
            record_fulfillment(
                ws, "Q3", outcome="handoff", actor="ops", evidence_used=True,
                now=T("2026-11-19T10:35:00Z"),
            )
            self.assertEqual(ws.fulfillments()[0]["version"], "v1")

    def test_incident_uses_booked_version_not_latest(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            accept_request(
                ws, "Q3", plan_kind="robot", start="2026-11-19T10:00:00Z", actor="ops",
                staff_id="S1", now=T("2026-11-19T08:05:00Z"),
            )
            ws.import_payload(
                {"table": "robots", "rows": [{"robot": "R2", "family": "F2", "version": "v2", "tasks": ["retrieve"]}]},
                now=T("2026-11-19T08:10:00Z"),
            )
            ws.import_payload(
                {"table": "capability_review", "rows": [{
                    "id": "CR9", "robot": "R2", "version": "v1", "task": "retrieve",
                    "result": "fail", "effective": "2026-11-19T08:15:00Z",
                    "received": "2026-11-19T08:15:00Z", "note": "v1 失败",
                }]},
                now=T("2026-11-19T08:15:00Z"),
            )
            ws.import_payload(
                {"table": "capability_review", "rows": [{
                    "id": "CR10", "robot": "R2", "version": "v2", "task": "retrieve",
                    "result": "pass", "effective": "2026-11-19T08:16:00Z",
                    "received": "2026-11-19T08:16:00Z", "note": "v2 通过",
                }]},
                now=T("2026-11-19T08:16:00Z"),
            )
            findings = affected_requests(ws, now=T("2026-11-19T08:20:00Z"))
            self.assertEqual([f["request"] for f in findings], ["Q3"])
            self.assertTrue(any("能力复查未通过" in r for r in findings[0]["reasons"]))

    def test_family_view_hides_operator_note(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            accept_request(
                ws, "Q1", plan_kind="human", start="2026-11-19T10:00:00Z", actor="ops",
                staff_id="S1", now=T("2026-11-19T08:05:00Z"),
            )
            record_fulfillment(
                ws, "Q1", outcome="refused", actor="ops",
                note="内部：住户情绪激动，需经理跟进", now=T("2026-11-19T10:35:00Z"),
            )
            view = family_view(ws, "F1", now=T("2026-11-19T10:40:00Z"))
            self.assertNotIn("情绪激动", json.dumps(view, ensure_ascii=False))

    def test_overview_and_family_view_respect_as_of(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            accept_request(
                ws, "Q1", plan_kind="human", start="2026-11-19T10:00:00Z", actor="ops",
                staff_id="S1", now=T("2026-11-19T08:05:00Z"),
            )
            record_fulfillment(
                ws, "Q1", outcome="refused", actor="ops", now=T("2026-11-19T10:35:00Z"),
            )
            early = operations_view(ws, now=T("2026-11-19T08:30:00Z"))
            self.assertEqual(early["requests"][0]["status"], "accepted")
            self.assertEqual(early["fulfilment"]["total"], 0)
            family_early = family_view(ws, "F1", now=T("2026-11-19T08:30:00Z"))
            self.assertEqual(family_early["requests"][0]["status"], "accepted")
            family_late = family_view(ws, "F1", now=T("2026-11-19T10:40:00Z"))
            self.assertEqual(family_late["requests"][0]["status"], "completed")

    def test_replay_does_not_show_cancelled_commitment(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            accept_request(
                ws, "Q1", plan_kind="robot", start="2026-11-19T10:00:00Z", actor="ops",
                staff_id="S1", now=T("2026-11-19T08:10:00Z"),
            )
            cancel_request(ws, "Q1", actor="family:F1", reason="改期", now=T("2026-11-19T09:00:00Z"))
            data = replay_request(ws, "Q1", as_of=T("2026-11-19T09:30:00Z"))
            self.assertEqual(data["status_then"], "cancelled")
            self.assertIsNone(data["plan_then"])
            before = replay_request(ws, "Q1", as_of=T("2026-11-19T08:30:00Z"))
            self.assertEqual(before["status_then"], "accepted")
            self.assertIsNotNone(before["plan_then"])

    def test_family_view_does_not_show_proposal_created_later(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            accept_request(
                ws, "Q1", plan_kind="human", start="2026-11-19T10:00:00Z", actor="ops",
                staff_id="S1", now=T("2026-11-19T08:05:00Z"),
            )
            create_proposal(
                ws, "Q1", action="reschedule", plan_kind="human",
                start="2026-11-19T10:30:00Z", actor="ops", now=T("2026-11-19T09:00:00Z"),
            )
            early = family_view(ws, "F1", now=T("2026-11-19T08:30:00Z"))
            self.assertEqual(early["requests"][0]["pending_proposals"], [])
            late = family_view(ws, "F1", now=T("2026-11-19T09:10:00Z"))
            self.assertEqual(len(late["requests"][0]["pending_proposals"]), 1)

    def test_reject_releases_held_resources(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            from service_pilot.operations import hold_request

            accept_request(
                ws, "Q1", plan_kind="robot", start="2026-11-19T10:00:00Z", actor="ops",
                staff_id="S1", now=T("2026-11-19T08:05:00Z"),
            )
            hold_request(ws, "Q1", actor="ops", reason="待核实", now=T("2026-11-19T08:10:00Z"))
            reject_request(ws, "Q1", actor="ops", reason="无法安排", now=T("2026-11-19T08:15:00Z"))
            self.assertEqual(ws.active_reservations(), [])

    def test_cannot_cancel_started_service(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            accept_request(
                ws, "Q1", plan_kind="human", start="2026-11-19T10:00:00Z", actor="ops",
                staff_id="S1", now=T("2026-11-19T08:05:00Z"),
            )
            with self.assertRaises(OperationError):
                cancel_request(
                    ws, "Q1", actor="family:F1", reason="临时有事", now=T("2026-11-19T10:10:00Z")
                )
            self.assertEqual(ws.request_row("Q1")["status"], "accepted")
            self.assertEqual(len(ws.active_reservations()), 1)

    def test_completion_closes_open_proposals(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            accept_request(
                ws, "Q1", plan_kind="human", start="2026-11-19T10:00:00Z", actor="ops",
                staff_id="S1", now=T("2026-11-19T08:05:00Z"),
            )
            create_proposal(
                ws, "Q1", action="reschedule", plan_kind="human",
                start="2026-11-19T10:30:00Z", actor="ops", now=T("2026-11-19T09:00:00Z"),
            )
            record_fulfillment(
                ws, "Q1", outcome="independent", actor="ops", now=T("2026-11-19T10:35:00Z")
            )
            self.assertEqual(ws.proposals("Q1", open_only=True), [])
            with self.assertRaises(OperationError):
                confirm_proposal(ws, 1, actor="family:F1", now=T("2026-11-19T10:40:00Z"))

    def test_import_cannot_backdate_before_current_clock(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp, now="2026-11-19T12:00:00Z") as ws:
            from service_pilot import sources as source_module

            with self.assertRaises(source_module.SourceError):
                ws.import_payload(
                    {"table": "history", "rows": [{
                        "id": "H900", "family": "F1", "robot": "R1", "version": "v2",
                        "task": "drink", "object": "cup", "environment": "clear",
                        "occurred": "2026-11-19T07:00:00Z", "outcome": "independent",
                        "reviewed": True,
                    }]},
                    now=T("2026-11-19T08:05:00Z"),
                )


class ReplayTests(unittest.TestCase):
    def test_replay_uses_only_evidence_known_then(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            ws.import_payload(
                {
                    "table": "maintenance",
                    "rows": [
                        {
                            "id": "N9",
                            "robot": "R1",
                            "start": "2026-11-19T10:00:00Z",
                            "end": "2026-11-19T11:00:00Z",
                            "received": "2026-11-19T08:50:00Z",
                        }
                    ],
                },
                now=T("2026-11-19T08:50:00Z"),
            )
            before = replay_request(ws, "Q1", as_of=T("2026-11-19T08:30:00Z"))
            after = replay_request(ws, "Q1", as_of=T("2026-11-19T09:00:00Z"))
            before_robot = [o for o in before["options_then"]["options"] if o["plan"] == "robot" and o["feasible"]]
            after_robot = [o for o in after["options_then"]["options"] if o["plan"] == "robot" and o["feasible"]]
            self.assertTrue(before_robot)
            self.assertFalse(any(o["start"].startswith("2026-11-19T10:00") for o in after_robot))
            self.assertEqual(before["later_facts"][0]["table"], "maintenance")
            # 后来资料不会改变当时结论
            self.assertTrue(before_robot)

    def test_duplicate_delivery_creates_no_new_activity(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            row = {
                "id": "H1",
                "family": "F2",
                "robot": "R2",
                "version": "v1",
                "task": "retrieve",
                "object": "box",
                "environment": "clear",
                "occurred": "2026-10-28T10:00:00Z",
                "outcome": "independent",
                "reviewed": True,
            }
            report = ws.import_payload({"table": "history", "rows": [row]}, now=T("2026-11-19T08:05:00Z"))
            self.assertEqual(report["history"]["duplicate"], 1)
            self.assertEqual(report["history"]["imported"], 0)

    def test_conflicting_source_version_is_preserved_as_issue(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            row = {
                "id": "H1",
                "family": "F2",
                "robot": "R2",
                "version": "v1",
                "task": "retrieve",
                "object": "box",
                "environment": "clear",
                "occurred": "2026-10-28T10:00:00Z",
                "outcome": "refused",  # 与已有事实不同
                "reviewed": True,
            }
            report = ws.import_payload({"table": "history", "rows": [row]}, now=T("2026-11-19T08:05:00Z"))
            self.assertEqual(report["history"]["conflict"], 1)
            issues = ws.source_issues()
            self.assertEqual(len(issues), 1)
            self.assertIn("保留原记录", issues[0]["detail"])


class FamilyIntakeTests(unittest.TestCase):
    def test_family_submission_is_not_a_commitment(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            from service_pilot.operations import submit_request

            result = submit_request(
                ws,
                family="F1",
                task="drink",
                earliest="2026-11-19T10:00:00Z",
                latest="2026-11-19T11:00:00Z",
                minutes=30,
                object_name="cup",
                environment="clear",
                collection_scope="service-only",
                now=T("2026-11-19T08:05:00Z"),
            )
            row = ws.request_row(result["request"])
            self.assertEqual(row["status"], "received")
            self.assertEqual(ws.active_reservations(), [])
            self.assertIn("不是承诺", row["status_reason"])

    def test_family_cannot_submit_task_outside_permission(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            from service_pilot.operations import submit_request

            with self.assertRaises(OperationError):
                submit_request(
                    ws,
                    family="F2",
                    task="drink",
                    earliest="2026-11-19T10:00:00Z",
                    latest="2026-11-19T11:00:00Z",
                    minutes=30,
                    object_name="cup",
                    environment="clear",
                    now=T("2026-11-19T08:05:00Z"),
                )

    def test_family_cannot_confirm_another_households_proposal(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            from service_pilot.operations import family_confirm_proposal

            accept_request(
                ws, "Q1", plan_kind="human", start="2026-11-19T10:00:00Z", actor="ops",
                staff_id="S1", now=T("2026-11-19T08:10:00Z"),
            )
            proposal = create_proposal(
                ws, "Q1", action="reschedule", plan_kind="human",
                start="2026-11-19T10:30:00Z", actor="ops", now=T("2026-11-19T08:15:00Z"),
            )
            with self.assertRaises(OperationError):
                family_confirm_proposal(
                    ws, proposal["proposal"], family="F2", now=T("2026-11-19T08:20:00Z")
                )
            family_confirm_proposal(
                ws, proposal["proposal"], family="F1", now=T("2026-11-19T08:25:00Z")
            )
            self.assertEqual(ws.request_row("Q1")["plan_kind"], "human")

    def test_robot_assurance_cannot_exceed_family_window(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            data = preview_plan(
                ws, family="F1", task="drink", earliest="2026-11-19T10:00:00Z",
                latest="2026-11-19T10:30:00Z", minutes=30, object_name="cup",
                environment="clear", now=T("2026-11-19T08:00:00Z"),
            )
            robot = [o for o in data["options"] if o["plan"] == "robot"]
            self.assertFalse(any(o["feasible"] for o in robot))
            self.assertTrue(any("无法完整保留" in " ".join(o["reasons"]) for o in robot))


class PrivacyTests(unittest.TestCase):
    def test_family_view_is_scoped(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            accept_request(
                ws, "Q1", plan_kind="human", start="2026-11-19T10:00:00Z", actor="ops",
                staff_id="S1", now=T("2026-11-19T08:10:00Z"),
            )
            view = family_view(ws, "F1", now=T("2026-11-19T08:20:00Z"))
            self.assertEqual(view["family"], "F1")
            self.assertEqual([r["request"] for r in view["requests"]], ["Q1"])
            with self.assertRaises(ValueError):
                family_view(ws, "F1", request_id="Q3", now=T("2026-11-19T08:20:00Z"))
            payload = json.dumps(view, ensure_ascii=False)
            self.assertNotIn("Q2", payload)
            self.assertNotIn("Q3", payload)
            self.assertNotIn("S2", payload)  # 未接手本户的人员

    def test_family_view_does_not_expose_internal_hold_reason(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            from service_pilot.operations import hold_request

            hold_request(
                ws, "Q1", actor="ops", reason="内部：该户欠费需先核实",
                now=T("2026-11-19T08:05:00Z"),
            )
            view = family_view(ws, "F1", now=T("2026-11-19T08:10:00Z"))
            payload = json.dumps(view, ensure_ascii=False)
            self.assertNotIn("欠费", payload)
            self.assertEqual(view["requests"][0]["status"], "held")

    def test_operations_view_links_load_risk_and_failures(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            accept_request(
                ws, "Q1", plan_kind="robot", start="2026-11-19T10:00:00Z", actor="ops",
                staff_id="S1", now=T("2026-11-19T08:10:00Z"),
            )
            view = operations_view(ws, now=T("2026-11-19T08:20:00Z"))
            self.assertEqual(view["staff_load"][0]["staff"], "S1")
            record_fulfillment(
                ws, "Q1", outcome="handoff", actor="ops", now=T("2026-11-19T10:35:00Z"),
            )
            view = operations_view(ws, now=T("2026-11-19T10:40:00Z"))
            self.assertEqual(view["staff_load"], [])
            self.assertEqual(view["fulfilment"]["by_outcome"]["人工接管后完成"], 1)
            self.assertTrue(view["weak_evidence"])
            self.assertTrue(all("risk_upper" in c for c in view["weak_evidence"]))


class PersistenceTests(unittest.TestCase):
    def test_state_survives_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = make_ws(tmp)
            accept_request(
                ws, "Q1", plan_kind="robot", start="2026-11-19T10:00:00Z", actor="ops",
                staff_id="S1", now=T("2026-11-19T08:10:00Z"),
            )
            ws.close()
            with Workspace.open(Path(tmp) / "work") as reopened:
                self.assertEqual(reopened.request_row("Q1")["status"], "accepted")
                self.assertEqual(len(reopened.active_reservations()), 2)
                events = reopened.request_events("Q1")
                self.assertEqual(events[-1]["kind"], "accepted")

    def test_workspaces_do_not_pollute_each_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws1 = make_ws(tmp)
            ws2 = Workspace.initialize(str(Path(tmp) / "other"), ROOT / "inputs", now=T("2026-11-19T08:00:00Z"))
            accept_request(
                ws1, "Q1", plan_kind="human", start="2026-11-19T10:00:00Z", actor="ops",
                staff_id="S1", now=T("2026-11-19T08:10:00Z"),
            )
            self.assertEqual(ws2.request_row("Q1")["status"], "received")
            self.assertEqual(ws2.active_reservations(), [])
            ws1.close()
            ws2.close()


class EdgeCaseTests(unittest.TestCase):
    def test_completed_request_no_longer_holds_future_slot(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            accept_request(
                ws, "Q1", plan_kind="robot", start="2026-11-19T10:00:00Z", actor="ops",
                staff_id="S1", now=T("2026-11-19T08:10:00Z"),
            )
            record_fulfillment(
                ws, "Q1", outcome="independent", actor="ops",
                started_at="2026-11-19T10:00:00Z", completed_at="2026-11-19T10:30:00Z",
                now=T("2026-11-19T10:35:00Z"),
            )
            self.assertEqual(ws.request_row("Q1")["status"], "completed")
            self.assertEqual(ws.active_reservations(), [])
            result = accept_request(
                ws, "Q3", plan_kind="human", start="2026-11-19T10:45:00Z", actor="ops",
                now=T("2026-11-19T10:40:00Z"),
            )
            self.assertEqual(result["status"], "accepted")

    def test_past_service_slot_cannot_be_accepted(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            with self.assertRaises(OperationError):
                accept_request(
                    ws, "Q1", plan_kind="human", start="2026-11-19T10:00:00Z", actor="ops",
                    now=T("2026-11-23T00:00:00Z"),
                )

    def test_latest_start_is_considered_even_off_grid(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            data = preview_plan(
                ws, family="F2", task="retrieve", earliest="2026-11-19T10:00:00Z",
                latest="2026-11-19T11:40:00Z", minutes=30, object_name="box",
                environment="clear", now=T("2026-11-19T08:00:00Z"),
            )
            starts = {o["start"] for o in data["options"] if o["plan"] == "human"}
            self.assertIn("2026-11-19T11:10:00Z", starts)

    def test_robot_without_declaration_is_blocked_even_if_family_has_other_robot(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            data = preview_plan(
                ws, family="F2", task="drink", earliest="2026-11-19T10:00:00Z",
                latest="2026-11-19T11:00:00Z", minutes=30, object_name="cup",
                environment="clear", now=T("2026-11-19T08:00:00Z"),
            )
            self.assertNotEqual(data["qualification"]["status"], "feasible")
            self.assertTrue(
                any(
                    "能力声明" in r or "设备" in r or "任务许可" in r
                    for r in data["qualification"]["reasons"]
                )
            )

    def test_duplicate_staff_booking_is_rejected_by_period_overlap(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            accept_request(
                ws, "Q1", plan_kind="human", start="2026-11-19T10:00:00Z", actor="ops",
                staff_id="S2", now=T("2026-11-19T08:10:00Z"),
            )
            # Q2 只有 S2 具备 south/drink；S2 已直接办理 Q1
            with self.assertRaises(OperationError):
                accept_request(
                    ws, "Q2", plan_kind="human", start="2026-11-19T10:15:00Z", actor="ops2",
                    now=T("2026-11-19T08:15:00Z"),
                )


class InputContractTests(unittest.TestCase):
    def test_initial_sources_are_marked_known_before_requests(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            snap = ws.snapshot(T("2026-11-19T08:00:00Z"))
            self.assertEqual(len(snap.history), 120)
            self.assertEqual(len(snap.families), 3)
            self.assertEqual(len(snap.robots), 3)
            self.assertEqual(len(snap.maintenance), 1)

    def test_later_received_cannot_backdate_facts(self):
        with tempfile.TemporaryDirectory() as tmp, make_ws(tmp) as ws:
            ws.import_payload(
                {
                    "table": "maintenance",
                    "rows": [
                        {
                            "id": "N3",
                            "robot": "R1",
                            "start": "2026-11-18T08:00:00Z",
                            "end": "2026-11-18T09:00:00Z",
                            "received": "2026-11-19T09:00:00Z",
                        }
                    ],
                },
                now=T("2026-11-19T09:00:00Z"),
            )
            before = ws.snapshot(T("2026-11-19T08:30:00Z"))
            after = ws.snapshot(T("2026-11-19T09:30:00Z"))
            self.assertEqual(len(before.maintenance), 1)
            self.assertEqual(len(after.maintenance), 2)


if __name__ == "__main__":
    unittest.main()
