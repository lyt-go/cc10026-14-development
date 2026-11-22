"""SLA 分类重算的领域示例数据。

这里刻意准备了三类容易被口径变化干扰的场景：
1. 查询窗口横跨 v1.0 / v2.0 两个分类版本；
2. 504 在 v1.0 属于「服务端5XX异常」，v2.0 起拆成「上游超时异常」；
3. 同一请求被两个采集批次覆盖，且重复采集发生在版本生效日之后，
   但事件原时间窗口仍在旧版本生效期内。

数据只依赖标准库，供 seed 与重算服务共同引用，避免循环导入。
"""
from __future__ import annotations

from typing import Any


CLASSIFICATION_VERSIONS: list[dict[str, Any]] = [
    {
        "version": "v1.0",
        "label": "5XX 统一归口（超时包含在内）",
        "effective_from": "2026-04-01T00:00:00+08:00",
        "effective_to": "2026-10-01T00:00:00+08:00",
        "rules": [
            {
                "type_code": "ERR-400",
                "type_name": "请求参数异常",
                "code_range": "4XX",
                "severity": "low",
                "retry": "修正请求参数后重试",
                "breaker": "-",
                "notify": "邮件",
            },
            {
                "type_code": "ERR-408",
                "type_name": "请求超时异常",
                "code_range": "408",
                "severity": "medium",
                "retry": "客户端退避后重试",
                "breaker": "连续10次",
                "notify": "企业微信",
            },
            {
                "type_code": "ERR-500",
                "type_name": "服务端5XX异常",
                "code_range": "5XX",
                "severity": "high",
                "retry": "服务端排查后重试",
                "breaker": "连续20次",
                "notify": "电话+企业微信",
            },
        ],
    },
    {
        "version": "v2.0",
        "label": "504 从 5XX 拆分为上游超时",
        "effective_from": "2026-10-01T00:00:00+08:00",
        "effective_to": None,
        "rules": [
            {
                "type_code": "ERR-400",
                "type_name": "请求参数异常",
                "code_range": "4XX",
                "severity": "low",
                "retry": "修正请求参数后重试",
                "breaker": "-",
                "notify": "邮件",
            },
            {
                "type_code": "ERR-408",
                "type_name": "请求超时异常",
                "code_range": "408",
                "severity": "medium",
                "retry": "客户端退避后重试",
                "breaker": "连续10次",
                "notify": "企业微信",
            },
            {
                "type_code": "ERR-500",
                "type_name": "服务端5XX异常",
                "code_range": "5XX",
                "severity": "high",
                "retry": "服务端排查后重试",
                "breaker": "连续20次",
                "notify": "电话+企业微信",
            },
            {
                "type_code": "ERR-504",
                "type_name": "上游超时异常",
                "code_range": "504",
                "severity": "critical",
                "retry": "检查上游依赖后重试",
                "breaker": "连续5次",
                "notify": "电话+短信",
            },
        ],
    },
]


def _event(
    event_id: str,
    request_id: str,
    api: str,
    caller: str,
    code: str,
    occurred_at: str,
    window_start: str,
    window_end: str,
    batch: str,
    collected_at: str,
    latency_ms: int,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "request_id": request_id,
        "api": api,
        "caller": caller,
        "response_code": code,
        "occurred_at": occurred_at,
        "window_start": window_start,
        "window_end": window_end,
        "latency_ms": latency_ms,
        "collect_batch": batch,
        "collected_at": collected_at,
    }


