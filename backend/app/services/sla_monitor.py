"""SLA监控领域服务：管理API服务质量指标、延迟监控和告警策略。

跨模块数据流：
- 消费 api_registry 快照 → 关联API加载SLA指标
- 监控异常 → 触发 error_classify 异常归类
- 监控异常 → 触发 query_route 降级路由
- 消费 sla_recompute 的指标明细 → 按分类重算结果重新统计窗口
- 达标/偏离状态 → 写共享快照通知消费方
"""
from __future__ import annotations

from typing import Any

from app.services import recompute_engine as engine
from app.services.base import BaseModuleService
from app.store import store


class SlaMonitorService(BaseModuleService):
    MODULE = "sla_monitor"
    ENTITY = "SLA指标"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["指标编号", "关联API", "可用率目标", "P99延迟", "错误率上限", "监控周期", "当前状态", "最近告警"]
    REQUIRED_FIELDS = ["指标编号", "关联API", "可用率目标"]
    STATUS_ORDER = ["草稿", "监控中", "已达标", "已偏离"]
    ACTION_RULES = {"启动监控": "监控中", "标记达标": "已达标", "标记偏离": "已偏离"}
    NEGATIVE_ACTIONS = []

    def record_metric(self, api_name: str, latency_ms: float, error: bool) -> dict[str, Any]:
        """记录一次调用指标并检查SLA。"""
        rows = store.rows(self.MODULE)
        for row in rows:
            if row.get("关联API") == api_name and row.get("status") in ("监控中", "已偏离"):
                p99 = float(row.get("P99延迟", "0") or "0")
                error_rate = float(row.get("错误率上限", "0") or "0")

                if latency_ms > p99:
                    row["最近告警"] = f"P99延迟超标：{latency_ms}ms > {p99}ms"
                    row["abnormal"] = True
                    self._classify_error(api_name, "latency_exceeded", f"延迟{latency_ms}ms")

                if error and error_rate > 0:
                    row["最近告警"] = f"出现调用错误"
                    row["abnormal"] = True
                    self._classify_error(api_name, "call_error", "调用返回错误")

                return row
        return {}

    def _classify_error(self, api_name: str, error_type: str, detail: str) -> None:
        """将SLA异常传递给error_classify进行归类。

        联动增强：偏离事件自动写入 error_classify 的异常入库记录，
        同时写入调用审计记录。
        """
        from app.services.data_manager import write_module_snapshot, write_audit, DATA_ROOT
        # 记录SLA事件快照给error_classify消费
        snap = {
            "module": self.MODULE,
            "api_name": api_name,
            "error_type": error_type,
            "detail": detail,
            "timestamp": self._now_iso(),
        }
        write_module_snapshot("sla_events", snap)

        # 联动增强：自动写入error_classify异常归类记录
        audit_id = max((int(f.stem) for f in DATA_ROOT.glob("audit/[0-9]*.json")), default=0) + 1
        write_audit(audit_id, {
            "audit_id": audit_id,
            "api_name": api_name,
            "caller": "sla_monitor",
            "event": error_type,
            "detail": detail,
            "classification": "已记录",
            "severity": "high" if error_type == "latency_exceeded" else "medium",
        })

    def _now_iso(self) -> str:
        from datetime import datetime, timedelta, timezone
        return datetime.now(timezone(timedelta(hours=8))).isoformat()

    def degrade_if_needed(self, api_name: str) -> dict[str, Any]:
        """检查是否需要降级路由。"""
        rows = store.rows(self.MODULE)
        for row in rows:
            if row.get("关联API") == api_name and row.get("status") == "已偏离":
                from app.services import service_for
                route_svc = service_for("query_route")
                if route_svc and hasattr(route_svc, "degrade_route"):
                    return route_svc.degrade_route(row.get("id", 0), "SLA偏离触发降级")
        return {}

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        if action == "启动监控":
            from app.services.data_manager import read_api_snapshot
            api_name = entry.get("关联API", "")
            registry = read_api_snapshot()
            for api_data in (registry.values() if isinstance(registry, dict) else []):
                if isinstance(api_data, dict) and api_data.get("api_name") == api_name:
                    break
            else:
                return None, "关联API尚未注册，无法启动SLA监控"

        result = super().run_action(entry_id, action)

        if action == "标记偏离":
            self.degrade_if_needed(entry.get("关联API", ""))
            self._classify_error(entry.get("关联API", ""), "sla_deviation", "SLA指标偏离")

        from app.services.data_manager import write_module_snapshot
        write_module_snapshot(self.MODULE, self.domain_snapshot())
        return result

    # ---------- 消费分类重算的 SLA 指标明细 ----------

    def consume_recompute(self, run_id: int) -> tuple[dict[str, Any] | None, str]:
        """消费一组分类重算的指标明细，重新统计窗口并可逐条关联异常事件。

        - metric_detail 每个异常类型都带 event_links，可直接下钻到事件；
        - 按 mini 窗口（监控周期分钟）重新聚合请求量/错误量，得到时间序列；
        - 消费结果落盘，后续可复核是哪个重算批次、哪版分类口径。
        """
        from app.services.data_manager import read_recompute, write_recompute_consumption

        run = read_recompute(run_id)
        if not run:
            return None, f"重算批次 {run_id} 不存在"
        stats = run.get("stats") or {}
        details = run.get("event_details") or []
        metric_detail = run.get("metric_detail") or []

        window_start = run.get("window_start")
        window_end = run.get("window_end")
        start_at = engine.parse_dt(window_start)
        end_at = engine.parse_dt(window_end)
        period_minutes = self._monitor_period_minutes(run)
        buckets = self._window_series(details, start_at, end_at, period_minutes)

        linked_ids: list[str] = []
        unlinked: list[dict[str, Any]] = []
        for metric in metric_detail:
            for link in metric.get("event_links", []):
                event_id = str(link.get("event_id") or "")
                if event_id:
                    linked_ids.append(event_id)
        detail_ids = {str(item.get("event_id")) for item in details if item.get("is_error")}
        for event_id in sorted(detail_ids - set(linked_ids)):
            unlinked.append({"event_id": event_id, "reason": "指标明细未覆盖该异常事件"})

        consumption = {
            "run_id": run_id,
            "title": run.get("title"),
            "window_start": window_start,
            "window_end": window_end,
            "period_minutes": period_minutes,
            "classification_policy": run.get("classification_policy"),
            "pinned_version": run.get("pinned_version"),
            "versions_used": [v.get("version") for v in stats.get("versions_used", [])],
            "dedupe": run.get("dedupe"),
            "duplicates_dropped": run.get("duplicates_dropped"),
            "restated": {
                "total_requests": stats.get("total_requests", 0),
                "success_events": stats.get("success_events", 0),
                "error_events": stats.get("error_events", 0),
                "error_rate": stats.get("error_rate", 0.0),
                "error_rate_pct": stats.get("error_rate_pct", 0.0),
                "by_type": [
                    {
                        "type_code": row.get("type_code"),
                        "type_name": row.get("type_name"),
                        "count": row.get("count"),
                        "error_rate_pct": row.get("error_rate_pct"),
                        "metric_id": row.get("metric_id"),
                        "event_ids": [link.get("event_id") for link in row.get("event_links", [])],
                    }
                    for row in metric_detail
                ],
                "window_series": buckets,
            },
            "metric_count": len(metric_detail),
            "linked_event_count": len(linked_ids),
            "unlinked_events": unlinked,
            "event_index": {
                str(item.get("event_id")): {
                    "request_id": item.get("request_id"),
                    "api": item.get("api"),
                    "type_name": item.get("type_name"),
                    "window_start": item.get("window_start"),
                    "window_end": item.get("window_end"),
                    "classification_version": item.get("classification_version"),
                }
                for item in details
            },
        }
        write_recompute_consumption(run_id, consumption)
        self._apply_restated_alert(consumption)
        return consumption, (
            f"已按重算批次 {run_id} 重新统计窗口："
            f"{consumption['restated']['total_requests']} 个请求，"
            f"{consumption['restated']['error_events']} 个异常，"
            f"可关联 {len(linked_ids)} 条事件"
        )

    def read_consumption(self, run_id: int) -> dict[str, Any]:
        from app.services.data_manager import read_recompute_consumption
        return read_recompute_consumption(run_id)

    def _monitor_period_minutes(self, run: dict[str, Any]) -> int:
        """优先用SLA指标配置的监控周期；没有匹配指标时按窗口跨度取默认值。"""
        api_names = {str(item.get("api")) for item in run.get("event_details", [])}
        for row in store.rows(self.MODULE):
            if str(row.get("关联API")) in api_names:
                try:
                    return max(1, int(float(row.get("监控周期", "0") or "0")))
                except (TypeError, ValueError):
                    continue
        start_at = engine.parse_dt(run.get("window_start"))
        end_at = engine.parse_dt(run.get("window_end"))
        if start_at and end_at:
            days = max(1, (end_at - start_at).days)
            return max(60, int(days * 24 * 60 / 24))
        return 60

    def _window_series(
        self,
        details: list[dict[str, Any]],
        start_at: Any,
        end_at: Any,
        period_minutes: int,
    ) -> list[dict[str, Any]]:
        """按监控周期把事件重新分桶，语义为 [bucket_start, bucket_end)。"""
        from datetime import timedelta

        if start_at is None or end_at is None or end_at <= start_at:
            return []
        step = timedelta(minutes=max(1, period_minutes))
        buckets: list[dict[str, Any]] = []
        cursor = start_at
        while cursor < end_at and len(buckets) < 1000:
            bucket_end = min(cursor + step, end_at)
            buckets.append({
                "bucket_start": cursor.isoformat(),
                "bucket_end": bucket_end.isoformat(),
                "total_requests": 0,
                "error_events": 0,
                "by_type": {},
            })
            cursor = bucket_end
        for item in details:
            at = engine.parse_dt(item.get("occurred_at") or item.get("window_start"))
            if at is None or at < start_at or at >= end_at:
                continue
            offset = int((at - start_at) // step)
            if offset < 0 or offset >= len(buckets):
                continue
            bucket = buckets[offset]
            bucket["total_requests"] += 1
            if item.get("is_error"):
                bucket["error_events"] += 1
                name = str(item.get("type_name") or "未归类")
                bucket["by_type"][name] = bucket["by_type"].get(name, 0) + 1
        for bucket in buckets:
            total = bucket["total_requests"]
            bucket["error_rate_pct"] = (
                round(bucket["error_events"] / total * 100, 2) if total else 0.0)
        return buckets

    def _apply_restated_alert(self, consumption: dict[str, Any]) -> None:
        """把重算后的窗口错误率回写到匹配的SLA指标，供告警与看板复用。"""
        api_names = {str(item.get("api")) for item in consumption.get("event_index", {}).values()}
        restated = consumption.get("restated") or {}
        for row in store.rows(self.MODULE):
            if str(row.get("关联API")) not in api_names:
                continue
            row["最近告警"] = (
                f"分类重算#{consumption['run_id']}：窗口错误率 "
                f"{restated.get('error_rate_pct', 0)}%，异常 {restated.get('error_events', 0)} 个"
            )

    def domain_snapshot(self) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        sla_list = []
        for row in rows:
            sla_list.append({
                "id": row.get("id"),
                "metric_no": row.get("指标编号"),
                "api": row.get("关联API"),
                "availability": row.get("可用率目标"),
                "p99": row.get("P99延迟"),
                "error_rate": row.get("错误率上限"),
                "period": row.get("监控周期"),
                "current_status": row.get("当前状态"),
                "last_alert": row.get("最近告警"),
                "status": row.get("status"),
            })
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(sla_list),
            "metrics": sla_list,
            "monitoring_count": sum(1 for r in sla_list if r.get("status") == "监控中"),
            "deviated_count": sum(1 for r in sla_list if r.get("status") == "已偏离"),
        }