from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.charging_session import EVChargingSession
from db.models.reference import EVChargingNetwork, EVLocationLookup
from web.dependencies import get_db
from web.queries.settings import get_all_networks
from web.queries.vehicles import get_active_vehicle, get_all_vehicles

router = APIRouter(prefix="/charging")
templates = Jinja2Templates(directory="web/templates")


# ---------------------------------------------------------------------------
# Query helpers for tab-based review queue
# ---------------------------------------------------------------------------


async def _networks_context(
    db: AsyncSession,
    q: Optional[str] = None,
    filter: str = "all",
    sort: str = "name",
) -> dict:
    """Build context for the networks tab with search, filter, sort."""
    # --- counts for sub-filter badges (always unfiltered by q) ---
    total_count_result = await db.execute(select(func.count()).select_from(EVChargingNetwork))
    total_all = total_count_result.scalar() or 0

    unverified_count_result = await db.execute(
        select(func.count())
        .select_from(EVChargingNetwork)
        .where(EVChargingNetwork.is_verified == False)  # noqa: E712
    )
    total_unverified = unverified_count_result.scalar() or 0
    total_verified = total_all - total_unverified

    filter_counts = {
        "all": total_all,
        "unverified": total_unverified,
        "verified": total_verified,
    }

    # --- session counts per network (subquery) ---
    session_count_sub = (
        select(func.count())
        .where(EVChargingSession.network_id == EVChargingNetwork.id)
        .correlate(EVChargingNetwork)
        .scalar_subquery()
        .label("session_count")
    )

    # --- main query ---
    stmt = select(EVChargingNetwork, session_count_sub)

    # filter
    if filter == "unverified":
        stmt = stmt.where(EVChargingNetwork.is_verified == False)  # noqa: E712
    elif filter == "verified":
        stmt = stmt.where(EVChargingNetwork.is_verified == True)  # noqa: E712

    # search
    if q and q.strip():
        stmt = stmt.where(func.lower(EVChargingNetwork.network_name).contains(q.strip().lower()))

    # sort
    if sort == "sessions":
        stmt = stmt.order_by(session_count_sub.desc(), EVChargingNetwork.network_name)
    elif sort == "status":
        stmt = stmt.order_by(EVChargingNetwork.is_verified.asc(), EVChargingNetwork.network_name)
    else:  # default: name
        stmt = stmt.order_by(EVChargingNetwork.network_name)

    result = await db.execute(stmt)
    rows = result.all()

    networks = []
    net_session_counts: dict[int, int] = {}
    for row in rows:
        net = row[0]
        count = row[1] or 0
        networks.append(net)
        net_session_counts[net.id] = count

    # All networks for merge target dropdowns (plan 03)
    all_networks = await get_all_networks(db)

    return {
        "networks": networks,
        "net_session_counts": net_session_counts,
        "all_networks": all_networks,
        "filter_counts": filter_counts,
        "active_filter": filter,
        "current_q": q or "",
        "current_sort": sort,
    }


