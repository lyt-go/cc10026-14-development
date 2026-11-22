"""分类重算引擎：不依赖内存仓库的纯计算逻辑。

把「历史 SLA 事件 → 去重 → 按事件原时间窗口选择分类版本 → 归类 → 聚合」
拆成可单独测试的纯函数，供种子数据构造、重算服务、交付报告核验和
SLA 监控消费共同复用。所有时间均为东八区 ISO 字符串。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable


TZ = timezone.utc


def parse_dt(value: Any) -> datetime | None:
    """解析 ISO 时间；无时区按东八区处理，便于种子和接口入参统一比较。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            try:
                dt = datetime.fromisoformat(text[:19])
            except ValueError:
                return None
    if dt.tzinfo is None:
        from datetime import timedelta

        dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
    return dt.astimezone(timezone.utc)


def in_window(value: Any, start: Any, end: Any) -> bool:
    """半开区间 [start, end) 判断。"""
    at = parse_dt(value)
    lo = parse_dt(start)
    hi = parse_dt(end)
    if at is None:
        return False
    if lo is not None and at < lo:
        return False
    if hi is not None and at >= hi:
        return False
    return True


def resolve_version(at: Any, versions: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """按事件发生时间（原时间窗口）选择生效的分类版本。

    生效区间为 [effective_from, effective_to)，effective_to 为空表示持续生效。
    同一时间命中多个版本时取 effective_from 最晚的一版。
    """
    moment = parse_dt(at)
    if moment is None:
        return None
    matched: list[tuple[datetime, dict[str, Any]]] = []
    for version in versions:
        start = parse_dt(version.get("effective_from"))
        end = parse_dt(version.get("effective_to"))
        if start is None:
            continue
        if moment < start:
            continue
        if end is not None and moment >= end:
            continue
        matched.append((start, version))
    if not matched:
        return None
    matched.sort(key=lambda pair: pair[0])
    return matched[-1][1]


def latest_version(versions: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    ordered = sorted(
        (v for v in versions if parse_dt(v.get("effective_from")) is not None),
        key=lambda v: parse_dt(v.get("effective_from")),  # type: ignore[arg-type,return-value]
    )
    return ordered[-1] if ordered else None


def earliest_version(versions: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    ordered = sorted(
        (v for v in versions if parse_dt(v.get("effective_from")) is not None),
        key=lambda v: parse_dt(v.get("effective_from")),  # type: ignore[arg-type,return-value]
    )
    return ordered[0] if ordered else None


def _pattern_specificity(pattern: str) -> tuple[int, int]:
    """越具体的规则优先级越高：精确码段优先于 XX 通配，长码段优先于短码段。"""
    wildcard = pattern.endswith("XX")
    return (0 if wildcard else 1, len(pattern))


def _rule_pattern(rule: dict[str, Any]) -> str:
    """规则可能来自版本快照（code_range）或规则表（错误码段）。"""
    value = rule.get("错误码段")
    if value is None:
        value = rule.get("code_range")
    return str(value or "").strip()


def _rule_version(rule: dict[str, Any]) -> str:
    value = rule.get("分类版本")
    if value is None:
        value = rule.get("version")
    return str(value or "")


def rules_for_version(
    rules: Iterable[dict[str, Any]], version: str | None
) -> list[dict[str, Any]]:
    if version is None:
        return []
    selected = [
        rule for rule in rules
        if _rule_version(rule) == str(version)
        and rule.get("status", "已生效") == "已生效"
    ]
    return sorted(
        selected,
        key=lambda rule: _pattern_specificity(_rule_pattern(rule)),
        reverse=True,
    )


def match_rule(error_code: str, rules: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """按错误码匹配分类规则；支持 5XX 形式通配，精确规则优先。"""
    code = str(error_code or "").strip()
    if not code:
        return None
    ordered = sorted(
        rules,
        key=lambda rule: _pattern_specificity(_rule_pattern(rule)),
        reverse=True,
    )
    for rule in ordered:
        pattern = _rule_pattern(rule)
        if not pattern:
            continue
        if pattern.endswith("XX"):
            if code.startswith(pattern[:-2]):
                return rule
        elif code.startswith(pattern):
            return rule
    return None


def response_code(record: dict[str, Any]) -> str:
    """事件里响应码字段兼容多种采集格式。"""
    for key in ("response_code", "响应码", "error_code", "错误码"):
        value = record.get(key)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return ""


def is_error_code(code: str) -> bool:
    try:
        return int(code) >= 400
    except (TypeError, ValueError):
        return False


def dedupe_records(
    records: Iterable[dict[str, Any]],
    *,
    key: str = "request_id",
    order_field: str = "collected_at",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """同一请求被多批采集覆盖时，本次重算只保留一条。

    保留 collected_at 最晚的采集记录；collected_at 相同则保留 event_id 较大的
    一条（后到覆盖先到）。被丢弃的记录不删除，作为 duplicate_event_ids 与
    source_batches 写入保留记录和报告，便于复核。
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        group_key = str(record.get(key) or record.get("event_id") or "")
        groups.setdefault(group_key, []).append(dict(record))

    unique: list[dict[str, Any]] = []
    duplicate_groups: list[dict[str, Any]] = []
    raw_count = 0
    for group_key, items in groups.items():
        raw_count += len(items)
        items.sort(
            key=lambda row: (
                str(row.get(order_field) or ""),
                str(row.get("event_id") or ""),
            )
        )
        kept = items[-1]
        dropped = items[:-1]
        kept["source_batches"] = sorted({
            str(row.get("collect_batch", "")) for row in items if row.get("collect_batch")
        })
        kept["duplicate_event_ids"] = [str(row.get("event_id", "")) for row in dropped]
        kept["raw_record_count"] = len(items)
        unique.append(kept)
        if dropped:
            duplicate_groups.append({
                "request_id": group_key,
                "kept_event_id": kept.get("event_id"),
                "kept_batch": kept.get("collect_batch"),
                "dropped_event_ids": [row.get("event_id") for row in dropped],
                "dropped_batches": [row.get("collect_batch") for row in dropped],
                "occurred_at": kept.get("occurred_at"),
            })

    unique.sort(key=lambda row: (str(row.get(order_field) or ""), str(row.get("event_id") or "")))
    stats = {
        "raw_records": raw_count,
        "unique_requests": len(unique),
        "duplicates_dropped": raw_count - len(unique),
        "duplicate_groups": duplicate_groups,
    }
    return unique, stats


def classify_records(
    records: Iterable[dict[str, Any]],
    versions: Iterable[dict[str, Any]],
    rules: Iterable[dict[str, Any]],
    *,
    forced_version: str | None = None,
) -> list[dict[str, Any]]:
    """逐条归类，并记录实际采用的分类版本与事件原时间窗口。"""
    versions = list(versions)
    rules = list(rules)
    details: list[dict[str, Any]] = []
    for record in records:
        code = response_code(record)
        window_start = record.get("window_start") or record.get("occurred_at")
        window_end = record.get("window_end")
        occurred_at = record.get("occurred_at") or window_start
        if forced_version:
            version = forced_version
        else:
            resolved = resolve_version(window_start, versions)
            version = str(resolved.get("version")) if resolved else None
        rule = match_rule(code, rules_for_version(rules, version))
        error = is_error_code(code)
        details.append({
            "event_id": record.get("event_id"),
            "request_id": record.get("request_id"),
            "api": record.get("api") or record.get("api_name"),
            "caller": record.get("caller"),
            "response_code": code,
            "is_error": error,
            "occurred_at": occurred_at,
            "window_start": window_start,
            "window_end": window_end,
            "collect_batch": record.get("collect_batch"),
            "collected_at": record.get("collected_at"),
            "source_batches": record.get("source_batches", []),
            "duplicate_event_ids": record.get("duplicate_event_ids", []),
            "raw_record_count": record.get("raw_record_count", 1),
            "classification_version": version,
            "type_code": rule.get("类型编号") if rule else None,
            "type_name": rule.get("类型名称") if rule else ("未归类" if error else "成功"),
            "severity": rule.get("分级策略") if rule else None,
            "classified": bool(rule),
        })
    return details


def aggregate(details: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """统计窗口内的请求量、错误率与分类明细。"""
    items = list(details)
    total = len(items)
    errors = [item for item in items if item.get("is_error")]
    successes = [item for item in items if not item.get("is_error")]

    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for item in errors:
        type_name = str(item.get("type_name") or "未归类")
        type_code = str(item.get("type_code") or "UNCLASSIFIED")
        bucket = buckets.setdefault((type_code, type_name), {
            "type_code": type_code,
            "type_name": type_name,
            "count": 0,
            "versions": {},
            "event_ids": [],
            "request_ids": [],
            "events": [],
        })
        bucket["count"] += 1
        version = str(item.get("classification_version") or "未定义")
        bucket["versions"][version] = bucket["versions"].get(version, 0) + 1
        bucket["event_ids"].append(item.get("event_id"))
        bucket["request_ids"].append(item.get("request_id"))
        bucket["events"].append({
            "event_id": item.get("event_id"),
            "request_id": item.get("request_id"),
            "api": item.get("api"),
            "response_code": item.get("response_code"),
            "occurred_at": item.get("occurred_at"),
            "window_start": item.get("window_start"),
            "window_end": item.get("window_end"),
            "classification_version": item.get("classification_version"),
        })

    by_type = []
    for bucket in buckets.values():
        count = int(bucket["count"])
        bucket["error_rate"] = round(count / total, 4) if total else 0.0
        bucket["error_rate_pct"] = round((count / total) * 100, 2) if total else 0.0
        bucket["share_of_errors"] = round(count / len(errors), 4) if errors else 0.0
        by_type.append(bucket)
    by_type.sort(key=lambda row: (-int(row["count"]), str(row["type_name"])))

    versions_used: dict[str, dict[str, Any]] = {}
    for item in items:
        version = str(item.get("classification_version") or "未定义")
        entry = versions_used.setdefault(version, {
            "version": version,
            "event_count": 0,
            "error_count": 0,
            "window_starts": [],
            "window_ends": [],
        })
        entry["event_count"] += 1
        if item.get("is_error"):
            entry["error_count"] += 1
        if item.get("window_start"):
            entry["window_starts"].append(str(item["window_start"]))
        if item.get("window_end"):
            entry["window_ends"].append(str(item["window_end"]))
    version_rows = []
    for entry in versions_used.values():
        starts = sorted(entry.pop("window_starts"))
        ends = sorted(entry.pop("window_ends"))
        entry["first_event_at"] = starts[0] if starts else None
        entry["last_event_at"] = ends[-1] if ends else None
        version_rows.append(entry)
    version_rows.sort(key=lambda row: str(row["version"]))

    return {
        "total_requests": total,
        "success_events": len(successes),
        "error_events": len(errors),
        "classified_error_events": sum(1 for item in errors if item.get("classified")),
        "unclassified_error_events": sum(1 for item in errors if not item.get("classified")),
        "error_rate": round(len(errors) / total, 4) if total else 0.0,
        "error_rate_pct": round((len(errors) / total) * 100, 2) if total else 0.0,
        "by_type": by_type,
        "versions_used": version_rows,
    }


def counts_by_version(
    details: Iterable[dict[str, Any]],
    *,
    version: str,
    rules: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """把同一组事件强制按指定版本重算，用于口径对比。"""
    rules = list(rules)
    matched: dict[str, int] = {}
    error_total = 0
    for item in details:
        if not item.get("is_error"):
            continue
        error_total += 1
        rule = match_rule(str(item.get("response_code") or ""), rules_for_version(rules, version))
        type_name = str(rule.get("类型名称")) if rule else "未归类"
        matched[type_name] = matched.get(type_name, 0) + 1
    return {"version": version, "error_total": error_total, "by_type": matched}
