"""SLA 分类重算领域服务：对历史 SLA 事件明细做批量归类并出可复核的报告。

核心约束：
- 重算按事件「原时间窗口」解析当时生效的分类版本，不按采集时间；
- 同一请求被多批采集覆盖时，单次重算只计一次，重复项留痕但不入指标；
- 每次重算固化分类版本快照、逐事件时间窗口和分类结果，报告可复现；
- 与已交付报告核对时，把「采集去重带来的量差」和「分类版本口径带来的
  结构差」拆开，避免把口径变化误读成异常增长。
"""
from __future__ import annotations

import csv
import io
from typing import Any

from app.services import recompute_engine as engine
from app.services.data_manager import (
    list_recomputes,
    list_sla_reports,
    next_recompute_id,
    read_classification_versions,
    read_recompute,
    read_sla_event_records,
    read_sla_report,
    write_classification_version,
    write_recompute,
    write_sla_report,
)
from app.store import store


def _version_rule_rows(version: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for rule in version.get("rules", []):
        rows.append({
            "类型编号": rule.get("type_code"),
            "类型名称": rule.get("type_name"),
            "错误码段": rule.get("code_range"),
            "分级策略": rule.get("severity"),
            "重试建议": rule.get("retry"),
            "熔断阈值": rule.get("breaker"),
            "通知方式": rule.get("notify"),
            "分类版本": version.get("version"),
            "status": "已生效",
        })
    return rows


def _all_rule_rows(versions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for version in versions:
        rows.extend(_version_rule_rows(version))
    return rows


def _event_fields(record: dict[str, Any]) -> dict[str, Any]:
    """进入分类器前只保留归类需要的字段，避免把采集批次信息带进比对。"""
    return {
        "event_id": record.get("event_id"),
        "request_id": record.get("request_id"),
        "api": record.get("api"),
        "caller": record.get("caller"),
        "response_code": record.get("response_code"),
        "occurred_at": record.get("occurred_at"),
        "window_start": record.get("window_start"),
        "window_end": record.get("window_end"),
    }


def _summary(stats: dict[str, Any]) -> dict[str, Any]:
    return {
        "total_requests": stats.get("total_requests", 0),
        "success_events": stats.get("success_events", 0),
        "error_events": stats.get("error_events", 0),
        "error_rate": stats.get("error_rate", 0.0),
        "error_rate_pct": stats.get("error_rate_pct", 0.0),
    }


def _type_counts(stats: dict[str, Any]) -> dict[str, int]:
    return {
        str(row.get("type_name")): int(row.get("count", 0))
        for row in stats.get("by_type", [])
    }


def _type_delta(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    before_counts = _type_counts(before)
    after_counts = _type_counts(after)
    rows = []
    for name in sorted(set(before_counts) | set(after_counts)):
        delta = after_counts.get(name, 0) - before_counts.get(name, 0)
        if delta:
            rows.append({
                "type_name": name,
                "before": before_counts.get(name, 0),
                "after": after_counts.get(name, 0),
                "delta": delta,
            })
    return rows


def _stats_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    return {
        "total_requests": after.get("total_requests", 0) - before.get("total_requests", 0),
        "error_events": after.get("error_events", 0) - before.get("error_events", 0),
        "error_rate_pct": round(
            after.get("error_rate_pct", 0.0) - before.get("error_rate_pct", 0.0), 2),
        "by_type": _type_delta(before, after),
    }


def _aggregate_source(
    source_records: list[dict[str, Any]],
    versions: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    *,
    forced_version: str | None,
) -> dict[str, Any]:
    details = engine.classify_records(
        [_event_fields(row) for row in source_records],
        versions, rules, forced_version=forced_version,
    )
    return engine.aggregate(details)


def _decompose_stats(
    records: list[dict[str, Any]],
    versions: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    *,
    baseline_version: str | None,
    dedupe: bool,
    actual_stats: dict[str, Any],
) -> dict[str, Any]:
    """做可加分解：原始采集量 → 去重后量 → 按版本窗口归类。

    volume_effect = 去重前后（同一基线口径）的差；
    classification_effect = 去重后（基线口径 → 实际口径）的差。
    """
    raw_uniform = _aggregate_source(
        records, versions, rules, forced_version=baseline_version)
    deduped_records = (
        engine.dedupe_records(records)[0] if dedupe else [dict(r) for r in records])
    deduped_uniform = _aggregate_source(
        deduped_records, versions, rules, forced_version=baseline_version)
    return {
        "baseline_version": baseline_version,
        "raw_stats": _summary(raw_uniform),
        "volume_stats": _summary(deduped_uniform),
        "actual_stats": _summary(actual_stats),
        "volume_effect": _stats_delta(raw_uniform, deduped_uniform),
        "classification_effect": _stats_delta(deduped_uniform, actual_stats),
        "notes": [
            "volume_effect 只包含同口径下重复采集去重带来的请求量和异常量变化。",
            "classification_effect 只包含同一批去重事件在新旧分类版本间的归类变化。",
            "两项相加等于实际重算值与原始采集口径的差。",
        ],
    }


class SlaRecomputeService:
    """分类重算服务；由专属 /api/sla-recompute 接口调用，不影响资源列表路由。"""

    # ---------- 分类版本 ----------

    def list_versions(self) -> list[dict[str, Any]]:
        versions = read_classification_versions()
        return [
            {
                "version": v.get("version"),
                "label": v.get("label"),
                "effective_from": v.get("effective_from"),
                "effective_to": v.get("effective_to"),
                "rules": [
                    {
                        "type_code": r.get("type_code"),
                        "type_name": r.get("type_name"),
                        "code_range": r.get("code_range"),
                        "severity": r.get("severity"),
                    }
                    for r in v.get("rules", [])
                ],
                "rule_count": len(v.get("rules", [])),
            }
            for v in versions
        ]

    def register_version(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        version = str(payload.get("version") or "").strip()
        effective_from = str(payload.get("effective_from") or "").strip()
        rules = payload.get("rules") or []
        if not version or not effective_from:
            return None, "分类版本号和生效时间不能为空"
        if engine.parse_dt(effective_from) is None:
            return None, "生效时间格式无法解析"
        if not isinstance(rules, list) or not rules:
            return None, "至少需要一条该版本的错误码规则"
        for rule in rules:
            if not str(rule.get("code_range") or "").strip():
                return None, "规则缺少错误码段"
            if not str(rule.get("type_name") or "").strip():
                return None, "规则缺少类型名称"
        record = {
            "version": version,
            "label": payload.get("label") or f"分类版本 {version}",
            "effective_from": effective_from,
            "effective_to": payload.get("effective_to") or None,
            "rules": rules,
        }
        write_classification_version(record)
        self._sync_store_rows()
        return record, "分类版本已登记，后续重算按事件原时间窗口自动选择版本"

    def _sync_store_rows(self) -> None:
        """把版本快照同步回异常归类模块，列表页能看到各版本规则。"""
        rows: list[dict[str, Any]] = []
        for version in read_classification_versions():
            for rule in version.get("rules", []):
                rows.append({
                    "id": len(rows) + 1,
                    "status": "已生效",
                    "pending": False,
                    "abnormal": False,
                    "类型编号": rule.get("type_code"),
                    "类型名称": rule.get("type_name"),
                    "错误码段": rule.get("code_range"),
                    "分级策略": rule.get("severity"),
                    "重试建议": rule.get("retry"),
                    "熔断阈值": rule.get("breaker"),
                    "通知方式": rule.get("notify"),
                    "分类版本": version.get("version"),
                })
        store.rows("error_classify")[:] = rows

    # ---------- 重算主流程 ----------

    def recompute(
        self,
        payload: dict[str, Any],
        *,
        parent_run: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        window_start = str(payload.get("window_start") or "").strip()
        window_end = str(payload.get("window_end") or "").strip()
        start_at = engine.parse_dt(window_start)
        end_at = engine.parse_dt(window_end)
        if start_at is None or end_at is None:
            return None, "查询窗口的起止时间不能为空且需可解析"
        if start_at >= end_at:
            return None, "查询窗口开始时间必须早于结束时间"

        dedupe = payload.get("dedupe", True)
        dedupe = True if dedupe is None else bool(dedupe)
        pinned_version = payload.get("classification_version") or None
        versions = read_classification_versions()

        if pinned_version:
            known = {str(v.get("version")) for v in versions}
            if str(pinned_version) not in known:
                return None, f"分类版本 {pinned_version} 未登记"

        records = [
            row for row in read_sla_event_records()
            if engine.in_window(
                row.get("occurred_at") or row.get("window_start"), window_start, window_end)
        ]
        if not records:
            return None, "查询窗口内没有历史 SLA 事件明细"

        rules = _all_rule_rows(versions)
        if dedupe:
            unique_records, dedupe_stats = engine.dedupe_records(records)
        else:
            unique_records = [dict(row) for row in records]
            dedupe_stats = {
                "raw_records": len(records),
                "unique_requests": len(unique_records),
                "duplicates_dropped": 0,
                "duplicate_groups": [],
            }

        details = engine.classify_records(
            unique_records, versions, rules, forced_version=pinned_version)
        stats = engine.aggregate(details)
        baseline_version = pinned_version or self._baseline_version(versions)
        decomposition = _decompose_stats(
            records, versions, rules,
            baseline_version=baseline_version,
            dedupe=dedupe,
            actual_stats=stats,
        )

        run_id = next_recompute_id()
        run: dict[str, Any] = {
            "run_id": run_id,
            "title": payload.get("title") or f"SLA分类重算 #{run_id}",
            "created_by": payload.get("created_by") or "值班运营",
            "created_at": self._now_iso(),
            "note": payload.get("note") or "",
            "window_start": window_start,
            "window_end": window_end,
            "dedupe": dedupe,
            "classification_policy": "pinned" if pinned_version else "window",
            "pinned_version": pinned_version,
            "baseline_version": baseline_version,
            "status": "已完成",
            "parent_run_id": parent_run.get("run_id") if parent_run else None,
            "versions_snapshot": [
                {
                    "version": v.get("version"),
                    "label": v.get("label"),
                    "effective_from": v.get("effective_from"),
                    "effective_to": v.get("effective_to"),
                    "rules": v.get("rules", []),
                }
                for v in versions
            ],
            "source_record_count": len(records),
            "raw_records": dedupe_stats["raw_records"],
            "unique_requests": dedupe_stats["unique_requests"],
            "duplicates_dropped": dedupe_stats["duplicates_dropped"],
            "duplicate_groups": dedupe_stats["duplicate_groups"],
            "stats": stats,
            "event_details": details,
            "decomposition": decomposition,
            "metric_detail": self._metric_detail(run_id, stats, details),
        }

        if parent_run:
            run["comparison"] = self._compare_runs(parent_run, run)
        write_recompute(run)
        return run, (
            f"分类重算完成：{stats['total_requests']} 个请求，"
            f"{stats['error_events']} 个错误事件，去重 {dedupe_stats['duplicates_dropped']} 条"
        )

    def _baseline_version(self, versions: list[dict[str, Any]]) -> str | None:
        earliest = engine.earliest_version(versions)
        return str(earliest.get("version")) if earliest else None

    def _metric_detail(
        self, run_id: int, stats: dict[str, Any], details: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """按异常类型拆出可直接被SLA监控消费的指标明细。"""
        detail_by_id = {item.get("event_id"): item for item in details}
        metrics = []
        for row in stats.get("by_type", []):
            links = []
            for event_id in row.get("event_ids", []):
                item = detail_by_id.get(event_id, {})
                links.append({
                    "event_id": event_id,
                    "request_id": item.get("request_id"),
                    "window_start": item.get("window_start"),
                    "window_end": item.get("window_end"),
                    "classification_version": item.get("classification_version"),
                    "api": item.get("api"),
                })
            metrics.append({
                "metric_id": f"RC-{run_id:04d}-{row.get('type_code')}",
                "run_id": run_id,
                "type_code": row.get("type_code"),
                "type_name": row.get("type_name"),
                "count": row.get("count"),
                "error_rate": row.get("error_rate"),
                "error_rate_pct": row.get("error_rate_pct"),
                "share_of_errors": row.get("share_of_errors"),
                "versions": row.get("versions"),
                "event_links": links,
            })
        return metrics

    # ---------- 查询与导出 ----------

    def list_runs(self) -> list[dict[str, Any]]:
        runs = []
        for run in list_recomputes():
            runs.append({
                "run_id": run.get("run_id"),
                "title": run.get("title"),
                "created_at": run.get("created_at"),
                "created_by": run.get("created_by"),
                "window_start": run.get("window_start"),
                "window_end": run.get("window_end"),
                "dedupe": run.get("dedupe"),
                "classification_policy": run.get("classification_policy"),
                "pinned_version": run.get("pinned_version"),
                "raw_records": run.get("raw_records"),
                "unique_requests": run.get("unique_requests"),
                "duplicates_dropped": run.get("duplicates_dropped"),
                "error_events": (run.get("stats") or {}).get("error_events"),
                "error_rate_pct": (run.get("stats") or {}).get("error_rate_pct"),
                "versions_used": [
                    v.get("version")
                    for v in (run.get("stats") or {}).get("versions_used", [])
                ],
                "parent_run_id": run.get("parent_run_id"),
                "delivered_report_id": run.get("delivered_report_id"),
            })
        runs.sort(key=lambda row: int(row.get("run_id") or 0), reverse=True)
        return runs

    def get_run(self, run_id: int, *, include_events: bool = True) -> dict[str, Any]:
        run = read_recompute(run_id)
        if run and not include_events:
            run = dict(run)
            run.pop("event_details", None)
        return run

    def rerun(
        self, run_id: int, payload: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        parent = read_recompute(run_id)
        if not parent:
            return None, f"重算批次 {run_id} 不存在"
        payload = dict(payload or {})
        request = {
            "window_start": payload.get("window_start") or parent.get("window_start"),
            "window_end": payload.get("window_end") or parent.get("window_end"),
            "dedupe": payload.get("dedupe", parent.get("dedupe", True)),
            "classification_version": payload.get(
                "classification_version", parent.get("pinned_version")),
            "created_by": payload.get("created_by") or parent.get("created_by"),
            "title": payload.get("title") or f"{parent.get('title')}（复算）",
            "note": payload.get("note") or "分类版本更新后复算",
        }
        return self.recompute(request, parent_run=parent)

    def _compare_runs(self, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
        """两次重算对比：请求量变化与归类口径变化分开呈现。

        before_actual / after_actual 各自保留原重算的逐事件归类结果（含各自
        实际采用的版本），因此复算时改了规则版本也不会被统一口径抹平。
        """
        before_versions = before.get("versions_snapshot") or []
        after_versions = after.get("versions_snapshot") or []
        before_rules = _all_rule_rows(before_versions)
        after_rules = _all_rule_rows(after_versions)
        before_records = before.get("event_details") or []
        after_records = after.get("event_details") or []

        before_actual = (
            engine.aggregate(before_records) if before_records
            else (before.get("stats") or {}))
        after_actual = (
            engine.aggregate(after_records) if after_records
            else (after.get("stats") or {}))

        # 同一批“本次”事件分别按前次规则集和本次规则集归类，隔离口径影响。
        after_uniform_old = _aggregate_source(
            after_records, before_versions, before_rules, forced_version=None)
        after_uniform_new = _aggregate_source(
            after_records, after_versions, after_rules, forced_version=None)
        before_uniform = _aggregate_source(
            before_records, before_versions, before_rules, forced_version=None)

        return {
            "before_run_id": before.get("run_id"),
            "after_run_id": after.get("run_id"),
            "before": _summary(before_actual),
            "after": _summary(after_actual),
            "total_delta": _stats_delta(before_actual, after_actual),
            "volume_effect": _stats_delta(before_uniform, after_uniform_old),
            "classification_effect": _stats_delta(after_uniform_old, after_uniform_new),
            "notes": [
                "volume_effect 用两批事件在同一套旧规则下的差异，反映请求量/去重变化。",
                "classification_effect 用同一批本次事件在前后两套规则下的差异，反映归类口径变化。",
                "两次重算的逐事件归类均按事件原时间窗口选版，不按采集时间或复算时间。",
            ],
        }

    def export_csv(self, run_id: int) -> tuple[str, str]:
        run = read_recompute(run_id)
        if not run:
            return "", ""
        output = io.StringIO()
        output.write("﻿")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow([
            "event_id", "request_id", "api", "caller", "response_code",
            "occurred_at", "window_start", "window_end", "classification_version",
            "type_code", "type_name", "severity", "collect_batch", "collected_at",
            "duplicate_event_ids", "source_batches",
        ])
        for item in run.get("event_details", []):
            writer.writerow([
                item.get("event_id"), item.get("request_id"), item.get("api"),
                item.get("caller"), item.get("response_code"), item.get("occurred_at"),
                item.get("window_start"), item.get("window_end"),
                item.get("classification_version"), item.get("type_code"),
                item.get("type_name"), item.get("severity"), item.get("collect_batch"),
                item.get("collected_at"), "|".join(item.get("duplicate_event_ids") or []),
                "|".join(item.get("source_batches") or []),
            ])
        return output.getvalue(), f"sla_recompute_{int(run_id):06d}.csv"

    # ---------- 交付报告核对 ----------

    def list_reports(self) -> list[dict[str, Any]]:
        reports = []
        for report in list_sla_reports():
            stats = report.get("stats") or {}
            reports.append({
                "report_id": report.get("report_id"),
                "title": report.get("title"),
                "window_start": report.get("window_start"),
                "window_end": report.get("window_end"),
                "delivered_at": report.get("delivered_at"),
                "delivered_by": report.get("delivered_by"),
                "classification_version": report.get("classification_version"),
                "dedupe_applied": report.get("dedupe_applied"),
                "total_requests": stats.get("total_requests"),
                "error_events": stats.get("error_events"),
                "error_rate_pct": stats.get("error_rate_pct"),
            })
        reports.sort(key=lambda row: str(row.get("delivered_at") or ""), reverse=True)
        return reports

    def deliver(
        self, run_id: int, payload: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        run = read_recompute(run_id)
        if not run:
            return None, f"重算批次 {run_id} 不存在"
        payload = dict(payload or {})
        report_id = str(payload.get("report_id") or f"RPT-RC-{int(run_id):04d}")
        if read_sla_report(report_id):
            return None, f"报告编号 {report_id} 已存在，交付报告不可覆盖"
        report = {
            "report_id": report_id,
            "title": payload.get("title") or run.get("title"),
            "window_start": run.get("window_start"),
            "window_end": run.get("window_end"),
            "delivered_at": self._now_iso(),
            "delivered_by": payload.get("delivered_by") or run.get("created_by"),
            "classification_version": (
                run.get("pinned_version")
                or ",".join(
                    str(v.get("version"))
                    for v in (run.get("stats") or {}).get("versions_used", [])
                )
            ),
            "classification_policy": run.get("classification_policy"),
            "dedupe_applied": run.get("dedupe"),
            "total_requests": (run.get("stats") or {}).get("total_requests"),
            "source_record_ids": [
                item.get("event_id") for item in run.get("event_details", [])
            ],
            "source_record_count": run.get("source_record_count"),
            "stats": run.get("stats"),
            "metric_detail": run.get("metric_detail"),
            # 冻结出报告时的版本快照与逐事件归类，后续登记新版本不影响核对
            "versions_snapshot": run.get("versions_snapshot") or [],
            "event_details": run.get("event_details") or [],
            "run_id": run_id,
            "note": payload.get("note") or "由分类重算结果交付，内容冻结，后续版本更新不回写。",
        }
        write_sla_report(report)
        run["delivered_report_id"] = report_id
        write_recompute(run)
        return report, f"报告 {report_id} 已交付并冻结"

    def reconcile(self, report_id: str, run_id: int | None = None) -> dict[str, Any]:
        """核对历史交付报告、重算数据与口径解释。"""
        report = read_sla_report(report_id)
        if not report:
            return {"ok": False, "message": f"报告 {report_id} 不存在"}
        run = read_recompute(run_id) if run_id else {}
        if run_id and not run:
            return {"ok": False, "message": f"重算批次 {run_id} 不存在"}
        if not run:
            linked = [
                r for r in list_recomputes()
                if r.get("delivered_report_id") == report_id
            ]
            run = linked[0] if linked else {}

        records = read_sla_event_records()
        reported_ids = {str(v) for v in (report.get("source_record_ids") or [])}
        selected = [
            row for row in records
            if (
                (reported_ids and str(row.get("event_id")) in reported_ids)
                or (
                    not reported_ids
                    and engine.in_window(
                        row.get("occurred_at"),
                        report.get("window_start"), report.get("window_end"))
                )
            )
        ]
        present_ids = {str(row.get("event_id")) for row in records}
        missing_ids = sorted(reported_ids - present_ids)

        # 优先用报告冻结的版本快照复现；旧报告没有快照时退回当前版本登记表
        current_versions = read_classification_versions()
        current_rules = _all_rule_rows(current_versions)
        report_versions = report.get("versions_snapshot") or current_versions
        report_rules = (
            _all_rule_rows(report_versions)
            if report.get("versions_snapshot") else current_rules
        )
        reported_version = report.get("classification_version")
        if reported_version and "," in str(reported_version):
            reported_version = None  # 多版本混合口径，不锁定单版

        reproduce_details = engine.classify_records(
            selected, report_versions, report_rules, forced_version=reported_version)
        reproduce_stats = engine.aggregate(reproduce_details)

        deduped_records, dedupe_stats = engine.dedupe_records(selected)
        dedupe_details = engine.classify_records(
            deduped_records, report_versions, report_rules,
            forced_version=reported_version)
        dedupe_result = engine.aggregate(dedupe_details)
        # 同一批去重事件按重算口径（事件原时间窗口 + 当前版本登记）归类
        actual_details = engine.classify_records(
            deduped_records, current_versions, current_rules)
        actual_result = engine.aggregate(actual_details)

        delivered_stats = report.get("stats") or {}
        reproduce_check = self._compare_stats(delivered_stats, reproduce_stats)
        run_stats = (run.get("stats") or {}) if run else None
        return {
            "ok": True,
            "report": {
                "report_id": report.get("report_id"),
                "title": report.get("title"),
                "delivered_at": report.get("delivered_at"),
                "classification_version": reported_version,
                "dedupe_applied": report.get("dedupe_applied"),
                "stats": delivered_stats,
            },
            "run": {
                "run_id": run.get("run_id"),
                "classification_policy": run.get("classification_policy"),
                "dedupe": run.get("dedupe"),
                "stats": run.get("stats"),
            } if run else None,
            "reproduce": {
                "matched": reproduce_check["matched"],
                "checks": reproduce_check["checks"],
                "missing_event_ids": missing_ids,
                "source_record_count": len(selected),
                "stats": _summary(reproduce_stats),
            },
            "dedupe": {
                "raw_records": dedupe_stats["raw_records"],
                "unique_requests": dedupe_stats["unique_requests"],
                "duplicates_dropped": dedupe_stats["duplicates_dropped"],
                "duplicate_groups": dedupe_stats["duplicate_groups"],
                "stats": _summary(dedupe_result),
            },
            "restated": {
                "matches_delivered": (
                    _type_counts(reproduce_stats) == _type_counts(actual_result)
                    and reproduce_stats.get("total_requests")
                    == actual_result.get("total_requests")
                ),
                "dedupe_applied": run.get("dedupe") if run else True,
                "stats": _summary(actual_result),
            },
            "effects": {
                "dedupe_effect": _stats_delta(reproduce_stats, dedupe_result),
                "classification_effect": _stats_delta(dedupe_result, actual_result),
                "total_delta": (
                    _stats_delta(delivered_stats, run_stats) if run_stats else None
                ),
            },
            "reasons": self._reasons(
                run, dedupe_stats, actual_details, reproduce_details,
                reported_version, delivered_stats),
        }

    def _compare_stats(self, expected: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
        checks = []
        for field in ("total_requests", "success_events", "error_events"):
            checks.append({
                "field": field,
                "delivered": expected.get(field),
                "reproduced": actual.get(field),
                "matched": expected.get(field) == actual.get(field),
            })
        expected_types = _type_counts(expected)
        actual_types = _type_counts(actual)
        for name in sorted(set(expected_types) | set(actual_types)):
            checks.append({
                "field": f"by_type.{name}",
                "delivered": expected_types.get(name, 0),
                "reproduced": actual_types.get(name, 0),
                "matched": expected_types.get(name, 0) == actual_types.get(name, 0),
            })
        return {"matched": all(c["matched"] for c in checks), "checks": checks}

    def _reasons(
        self,
        run: dict[str, Any],
        dedupe_stats: dict[str, Any],
        actual_details: list[dict[str, Any]],
        reproduce_details: list[dict[str, Any]],
        reported_version: str | None,
        delivered_stats: dict[str, Any],
    ) -> list[dict[str, Any]]:
        reasons: list[dict[str, Any]] = []

        if dedupe_stats.get("duplicate_groups"):
            delivered_total = delivered_stats.get("total_requests", 0)
            reasons.append({
                "code": "duplicate_collection",
                "title": "同一请求被多批采集覆盖",
                "detail": (
                    f"交付报告按 {delivered_total} 条原始采集记录统计；"
                    f"重算把 {dedupe_stats['duplicates_dropped']} 条重复采集记录合并为一次，"
                    f"去重后为 {dedupe_stats['unique_requests']} 个请求。"
                ),
                "evidence": dedupe_stats["duplicate_groups"],
            })

        old_names = {
            str(item.get("event_id")): item.get("type_name")
            for item in reproduce_details
        }
        reclassified = []
        for item in actual_details:
            if not item.get("is_error"):
                continue
            old_name = old_names.get(str(item.get("event_id")))
            if old_name and old_name != item.get("type_name"):
                reclassified.append({
                    "event_id": item.get("event_id"),
                    "request_id": item.get("request_id"),
                    "response_code": item.get("response_code"),
                    "window_start": item.get("window_start"),
                    "from_type": old_name,
                    "from_version": reported_version,
                    "to_type": item.get("type_name"),
                    "to_version": item.get("classification_version"),
                })
        if reclassified:
            reasons.append({
                "code": "code_reclassified",
                "title": "错误码在新旧规则下划到不同异常类型",
                "detail": (
                    f"{len(reclassified)} 条事件的错误码因分类版本变化改变归类；"
                    "异常总量可能不变，但各异常类型的构成会变化。"
                ),
                "evidence": reclassified,
            })

        version_moves = [
            {
                "event_id": item.get("event_id"),
                "request_id": item.get("request_id"),
                "response_code": item.get("response_code"),
                "window_start": item.get("window_start"),
                "classification_version": item.get("classification_version"),
                "type_name": item.get("type_name"),
            }
            for item in actual_details
            if item.get("is_error")
            and reported_version
            and item.get("classification_version") != reported_version
        ]
        if version_moves:
            reasons.append({
                "code": "version_window_resolution",
                "title": "重算按事件原时间窗口选择分类版本",
                "detail": (
                    f"{len(version_moves)} 条事件的原时间窗口落在交付报告口径之外的版本生效期，"
                    "重算采用事件发生当时生效的版本；交付报告按统一版本出数。"
                ),
                "evidence": version_moves,
            })
        return reasons

    def _now_iso(self) -> str:
        from datetime import datetime, timedelta, timezone
        return datetime.now(timezone(timedelta(hours=8))).isoformat()


sla_recompute_service = SlaRecomputeService()