async def _locations_context(
    db: AsyncSession,
    q: Optional[str] = None,
    filter: str = "all",
    sort: str = "name",
) -> dict:
    """Build context for the locations tab with search, filter, sort."""
    # --- counts for sub-filter badges ---
    total_count_result = await db.execute(select(func.count()).select_from(EVLocationLookup))
    total_all = total_count_result.scalar() or 0

    unverified_count_result = await db.execute(
        select(func.count())
        .select_from(EVLocationLookup)
        .where(EVLocationLookup.is_verified == False)  # noqa: E712
    )
    total_unverified = unverified_count_result.scalar() or 0
    total_verified = total_all - total_unverified

    filter_counts = {
        "all": total_all,
        "unverified": total_unverified,
        "verified": total_verified,
    }

    # --- session counts per location (subquery) ---
    session_count_sub = (
        select(func.count())
        .where(EVChargingSession.location_id == EVLocationLookup.id)
        .correlate(EVLocationLookup)
        .scalar_subquery()
        .label("session_count")
    )

    # --- main query ---
    stmt = select(EVLocationLookup, session_count_sub)

    # filter
    if filter == "unverified":
        stmt = stmt.where(EVLocationLookup.is_verified == False)  # noqa: E712
    elif filter == "verified":
        stmt = stmt.where(EVLocationLookup.is_verified == True)  # noqa: E712

    # search
    if q and q.strip():
        stmt = stmt.where(func.lower(EVLocationLookup.location_name).contains(q.strip().lower()))

    # sort
    if sort == "sessions":
        stmt = stmt.order_by(session_count_sub.desc(), EVLocationLookup.location_name)
    elif sort == "status":
        stmt = stmt.order_by(EVLocationLookup.is_verified.asc(), EVLocationLookup.location_name)
    else:  # default: name
        stmt = stmt.order_by(EVLocationLookup.location_name)

    result = await db.execute(stmt)
    rows = result.all()

    locations = []
    loc_session_counts: dict[int, int] = {}
    for row in rows:
        loc = row[0]
        count = row[1] or 0
        locations.append(loc)
        loc_session_counts[loc.id] = count

    # All networks for dropdowns (edit form network select, merge targets)
    all_networks = await get_all_networks(db)

    # All locations for merge target dropdown (plan 03)
    all_locs_result = await db.execute(
        select(EVLocationLookup).order_by(EVLocationLookup.location_name)
    )
    all_locations = list(all_locs_result.scalars().all())

    return {
        "locations": locations,
        "loc_session_counts": loc_session_counts,
        "all_networks": all_networks,
        "all_locations": all_locations,
        "filter_counts": filter_counts,
        "active_filter": filter,
        "current_q": q or "",
        "current_sort": sort,
    }


# ---------------------------------------------------------------------------
# Full page + tab partial endpoints
# ---------------------------------------------------------------------------


@router.get("/review", response_class=HTMLResponse)
async def review_queue(
    request: Request,
    tab: str = "networks",
    q: Optional[str] = None,
    filter: str = "all",
    sort: str = "name",
    db: AsyncSession = Depends(get_db),
):
    """Review queue page with Networks/Locations tabs."""
    # Validate tab
    if tab not in ("networks", "locations"):
        tab = "networks"

    # Build context for the active tab
    if tab == "networks":
        tab_ctx = await _networks_context(db, q=q, filter=filter, sort=sort)
    else:
        tab_ctx = await _locations_context(db, q=q, filter=filter, sort=sort)

    # Total counts for tab badges (always unfiltered)
    net_count_result = await db.execute(select(func.count()).select_from(EVChargingNetwork))
    network_count = net_count_result.scalar() or 0

    loc_count_result = await db.execute(select(func.count()).select_from(EVLocationLookup))
    location_count = loc_count_result.scalar() or 0

    active_vehicle = await get_active_vehicle(db)
    all_vehicles = await get_all_vehicles(db)

    return templates.TemplateResponse(
        request,
        "charging/review_queue.html",
        {
            **tab_ctx,
            "active_tab": tab,
            "network_count": network_count,
            "location_count": location_count,
            "active_page": "review_queue",
            "page_title": "Review Queue",
            "active_vehicle": active_vehicle,
            "all_vehicles": all_vehicles,
        },
    )


@router.get("/review/networks", response_class=HTMLResponse)
async def review_networks(
    request: Request,
    q: Optional[str] = None,
    filter: str = "all",
    sort: str = "name",
    db: AsyncSession = Depends(get_db),
):
    """Networks tab partial -- used by HTMX tab clicks and search/filter/sort."""
    ctx = await _networks_context(db, q=q, filter=filter, sort=sort)
    return templates.TemplateResponse(
        request,
        "charging/partials/review_networks_table.html",
        ctx,
    )


@router.get("/review/locations", response_class=HTMLResponse)
async def review_locations(
    request: Request,
    q: Optional[str] = None,
    filter: str = "all",
    sort: str = "name",
    db: AsyncSession = Depends(get_db),
):
    """Locations tab partial -- used by HTMX tab clicks and search/filter/sort."""
    ctx = await _locations_context(db, q=q, filter=filter, sort=sort)
    return templates.TemplateResponse(
        request,
        "charging/partials/review_locations_table.html",
        ctx,
    )