# 查询窗口：2026-09-28 00:00:00+08:00 ~ 2026-10-05 00:00:00+08:00
# 分类版本切换点：2026-10-01 00:00:00+08:00
SLA_EVENT_RECORDS: list[dict[str, Any]] = [
    _event("EVT-0001", "REQ-9001", "订单查询API", "结算中心", "200",
           "2026-09-28T10:00:00+08:00", "2026-09-28T09:59:55+08:00",
           "2026-09-28T10:00:05+08:00", "B-0928", "2026-09-28T10:01:00+08:00", 82),
    _event("EVT-0002", "REQ-9002", "订单查询API", "结算中心", "500",
           "2026-09-29T11:20:00+08:00", "2026-09-29T11:19:54+08:00",
           "2026-09-29T11:20:06+08:00", "B-0929", "2026-09-29T11:21:00+08:00", 310),
    _event("EVT-0003", "REQ-9003", "订单查询API", "结算中心", "504",
           "2026-09-29T14:05:00+08:00", "2026-09-29T14:04:50+08:00",
           "2026-09-29T14:05:10+08:00", "B-0929", "2026-09-29T14:06:00+08:00", 5002),
    _event("EVT-0004", "REQ-9004", "库存同步API", "仓储服务", "503",
           "2026-09-30T09:15:00+08:00", "2026-09-30T09:14:56+08:00",
           "2026-09-30T09:15:04+08:00", "B-0930", "2026-09-30T09:16:00+08:00", 128),
    _event("EVT-0005", "REQ-9005", "库存同步API", "仓储服务", "200",
           "2026-09-30T16:40:00+08:00", "2026-09-30T16:39:55+08:00",
           "2026-09-30T16:40:05+08:00", "B-0930", "2026-09-30T16:41:00+08:00", 96),
    # REQ-9006 第一次采集：原时间窗口在 v1.0 生效期内
    _event("EVT-0006", "REQ-9006", "订单查询API", "结算中心", "504",
           "2026-09-30T23:58:30+08:00", "2026-09-30T23:58:25+08:00",
           "2026-09-30T23:58:35+08:00", "B-1001", "2026-10-01T00:10:00+08:00", 4800),
    # REQ-9002 的重复采集：采集发生在 v2.0 生效后，原窗口仍在 09-29
    _event("EVT-0007", "REQ-9002", "订单查询API", "结算中心", "500",
           "2026-09-29T11:20:00+08:00", "2026-09-29T11:19:54+08:00",
           "2026-09-29T11:20:06+08:00", "B-1001", "2026-10-01T00:12:00+08:00", 310),
    _event("EVT-0008", "REQ-9007", "订单查询API", "结算中心", "504",
           "2026-10-01T08:30:00+08:00", "2026-10-01T08:29:52+08:00",
           "2026-10-01T08:30:08+08:00", "B-1001", "2026-10-01T08:31:00+08:00", 5200),
    _event("EVT-0009", "REQ-9008", "库存同步API", "仓储服务", "500",
           "2026-10-02T10:10:00+08:00", "2026-10-02T10:09:57+08:00",
           "2026-10-02T10:10:03+08:00", "B-1002", "2026-10-02T10:11:00+08:00", 288),
    _event("EVT-0010", "REQ-9009", "库存同步API", "仓储服务", "408",
           "2026-10-02T13:45:00+08:00", "2026-10-02T13:44:55+08:00",
           "2026-10-02T13:45:05+08:00", "B-1002", "2026-10-02T13:46:00+08:00", 30000),
    _event("EVT-0011", "REQ-9010", "订单查询API", "结算中心", "200",
           "2026-10-03T09:05:00+08:00", "2026-10-03T09:04:58+08:00",
           "2026-10-03T09:05:02+08:00", "B-1003", "2026-10-03T09:06:00+08:00", 77),
    _event("EVT-0012", "REQ-9011", "订单查询API", "结算中心", "502",
           "2026-10-03T15:30:00+08:00", "2026-10-03T15:29:50+08:00",
           "2026-10-03T15:30:10+08:00", "B-1003", "2026-10-03T15:31:00+08:00", 640),
    # REQ-9006 的再次采集：跨过生效日期，最后一次采集发生在 v2.0 生效后
    _event("EVT-0013", "REQ-9006", "订单查询API", "结算中心", "504",
           "2026-09-30T23:58:30+08:00", "2026-09-30T23:58:25+08:00",
           "2026-09-30T23:58:35+08:00", "B-1002", "2026-10-02T00:05:00+08:00", 4800),
    _event("EVT-0014", "REQ-9012", "库存同步API", "仓储服务", "404",
           "2026-10-04T11:00:00+08:00", "2026-10-04T10:59:56+08:00",
           "2026-10-04T11:00:04+08:00", "B-1004", "2026-10-04T11:01:00+08:00", 45),
    _event("EVT-0015", "REQ-9013", "订单查询API", "结算中心", "200",
           "2026-10-04T12:00:00+08:00", "2026-10-04T11:59:57+08:00",
           "2026-10-04T12:00:03+08:00", "B-1004", "2026-10-04T12:01:00+08:00", 88),
]


DELIVERED_REPORTS: list[dict[str, Any]] = [
    {
        "report_id": "RPT-SLA-2026W40",
        "title": "SLA周报 2026-09-28 ~ 2026-10-04",
        "window_start": "2026-09-28T00:00:00+08:00",
        "window_end": "2026-10-05T00:00:00+08:00",
        "delivered_at": "2026-10-05T09:00:00+08:00",
        "delivered_by": "SLA监控周报任务",
        "classification_version": "v1.0",
        "classification_policy": "uniform",
        "dedupe_applied": False,
        "source_record_ids": [row["event_id"] for row in SLA_EVENT_RECORDS],
        "source_record_count": len(SLA_EVENT_RECORDS),
        "stats": {
            "total_requests": 15,
            "success_events": 4,
            "error_events": 11,
            "classified_error_events": 11,
            "unclassified_error_events": 0,
            "error_rate": 0.7333,
            "error_rate_pct": 73.33,
            "by_type": [
                {
                    "type_code": "ERR-500",
                    "type_name": "服务端5XX异常",
                    "count": 9,
                    "error_rate": 0.6,
                    "error_rate_pct": 60.0,
                    "share_of_errors": 0.8182,
                },
                {
                    "type_code": "ERR-408",
                    "type_name": "请求超时异常",
                    "count": 1,
                    "error_rate": 0.0667,
                    "error_rate_pct": 6.67,
                    "share_of_errors": 0.0909,
                },
                {
                    "type_code": "ERR-400",
                    "type_name": "请求参数异常",
                    "count": 1,
                    "error_rate": 0.0667,
                    "error_rate_pct": 6.67,
                    "share_of_errors": 0.0909,
                },
            ],
        },
        "note": (
            "历史报告交付时分类任务仍锁定 v1.0，且按采集原始记录统计，"
            "未做跨批次去重；报告保留原始记录快照供事后核对。"
        ),
    },
]


def _rule_store_row(index: int, version: dict[str, Any], rule: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": index,
        "status": "已生效",
        "pending": False,
        "abnormal": False,
        "类型编号": rule["type_code"],
        "类型名称": rule["type_name"],
        "错误码段": rule["code_range"],
        "分级策略": rule["severity"],
        "重试建议": rule["retry"],
        "熔断阈值": rule["breaker"],
        "通知方式": rule["notify"],
        "分类版本": version["version"],
    }


ERROR_CLASSIFY_ROWS: list[dict[str, Any]] = []
for _version in CLASSIFICATION_VERSIONS:
    for _rule in _version["rules"]:
        ERROR_CLASSIFY_ROWS.append(
            _rule_store_row(len(ERROR_CLASSIFY_ROWS) + 1, _version, _rule)
        )
