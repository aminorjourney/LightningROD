from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.battery_status import EVBatteryStatus
from db.models.charging_session import EVChargingSession
from web.dependencies import get_db
from web.queries.battery import (
    build_charge_curve_chart,
    build_degradation_chart,
    build_soc_timeline_chart,
    detect_charging_regions,
    query_charge_curve,
    query_degradation_data,
    query_recent_sessions_for_picker,
    query_soc_timeline,
)
from web.queries.vehicles import get_active_device_id, get_active_vehicle, get_all_vehicles

router = APIRouter()
templates = Jinja2Templates(directory="web/templates")


@router.get("/battery", response_class=HTMLResponse)
async def battery(
    request: Request,
    db: AsyncSession = Depends(get_db),
    range: Optional[str] = "7d",
    session: Optional[int] = None,
    hx_request: Annotated[Optional[str], Header()] = None,
):
    time_range = range or "7d"

    # Vehicle scoping
    active_device_id = await get_active_device_id(db)
    active_vehicle = await get_active_vehicle(db)
    all_vehicles = await get_all_vehicles(db)

    # Rated capacity from vehicle, fallback 75.0 kWh
    rated_capacity = 75.0
    if active_vehicle and active_vehicle.battery_capacity_kwh:
        rated_capacity = float(active_vehicle.battery_capacity_kwh)

    # 1. SOC timeline
    soc_data = await query_soc_timeline(db, time_range=time_range, device_id=active_device_id)
    charging_regions = detect_charging_regions(soc_data)
    soc_chart = build_soc_timeline_chart(soc_data, charging_regions)

    # Build session time windows for click-to-drill JS
    session_time_windows = []
    recent_sessions = await query_recent_sessions_for_picker(db, device_id=active_device_id)
    for s in recent_sessions:
        if s.get("session_start_utc"):
            session_time_windows.append({
                "id": s["id"],
                "start": s["session_start_utc"].isoformat() if hasattr(s["session_start_utc"], "isoformat") else str(s["session_start_utc"]),
            })

    # 2. Degradation
    degradation_data = await query_degradation_data(db, time_range=time_range, device_id=active_device_id)
    degradation_chart = build_degradation_chart(degradation_data, rated_capacity)

    # 3. Charge curve
    charge_curve_chart = ""
    active_session = session
    if session:
        curve_data = await query_charge_curve(db, session_id=session)
        charge_curve_chart = build_charge_curve_chart(curve_data)

    # 4. Summary card values
    summary = {
        "battery_health_pct": None,
        "battery_health_capacity": None,
        "rated_capacity": rated_capacity,
        "current_soc": None,
        "latest_range": None,
        "total_sessions": 0,
    }

    # Latest SOC from battery_status
    latest_stmt = (
        select(
            EVBatteryStatus.hv_battery_soc,
            EVBatteryStatus.hv_battery_range,
            EVBatteryStatus.hv_battery_capacity,
        )
        .order_by(EVBatteryStatus.recorded_at.desc())
        .limit(1)
    )
    if active_device_id:
        latest_stmt = latest_stmt.where(EVBatteryStatus.device_id == active_device_id)
    latest_result = await db.execute(latest_stmt)
    latest = latest_result.first()
    if latest:
        if latest.hv_battery_soc is not None:
            summary["current_soc"] = float(latest.hv_battery_soc)
        if latest.hv_battery_range is not None:
            summary["latest_range"] = float(latest.hv_battery_range)
        if latest.hv_battery_capacity is not None:
            cap = float(latest.hv_battery_capacity)
            summary["battery_health_capacity"] = cap
            summary["battery_health_pct"] = (cap / rated_capacity) * 100

    # Total session count
    count_stmt = select(func.count(EVChargingSession.id))
    if active_device_id:
        count_stmt = count_stmt.where(EVChargingSession.device_id == active_device_id)
    count_result = await db.execute(count_stmt)
    summary["total_sessions"] = count_result.scalar() or 0

    context = {
        "soc_chart": soc_chart,
        "degradation_chart": degradation_chart,
        "charge_curve_chart": charge_curve_chart,
        "summary": summary,
        "sessions_list": recent_sessions,
        "session_time_windows": session_time_windows,
        "active_range": time_range,
        "active_session": active_session,
        "active_page": "battery",
        "page_title": "Battery Analytics",
        "active_vehicle": active_vehicle,
        "all_vehicles": all_vehicles,
    }

    if hx_request:
        return templates.TemplateResponse(request, "battery/partials/summary.html", context)
    return templates.TemplateResponse(request, "battery/index.html", context)
