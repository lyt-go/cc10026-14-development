"""SLA 分类重算与报告核对测试。

覆盖：
1. 按事件原时间窗口选择分类版本（不按采集时间）；
2. 同一请求多批采集覆盖时只计一次，且留痕；
3. 交付报告可按原口径复现，差异可归因到去重/口径；
4. 版本更新后复算，异常量变化与归类口径变化分开；
5. SLA监控消费整组指标明细，可逐条关联事件；
6. HTTP 接口与通用资源路由不冲突。
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("DATA_ROOT", tempfile.mkdtemp(prefix="sla_recompute_test_"))

WINDOW_START = "2026-09-28T00:00:00+08:00"
WINDOW_END = "2026-10-05T00:00:00+08:00"


@pytest.fixture(autouse=True)
def clean_state():
    """每个测试重置内存仓库与落盘数据，避免重算批次互相影响。"""
    import shutil
    from app.seed import SEED_ROWS
    from app.store import store
    from app.services.data_manager import DATA_ROOT

    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    shutil.rmtree(DATA_ROOT, ignore_errors=True)
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    yield
    shutil.rmtree(DATA_ROOT, ignore_errors=True)


def _run(window_start=WINDOW_START, window_end=WINDOW_END, **extra):
    from app.services.sla_recompute import sla_recompute_service as svc

    payload = {"window_start": window_start, "window_end": window_end, **extra}
    run, message = svc.recompute(payload)
    assert run is not None, message
    return run


def test_version_resolved_by_event_window_not_collection():
    """跨版本窗口的事件按原时间窗口归类，采集时间不改变版本。"""
    run = _run()
    details = {item["event_id"]: item for item in run["event_details"]}

    # 09-30 的事件在采集于 10-02 后仍应使用 v1.0
    assert details["EVT-0013"]["classification_version"] == "v1.0"
    assert details["EVT-0013"]["window_start"].startswith("2026-09-30")
    assert details["EVT-0013"]["collected_at"].startswith("2026-10-02")

    # 10-01 起的事件使用 v2.0
    assert details["EVT-0008"]["classification_version"] == "v2.0"
    versions_used = {row["version"] for row in run["stats"]["versions_used"]}
    assert versions_used == {"v1.0", "v2.0"}


def test_error_code_reclassified_between_versions():
    """504 在 v1.0 属于 5XX，在 v2.0 拆分为上游超时。"""
    run = _run()
    details = {item["event_id"]: item for item in run["event_details"]}

    assert details["EVT-0003"]["response_code"] == "504"
    assert details["EVT-0003"]["type_name"] == "服务端5XX异常"
    assert details["EVT-0008"]["response_code"] == "504"
    assert details["EVT-0008"]["type_name"] == "上游超时异常"

    counts = {row["type_name"]: row["count"] for row in run["stats"]["by_type"]}
    assert counts["服务端5XX异常"] == 6
    assert counts["上游超时异常"] == 1


def test_duplicate_collection_counted_once_and_traceable():
    """同一请求多批采集只计一次，重复项留痕但不入指标。"""
    run = _run()
    assert run["raw_records"] == 15
    assert run["unique_requests"] == 13
    assert run["duplicates_dropped"] == 2
    assert run["stats"]["total_requests"] == 13

    groups = {group["request_id"]: group for group in run["duplicate_groups"]}
    assert set(groups) == {"REQ-9002", "REQ-9006"}
    assert groups["REQ-9006"]["kept_event_id"] == "EVT-0013"
    assert groups["REQ-9006"]["dropped_event_ids"] == ["EVT-0006"]

    kept = {item["event_id"]: item for item in run["event_details"]}
    assert "EVT-0006" not in kept
    assert kept["EVT-0013"]["duplicate_event_ids"] == ["EVT-0006"]
    assert set(kept["EVT-0013"]["source_batches"]) == {"B-1001", "B-1002"}


def test_dedupe_can_be_disabled_for_audit():
    """关闭去重时按原始采集记录统计，与交付报告口径一致。"""
    run = _run(dedupe=False)
    assert run["stats"]["total_requests"] == 15
    assert run["stats"]["error_events"] == 11
    assert run["duplicates_dropped"] == 0


def test_metric_detail_links_every_error_event():
    """SLA 指标明细逐条关联异常事件，总数与异常量对齐。"""
    run = _run()
    metric_ids = []
    for metric in run["metric_detail"]:
        for link in metric["event_links"]:
            metric_ids.append(link["event_id"])
    assert len(metric_ids) == run["stats"]["error_events"]
    assert len(set(metric_ids)) == len(metric_ids)
    assert all(link["window_start"] and link["window_end"] for link in
               [l for m in run["metric_detail"] for l in m["event_links"]])


def test_decomposition_separates_volume_and_classification():
    """去重影响与口径影响可加，且口径影响只体现类型构成变化。"""
    run = _run()
    volume = run["decomposition"]["volume_effect"]
    classification = run["decomposition"]["classification_effect"]
    raw = run["decomposition"]["raw_stats"]
    actual = run["decomposition"]["actual_stats"]

    # 去重只减少请求量与异常量，总量变化由去重解释
    assert volume["total_requests"] == -2
    assert volume["error_events"] == -2
    assert classification["total_requests"] == 0
    assert classification["error_events"] == 0

    # 口径变化把 1 个 504 从 5XX 移到上游超时
    moved = {row["type_name"]: row["delta"] for row in classification["by_type"]}
    assert moved["上游超时异常"] == 1
    assert moved["服务端5XX异常"] == -1

    assert raw["total_requests"] + volume["total_requests"] + \
        classification["total_requests"] == actual["total_requests"]
    assert raw["error_events"] + volume["error_events"] + \
        classification["error_events"] == actual["error_events"]


def test_reconcile_legacy_report_reproduces_and_explains():
    """历史交付报告可按原口径复现，并能解释去重与口径差异。"""
    from app.services.sla_recompute import sla_recompute_service as svc

    run = _run()
    result = svc.reconcile("RPT-SLA-2026W40", run_id=run["run_id"])
    assert result["ok"] is True

    # 报告本身按原始采集记录与 v1.0 统一口径，能够逐项复现
    assert result["reproduce"]["matched"] is True
    assert result["reproduce"]["missing_event_ids"] == []
    report_stats = result["report"]["stats"]
    assert report_stats["total_requests"] == 15
    assert report_stats["error_events"] == 11

    # 重算结果与交付报告不同，但差异被拆成去重 + 口径两部分
    assert result["restated"]["matches_delivered"] is False
    assert result["restated"]["dedupe_applied"] is True
    assert result["restated"]["stats"]["total_requests"] == 13
    dedupe_effect = result["effects"]["dedupe_effect"]
    assert dedupe_effect["total_requests"] == -2
    assert dedupe_effect["error_events"] == -2
    class_effect = {row["type_name"]: row["delta"]
                    for row in result["effects"]["classification_effect"]["by_type"]}
    assert class_effect["上游超时异常"] == 1
    assert class_effect["服务端5XX异常"] == -1

    codes = {reason["code"] for reason in result["reasons"]}
    assert {"duplicate_collection", "code_reclassified",
            "version_window_resolution"} <= codes

    reclassified = next(r for r in result["reasons"] if r["code"] == "code_reclassified")
    assert reclassified["evidence"][0]["response_code"] == "504"
    assert reclassified["evidence"][0]["from_version"] == "v1.0"
    assert reclassified["evidence"][0]["to_version"] == "v2.0"


def test_reconcile_report_without_run_still_reproduces():
    """不指定重算批次时，只核对报告自身是否可复现。"""
    from app.services.sla_recompute import sla_recompute_service as svc

    result = svc.reconcile("RPT-SLA-2026W40")
    assert result["ok"] is True
    assert result["reproduce"]["matched"] is True
    assert result["run"] is None
    assert result["effects"]["total_delta"] is None


def test_register_version_and_rerun_separates_metric_delta():
    """分类版本更新后复算，异常量不变但类型构成按口径变化。"""
    from app.services.sla_recompute import sla_recompute_service as svc

    first = _run(title="初版重算")
    record, message = svc.register_version({
        "version": "v2.1",
        "label": "502 归入上游网关",
        "effective_from": "2026-10-03T00:00:00+08:00",
        "rules": [
            {"type_code": "ERR-400", "type_name": "请求参数异常", "code_range": "4XX"},
            {"type_code": "ERR-408", "type_name": "请求超时异常", "code_range": "408"},
            {"type_code": "ERR-500", "type_name": "服务端5XX异常", "code_range": "5XX"},
            {"type_code": "ERR-504", "type_name": "上游超时异常", "code_range": "504"},
            {"type_code": "ERR-502X", "type_name": "上游网关异常", "code_range": "502"},
        ],
    })
    assert record is not None, message

    second, message = svc.rerun(first["run_id"])
    assert second is not None, message
    assert second["parent_run_id"] == first["run_id"]
    assert "v2.1" in {row["version"] for row in second["stats"]["versions_used"]}

    comparison = second["comparison"]
    assert comparison["total_delta"]["error_events"] == 0
    assert comparison["volume_effect"]["by_type"] == []
    moved = {row["type_name"]: row["delta"]
             for row in comparison["classification_effect"]["by_type"]}
    assert moved == {"上游网关异常": 1, "服务端5XX异常": -1}


def test_pinned_version_freezes_classification():
    """锁定版本重算时全部事件使用同一版本，口径影响为零。"""
    run = _run(classification_version="v1.0")
    assert {item["classification_version"] for item in run["event_details"]} == {"v1.0"}
    assert run["decomposition"]["classification_effect"]["by_type"] == []
    assert run["decomposition"]["classification_effect"]["error_events"] == 0


def test_unknown_version_rejected():
    from app.services.sla_recompute import sla_recompute_service as svc

    run, message = svc.recompute({
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
        "classification_version": "v9.9",
    })
    assert run is None
    assert "未登记" in message


def test_empty_window_rejected():
    from app.services.sla_recompute import sla_recompute_service as svc

    run, message = svc.recompute({
        "window_start": "2020-01-01T00:00:00+08:00",
        "window_end": "2020-01-02T00:00:00+08:00",
    })
    assert run is None
    assert "没有历史" in message


def test_delivered_report_is_frozen():
    """交付后登记新版本，重新核对报告仍按冻结快照复现。"""
    from app.services.sla_recompute import sla_recompute_service as svc

    run = _run()
    report, message = svc.deliver(run["run_id"], {"report_id": "RPT-FREEZE"})
    assert report is not None, message

    svc.register_version({
        "version": "v3.0",
        "label": "全量重新拆分",
        "effective_from": "2026-10-03T12:00:00+08:00",
        "rules": [
            {"type_code": "ERR-ALL", "type_name": "统一异常", "code_range": "4XX"},
            {"type_code": "ERR-ALL2", "type_name": "统一异常5XX", "code_range": "5XX"},
        ],
    })
    result = svc.reconcile("RPT-FREEZE")
    assert result["reproduce"]["matched"] is True
    assert result["report"]["stats"]["total_requests"] == run["stats"]["total_requests"]

    # 重复交付会被拒绝，保证已交付报告不被覆盖
    again, message = svc.deliver(run["run_id"], {"report_id": "RPT-FREEZE"})
    assert again is None
    assert "不可覆盖" in message


def test_sla_monitor_consumes_recompute_metrics():
    """SLA监控消费整组指标明细，重新统计窗口并逐条关联事件。"""
    from app.services.sla_monitor import SlaMonitorService

    run = _run()
    service = SlaMonitorService()
    consumption, message = service.consume_recompute(run["run_id"])
    assert consumption is not None, message

    restated = consumption["restated"]
    assert restated["total_requests"] == run["stats"]["total_requests"]
    assert restated["error_events"] == run["stats"]["error_events"]
    assert consumption["linked_event_count"] == run["stats"]["error_events"]
    assert consumption["unlinked_events"] == []
    assert consumption["metric_count"] == len(run["metric_detail"])

    # 分桶按监控周期（订单查询API为1440分钟）且请求总量守恒
    assert len(restated["window_series"]) > 1
    assert sum(b["total_requests"] for b in restated["window_series"]) == \
        run["stats"]["total_requests"]

    # 每条指标的事件都能在事件索引中反查
    for row in restated["by_type"]:
        for event_id in row["event_ids"]:
            assert event_id in consumption["event_index"]

    # 消费结果落盘可复核
    stored = service.read_consumption(run["run_id"])
    assert stored["run_id"] == run["run_id"]


def test_consumption_of_missing_run_returns_error():
    from app.services.sla_monitor import SlaMonitorService

    consumption, message = SlaMonitorService().consume_recompute(9999)
    assert consumption is None
    assert "不存在" in message


def test_http_api_exposes_recompute_and_does_not_shadow_modules():
    """重算接口可用，且不会抢走通用资源路由。"""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/sla_monitor").status_code == 200

    response = client.post("/api/sla-recompute/runs", json={
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
        "title": "HTTP 重算",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    run_id = body["run"]["run_id"]

    detail = client.get(f"/api/sla-recompute/runs/{run_id}")
    assert detail.status_code == 200
    assert len(detail.json()["event_details"]) == body["run"]["unique_requests"]

    consumption = client.post(f"/api/sla-recompute/runs/{run_id}/consume")
    assert consumption.status_code == 200
    assert consumption.json()["consumption"]["restated"]["error_events"] == 9

    reconcile = client.get("/api/sla-recompute/reconcile", params={
        "report_id": "RPT-SLA-2026W40", "run_id": run_id,
    })
    assert reconcile.status_code == 200
    assert reconcile.json()["reproduce"]["matched"] is True

    export = client.get(f"/api/sla-recompute/runs/{run_id}/export")
    assert export.status_code == 200
    assert "classification_version" in export.text.splitlines()[0]

    versions = client.get("/api/sla-recompute/versions")
    assert versions.status_code == 200
    assert {v["version"] for v in versions.json()["items"]} == {"v1.0", "v2.0"}


def test_engine_dedupe_prefers_latest_batch():
    """去重保留采集时间最晚的一条，批次相同时保留事件号较大的。"""
    from app.services.recompute_engine import dedupe_records

    records = [
        {"event_id": "E1", "request_id": "R1", "collected_at": "2026-10-01T00:00:00+08:00",
         "collect_batch": "B1"},
        {"event_id": "E2", "request_id": "R1", "collected_at": "2026-10-02T00:00:00+08:00",
         "collect_batch": "B2"},
    ]
    unique, stats = dedupe_records(records)
    assert stats["duplicates_dropped"] == 1
    assert unique[0]["event_id"] == "E2"
    assert unique[0]["duplicate_event_ids"] == ["E1"]
    assert unique[0]["source_batches"] == ["B1", "B2"]


def test_engine_match_rule_prefers_exact_code():
    """精确错误码优先于通配码段。"""
    from app.services.recompute_engine import match_rule

    rules = [
        {"类型名称": "服务端5XX异常", "错误码段": "5XX"},
        {"类型名称": "上游超时异常", "错误码段": "504"},
    ]
    assert match_rule("504", rules)["类型名称"] == "上游超时异常"
    assert match_rule("503", rules)["类型名称"] == "服务端5XX异常"
    assert match_rule("200", rules) is None


def test_engine_version_boundaries_are_half_open():
    """版本生效区间为 [from, to)，切换点整点归入新版本。"""
    from app.services.recompute_engine import resolve_version
    from app.domain_seed import CLASSIFICATION_VERSIONS

    just_before = resolve_version("2026-09-30T23:59:59+08:00", CLASSIFICATION_VERSIONS)
    assert just_before["version"] == "v1.0"
    on_boundary = resolve_version("2026-10-01T00:00:00+08:00", CLASSIFICATION_VERSIONS)
    assert on_boundary["version"] == "v2.0"


def test_existing_error_classify_flow_still_works():
    """原有异常归类接口保留：列表与按错误码归类不受重算影响。"""
    from app.services.error_classify import ErrorClassifyService

    service = ErrorClassifyService()
    rows, total = service.list_entries(page=1, size=100)
    assert total == len(rows) == 7
    result = service.classify("504", "gateway timeout")
    assert "matched" in result


def test_error_classify_supports_historical_at():
    """单条归类可按事件时间取当时的分类版本。"""
    from app.services.error_classify import ErrorClassifyService

    service = ErrorClassifyService()
    old = service.classify("504", at="2026-09-30T12:00:00+08:00")
    new = service.classify("504", at="2026-10-02T12:00:00+08:00")
    latest = service.classify("504")
    assert old["type_name"] == "服务端5XX异常"
    assert old["classification_version"] == "v1.0"
    assert new["type_name"] == "上游超时异常"
    assert new["classification_version"] == "v2.0"
    assert latest["classification_version"] == "v2.0"
    assert service.classify("200")["matched"] is False


def test_consumption_writes_back_alert_to_sla_metric():
    """消费重算结果后，匹配的SLA指标能看到重算后的窗口错误率。"""
    from app.services.sla_monitor import SlaMonitorService
    from app.store import store

    run = _run()
    service = SlaMonitorService()
    consumption, message = service.consume_recompute(run["run_id"])
    assert consumption is not None, message

    alerts = [
        row.get("最近告警", "") for row in store.rows("sla_monitor")
        if row.get("关联API") in ("订单查询API", "库存同步API")
    ]
    assert alerts
    assert all(f"#{run['run_id']}" in text for text in alerts)
    assert all("69.23%" in text for text in alerts)
