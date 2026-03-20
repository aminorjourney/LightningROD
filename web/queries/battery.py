"""Battery analytics query layer and chart builders.

Provides SOC timeline, charge curve, and degradation trend data queries
with adaptive downsampling, plus Plotly chart builders for each.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.battery_status import EVBatteryStatus
from db.models.charging_session import EVChargingSession
from db.models.vehicle_status import EVVehicleStatus
from web.queries.dashboard import _HOVER_LABEL, _PLOTLY_CONFIG, _wrap_chart

# Module-level cache for reference charge curve JSON data
_CURVE_CACHE: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# Time filter
# ---------------------------------------------------------------------------


def build_battery_time_filter(range_str: str):
    """Return a SQLAlchemy where clause for EVBatteryStatus.recorded_at.

    Same logic as costs.build_time_filter but targets EVBatteryStatus.recorded_at.
    Returns None for 'all' (no filter).
    Accepts: '7d', '30d', '90d', 'ytd', '1y', 'all'
    """
    if not range_str or range_str == "all":
        return None

    now = datetime.now(timezone.utc)

    if range_str == "7d":
        cutoff = now - timedelta(days=7)
    elif range_str == "30d":
        cutoff = now - timedelta(days=30)
    elif range_str == "90d":
        cutoff = now - timedelta(days=90)
    elif range_str == "ytd":
        cutoff = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    elif range_str == "1y":
        cutoff = now - timedelta(days=365)
    else:
        return None

    return EVBatteryStatus.recorded_at >= cutoff


# ---------------------------------------------------------------------------
# Query functions
# ---------------------------------------------------------------------------


async def query_soc_timeline(
    db: AsyncSession,
    time_range: str = "7d",
    device_id: Optional[str] = None,
) -> list[dict]:
    """Query SOC timeline data with adaptive time-bucket downsampling.

    Uses SQL-level date_trunc downsampling for datasets >10k rows to avoid
    fetching all rows into Python. Falls back to pandas resampling for
    smaller datasets.

    Returns list of dicts with keys: recorded_at, soc, kw, range.
    Empty list when no data found.
    """
    time_filter = build_battery_time_filter(time_range)

    # Count rows first to decide downsampling strategy
    count_stmt = select(func.count()).select_from(EVBatteryStatus).where(
        EVBatteryStatus.hv_battery_soc.isnot(None)
    )
    if time_filter is not None:
        count_stmt = count_stmt.where(time_filter)
    if device_id:
        count_stmt = count_stmt.where(EVBatteryStatus.device_id == device_id)
    count_result = await db.execute(count_stmt)
    total_rows = count_result.scalar() or 0

    if total_rows == 0:
        return []

    # SQL-level downsampling for large datasets
    if total_rows > 10000:
        if total_rows > 50000:
            bucket = "6 hours"
        elif total_rows > 20000:
            bucket = "4 hours"
        else:
            bucket = "2 hours"

        bucket_col = func.date_trunc(bucket, EVBatteryStatus.recorded_at).label("bucket")
        stmt = (
            select(
                bucket_col,
                func.avg(EVBatteryStatus.hv_battery_soc).label("soc"),
                func.avg(EVBatteryStatus.hv_battery_kw).label("kw"),
                func.max(EVBatteryStatus.hv_battery_range).label("range"),
            )
            .where(EVBatteryStatus.hv_battery_soc.isnot(None))
            .group_by(bucket_col)
            .order_by(bucket_col)
        )
        if time_filter is not None:
            stmt = stmt.where(time_filter)
        if device_id:
            stmt = stmt.where(EVBatteryStatus.device_id == device_id)

        result = await db.execute(stmt)
        return [
            {
                "recorded_at": row.bucket,
                "soc": float(row.soc) if row.soc is not None else None,
                "kw": float(row.kw) if row.kw is not None else None,
                "range": float(row.range) if row.range is not None else None,
            }
            for row in result.all()
        ]

    # Fetch all rows for smaller datasets
    stmt = select(
        EVBatteryStatus.recorded_at,
        EVBatteryStatus.hv_battery_soc,
        EVBatteryStatus.hv_battery_kw,
        EVBatteryStatus.hv_battery_range,
    ).order_by(EVBatteryStatus.recorded_at)

    if time_filter is not None:
        stmt = stmt.where(time_filter)
    if device_id:
        stmt = stmt.where(EVBatteryStatus.device_id == device_id)

    result = await db.execute(stmt)
    rows = result.all()

    if not rows:
        return []

    df = pd.DataFrame(rows, columns=["recorded_at", "soc", "kw", "range"])
    df = df.dropna(subset=["soc"])

    if df.empty:
        return []

    # Adaptive Python-level downsampling for moderate datasets
    if len(df) > 800:
        df = df.set_index("recorded_at")
        if len(df) > 5000:
            bucket = "2h"
        elif len(df) > 2000:
            bucket = "1h"
        else:
            bucket = "30min"
        df = (
            df.resample(bucket)
            .agg({"soc": "mean", "kw": "mean", "range": "last"})
            .dropna(subset=["soc"])
            .reset_index()
        )

    return df.to_dict("records")


def detect_charging_regions(data: list[dict]) -> list[tuple[int, int]]:
    """Return list of (start_idx, end_idx) for charging periods.

    A charging period = consecutive rows where kW > threshold (positive = charging).
    """
    CHARGE_THRESHOLD_KW = 0.5
    regions: list[tuple[int, int]] = []
    in_charge = False
    start_idx = 0

    for i, row in enumerate(data):
        kw = row.get("kw") or 0
        # Charging = negative kW (power flowing into battery) OR positive above threshold
        if abs(float(kw)) > CHARGE_THRESHOLD_KW and float(kw) < 0:
            if not in_charge:
                start_idx = i
                in_charge = True
        else:
            if in_charge:
                regions.append((start_idx, i - 1))
                in_charge = False

    if in_charge:
        regions.append((start_idx, len(data) - 1))

    return regions


async def query_charge_curve(
    db: AsyncSession,
    session_id: int,
) -> dict:
    """Query battery status data during a charging session for charge curve.

    Returns dict with:
    - detailed: list of {soc, kw, timestamp} from battery_status (if available)
    - fallback: dict with start_soc, end_soc, charging_kw, max_power from session
    - session: the EVChargingSession object (or None)
    """
    session_result = await db.execute(
        select(EVChargingSession).where(EVChargingSession.id == session_id)
    )
    session = session_result.scalar_one_or_none()
    if not session:
        return {"detailed": [], "fallback": None, "session": None}

    detailed: list[dict] = []
    if session.session_start_utc and session.session_end_utc:
        stmt = (
            select(
                EVBatteryStatus.hv_battery_soc,
                EVBatteryStatus.hv_battery_kw,
                EVBatteryStatus.recorded_at,
            )
            .where(
                EVBatteryStatus.device_id == session.device_id,
                EVBatteryStatus.recorded_at >= session.session_start_utc,
                EVBatteryStatus.recorded_at <= session.session_end_utc,
                EVBatteryStatus.hv_battery_soc.isnot(None),
            )
            .order_by(EVBatteryStatus.recorded_at)
        )
        result = await db.execute(stmt)
        detailed = [
            {
                "soc": float(r.hv_battery_soc),
                "kw": float(r.hv_battery_kw or 0),
                "timestamp": r.recorded_at,
            }
            for r in result.all()
        ]

    fallback = {
        "start_soc": float(session.start_soc) if session.start_soc else None,
        "end_soc": float(session.end_soc) if session.end_soc else None,
        "charging_kw": float(session.charging_kw) if session.charging_kw else None,
        "max_power": float(session.max_power) if session.max_power else None,
    }

    return {"detailed": detailed, "fallback": fallback, "session": session}


async def query_degradation_data(
    db: AsyncSession,
    time_range: str = "all",
    device_id: Optional[str] = None,
) -> list[dict]:
    """Query daily max battery capacity for degradation trend.

    Returns list of dicts with keys: date, max_capacity.
    """
    date_col = cast(EVBatteryStatus.recorded_at, Date)

    stmt = (
        select(
            date_col.label("date"),
            func.max(EVBatteryStatus.hv_battery_capacity).label("max_capacity"),
        )
        .where(EVBatteryStatus.hv_battery_capacity.isnot(None))
        .group_by(date_col)
        .order_by(date_col)
    )

    time_filter = build_battery_time_filter(time_range)
    if time_filter is not None:
        stmt = stmt.where(time_filter)
    if device_id:
        stmt = stmt.where(EVBatteryStatus.device_id == device_id)

    result = await db.execute(stmt)
    return [
        {"date": row.date, "max_capacity": float(row.max_capacity)}
        for row in result.all()
    ]


async def query_recent_sessions_for_picker(
    db: AsyncSession,
    device_id: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """Query recent charging sessions for the charge curve session dropdown.

    Returns list of dicts with keys: id, session_start_utc, location_name, energy_kwh.
    """
    stmt = (
        select(
            EVChargingSession.id,
            EVChargingSession.session_start_utc,
            EVChargingSession.location_name,
            EVChargingSession.energy_kwh,
        )
        .order_by(EVChargingSession.session_start_utc.desc())
        .limit(limit)
    )

    if device_id:
        stmt = stmt.where(EVChargingSession.device_id == device_id)

    result = await db.execute(stmt)
    return [
        {
            "id": row.id,
            "session_start_utc": row.session_start_utc,
            "location_name": row.location_name,
            "energy_kwh": float(row.energy_kwh) if row.energy_kwh else None,
        }
        for row in result.all()
    ]


def load_reference_charge_curve(vehicle) -> dict | None:
    """Load reference charge curve JSON for a vehicle.

    Args:
        vehicle: EVVehicle object (or None).

    Returns:
        Parsed dict with name, battery_capacity_kwh, max_dc_kw, curve keys,
        or None if vehicle doesn't match a known preset.
    """
    if vehicle is None:
        return None
    make = getattr(vehicle, "make", None) or ""
    model = getattr(vehicle, "model", None) or ""
    if make.lower() != "ford" or "lightning" not in model.lower():
        return None

    # Determine variant from trim or battery capacity
    trim = (getattr(vehicle, "trim", None) or "").lower()
    cap = getattr(vehicle, "battery_capacity_kwh", None)

    if "extended" in trim or "er" == trim:
        filename = "f150_lightning_er.json"
    elif "standard" in trim or "sr" == trim:
        filename = "f150_lightning_sr.json"
    elif cap is not None and float(cap) >= 120:
        filename = "f150_lightning_er.json"
    else:
        filename = "f150_lightning_sr.json"

    if filename in _CURVE_CACHE:
        return _CURVE_CACHE[filename]

    curve_path = Path(__file__).parent.parent.parent / "data" / "charge_curves" / filename
    if not curve_path.exists():
        return None

    data = json.loads(curve_path.read_text())
    _CURVE_CACHE[filename] = data
    return data


async def query_degradation_by_mileage(
    db: AsyncSession,
    time_range: str = "all",
    device_id: Optional[str] = None,
) -> list[dict]:
    """Query daily max battery capacity correlated with odometer mileage.

    Joins ev_battery_status with ev_vehicle_status via pd.merge_asof on
    timestamp proximity (4h tolerance) to correlate capacity with mileage.

    Returns list of dicts: {odometer, max_capacity, date, recorded_at}.
    Empty list if no valid data after merge.
    """
    # Daily max capacity with latest timestamp per day
    date_col = cast(EVBatteryStatus.recorded_at, Date)
    cap_stmt = (
        select(
            date_col.label("date"),
            func.max(EVBatteryStatus.hv_battery_capacity).label("max_capacity"),
            func.max(EVBatteryStatus.recorded_at).label("latest_ts"),
        )
        .where(EVBatteryStatus.hv_battery_capacity.isnot(None))
        .group_by(date_col)
        .order_by(date_col)
    )

    time_filter = build_battery_time_filter(time_range)
    if time_filter is not None:
        cap_stmt = cap_stmt.where(time_filter)
    if device_id:
        cap_stmt = cap_stmt.where(EVBatteryStatus.device_id == device_id)

    cap_result = await db.execute(cap_stmt)
    cap_rows = cap_result.all()

    if not cap_rows:
        return []

    # Odometer readings
    odo_stmt = (
        select(EVVehicleStatus.recorded_at, EVVehicleStatus.odometer)
        .where(EVVehicleStatus.odometer.isnot(None))
        .order_by(EVVehicleStatus.recorded_at)
    )
    if device_id:
        odo_stmt = odo_stmt.where(EVVehicleStatus.device_id == device_id)

    odo_result = await db.execute(odo_stmt)
    odo_rows = odo_result.all()

    if not odo_rows:
        return []

    # Build DataFrames and merge on timestamp proximity
    cap_df = pd.DataFrame(
        [
            {
                "date": r.date,
                "max_capacity": float(r.max_capacity),
                "latest_ts": r.latest_ts,
            }
            for r in cap_rows
        ]
    )
    odo_df = pd.DataFrame(
        [
            {"recorded_at": r.recorded_at, "odometer": float(r.odometer)}
            for r in odo_rows
        ]
    )

    # Ensure timezone-aware timestamps for merge
    cap_df["latest_ts"] = pd.to_datetime(cap_df["latest_ts"], utc=True)
    odo_df["recorded_at"] = pd.to_datetime(odo_df["recorded_at"], utc=True)

    cap_df = cap_df.sort_values("latest_ts")
    odo_df = odo_df.sort_values("recorded_at")

    merged = pd.merge_asof(
        cap_df,
        odo_df,
        left_on="latest_ts",
        right_on="recorded_at",
        tolerance=pd.Timedelta("4h"),
        direction="nearest",
    )

    merged = merged.dropna(subset=["odometer"])

    if merged.empty:
        return []

    return [
        {
            "odometer": float(row["odometer"]),
            "max_capacity": float(row["max_capacity"]),
            "date": row["date"],
            "recorded_at": row["latest_ts"],
        }
        for _, row in merged.iterrows()
    ]


async def query_lv_battery_timeline(
    db: AsyncSession,
    time_range: str = "7d",
    device_id: Optional[str] = None,
) -> list[dict]:
    """Query 12v battery voltage and level timeline with adaptive downsampling.

    Returns list of dicts: {recorded_at, voltage, level}.
    """
    stmt = (
        select(
            EVBatteryStatus.recorded_at,
            EVBatteryStatus.lv_battery_voltage,
            EVBatteryStatus.lv_battery_level,
        )
        .where(EVBatteryStatus.lv_battery_voltage.isnot(None))
        .order_by(EVBatteryStatus.recorded_at)
    )

    time_filter = build_battery_time_filter(time_range)
    if time_filter is not None:
        stmt = stmt.where(time_filter)
    if device_id:
        stmt = stmt.where(EVBatteryStatus.device_id == device_id)

    result = await db.execute(stmt)
    rows = result.all()

    if not rows:
        return []

    df = pd.DataFrame(rows, columns=["recorded_at", "voltage", "level"])

    # Adaptive downsampling matching SOC timeline thresholds
    if len(df) > 800:
        df = df.set_index("recorded_at")
        if len(df) > 5000:
            bucket = "2h"
        elif len(df) > 2000:
            bucket = "1h"
        else:
            bucket = "30min"
        df = (
            df.resample(bucket)
            .agg({"voltage": "mean", "level": "mean"})
            .dropna(subset=["voltage"])
            .reset_index()
        )

    return [
        {
            "recorded_at": row["recorded_at"],
            "voltage": float(row["voltage"]) if pd.notna(row["voltage"]) else None,
            "level": float(row["level"]) if pd.notna(row["level"]) else None,
        }
        for _, row in df.iterrows()
    ]


async def query_average_charge_curve(
    db: AsyncSession,
    device_id: Optional[str] = None,
) -> list[dict]:
    """Compute average kW per 2% SOC bucket across all charging sessions.

    Charging is detected as hv_battery_kw < -0.5 (negative = power into battery).
    Returns list of dicts: {soc, kw} sorted by soc ascending.
    """
    stmt = select(
        EVBatteryStatus.hv_battery_soc,
        EVBatteryStatus.hv_battery_kw,
    ).where(
        EVBatteryStatus.hv_battery_soc.isnot(None),
        EVBatteryStatus.hv_battery_kw < -0.5,
    )

    if device_id:
        stmt = stmt.where(EVBatteryStatus.device_id == device_id)

    result = await db.execute(stmt)
    rows = result.all()

    if not rows:
        return []

    df = pd.DataFrame(rows, columns=["soc", "kw"])
    df["soc"] = df["soc"].astype(float)
    df["kw"] = df["kw"].astype(float).abs()

    # Create 2% SOC buckets
    df["soc_bucket"] = (df["soc"] / 2).round() * 2

    avg_by_bucket = df.groupby("soc_bucket")["kw"].mean().reset_index()
    avg_by_bucket = avg_by_bucket.sort_values("soc_bucket")

    return [
        {"soc": float(row["soc_bucket"]), "kw": float(row["kw"])}
        for _, row in avg_by_bucket.iterrows()
    ]


# ---------------------------------------------------------------------------
# Chart builders
# ---------------------------------------------------------------------------


def build_soc_timeline_chart(
    data: list[dict], charging_regions: list[tuple[int, int]]
) -> str:
    """Build SOC timeline Plotly chart with color-coded charging regions.

    Returns HTML string. Empty string if no data.
    """
    if not data:
        return ""

    pio.templates.default = "plotly_dark"
    fig = go.Figure()

    timestamps = [row["recorded_at"] for row in data]
    soc_values = [row.get("soc") for row in data]
    kw_values = [row.get("kw") for row in data]
    range_values = [row.get("range") for row in data]

    # Build rich tooltip text
    hover_texts = []
    for i, row in enumerate(data):
        ts = row["recorded_at"]
        ts_str = ts.strftime("%b %d, %Y %H:%M") if hasattr(ts, "strftime") else str(ts)
        soc = row.get("soc")
        kw = row.get("kw")
        rng = row.get("range")
        parts = [f"<b>{ts_str}</b>"]
        if soc is not None:
            parts.append(f"SOC: {soc:.1f}%")
        if kw is not None:
            parts.append(f"Power: {kw:.1f} kW")
        if rng is not None:
            parts.append(f"Range: {rng:.0f} mi")
        hover_texts.append("<br>".join(parts))

    # Main SOC trace — connectgaps=False to show data gaps as breaks
    fig.add_trace(
        go.Scatter(
            x=timestamps,
            y=soc_values,
            mode="lines",
            name="SOC %",
            line=dict(color="#47A8E5", width=2),
            connectgaps=False,
            hovertext=hover_texts,
            hoverinfo="text",
        )
    )

    # Color-coded charging regions as vertical rectangles
    for start_idx, end_idx in charging_regions:
        if start_idx < len(timestamps) and end_idx < len(timestamps):
            fig.add_vrect(
                x0=timestamps[start_idx],
                x1=timestamps[end_idx],
                fillcolor="rgba(74, 222, 128, 0.15)",
                layer="below",
                line_width=0,
                annotation_text="Charging",
                annotation_position="top left",
                annotation_font_size=10,
                annotation_font_color="rgba(74, 222, 128, 0.6)",
            )

    fig.update_layout(
        height=350,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font_color="#e5e7eb",
        margin=dict(l=20, r=20, t=20, b=20),
        xaxis=dict(title=""),
        yaxis=dict(title="SOC %", range=[0, 100]),
        showlegend=False,
        hovermode="x unified",
        hoverlabel=_HOVER_LABEL,
    )

    return _wrap_chart(
        fig.to_html(full_html=False, include_plotlyjs=False, config=_PLOTLY_CONFIG)
    )


def build_charge_curve_chart(data: dict) -> str:
    """Build charge curve chart (dual-axis: SOC% + kW vs time).

    Accepts the dict from query_charge_curve. Uses detailed data if >= 3 points,
    otherwise falls back to session-level start/end SOC.
    Returns HTML string. Empty string if no data at all.
    """
    if not data or not data.get("session"):
        return ""

    detailed = data.get("detailed", [])
    fallback = data.get("fallback", {})

    pio.templates.default = "plotly_dark"

    if detailed and len(detailed) >= 3:
        # Detailed dual-axis chart
        from plotly.subplots import make_subplots

        fig = make_subplots(specs=[[{"secondary_y": True}]])

        timestamps = [d["timestamp"] for d in detailed]
        soc_vals = [d["soc"] for d in detailed]
        kw_vals = [d["kw"] for d in detailed]

        # SOC% on primary y-axis
        fig.add_trace(
            go.Scatter(
                x=timestamps,
                y=soc_vals,
                mode="lines+markers",
                name="SOC %",
                line=dict(color="#47A8E5", width=2),
                marker=dict(size=5),
                hovertemplate="<b>%{x|%H:%M}</b><br>SOC: %{y:.1f}%<extra></extra>",
            ),
            secondary_y=False,
        )

        # kW on secondary y-axis
        fig.add_trace(
            go.Scatter(
                x=timestamps,
                y=kw_vals,
                mode="lines",
                name="Power (kW)",
                line=dict(color="#f97316", width=1.5),
                fill="tozeroy",
                fillcolor="rgba(249, 115, 22, 0.1)",
                hovertemplate="<b>%{x|%H:%M}</b><br>Power: %{y:.1f} kW<extra></extra>",
            ),
            secondary_y=True,
        )

        fig.update_yaxes(title_text="SOC %", range=[0, 100], secondary_y=False)
        fig.update_yaxes(title_text="kW", secondary_y=True, showgrid=False)

    elif fallback and (fallback.get("start_soc") is not None or fallback.get("end_soc") is not None):
        # Fallback: simple two-point chart
        fig = go.Figure()

        start_soc = fallback.get("start_soc") or 0
        end_soc = fallback.get("end_soc") or 0
        x_labels = ["Start", "End"]

        fig.add_trace(
            go.Scatter(
                x=x_labels,
                y=[start_soc, end_soc],
                mode="lines+markers+text",
                name="SOC %",
                line=dict(color="#47A8E5", width=3),
                marker=dict(size=10),
                text=[f"{start_soc:.0f}%", f"{end_soc:.0f}%"],
                textposition="top center",
                textfont=dict(color="#e5e7eb"),
                hoverinfo="skip",
            )
        )

        # Annotate power metrics
        annotations = []
        if fallback.get("charging_kw"):
            annotations.append(f"Avg: {fallback['charging_kw']:.1f} kW")
        if fallback.get("max_power"):
            annotations.append(f"Max: {fallback['max_power']:.1f} kW")
        if annotations:
            fig.add_annotation(
                x=0.5,
                y=0.95,
                xref="paper",
                yref="paper",
                text=" | ".join(annotations),
                showarrow=False,
                font=dict(color="#9ca3af", size=12),
            )

        fig.update_layout(yaxis=dict(title="SOC %", range=[0, 100]))
    else:
        return ""

    fig.update_layout(
        height=350,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font_color="#e5e7eb",
        margin=dict(l=20, r=20, t=30, b=20),
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="x unified",
        hoverlabel=_HOVER_LABEL,
    )

    return _wrap_chart(
        fig.to_html(full_html=False, include_plotlyjs=False, config=_PLOTLY_CONFIG)
    )


def build_degradation_chart(data: list[dict], rated_capacity_kwh: float) -> str:
    """Build battery degradation trend chart with reference line and projection.

    Y-axis: percentage of rated capacity. Includes linear trend with 90-day projection.
    Returns HTML string. Empty string if no data.
    """
    if not data or rated_capacity_kwh <= 0:
        return ""

    pio.templates.default = "plotly_dark"
    fig = go.Figure()

    dates = [row["date"] for row in data]
    capacities = [row["max_capacity"] for row in data]
    pct_values = [(c / rated_capacity_kwh) * 100 for c in capacities]

    # Scatter points for daily max capacity
    hover_texts = []
    for i, row in enumerate(data):
        d = row["date"]
        d_str = d.strftime("%b %d, %Y") if hasattr(d, "strftime") else str(d)
        hover_texts.append(
            f"<b>{d_str}</b><br>"
            f"Capacity: {capacities[i]:.1f} kWh<br>"
            f"Health: {pct_values[i]:.1f}%"
        )

    fig.add_trace(
        go.Scatter(
            x=dates,
            y=pct_values,
            mode="markers",
            name="Daily Max Capacity",
            marker=dict(color="#47A8E5", size=6),
            hovertext=hover_texts,
            hoverinfo="text",
        )
    )

    # 100% reference line
    fig.add_hline(
        y=100,
        line_dash="dash",
        line_color="rgba(156, 163, 175, 0.5)",
        annotation_text="Rated Capacity",
        annotation_position="top right",
        annotation_font_color="#9ca3af",
    )

    # Linear trend + forward projection (90 days)
    if len(data) >= 2:
        # Convert dates to numeric (days since first reading)
        first_date = dates[0]
        x_numeric = np.array(
            [(d - first_date).days if hasattr(d, "__sub__") else 0 for d in dates],
            dtype=float,
        )
        y_pct = np.array(pct_values, dtype=float)

        coeffs = np.polyfit(x_numeric, y_pct, 1)
        slope, intercept = coeffs

        # Trend line over data range
        trend_y = slope * x_numeric + intercept

        fig.add_trace(
            go.Scatter(
                x=dates,
                y=trend_y.tolist(),
                mode="lines",
                name="Trend",
                line=dict(color="#facc15", width=2, dash="dash"),
                hoverinfo="skip",
            )
        )

        # Project forward ~90 days
        last_day = x_numeric[-1]
        proj_days = np.arange(last_day, last_day + 91, 1)
        proj_y = slope * proj_days + intercept

        # Build projected dates
        from datetime import date as date_type

        if hasattr(first_date, "year"):
            proj_dates = [
                first_date + timedelta(days=int(d)) for d in proj_days
            ]
        else:
            proj_dates = list(proj_days)

        fig.add_trace(
            go.Scatter(
                x=proj_dates,
                y=proj_y.tolist(),
                mode="lines",
                name="Projection",
                line=dict(color="#facc15", width=1.5, dash="dot"),
                opacity=0.5,
                hoverinfo="skip",
            )
        )

    fig.update_layout(
        height=350,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font_color="#e5e7eb",
        margin=dict(l=20, r=20, t=20, b=20),
        xaxis=dict(title=""),
        yaxis=dict(title="% of Rated Capacity"),
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="x unified",
        hoverlabel=_HOVER_LABEL,
    )

    return _wrap_chart(
        fig.to_html(full_html=False, include_plotlyjs=False, config=_PLOTLY_CONFIG)
    )


def build_mini_charge_curve(session) -> str:
    """Build compact charge curve for session drawer (no axes, minimal chrome).

    Returns HTML string. Empty string if no SOC data on session.
    """
    if not session:
        return ""
    if not getattr(session, "start_soc", None) and not getattr(session, "end_soc", None):
        return ""

    pio.templates.default = "plotly_dark"
    fig = go.Figure()

    x = [0, 1]
    y = [float(session.start_soc or 0), float(session.end_soc or 0)]

    fig.add_trace(
        go.Scatter(
            x=x,
            y=y,
            mode="lines+markers",
            line=dict(color="#47A8E5", width=2),
            marker=dict(size=6),
            hoverinfo="skip",
        )
    )

    fig.update_layout(
        height=60,
        margin=dict(l=0, r=0, t=0, b=0),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(visible=False),
        yaxis=dict(visible=False, range=[0, 100]),
        showlegend=False,
    )

    return fig.to_html(
        full_html=False,
        include_plotlyjs=False,
        config={"displayModeBar": False, "staticPlot": True},
    )