@router.get("/review/table", response_class=HTMLResponse)
async def review_table(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Legacy endpoint -- redirects to networks tab partial for backwards compat."""
    ctx = await _networks_context(db)
    return templates.TemplateResponse(
        request,
        "charging/partials/review_networks_table.html",
        ctx,
    )


# ---------------------------------------------------------------------------
# Action endpoints (verify, edit, delete)
# ---------------------------------------------------------------------------


@router.post("/review/location/{location_id}/verify", response_class=HTMLResponse)
async def verify_location(
    location_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Mark a location as verified."""
    result = await db.execute(
        select(EVLocationLookup).where(EVLocationLookup.id == location_id)
    )
    loc = result.scalar_one_or_none()
    if loc:
        loc.is_verified = True
        loc.source_system = "manual"
        await db.commit()
    ctx = await _locations_context(db)
    return templates.TemplateResponse(
        request,
        "charging/partials/review_locations_table.html",
        ctx,
    )


@router.post("/review/network/{network_id}/verify", response_class=HTMLResponse)
async def verify_network(
    network_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Mark a network as verified."""
    result = await db.execute(
        select(EVChargingNetwork).where(EVChargingNetwork.id == network_id)
    )
    net = result.scalar_one_or_none()
    if net:
        net.is_verified = True
        net.source_system = "manual"
        await db.commit()
    ctx = await _networks_context(db)
    return templates.TemplateResponse(
        request,
        "charging/partials/review_networks_table.html",
        ctx,
    )


@router.post("/review/location/{location_id}/edit", response_class=HTMLResponse)
async def edit_location(
    location_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    location_name: str = Form(...),
    address: Optional[str] = Form(None),
    location_type: Optional[str] = Form(None),
    network_id: Optional[int] = Form(None),
    latitude: Optional[float] = Form(None),
    longitude: Optional[float] = Form(None),
    cost_per_kwh: Optional[float] = Form(None),
):
    """Edit a location."""
    result = await db.execute(
        select(EVLocationLookup).where(EVLocationLookup.id == location_id)
    )
    loc = result.scalar_one_or_none()
    if loc:
        loc.location_name = location_name
        loc.address = address or None
        loc.location_type = location_type or None
        loc.network_id = network_id or None
        loc.latitude = latitude
        loc.longitude = longitude
        loc.cost_per_kwh = cost_per_kwh
        await db.commit()
    ctx = await _locations_context(db)
    return templates.TemplateResponse(
        request,
        "charging/partials/review_locations_table.html",
        ctx,
    )


@router.post("/review/location/{location_id}/delete", response_class=HTMLResponse)
async def delete_location(
    location_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Delete an unverified location (safety check: only if is_verified=False)."""
    result = await db.execute(
        select(EVLocationLookup).where(
            EVLocationLookup.id == location_id,
            EVLocationLookup.is_verified == False,  # noqa: E712
        )
    )
    loc = result.scalar_one_or_none()
    if loc:
        await db.delete(loc)
        await db.commit()
    ctx = await _locations_context(db)
    return templates.TemplateResponse(
        request,
        "charging/partials/review_locations_table.html",
        ctx,
    )


@router.post("/review/network/{network_id}/delete", response_class=HTMLResponse)
async def delete_network(
    network_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Delete an unverified network (safety check: only if is_verified=False)."""
    result = await db.execute(
        select(EVChargingNetwork).where(
            EVChargingNetwork.id == network_id,
            EVChargingNetwork.is_verified == False,  # noqa: E712
        )
    )
    net = result.scalar_one_or_none()
    if net:
        await db.delete(net)
        await db.commit()
    ctx = await _networks_context(db)
    return templates.TemplateResponse(
        request,
        "charging/partials/review_networks_table.html",
        ctx,
    )
