"""来源资料加载、字段校验与来源身份处理。

来源文件只读。初始资料中的 history 以发生时间、consent 以生效时间作为
“可知时点”；之后导入的记录若有 received 则以 received 为准，否则以导入
时刻为准，确保后来的资料不会被用于更早的分析。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .domain import OUTCOMES, parse_ts

BASELINE_KNOWN_AT = "1970-01-01T00:00:00Z"

TABLES = (
    "families",
    "robots",
    "history",
    "consent",
    "staff",
    "requests",
    "task_policy",
    "maintenance",
)

# 业务字段；source_version / received 是可选的来源元数据，不属于业务答案。
OPTIONAL_COLUMNS = {"source_version", "received"}


class SourceError(Exception):
    """资料结构或引用错误。错误信息面向操作人员。"""

    def __init__(self, messages: list[str]):
        self.messages = messages
        super().__init__("; ".join(messages))


@dataclass
class SourceBundle:
    tables: dict[str, list[dict]]
    hashes: dict[str, str] = field(default_factory=dict)
    issues: list[dict] = field(default_factory=list)
    origin: str = "initial"

    def rows(self, table: str) -> list[dict]:
        return self.tables.get(table, [])


def _hash_row(row: dict) -> str:
    return hashlib.sha256(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def load_source_bundle(source_dir: str | Path, *, check_manifest: bool = True) -> SourceBundle:
    source_dir = Path(source_dir)
    schema_path = source_dir / "schema.json"
    if not schema_path.exists():
        raise SourceError([f"缺少 schema.json: {source_dir}"])
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    tables: dict[str, list[dict]] = {}
    hashes: dict[str, str] = {}
    messages: list[str] = []

    manifest_path = source_dir / "manifest.json"
    if check_manifest:
        if not manifest_path.exists():
            raise SourceError([f"缺少 manifest.json: {source_dir}"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for filename, expected in manifest.get("files", {}).items():
            path = source_dir / filename
            if not path.exists():
                messages.append(f"清单文件缺失: {filename}")
                continue
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != expected:
                messages.append(f"清单哈希不匹配: {filename}")
        if messages:
            raise SourceError(messages)

    for table in TABLES:
        path = source_dir / f"{table}.json"
        if not path.exists():
            messages.append(f"缺少资料文件: {table}.json")
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            messages.append(f"{table}.json 不是合法 JSON: {exc}")
            continue
        if not isinstance(raw, list) or not raw:
            messages.append(f"{table}.json 必须是非空数组")
            continue
        definition = schema.get(table, {})
        columns = set(definition.get("columns", {}))
        if not columns:
            messages.append(f"schema.json 缺少表定义: {table}")
            continue
        cleaned: list[dict] = []
        for index, row in enumerate(raw):
            if not isinstance(row, dict):
                messages.append(f"{table}[{index}] 不是对象")
                continue
            extra = set(row) - columns - OPTIONAL_COLUMNS
            missing = columns - set(row)
            if missing:
                messages.append(f"{table}[{index}] 缺少字段: {', '.join(sorted(missing))}")
                continue
            if extra:
                messages.append(f"{table}[{index}] 含未知字段: {', '.join(sorted(extra))}")
                continue
            cleaned.append(row)
        tables[table] = cleaned
        hashes[f"{table}.json"] = hashlib.sha256(path.read_bytes()).hexdigest()

    if messages:
        raise SourceError(messages)

    bundle = SourceBundle(tables=tables, hashes=hashes)
    validate_bundle(bundle)
    return bundle


def validate_bundle(bundle: SourceBundle) -> None:
    """字段类型、主键、引用与时间关系校验。"""
    messages: list[str] = []

    def need_ts(row: dict, key: str, where: str) -> datetime | None:
        try:
            value = parse_ts(row.get(key))
        except (ValueError, TypeError) as exc:
            messages.append(f"{where}.{key} 时间非法: {exc}")
            return None
        if value is None:
            messages.append(f"{where}.{key} 不能为空")
        return value

    families = {r["family"]: r for r in bundle.rows("families")}
    if len(families) != len(bundle.rows("families")):
        messages.append("families.family 重复")
    for i, row in enumerate(bundle.rows("families")):
        where = f"families[{i}]"
        if not isinstance(row.get("allowed_tasks"), list) or not row["allowed_tasks"]:
            messages.append(f"{where}.allowed_tasks 必须是非空数组")
        for key in ("start_hour", "end_hour"):
            value = row.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 24:
                messages.append(f"{where}.{key} 必须是 0..24 的整数")
        if row.get("start_hour") == row.get("end_hour"):
            messages.append(f"{where} 可接受小时区间为空")

    robots_seen: set[tuple] = set()
    for i, row in enumerate(bundle.rows("robots")):
        where = f"robots[{i}]"
        key = (row.get("robot"), row.get("version"))
        if key in robots_seen:
            messages.append(f"{where} 设备/版本重复: {key}")
        robots_seen.add(key)
        if row.get("family") not in families:
            messages.append(f"{where}.family 未登记: {row.get('family')}")
        if not isinstance(row.get("tasks"), list) or not row["tasks"]:
            messages.append(f"{where}.tasks 必须是非空数组")

    policy_tasks = {r["task"] for r in bundle.rows("task_policy")}
    for i, row in enumerate(bundle.rows("task_policy")):
        where = f"task_policy[{i}]"
        for key in ("min_observations", "min_reviewed"):
            value = row.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                messages.append(f"{where}.{key} 必须是非负整数")
        frac = row.get("max_unknown_fraction")
        if not isinstance(frac, (int, float)) or isinstance(frac, bool) or not 0 <= frac <= 1:
            messages.append(f"{where}.max_unknown_fraction 必须在 0..1")
        if not isinstance(row.get("review_required"), bool):
            messages.append(f"{where}.review_required 必须是布尔值")

    history_ids: set[tuple] = set()
    for i, row in enumerate(bundle.rows("history")):
        where = f"history[{i}]"
        key = (row.get("id"), row.get("version"))
        if key in history_ids:
            messages.append(f"{where} 来源身份重复: {key}")
        history_ids.add(key)
        if row.get("family") not in families:
            messages.append(f"{where}.family 未登记: {row.get('family')}")
        if row.get("outcome") not in OUTCOMES:
            messages.append(f"{where}.outcome 非法: {row.get('outcome')}")
        if not isinstance(row.get("reviewed"), bool):
            messages.append(f"{where}.reviewed 必须是布尔值")
        occurred = need_ts(row, "occurred", where)
        received = parse_ts(row.get("received")) if row.get("received") is not None else None
        if occurred and received and received < occurred:
            messages.append(f"{where}.received 早于 occurred")
        robot_key = (row.get("robot"), row.get("version"))
        if robot_key not in robots_seen:
            messages.append(f"{where} 设备/版本未登记: {robot_key}")
        if row.get("task") not in policy_tasks:
            messages.append(f"{where}.task 无任务政策: {row.get('task')}")

    consent_ids: set[str] = set()
    for i, row in enumerate(bundle.rows("consent")):
        where = f"consent[{i}]"
        if row.get("id") in consent_ids:
            messages.append(f"{where} 来源身份重复: {row.get('id')}")
        consent_ids.add(row.get("id"))
        if row.get("family") not in families:
            messages.append(f"{where}.family 未登记: {row.get('family')}")
        scope = str(row.get("scope", ""))
        if scope != "analysis" and not scope.startswith("service/"):
            messages.append(f"{where}.scope 非法: {scope}")
        if row.get("status") not in ("active", "revoked"):
            messages.append(f"{where}.status 非法: {row.get('status')}")
        # 授权可以在未来生效，因此不在结构校验中强制 received >= effective；
        # 分析时点早于 effective 的授权自然不生效。
        need_ts(row, "effective", where)

    staff_ids: set[tuple] = set()
    for i, row in enumerate(bundle.rows("staff")):
        where = f"staff[{i}]"
        key = (row.get("staff"), row.get("start"), row.get("end"))
        if key in staff_ids:
            messages.append(f"{where} 班次重复: {key}")
        staff_ids.add(key)
        if not isinstance(row.get("regions"), list) or not row["regions"]:
            messages.append(f"{where}.regions 必须是非空数组")
        if not isinstance(row.get("tasks"), list) or not row["tasks"]:
            messages.append(f"{where}.tasks 必须是非空数组")
        start = need_ts(row, "start", where)
        end = need_ts(row, "end", where)
        if start and end and end <= start:
            messages.append(f"{where} 结束必须晚于开始")

    request_ids: set[str] = set()
    for i, row in enumerate(bundle.rows("requests")):
        where = f"requests[{i}]"
        if row.get("request") in request_ids:
            messages.append(f"{where} 请求重复: {row.get('request')}")
        request_ids.add(row.get("request"))
        if row.get("family") not in families:
            messages.append(f"{where}.family 未登记: {row.get('family')}")
        if row.get("task") not in policy_tasks:
            messages.append(f"{where}.task 无任务政策: {row.get('task')}")
        minutes = row.get("minutes")
        if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes <= 0:
            messages.append(f"{where}.minutes 必须是正整数")
        earliest = need_ts(row, "earliest", where)
        latest = need_ts(row, "latest", where)
        if earliest and latest and latest <= earliest:
            messages.append(f"{where} latest 必须晚于 earliest")

    maintenance_ids: set[tuple] = set()
    for i, row in enumerate(bundle.rows("maintenance")):
        where = f"maintenance[{i}]"
        key = (row.get("id"), row.get("start"), row.get("end"), row.get("received"))
        if key in maintenance_ids:
            messages.append(f"{where} 维护记录重复: {key}")
        maintenance_ids.add(key)
        if row.get("robot") not in {r.get("robot") for r in bundle.rows("robots")}:
            messages.append(f"{where}.robot 未登记: {row.get('robot')}")
        start = need_ts(row, "start", where)
        end = need_ts(row, "end", where)
        if start and end and end <= start:
            messages.append(f"{where} 结束必须晚于开始")
        need_ts(row, "received", where)

    if messages:
        raise SourceError(messages)


def load_records_file(path: str | Path) -> tuple[str | None, list[dict]]:
    """读取要导入的新记录。

    支持两种形式：
      - {"table": "history", "rows": [...]}
      - {"history": [...], "maintenance": [...]}
    """
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "rows" in data:
        table = data.get("table")
        rows = data["rows"]
        if not isinstance(rows, list):
            raise SourceError(["rows 必须是数组"])
        return table, rows
    if isinstance(data, dict):
        found = [(key, value) for key, value in data.items() if key in TABLES]
        if len(found) == 1 and isinstance(found[0][1], list):
            return found[0]
        if found:
            return None, []  # 由调用方逐表处理
    raise SourceError(["导入文件必须包含 table/rows，或单个资料表的数组字段"])


def known_at_for(table: str, row: dict, *, origin: str, import_time: datetime) -> datetime:
    """事实进入系统（可被分析使用）的时点。"""
    received = parse_ts(row.get("received"))
    if received is not None:
        return received
    if origin == "initial":
        if table == "history":
            return parse_ts(row["occurred"])  # initial 资料以发生时间视为已知
        if table == "consent":
            return parse_ts(row["effective"])
        if table == "maintenance":
            return parse_ts(row["received"])
        return parse_ts(BASELINE_KNOWN_AT)  # 基线资料视为决策前已知
    return import_time


def raw_hash(row: dict) -> str:
    return _hash_row(row)
