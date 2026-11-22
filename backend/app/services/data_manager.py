"""跨模块共享数据管理器：读写 data/ 目录下的业务数据产物。

职责：
- 每个服务产出 domain_snapshot 后序列化到 data/{module}/snapshot.json
- 下游服务从 data/ 目录读取上游的标准化数据产物
- 审计记录写入 data/audit/
- 用量记录写入 data/usage/
- 导出任务结果写入 data/exports/
- API 注册快照写入 data/api/
- 订阅审批记录写入 data/subscriptions/
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# 数据目录相对于仓库根
DATA_ROOT = Path(os.environ.get("DATA_ROOT", str(
    Path(__file__).resolve().parents[3] / "data"
)))

# 东八区
TZ = timezone(timedelta(hours=8))


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data: Any) -> None:
    _ensure_dir(path.parent)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(TZ).isoformat()


# ========== API 注册快照 ==========


def write_api_snapshot(api_id: int, api_data: dict[str, Any]) -> None:
    """将单个 API 注册数据写入共享快照，供路由、缓存、限流等模块消费。"""
    snapshot = _read_json(DATA_ROOT / "api" / "registry.json", {})
    key = str(api_id)
    api_data["_synced_at"] = _now_iso()
    snapshot[key] = api_data
    _write_json(DATA_ROOT / "api" / "registry.json", snapshot)


def read_api_snapshot(api_id: int | None = None) -> dict[str, Any] | dict[str, dict[str, Any]]:
    """读取 API 注册快照。返回单个或全部。"""
    snapshot = _read_json(DATA_ROOT / "api" / "registry.json", {})
    if api_id is not None:
        return snapshot.get(str(api_id), {})
    return snapshot


def remove_api_snapshot(api_id: int) -> None:
    """标记 API 为下线。"""
    snapshot = _read_json(DATA_ROOT / "api" / "registry.json", {})
    key = str(api_id)
    if key in snapshot:
        snapshot[key]["status"] = "已下线"
        snapshot[key]["_deleted_at"] = _now_iso()
        _write_json(DATA_ROOT / "api" / "registry.json", snapshot)


# ========== 订阅审批 ==========


def write_subscription(sub_id: int, sub_data: dict[str, Any]) -> None:
    """写入订阅申请记录到共享目录。"""
    record = {
        "subscription_id": sub_id,
        **sub_data,
        "_created_at": sub_data.get("_created_at", _now_iso()),
        "_updated_at": _now_iso(),
    }
    _write_json(DATA_ROOT / "subscriptions" / f"{sub_id:06d}.json", record)


def read_subscription(sub_id: int) -> dict[str, Any]:
    return _read_json(DATA_ROOT / "subscriptions" / f"{sub_id:06d}.json", {})


def list_subscriptions() -> list[dict[str, Any]]:
    """列出所有订阅记录。"""
    _ensure_dir(DATA_ROOT / "subscriptions")
    records = []
    for f in sorted(DATA_ROOT.glob("subscriptions/*.json")):
        record = _read_json(f)
        if record:
            records.append(record)
    return records


def approve_subscription(sub_id: int, approved: bool, approver: str = "system") -> dict[str, Any]:
    """审批订阅，记录审批流程。"""
    record = read_subscription(sub_id)
    if not record:
        return {}
    record["approval_status"] = "已通过" if approved else "已驳回"
    record["approver"] = approver
    record["_approved_at"] = _now_iso()
    write_subscription(sub_id, record)
    return record


# ========== 用量计费 ==========


def write_usage(usage_id: int, usage_data: dict[str, Any]) -> None:
    """写入用量记录到共享目录。"""
    usage_data["_created_at"] = usage_data.get("_created_at", _now_iso())
    usage_data["_updated_at"] = _now_iso()
    _write_json(DATA_ROOT / "usage" / f"{usage_id:06d}.json", usage_data)


def read_usage(usage_id: int) -> dict[str, Any]:
    return _read_json(DATA_ROOT / "usage" / f"{usage_id:06d}.json", {})


def list_usage(billing_cycle: str | None = None) -> list[dict[str, Any]]:
    """列出用量记录，支持按计费周期过滤。"""
    _ensure_dir(DATA_ROOT / "usage")
    records = []
    for f in sorted(DATA_ROOT.glob("usage/*.json")):
        record = _read_json(f)
        if record:
            if billing_cycle is None or record.get("billing_cycle") == billing_cycle:
                records.append(record)
    return records


def settle_billing(billing_cycle: str, use_auto_settlement: bool = True) -> dict[str, Any]:
    """结算指定计费周期，生成结算汇总。"""
    records = list_usage(billing_cycle)
    total_amount = sum(float(r.get("amount", 0)) for r in records)
    total_calls = sum(int(r.get("call_count", 0)) for r in records)
    settlement = {
        "billing_cycle": billing_cycle,
        "total_amount": total_amount,
        "total_calls": total_calls,
        "record_count": len(records),
        "settled_at": _now_iso(),
        "auto_settlement": use_auto_settlement,
    }
    _write_json(DATA_ROOT / "usage" / f"_settlement_{billing_cycle}.json", settlement)

    for r in records:
        r["settlement_status"] = "已结算"
        r["_settled_at"] = _now_iso()
        write_usage(r.get("subscription_id", 0), r)
    return settlement


# ========== 调用审计 ==========


def write_audit(audit_id: int, audit_data: dict[str, Any]) -> None:
    """写入审计记录。"""
    audit_data["_logged_at"] = _now_iso()
    _write_json(DATA_ROOT / "audit" / f"{audit_id:06d}.json", audit_data)


def read_audit(audit_id: int) -> dict[str, Any]:
    return _read_json(DATA_ROOT / "audit" / f"{audit_id:06d}.json", {})


def list_audit(api_name: str | None = None, caller: str | None = None,
               start_time: str | None = None, end_time: str | None = None) -> list[dict[str, Any]]:
    """按条件查询审计记录。"""
    _ensure_dir(DATA_ROOT / "audit")
    records = []
    for f in sorted(DATA_ROOT.glob("audit/*.json")):
        record = _read_json(f)
        if not record:
            continue
        if api_name and record.get("api_name") != api_name:
            continue
        if caller and record.get("caller") != caller:
            continue
        if start_time and record.get("_logged_at", "") < start_time:
            continue
        if end_time and record.get("_logged_at", "") > end_time:
            continue
        records.append(record)
    return records


def archive_audit_records(before_date: str) -> dict[str, Any]:
    """归档指定日期之前的审计记录。"""
    records = list_audit()
    archived = [r for r in records if r.get("_logged_at", "")[:10] < before_date]
    archive_data = {
        "archive_date": _now_iso(),
        "before_date": before_date,
        "archived_count": len(archived),
        "records": archived,
    }
    _write_json(DATA_ROOT / "audit" / f"_archive_{before_date}.json", archive_data)
    # 标记已归档
    for r in archived:
        rid = r.get("audit_id", 0)
        r["status"] = "已归档"
        write_audit(rid, r)
    return archive_data


# ========== 导出任务 ==========


def write_export(export_id: int, export_data: dict[str, Any]) -> None:
    """写入导出任务结果。"""
    export_data["_created_at"] = export_data.get("_created_at", _now_iso())
    export_data["_updated_at"] = _now_iso()
    _write_json(DATA_ROOT / "exports" / f"{export_id:06d}.json", export_data)
    # 同时生成一份导出 CSV
    _write_export_csv(export_id, export_data)


def _write_export_csv(export_id: int, export_data: dict[str, Any]) -> None:
    """根据导出数据生成 CSV 文件头。"""
    columns = export_data.get("columns", [])
    rows = export_data.get("rows", [])
    csv_path = DATA_ROOT / "exports" / f"{export_id:06d}.csv"
    lines = [",".join(columns)]
    for row in rows:
        lines.append(",".join(str(v) for v in row))
    csv_path.write_text("\n".join(lines), encoding="utf-8")


def read_export(export_id: int) -> dict[str, Any]:
    return _read_json(DATA_ROOT / "exports" / f"{export_id:06d}.json", {})


def list_exports() -> list[dict[str, Any]]:
    _ensure_dir(DATA_ROOT / "exports")
    records = []
    for f in sorted(DATA_ROOT.glob("exports/[0-9]*.json")):
        record = _read_json(f)
        if record:
            records.append(record)
    return records


# ========== 模块数据快照 ==========


def write_module_snapshot(module: str, snapshot: dict[str, Any]) -> None:
    """各服务将自己的 domain_snapshot 写入共享目录，供跨模块消费。"""
    snapshot["_snapshot_at"] = _now_iso()
    _write_json(DATA_ROOT / module / "snapshot.json", snapshot)


def read_module_snapshot(module: str) -> dict[str, Any]:
    return _read_json(DATA_ROOT / module / "snapshot.json", {})


def list_module_snapshots() -> list[dict[str, Any]]:
    """读取所有模块的快照汇总。"""
    snapshots = []
    for module_dir in DATA_ROOT.iterdir():
        if module_dir.is_dir() and not module_dir.name.startswith("_"):
            snap = _read_json(module_dir / "snapshot.json")
            if snap:
                snapshots.append(snap)
    return snapshots


# ========== SLA 历史事件 ==========


def read_sla_event_records() -> list[dict[str, Any]]:
    """读取用于分类重算的历史 SLA 事件明细。

    优先使用 data/sla_events/records.json（采集侧落盘的全量明细）；
    没有落盘文件时退回内置示例数据，保证克隆下来即可复算。
    """
    from app.domain_seed import SLA_EVENT_RECORDS

    stored = _read_json(DATA_ROOT / "sla_events" / "records.json", None)
    if isinstance(stored, list) and stored:
        return [dict(row) for row in stored]
    return [dict(row) for row in SLA_EVENT_RECORDS]


def write_sla_event_records(records: list[dict[str, Any]]) -> None:
    """采集侧写入历史事件明细，供后续重算复用。"""
    _write_json(DATA_ROOT / "sla_events" / "records.json", [dict(r) for r in records])


# ========== 分类版本时间线 ==========


def read_classification_versions() -> list[dict[str, Any]]:
    """读取分类版本快照；落盘版本覆盖内置版本，按生效时间排序。"""
    from app.domain_seed import CLASSIFICATION_VERSIONS

    versions = {v["version"]: dict(v) for v in CLASSIFICATION_VERSIONS}
    stored = _read_json(DATA_ROOT / "error_classify" / "versions.json", [])
    if isinstance(stored, list):
        for item in stored:
            if isinstance(item, dict) and item.get("version"):
                versions[str(item["version"])] = item
    ordered = sorted(versions.values(), key=lambda v: str(v.get("effective_from", "")))
    return ordered


def write_classification_version(version: dict[str, Any]) -> dict[str, Any]:
    """登记或更新一个分类版本（含该版本的错误码规则快照）。"""
    stored = _read_json(DATA_ROOT / "error_classify" / "versions.json", [])
    if not isinstance(stored, list):
        stored = []
    stored = [
        item for item in stored
        if not (isinstance(item, dict) and item.get("version") == version.get("version"))
    ]
    stored.append(version)
    stored.sort(key=lambda v: str(v.get("effective_from", "")))
    _write_json(DATA_ROOT / "error_classify" / "versions.json", stored)
    return version


# ========== 分类重算运行记录 ==========


def _recompute_path(run_id: int) -> Path:
    return DATA_ROOT / "recompute" / f"{run_id:06d}.json"


def write_recompute(run: dict[str, Any]) -> None:
    run["_updated_at"] = _now_iso()
    _write_json(_recompute_path(int(run["run_id"])), run)


def read_recompute(run_id: int) -> dict[str, Any]:
    return _read_json(_recompute_path(int(run_id)), {})


def list_recomputes() -> list[dict[str, Any]]:
    _ensure_dir(DATA_ROOT / "recompute")
    runs = []
    for f in sorted(DATA_ROOT.glob("recompute/[0-9]*.json")):
        record = _read_json(f)
        if record:
            runs.append(record)
    return runs


def next_recompute_id() -> int:
    _ensure_dir(DATA_ROOT / "recompute")
    return max((int(f.stem) for f in DATA_ROOT.glob("recompute/[0-9]*.json")), default=0) + 1


# ========== 已交付的 SLA 报告 ==========


def _report_path(report_id: str) -> Path:
    safe = "".join(ch for ch in str(report_id) if ch.isalnum() or ch in "-_")
    return DATA_ROOT / "reports" / f"{safe}.json"


def _ensure_report_seeds() -> None:
    """首次读取时把内置历史报告落盘，之后保持不变供事后核对。"""
    from app.domain_seed import DELIVERED_REPORTS

    for report in DELIVERED_REPORTS:
        path = _report_path(report["report_id"])
        if not path.exists():
            _write_json(path, dict(report))


def list_sla_reports() -> list[dict[str, Any]]:
    _ensure_report_seeds()
    _ensure_dir(DATA_ROOT / "reports")
    reports = []
    for f in sorted(DATA_ROOT.glob("reports/*.json")):
        record = _read_json(f)
        if record:
            reports.append(record)
    return reports


def read_sla_report(report_id: str) -> dict[str, Any]:
    _ensure_report_seeds()
    return _read_json(_report_path(report_id), {})


def write_sla_report(report: dict[str, Any]) -> None:
    _write_json(_report_path(str(report.get("report_id", "unknown"))), report)


# ========== SLA监控对分类重算的消费记录 ==========


def write_recompute_consumption(run_id: int, consumption: dict[str, Any]) -> None:
    consumption["_consumed_at"] = _now_iso()
    _write_json(DATA_ROOT / "sla_monitor" / f"recompute_{int(run_id):06d}.json", consumption)


def read_recompute_consumption(run_id: int) -> dict[str, Any]:
    return _read_json(DATA_ROOT / "sla_monitor" / f"recompute_{int(run_id):06d}.json", {})