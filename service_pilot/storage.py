"""本地持久化：SQLite 状态库。

每个工作目录独立一个 state.db。来源资料只读复制为“来源事实”，附带
known_at：基线资料按发生/生效时点视为已知，后来导入的资料以 received
或导入时刻为准。这样任何分析都能严格按当时可知证据回溯。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from . import sources
from .domain import (
    REQ_ACCEPTED,
    REQ_CANCELLED,
    REQ_COMPLETED,
    REQ_HELD,
    REQ_PENDING,
    REQ_RECEIVED,
    REQ_REJECTED,
    UTC,
    iso,
    parse_ts,
)
from .evidence import EvidenceSnapshot

SCHEMA_VERSION = 2

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_fact (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,
    source_key TEXT NOT NULL,
    source_version TEXT NOT NULL DEFAULT '',
    digest TEXT NOT NULL,
    payload TEXT NOT NULL,
    known_at TEXT NOT NULL,
    origin TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    UNIQUE (table_name, source_key, source_version, digest)
);
CREATE INDEX IF NOT EXISTS idx_source_fact_lookup
    ON source_fact (table_name, source_key, source_version);
CREATE INDEX IF NOT EXISTS idx_source_fact_known ON source_fact (known_at, id);
CREATE TABLE IF NOT EXISTS source_issue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    table_name TEXT NOT NULL,
    source_key TEXT NOT NULL,
    source_version TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS request_state (
    request TEXT PRIMARY KEY,
    family TEXT NOT NULL,
    task TEXT NOT NULL,
    earliest TEXT NOT NULL,
    latest TEXT NOT NULL,
    minutes INTEGER NOT NULL,
    object TEXT,
    environment TEXT,
    received TEXT NOT NULL,
    status TEXT NOT NULL,
    status_reason TEXT,
    plan_kind TEXT,
    plan_start TEXT,
    plan_end TEXT,
    robot TEXT,
    version TEXT,
    staff_required INTEGER,
    staff_ids TEXT,
    risk_basis TEXT,
    risk_upper REAL,
    risk_band TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS request_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request TEXT NOT NULL,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_request_event ON request_event (request, at, id);
CREATE TABLE IF NOT EXISTS reservation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request TEXT NOT NULL,
    kind TEXT NOT NULL,
    resource TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    released_at TEXT,
    release_reason TEXT,
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_reservation_window ON reservation (resource, start, end, status);
CREATE INDEX IF NOT EXISTS idx_reservation_request ON reservation (request, status);
CREATE TABLE IF NOT EXISTS proposal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request TEXT NOT NULL,
    action TEXT NOT NULL,
    plan_kind TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    robot TEXT,
    staff_ids TEXT NOT NULL DEFAULT '[]',
    coverage_start TEXT,
    coverage_end TEXT,
    risk_basis TEXT,
    risk_upper REAL,
    risk_band TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_proposal_request ON proposal (request, status);
CREATE TABLE IF NOT EXISTS fulfillment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request TEXT NOT NULL,
    family TEXT NOT NULL,
    task TEXT NOT NULL,
    robot TEXT,
    version TEXT,
    outcome TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    recorded_at TEXT NOT NULL,
    note TEXT,
    evidence_used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


def _digest(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _now_iso(now: datetime | None) -> str:
    return iso(now or datetime.now(UTC))


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "state.db"
        self.conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 15000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA_SQL)
        self._migrate()
        version = self.meta_get("schema_version")
        if version is None:
            self.meta_set("schema_version", str(SCHEMA_VERSION))
        elif int(version) > SCHEMA_VERSION:
            raise RuntimeError(f"工作目录数据库版本 {version} 高于程序版本 {SCHEMA_VERSION}")
        elif int(version) < SCHEMA_VERSION:
            self.meta_set("schema_version", str(SCHEMA_VERSION))

    def _migrate(self) -> None:
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(fulfillment)")}
        if "robot" not in columns:
            self.conn.execute("ALTER TABLE fulfillment ADD COLUMN robot TEXT")
        if "version" not in columns:
            self.conn.execute("ALTER TABLE fulfillment ADD COLUMN version TEXT")

    # ---------- 基础 ----------
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Workspace":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def meta_get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def meta_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def now(self) -> datetime:
        stored = self.meta_get("clock_now")
        if stored:
            return parse_ts(stored)
        return datetime.now(UTC)

    def set_clock(self, now: datetime, *, actor: str = "system") -> None:
        """演练用固定时钟；只前进不后退，保证历史不会被重写。"""
        current = self.meta_get("clock_now")
        if current and parse_ts(current) > now:
            raise ValueError("演练时钟不能倒退")
        if current and parse_ts(current) == now:
            return
        self.meta_set("clock_now", iso(now))
        self.conn.execute(
            "INSERT INTO audit(at, actor, action, detail) VALUES (?, ?, 'clock', ?)",
            (iso(now), actor, current or ""),
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def audit(self, actor: str, action: str, detail: dict | str, at: datetime | None = None) -> None:
        payload = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False, sort_keys=True)
        self.conn.execute(
            "INSERT INTO audit(at, actor, action, detail) VALUES (?, ?, ?, ?)",
            (iso(at or self.now()), actor, action, payload),
        )

    # ---------- 初始化与导入 ----------
    @classmethod
    def initialize(
        cls,
        root: str | Path,
        source_dir: str | Path,
        *,
        now: datetime | None = None,
        check_manifest: bool = True,
    ) -> "Workspace":
        root = Path(root)
        if (root / "state.db").exists():
            raise FileExistsError(f"工作目录已有状态库，请换目录或继续打开: {root}")
        ws = cls(root)
        ws.meta_set("created_at", iso(now or datetime.now(UTC)))
        ws.meta_set("source_dir", str(Path(source_dir).resolve()))
        ws.meta_set("clock_now", iso(now or datetime.now(UTC)))
        try:
            bundle = sources.load_source_bundle(source_dir, check_manifest=check_manifest)
            ws._seed(bundle, now or datetime.now(UTC))
        except Exception:
            ws.close()
            # 失败时不留下半成品工作目录
            for name in ("state.db", "state.db-wal", "state.db-shm"):
                path = root / name
                if path.exists():
                    path.unlink()
            raise
        return ws

    @classmethod
    def open(cls, root: str | Path) -> "Workspace":
        root = Path(root)
        if not (root / "state.db").exists():
            raise FileNotFoundError(f"工作目录尚未初始化: {root}")
        return cls(root)

    def _seed(self, bundle: sources.SourceBundle, import_time: datetime) -> None:
        with self.transaction():
            for filename, digest in bundle.hashes.items():
                self.conn.execute(
                    "INSERT INTO meta(key, value) VALUES (?, ?)", (f"source_hash:{filename}", digest)
                )
            initial_known = import_time
            for table in sources.TABLES:
                for row in bundle.rows(table):
                    if table == "history":
                        known_at = parse_ts(row["occurred"])
                    elif table == "consent":
                        known_at = parse_ts(row["effective"])
                    elif table == "maintenance":
                        known_at = parse_ts(row["received"])
                    elif table == "requests":
                        known_at = initial_known
                    else:
                        known_at = parse_ts(sources.BASELINE_KNOWN_AT)
                    self._insert_fact(table, row, known_at=known_at, origin="initial", imported_at=import_time)
                    if table == "requests":
                        self._create_request(row, received=initial_known, actor="system:init", at=initial_known)
            self.audit(
                "system:init",
                "initialize",
                {"tables": {table: len(bundle.rows(table)) for table in sources.TABLES}},
                import_time,
            )

    def _source_identity(self, table: str, row: dict) -> tuple[str, str]:
        if table == "families":
            return str(row["family"]), ""
        if table == "robots":
            return str(row["robot"]), str(row["version"])
        if table == "history":
            return str(row["id"]), str(row["version"])
        if table == "consent":
            return str(row["id"]), ""
        if table == "staff":
            return f"{row['staff']}|{row['start']}|{row['end']}", ""
        if table == "requests":
            return str(row["request"]), ""
        if table == "task_policy":
            return str(row["task"]), ""
        if table == "maintenance":
            return str(row["id"]), ""
        if table == "capability_review":
            return str(row["id"]), ""
        if table == "staff_unavailability":
            return str(row["id"]), ""
        raise ValueError(f"未知来源表: {table}")

    def _insert_fact(
        self,
        table: str,
        row: dict,
        *,
        known_at: datetime,
        origin: str,
        imported_at: datetime,
    ) -> str:
        key, version = self._source_identity(table, row)
        digest = _digest(row)
        existing = self.conn.execute(
            "SELECT digest FROM source_fact WHERE table_name = ? AND source_key = ? AND source_version = ?",
            (table, key, version),
        ).fetchall()
        if any(item["digest"] == digest for item in existing):
            return "duplicate"
        if existing and table not in _REVISION_TABLES:
            detail = (
                f"{table} 来源身份 {key} 已有不同内容；保留原记录并登记冲突，不合并、不覆盖"
            )
            self.conn.execute(
                "INSERT INTO source_issue(at, table_name, source_key, source_version, kind, detail) "
                "VALUES (?, ?, ?, ?, 'version_conflict', ?)",
                (iso(imported_at), table, key, version, detail),
            )
            self.audit("system:import", "source_conflict", detail, imported_at)
            return "conflict"
        self.conn.execute(
            "INSERT INTO source_fact(table_name, source_key, source_version, digest, payload, known_at, origin, imported_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                table,
                key,
                version,
                digest,
                json.dumps(row, ensure_ascii=False, sort_keys=True),
                iso(known_at),
                origin,
                iso(imported_at),
            ),
        )
        return "imported"

    def import_payload(
        self,
        payload: dict | list,
        *,
        actor: str = "ops",
        now: datetime | None = None,
        table_hint: str | None = None,
    ) -> dict:
        """导入新记录。返回每张表的 imported/duplicate/conflict 计数。"""
        at = now or self.now()
        current = self.now()
        if at < current:
            raise sources.SourceError(
                [f"导入时点 {iso(at)} 早于工作目录当前时点 {iso(current)}；不能用后来的资料回填更早的分析"]
            )
        groups: list[tuple[str, list[dict]]] = []
        if isinstance(payload, list):
            if not table_hint:
                raise sources.SourceError(["数组导入需要指定 table"])
            groups.append((table_hint, payload))
        elif isinstance(payload, dict) and "rows" in payload:
            table = payload.get("table") or table_hint
            if not table:
                raise sources.SourceError(["导入文件缺少 table"])
            groups.append((table, payload["rows"]))
        elif isinstance(payload, dict):
            for key, value in payload.items():
                if key in _IMPORT_TABLES:
                    if not isinstance(value, list):
                        raise sources.SourceError([f"{key} 必须是数组"])
                    groups.append((key, value))
                else:
                    raise sources.SourceError([f"未知导入表: {key}"])
        else:
            raise sources.SourceError(["导入内容必须是对象或数组"])

        report: dict[str, dict] = {}
        new_requests: list[tuple[dict, datetime]] = []
        with self.transaction():
            for table, rows in groups:
                if table not in _IMPORT_TABLES:
                    raise sources.SourceError([f"不支持导入表: {table}"])
                counts = {"imported": 0, "duplicate": 0, "conflict": 0, "rejected": 0, "errors": []}
                for index, row in enumerate(rows):
                    if not isinstance(row, dict):
                        counts["rejected"] += 1
                        counts["errors"].append(f"{table}[{index}] 不是对象")
                        continue
                    try:
                        cleaned = _validate_import_row(table, row, self, at)
                        _validate_references(table, cleaned, self, at)
                    except ValueError as exc:
                        counts["rejected"] += 1
                        counts["errors"].append(f"{table}[{index}] {exc}")
                        continue
                    if table == "requests":
                        existing = self.request_row(cleaned["request"])
                        if existing is not None:
                            same = (
                                existing["family"] == cleaned["family"]
                                and existing["task"] == cleaned["task"]
                                and existing["earliest"] == iso(parse_ts(cleaned["earliest"]))
                                and existing["latest"] == iso(parse_ts(cleaned["latest"]))
                                and existing["minutes"] == cleaned["minutes"]
                                and existing["object"] == cleaned.get("object")
                                and existing["environment"] == cleaned.get("environment")
                            )
                            if same:
                                counts["duplicate"] += 1
                            else:
                                detail = (
                                    f"requests 来源身份 {cleaned['request']} 已有不同内容；"
                                    "保留原请求并登记冲突，不覆盖"
                                )
                                self.conn.execute(
                                    "INSERT INTO source_issue(at, table_name, source_key, source_version, kind, detail) "
                                    "VALUES (?, 'requests', ?, '', 'version_conflict', ?)",
                                    (iso(at), cleaned["request"], detail),
                                )
                                counts["conflict"] += 1
                            continue
                    received = parse_ts(cleaned.get("received")) or at
                    known_at = received if table in _RECEIVED_TABLES else at
                    if table == "staff":
                        known_at = at
                    result = self._insert_fact(
                        table,
                        cleaned,
                        known_at=known_at,
                        origin="import",
                        imported_at=at,
                    )
                    counts[result] += 1
                    if result == "imported" and table == "requests":
                        new_requests.append((cleaned, received))
                report[table] = counts
            for row, received in new_requests:
                self._create_request(row, received=received, actor=actor, at=at)
            self.audit(actor, "import", report, at)
        self.set_clock(at, actor=actor)
        return report

    def _create_request(self, row: dict, *, received: datetime, actor: str, at: datetime) -> None:
        if self.request_row(row["request"]) is not None:
            return
        self.conn.execute(
            "INSERT INTO request_state(request, family, task, earliest, latest, minutes, object, environment, "
            "received, status, status_reason, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'received', ?, ?)",
            (
                row["request"],
                row["family"],
                row["task"],
                iso(parse_ts(row["earliest"])),
                iso(parse_ts(row["latest"])),
                int(row["minutes"]),
                row.get("object"),
                row.get("environment"),
                iso(received),
                "等待运营评估",
                iso(at),
            ),
        )
        self._event(row["request"], at, actor, "received", {"family": row["family"], "task": row["task"]})

    def _event(self, request: str, at: datetime, actor: str, kind: str, detail: dict) -> None:
        self.conn.execute(
            "INSERT INTO request_event(request, at, actor, kind, detail) VALUES (?, ?, ?, ?, ?)",
            (request, iso(at), actor, kind, json.dumps(detail, ensure_ascii=False, sort_keys=True)),
        )

    def add_event(self, request: str, at: datetime, actor: str, kind: str, detail: dict) -> None:
        with self.transaction():
            self._event(request, at, actor, kind, detail)

    # ---------- 读取 ----------
    def request_row(self, request: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM request_state WHERE request = ?", (request,)).fetchone()

    def requests(self, as_of: datetime | None = None) -> list[dict]:
        if as_of is None:
            rows = self.conn.execute("SELECT * FROM request_state ORDER BY received, request").fetchall()
            return [dict(row) for row in rows]
        result = []
        for stored in self.conn.execute(
            "SELECT * FROM request_state WHERE received <= ? ORDER BY received, request",
            (iso(as_of),),
        ).fetchall():
            row = dict(stored)
            state = self.request_state_as_of(row["request"], as_of)
            if state is None:
                continue
            row["status"] = state["status"]
            row["status_reason"] = state.get("status_reason")
            row["plan_kind"] = state.get("plan_kind")
            row["plan_start"] = state.get("plan_start")
            row["plan_end"] = state.get("plan_end")
            row["robot"] = state.get("robot")
            row["version"] = state.get("version")
            row["staff_ids"] = state.get("staff_ids")
            row["risk_band"] = state.get("risk_band")
            result.append(row)
        return result

    def request_state_as_of(self, request: str, as_of: datetime) -> dict | None:
        """从请求事件重建截至某时点的状态，不使用当前行里的后来状态。"""
        events = self.request_events(request, as_of)
        if not events:
            return None
        state: dict = {
            "status": "received",
            "status_reason": None,
            "plan_kind": None,
            "plan_start": None,
            "plan_end": None,
            "robot": None,
            "version": None,
            "staff_ids": None,
            "risk_band": None,
        }
        for event in events:
            kind = event["kind"]
            detail = json.loads(event["detail"])
            if kind in (
                REQ_RECEIVED,
                REQ_PENDING,
                REQ_ACCEPTED,
                REQ_HELD,
                REQ_CANCELLED,
                REQ_REJECTED,
                REQ_COMPLETED,
                "fulfillment",
            ):
                if kind != "fulfillment":
                    state["status"] = kind
                state["status"] = kind
            if kind == REQ_ACCEPTED:
                state["plan_kind"] = detail.get("plan")
                state["plan_start"] = detail.get("start")
                state["plan_end"] = detail.get("end")
                state["robot"] = detail.get("robot")
                state["version"] = detail.get("version")
                state["staff_ids"] = json.dumps(detail.get("staff") or [], ensure_ascii=False)
                state["risk_band"] = detail.get("risk_band")
            if kind == "fulfillment":
                state["status"] = REQ_COMPLETED
            elif kind == "proposal":
                if state["status"] == REQ_ACCEPTED:
                    state["status_reason"] = "替代安排待家庭确认"
            elif kind == REQ_CANCELLED:
                state["status_reason"] = detail.get("reason")
            elif kind == REQ_REJECTED:
                state["status_reason"] = detail.get("reason")
        return state

    def request_events(self, request: str, as_of: datetime | None = None) -> list[sqlite3.Row]:
        if as_of is None:
            return list(
                self.conn.execute(
                    "SELECT * FROM request_event WHERE request = ? ORDER BY at, id", (request,)
                )
            )
        return list(
            self.conn.execute(
                "SELECT * FROM request_event WHERE request = ? AND at <= ? ORDER BY at, id",
                (request, iso(as_of)),
            )
        )

    def request_status_as_of(self, request: str, as_of: datetime) -> dict | None:
        events = self.request_events(request, as_of)
        if not events:
            return None
        last = events[-1]
        detail = json.loads(last["detail"])
        return {
            "request": request,
            "status": last["kind"],
            "at": last["at"],
            "actor": last["actor"],
            "detail": detail,
        }

    def source_facts(self, as_of: datetime | None = None, table: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM source_fact"
        clauses: list[str] = []
        params: list[Any] = []
        if as_of is not None:
            clauses.append("known_at <= ?")
            params.append(iso(as_of))
        if table is not None:
            clauses.append("table_name = ?")
            params.append(table)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY known_at, id"
        return list(self.conn.execute(sql, params))

    def active_reservations(self, as_of: datetime | None = None) -> list[sqlite3.Row]:
        at = iso(as_of) if as_of else None
        if at is None:
            return list(
                self.conn.execute(
                    "SELECT * FROM reservation WHERE status = 'held' ORDER BY start, resource"
                )
            )
        # 回看某时点时，占用可能在之后才释放；此时 status 已是 released，
        # 但 released_at 晚于查询时点，仍应在该时点视为有效。
        return list(
            self.conn.execute(
                "SELECT * FROM reservation WHERE created_at <= ? "
                "AND (released_at IS NULL OR released_at > ?) "
                "ORDER BY start, resource",
                (at, at),
            )
        )

    def reservations_for(self, request: str, *, active_only: bool = True) -> list[sqlite3.Row]:
        if active_only:
            return list(
                self.conn.execute(
                    "SELECT * FROM reservation WHERE request = ? AND status = 'held' ORDER BY start, resource",
                    (request,),
                )
            )
        return list(
            self.conn.execute(
                "SELECT * FROM reservation WHERE request = ? ORDER BY id", (request,)
            )
        )

    def proposals(self, request: str | None = None, *, open_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM proposal"
        clauses, params = [], []
        if request:
            clauses.append("request = ?")
            params.append(request)
        if open_only:
            clauses.append("status = 'awaiting_family'")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC"
        return list(self.conn.execute(sql, params))

    def proposal(self, proposal_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM proposal WHERE id = ?", (proposal_id,)).fetchone()

    def fulfillments(
        self, family: str | None = None, as_of: datetime | None = None
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        if family:
            clauses.append("family = ?")
            params.append(family)
        if as_of is not None:
            clauses.append("recorded_at <= ?")
            params.append(iso(as_of))
        sql = "SELECT * FROM fulfillment"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY recorded_at, id"
        return list(self.conn.execute(sql, params))

    # ---------- 快照 ----------
    def snapshot(self, as_of: datetime | None = None) -> EvidenceSnapshot:
        at = as_of or self.now()
        facts = self.source_facts(at)
        by_table: dict[str, list[dict]] = {name: [] for name in sources.TABLES}
        by_table.update({"capability_review": [], "staff_unavailability": []})
        for fact in facts:
            table = fact["table_name"]
            if table in by_table:
                by_table[table].append(json.loads(fact["payload"]))

        families: dict[str, dict] = {}
        for row in by_table["families"]:
            families[row["family"]] = row

        # 每台设备只保留截至分析时点已知的最新版本。
        # 按事实进入系统的顺序（known_at, id）取名，而不是按版本字符串排序。
        robot_latest: dict[str, dict] = {}
        for fact in facts:
            if fact["table_name"] != "robots":
                continue
            row = json.loads(fact["payload"])
            robot_latest[row["robot"]] = row

        policy: dict[str, dict] = {}
        for row in by_table["task_policy"]:
            policy[row["task"]] = row

        staff: list[dict] = []
        seen_staff: set[tuple] = set()
        for row in by_table["staff"]:
            key = (row["staff"], row["start"], row["end"])
            if key in seen_staff:
                continue
            seen_staff.add(key)
            staff.append(row)

        requests_by_id = {
            row["request"]: row
            for row in self.conn.execute("SELECT * FROM request_state")
        }
        fulfilments = []
        for row in self.conn.execute(
            "SELECT * FROM fulfillment WHERE recorded_at <= ? ORDER BY recorded_at, id", (iso(at),)
        ):
            request_row = requests_by_id.get(row["request"])
            fulfilments.append(
                {
                    "id": f"FUL-{row['id']}",
                    "family": row["family"],
                    "robot": row["robot"] if row["robot"] is not None else (
                        request_row["robot"] if request_row else None
                    ),
                    "version": row["version"] if row["version"] is not None else (
                        request_row["version"] if request_row else None
                    ),
                    "task": row["task"],
                    "object": request_row["object"] if request_row else None,
                    "environment": request_row["environment"] if request_row else None,
                    "occurred": row["completed_at"] or row["recorded_at"],
                    "outcome": row["outcome"],
                    # 运营记录的事实不自动视为人工复核过的证据
                    "reviewed": False,
                    "recorded_at": row["recorded_at"],
                    "from_fulfillment": True,
                    "request": row["request"],
                    "evidence_used": bool(row["evidence_used"]),
                }
            )

        return EvidenceSnapshot(
            at=at,
            families=families,
            robots=list(robot_latest.values()),
            policy=policy,
            history=by_table["history"],
            consents=by_table["consent"],
            staff=staff,
            maintenance=by_table["maintenance"],
            fulfilments=fulfilments,
            capability_reviews=by_table["capability_review"],
            staff_unavailability=by_table["staff_unavailability"],
            analysis_denied=set(),
            withheld_families=set(),
            notes=[],
        )

    def source_issues(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM source_issue ORDER BY id"))


# 同一来源身份、不同内容视为“更正/延期”而不是冲突的表。
# maintenance 的更正只允许延长不可用窗口；旧记录保留，按不可用区间并集处理。
_REVISION_TABLES = {"maintenance"}
_IMPORT_TABLES = set(sources.TABLES) | {"capability_review", "staff_unavailability"}


def _validate_references(table: str, row: dict, ws: Workspace, at: datetime) -> None:
    """导入时检查业务引用，避免不可解释的来源事实进入分析。"""
    families = {
        fact["source_key"] for fact in ws.source_facts(at, "families")
    }
    robots = {
        (fact["source_key"], fact["source_version"])
        for fact in ws.source_facts(at, "robots")
    }
    if table in ("history", "robots", "consent", "requests"):
        family = row.get("family")
        if family is not None and family not in families:
            raise ValueError(f"家庭未登记: {family}")
    if table == "capability_review" and row.get("family") is not None:
        # 预留：复查记录若带 family 字段也须有效；当前契约不含该字段。
        if row["family"] not in families:
            raise ValueError(f"家庭未登记: {row['family']}")
    if table == "history":
        if (row["robot"], row["version"]) not in robots:
            raise ValueError(f"设备/版本未登记: {row['robot']}/{row['version']}")
    if table == "capability_review":
        if (row["robot"], row["version"]) not in robots:
            raise ValueError(f"设备/版本未登记: {row['robot']}/{row['version']}")
    if table == "maintenance":
        if row["robot"] not in {key for key, _ in robots}:
            raise ValueError(f"设备未登记: {row['robot']}")
    if table == "staff_unavailability":
        staff_ids = {fact["source_key"].split("|", 1)[0] for fact in ws.source_facts(at, "staff")}
        if row["staff"] not in staff_ids:
            raise ValueError(f"工作人员未登记: {row['staff']}")
_RECEIVED_TABLES = {"history", "consent", "maintenance", "capability_review", "staff_unavailability", "requests"}


def _validate_import_row(table: str, row: dict, ws: Workspace, at: datetime) -> dict:
    """导入行的最小结构校验；业务引用在评估时还会再校验。"""
    required = {
        "families": {"family", "region", "allowed_tasks", "start_hour", "end_hour"},
        "robots": {"robot", "family", "version", "tasks"},
        "history": {"id", "family", "robot", "version", "task", "object", "environment", "occurred", "outcome", "reviewed"},
        "consent": {"id", "family", "scope", "effective", "status"},
        "staff": {"staff", "regions", "tasks", "start", "end"},
        "requests": {"request", "family", "task", "earliest", "latest", "minutes", "object", "environment"},
        "task_policy": {"task", "min_observations", "min_reviewed", "max_unknown_fraction", "review_required"},
        "maintenance": {"id", "robot", "start", "end", "received"},
        "capability_review": {"id", "robot", "version", "task", "result", "effective", "received", "note"},
        "staff_unavailability": {"id", "staff", "start", "end", "received"},
    }[table]
    missing = required - set(row)
    if missing:
        raise ValueError(f"缺少字段: {', '.join(sorted(missing))}")
    extra = set(row) - required - {"source_version", "received"}
    if extra:
        raise ValueError(f"未知字段: {', '.join(sorted(extra))}")
    clean = {k: v for k, v in row.items() if k in required or k == "received"}
    for key in ("occurred", "effective", "start", "end", "received", "earliest", "latest"):
        if key in clean:
            value = parse_ts(clean[key])
            if value is None:
                raise ValueError(f"{key} 不能为空")
    if table == "history":
        if clean["outcome"] not in ("independent", "handoff", "refused", "unknown"):
            raise ValueError("outcome 非法")
        if not isinstance(clean["reviewed"], bool):
            raise ValueError("reviewed 必须是布尔值")
    if table == "task_policy":
        for key in ("min_observations", "min_reviewed"):
            if not isinstance(clean[key], int) or isinstance(clean[key], bool) or clean[key] < 0:
                raise ValueError(f"{key} 必须是非负整数")
        fraction = clean["max_unknown_fraction"]
        if not isinstance(fraction, (int, float)) or isinstance(fraction, bool) or not 0 <= fraction <= 1:
            raise ValueError("max_unknown_fraction 必须在 0..1")
        if not isinstance(clean["review_required"], bool):
            raise ValueError("review_required 必须是布尔值")
    if table == "consent":
        if clean["status"] not in ("active", "revoked"):
            raise ValueError("status 非法")
        if clean["scope"] != "analysis" and not str(clean["scope"]).startswith("service/"):
            raise ValueError("scope 非法")
        # 授权可以在未来生效；分析时点早于 effective 时不会生效，允许导入。
    if table == "capability_review":
        if clean["result"] not in ("pass", "fail"):
            raise ValueError("result 必须是 pass/fail")
        if clean["note"] is None:
            clean["note"] = ""
    if table == "staff_unavailability":
        if not str(clean["staff"]).strip():
            raise ValueError("staff 不能为空")
    if table == "requests":
        if not isinstance(clean["minutes"], int) or isinstance(clean["minutes"], bool) or clean["minutes"] <= 0:
            raise ValueError("minutes 必须是正整数")
        if parse_ts(clean["latest"]) <= parse_ts(clean["earliest"]):
            raise ValueError("latest 必须晚于 earliest")
    if table == "staff":
        if not isinstance(clean["regions"], list) or not clean["regions"]:
            raise ValueError("regions 必须是非空数组")
        if not isinstance(clean["tasks"], list) or not clean["tasks"]:
            raise ValueError("tasks 必须是非空数组")
        if parse_ts(clean["end"]) <= parse_ts(clean["start"]):
            raise ValueError("结束必须晚于开始")
    if table in ("maintenance", "staff_unavailability"):
        if parse_ts(clean["end"]) <= parse_ts(clean["start"]):
            raise ValueError("结束必须晚于开始")
    if table == "capability_review":
        if parse_ts(clean["effective"]) is None:
            raise ValueError("effective 不能为空")
    return clean
