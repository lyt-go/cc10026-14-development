"""SLA 分类重算专属接口：版本登记、批量重算、交付报告与核对。

这些路径比通用资源路由更具体，必须在 /api/{module} 动态路由之前注册。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.services.sla_recompute import sla_recompute_service as recompute_svc

router = APIRouter(prefix="/api/sla-recompute", tags=["SLA分类重算"])


@router.get("/versions")
def list_versions() -> dict[str, Any]:
    items = recompute_svc.list_versions()
    return {"items": items, "total": len(items)}


@router.post("/versions")
def register_version(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    record, message = recompute_svc.register_version(payload)
    if record is None:
        return {"ok": False, "message": message}
    return {"ok": True, "message": message, "version": record}


@router.get("/runs")
def list_runs() -> dict[str, Any]:
    items = recompute_svc.list_runs()
    return {"items": items, "total": len(items)}


@router.post("/runs")
def create_run(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    run, message = recompute_svc.recompute(payload)
    if run is None:
        return {"ok": False, "message": message}
    return {"ok": True, "message": message, "run": run}


@router.get("/runs/{run_id}")
def get_run(run_id: int, include_events: bool = Query(default=True)) -> dict[str, Any]:
    run = recompute_svc.get_run(run_id, include_events=include_events)
    if not run:
        raise HTTPException(status_code=404, detail=f"重算批次 {run_id} 不存在")
    return run


@router.get("/runs/{run_id}/export")
def export_run(run_id: int) -> StreamingResponse:
    content, filename = recompute_svc.export_csv(run_id)
    if not content:
        raise HTTPException(status_code=404, detail=f"重算批次 {run_id} 不存在")
    return StreamingResponse(
        iter([content]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/runs/{run_id}/rerun")
def rerun(run_id: int, payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    run, message = recompute_svc.rerun(run_id, payload)
    if run is None:
        return {"ok": False, "message": message}
    return {"ok": True, "message": message, "run": run}


@router.post("/runs/{run_id}/deliver")
def deliver(run_id: int, payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    report, message = recompute_svc.deliver(run_id, payload)
    if report is None:
        return {"ok": False, "message": message}
    return {"ok": True, "message": message, "report": report}


@router.get("/reports")
def list_reports() -> dict[str, Any]:
    items = recompute_svc.list_reports()
    return {"items": items, "total": len(items)}


@router.get("/reconcile")
def reconcile(
    report_id: str = Query(...),
    run_id: int | None = Query(default=None),
) -> dict[str, Any]:
    result = recompute_svc.reconcile(report_id, run_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message", "核对失败"))
    return result


# ---------- SLA监控消费分类重算结果 ----------


@router.post("/runs/{run_id}/consume")
def consume_run(run_id: int) -> dict[str, Any]:
    from app.services.sla_monitor import SlaMonitorService

    consumption, message = SlaMonitorService().consume_recompute(run_id)
    if consumption is None:
        raise HTTPException(status_code=404, detail=message)
    return {"ok": True, "message": message, "consumption": consumption}


@router.get("/runs/{run_id}/consumption")
def get_consumption(run_id: int) -> dict[str, Any]:
    from app.services.sla_monitor import SlaMonitorService

    consumption = SlaMonitorService().read_consumption(run_id)
    if not consumption:
        raise HTTPException(status_code=404, detail=f"重算批次 {run_id} 尚未被SLA监控消费")
    return consumption
