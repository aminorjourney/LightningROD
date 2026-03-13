"""Seed sample vehicle, battery status, and charging sessions into PostgreSQL.

Creates a sample 2024 F-150 Lightning SR XLT, sets it as the active vehicle,
then seeds correlated battery telemetry and charging session data so that
battery snapshots align with session records for testing.

Usage:
    uv run python scripts/seed_sample.py
    uv run python scripts/seed_sample.py --dry-run
    uv run python scripts/seed_sample.py --battery-only
    uv run python scripts/seed_sample.py --sessions-only
    uv run python scripts/seed_sample.py --device-id CUSTOM_ID
"""

import argparse
import asyncio
import csv
import hashlib
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from db.engine import AsyncSessionLocal
from db.models.battery_status import EVBatteryStatus
from db.models.charging_session import EVChargingSession
from db.models.vehicle import EVVehicle

SOURCE_SYSTEM = "sample_generator"

# Sample vehicle definition
SAMPLE_VEHICLE = {
    "display_name": "F-150 Lightning SR",
    "make": "Ford",
    "model": "F-150 Lightning",
    "year": 2024,
    "trim": "XLT",
    "battery_capacity_kwh": 98.0,
    "vin": "1FT8W3ED5LFB0D19",
    "source_system": SOURCE_SYSTEM,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def float_or_none(v: str) -> Optional[float]:
    v = v.strip() if v else ""
    if not v:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def str_or_none(v: str) -> Optional[str]:
    v = v.strip() if v else ""
    return v if v else None


def int_or_none(v: str) -> Optional[int]:
    v = v.strip() if v else ""
    if not v:
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


def parse_timestamp(v: str) -> Optional[datetime]:
    v = v.strip() if v else ""
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def parse_uuid(v: str) -> Optional[uuid.UUID]:
    v = v.strip() if v else ""
    if not v:
        return None
    try:
        return uuid.UUID(v)
    except (ValueError, AttributeError):
        return None


def parse_bool(v: str) -> bool:
    return v.strip().lower() in ("true", "1", "yes") if v else False


# ---------------------------------------------------------------------------
# Battery status transform
# ---------------------------------------------------------------------------

def transform_battery_row(csv_row: dict, device_id: str) -> Optional[dict]:
    db_row = {
        "recorded_at": parse_timestamp(csv_row.get("recorded_at", "")),
        "hv_battery_soc": float_or_none(csv_row.get("hv_battery_soc", "")),
        "hv_battery_actual_soc": float_or_none(csv_row.get("hv_battery_actual_soc", "")),
        "hv_battery_voltage": float_or_none(csv_row.get("hv_battery_voltage", "")),
        "hv_battery_amperage": float_or_none(csv_row.get("hv_battery_amperage", "")),
        "hv_battery_kw": float_or_none(csv_row.get("hv_battery_kw", "")),
        "hv_battery_capacity": float_or_none(csv_row.get("hv_battery_capacity", "")),
        "hv_battery_range": float_or_none(csv_row.get("hv_battery_range", "")),
        "hv_battery_max_range": float_or_none(csv_row.get("hv_battery_max_range", "")),
        "hv_battery_temperature": float_or_none(csv_row.get("hv_battery_temperature", "")),
        "lv_battery_level": float_or_none(csv_row.get("lv_battery_level", "")),
        "lv_battery_voltage": float_or_none(csv_row.get("lv_battery_voltage", "")),
        "motor_voltage": float_or_none(csv_row.get("motor_voltage", "")),
        "motor_amperage": float_or_none(csv_row.get("motor_amperage", "")),
        "motor_kw": float_or_none(csv_row.get("motor_kw", "")),
        "performance_status": str_or_none(csv_row.get("performance_status", "")),
        "device_id": device_id,
        "source_system": SOURCE_SYSTEM,
    }
    if db_row["recorded_at"] is None:
        return None
    db_row["original_timestamp"] = db_row["recorded_at"]
    return db_row


# ---------------------------------------------------------------------------
# Charging session transform
# ---------------------------------------------------------------------------

def transform_session_row(csv_row: dict, device_id: str) -> Optional[dict]:
    session_start = parse_timestamp(csv_row.get("session_start_utc", ""))
    energy_kwh = float_or_none(csv_row.get("energy_kwh", ""))

    if session_start is None and energy_kwh is None:
        return None

    session_id = parse_uuid(csv_row.get("session_id", ""))
    if session_id is None:
        # Generate deterministic UUID
        loc = csv_row.get("location_name", "")
        key = f"{session_start.isoformat() if session_start else ''}|{loc}|{energy_kwh or ''}"
        session_id = uuid.UUID(bytes=hashlib.md5(key.encode()).digest())

    is_free_raw = csv_row.get("is_free", "")
    is_free = None
    if is_free_raw.strip():
        is_free = is_free_raw.strip().lower() in ("true", "1", "yes")

    db_row = {
        "session_id": session_id,
        "device_id": device_id,
        "charge_type": str_or_none(csv_row.get("charge_type", "")),
        "location_name": str_or_none(csv_row.get("location_name", "")),
        "location_type": str_or_none(csv_row.get("location_type", "")),
        "is_free": is_free,
        "charging_voltage": float_or_none(csv_row.get("charging_voltage", "")),
        "charging_amperage": float_or_none(csv_row.get("charging_amperage", "")),
        "charging_kw": float_or_none(csv_row.get("charging_kw", "")),
        "session_start_utc": session_start,
        "session_end_utc": parse_timestamp(csv_row.get("session_end_utc", "")),
        "recorded_at": parse_timestamp(csv_row.get("recorded_at", "")),
        "charge_duration_seconds": float_or_none(csv_row.get("charge_duration_seconds", "")),
        "start_soc": float_or_none(csv_row.get("start_soc", "")),
        "end_soc": float_or_none(csv_row.get("end_soc", "")),
        "energy_kwh": energy_kwh,
        "cost": float_or_none(csv_row.get("cost", "")),
        "cost_without_overrides": float_or_none(csv_row.get("cost_without_overrides", "")),
        "is_complete": parse_bool(csv_row.get("is_complete", "True")),
        "location_id": int_or_none(csv_row.get("location_id", "")),
        "address": str_or_none(csv_row.get("location_address", "")),
        "latitude": float_or_none(csv_row.get("latitude", "")),
        "longitude": float_or_none(csv_row.get("longitude", "")),
        "max_power": float_or_none(csv_row.get("max_power", "")),
        "min_power": float_or_none(csv_row.get("min_power", "")),
        "miles_added": float_or_none(csv_row.get("miles_added", "")),
        "evse_voltage": float_or_none(csv_row.get("evse_voltage", "")),
        "evse_amperage": float_or_none(csv_row.get("evse_amperage", "")),
        "evse_kw": float_or_none(csv_row.get("evse_kw", "")),
        "evse_energy_kwh": float_or_none(csv_row.get("evse_energy_kwh", "")),
        "evse_max_power_kw": float_or_none(csv_row.get("evse_max_power_kw", "")),
        "evse_source": str_or_none(csv_row.get("evse_source", "")),
        "source_system": SOURCE_SYSTEM,
    }
    return db_row


# Session columns to update on upsert conflict
SESSION_UPDATABLE = [
    "device_id", "charge_type", "location_name", "location_type", "is_free",
    "session_start_utc", "session_end_utc", "charge_duration_seconds",
    "energy_kwh", "charging_kw", "max_power", "min_power", "start_soc", "end_soc",
    "cost", "cost_without_overrides", "miles_added", "charging_voltage",
    "charging_amperage", "is_complete", "recorded_at", "source_system",
    "location_id", "address", "latitude", "longitude",
    "evse_voltage", "evse_amperage", "evse_kw", "evse_energy_kwh",
    "evse_max_power_kw", "evse_source",
]


# ---------------------------------------------------------------------------
# Seed logic
# ---------------------------------------------------------------------------

async def seed_vehicle(device_id: str, dry_run: bool) -> None:
    """Create or update the sample vehicle record."""
    print(f"\n{'='*60}")
    print(f"  SAMPLE VEHICLE")
    print(f"{'='*60}")

    vehicle_data = {**SAMPLE_VEHICLE, "device_id": device_id}
    print(f"  {vehicle_data['year']} {vehicle_data['make']} {vehicle_data['model']} {vehicle_data['trim']}")
    print(f"  VIN: {vehicle_data['vin']}")
    print(f"  Device ID: {device_id}")
    print(f"  Battery: {vehicle_data['battery_capacity_kwh']} kWh")

    if dry_run:
        print(f"  [DRY RUN] Would create/update vehicle")
        return

    async with AsyncSessionLocal() as session:
        # Check if vehicle already exists by device_id
        result = await session.execute(
            select(EVVehicle).where(EVVehicle.device_id == device_id)
        )
        existing = result.scalar_one_or_none()

        if existing:
            # Update existing vehicle
            for key, value in vehicle_data.items():
                if key != "device_id":
                    setattr(existing, key, value)
            print(f"  Updated existing vehicle (id={existing.id})")
        else:
            # Check for VIN conflict (different device_id but same VIN)
            vin_result = await session.execute(
                select(EVVehicle).where(EVVehicle.vin == vehicle_data["vin"])
            )
            vin_existing = vin_result.scalar_one_or_none()
            if vin_existing:
                # Update the existing VIN record to use our device_id
                for key, value in vehicle_data.items():
                    setattr(vin_existing, key, value)
                print(f"  Updated existing vehicle with matching VIN (id={vin_existing.id})")
            else:
                vehicle = EVVehicle(**vehicle_data)
                session.add(vehicle)
                print(f"  Created new vehicle")

        try:
            await session.commit()
        except IntegrityError as e:
            await session.rollback()
            print(f"  WARNING: Could not create vehicle: {e}")

    # Set as active vehicle
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(EVVehicle).where(EVVehicle.device_id == device_id)
        )
        vehicle = result.scalar_one_or_none()
        if vehicle:
            from web.queries.settings import set_app_setting
            await set_app_setting(session, "active_vehicle_id", str(vehicle.id))
            print(f"  Set as active vehicle (id={vehicle.id})")


async def seed_battery(device_id: str, csv_path: str, dry_run: bool) -> int:
    print(f"\n{'='*60}")
    print(f"  BATTERY STATUS")
    print(f"{'='*60}")
    print(f"  CSV: {csv_path}")

    with open(csv_path, newline="", encoding="utf-8") as f:
        raw_rows = list(csv.DictReader(f))
    print(f"  Loaded {len(raw_rows)} rows")

    transformed = []
    for raw_row in raw_rows:
        db_row = transform_battery_row(raw_row, device_id)
        if db_row:
            transformed.append(db_row)
    print(f"  Transformed {len(transformed)} rows")

    if dry_run:
        print(f"  [DRY RUN] Would insert {len(transformed)} rows")
        return len(transformed)

    batch_size = 500
    async with AsyncSessionLocal() as session:
        # Clear existing sample data
        result = await session.execute(
            text("DELETE FROM ev_battery_status WHERE source_system = :src AND device_id = :did"),
            {"src": SOURCE_SYSTEM, "did": device_id},
        )
        print(f"  Cleared {result.rowcount} existing sample rows")

        for i in range(0, len(transformed), batch_size):
            batch = transformed[i : i + batch_size]
            await session.execute(pg_insert(EVBatteryStatus).values(batch))

        await session.commit()
    print(f"  Inserted {len(transformed)} rows")
    return len(transformed)


async def seed_sessions(device_id: str, csv_path: str, dry_run: bool) -> int:
    print(f"\n{'='*60}")
    print(f"  CHARGING SESSIONS")
    print(f"{'='*60}")
    print(f"  CSV: {csv_path}")

    with open(csv_path, newline="", encoding="utf-8") as f:
        raw_rows = list(csv.DictReader(f))
    print(f"  Loaded {len(raw_rows)} rows")

    transformed = []
    for raw_row in raw_rows:
        db_row = transform_session_row(raw_row, device_id)
        if db_row:
            transformed.append(db_row)
    print(f"  Transformed {len(transformed)} rows")

    if dry_run:
        print(f"  [DRY RUN] Would upsert {len(transformed)} rows")
        return len(transformed)

    async with AsyncSessionLocal() as session:
        # Upsert by session_id
        stmt = pg_insert(EVChargingSession).values(transformed)
        stmt = stmt.on_conflict_do_update(
            index_elements=["session_id"],
            set_={col: stmt.excluded[col] for col in SESSION_UPDATABLE},
        )
        await session.execute(stmt)
        await session.commit()
    print(f"  Upserted {len(transformed)} rows")
    return len(transformed)


async def verify(device_id: str):
    print(f"\n{'='*60}")
    print(f"  VERIFICATION")
    print(f"{'='*60}")

    async with AsyncSessionLocal() as session:
        # Battery stats
        result = await session.execute(
            text("""
                SELECT COUNT(*) AS total,
                       MIN(recorded_at) AS earliest,
                       MAX(recorded_at) AS latest,
                       ROUND(AVG(hv_battery_soc)::numeric, 1) AS avg_soc,
                       COUNT(*) FILTER (WHERE hv_battery_kw < -1) AS charging,
                       COUNT(*) FILTER (WHERE motor_kw > 1) AS driving
                FROM ev_battery_status WHERE device_id = :did
            """),
            {"did": device_id},
        )
        b = result.fetchone()

        # Session stats
        result = await session.execute(
            text("""
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE charge_type = 'AC') AS ac,
                       COUNT(*) FILTER (WHERE charge_type = 'DC') AS dc,
                       ROUND(SUM(energy_kwh)::numeric, 1) AS total_kwh,
                       ROUND(SUM(cost)::numeric, 2) AS total_cost,
                       COUNT(*) FILTER (WHERE location_type = 'home') AS home,
                       COUNT(*) FILTER (WHERE location_type = 'work') AS work
                FROM ev_charging_session WHERE device_id = :did
            """),
            {"did": device_id},
        )
        s = result.fetchone()

    print(f"\n  Battery Status:")
    print(f"    Total rows:   {b.total}")
    print(f"    Date range:   {str(b.earliest)[:10]} to {str(b.latest)[:10]}")
    print(f"    Avg SOC:      {b.avg_soc}%")
    print(f"    Charging:     {b.charging} snapshots")
    print(f"    Driving:      {b.driving} snapshots")

    print(f"\n  Charging Sessions:")
    print(f"    Total:        {s.total}")
    print(f"    AC: {s.ac} | DC: {s.dc}")
    print(f"    Home: {s.home} | Work: {s.work} | Other: {s.total - s.home - s.work}")
    print(f"    Total energy: {s.total_kwh} kWh")
    print(f"    Total cost:   ${s.total_cost}")


async def seed(args: argparse.Namespace):
    device_id = args.device_id
    dry_run = args.dry_run
    data_dir = Path("data")

    battery_csv = str(data_dir / "battery_status_sample.csv")
    sessions_csv = str(data_dir / "charging_sessions_sample.csv")

    print(f"\n  Device ID: {device_id}")
    print(f"  Mode: {'DRY RUN' if dry_run else 'LIVE'}")

    # Always create/update the sample vehicle first
    await seed_vehicle(device_id, dry_run)

    if not args.sessions_only:
        await seed_battery(device_id, battery_csv, dry_run)

    if not args.battery_only:
        await seed_sessions(device_id, sessions_csv, dry_run)

    if not dry_run:
        await verify(device_id)

    print(f"\n  Done.")


def main():
    parser = argparse.ArgumentParser(
        description="Seed correlated battery + charging sample data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run python scripts/seed_sample.py
  uv run python scripts/seed_sample.py --dry-run
  uv run python scripts/seed_sample.py --sessions-only
  uv run python scripts/seed_sample.py --device-id CUSTOM_VIN
        """,
    )
    parser.add_argument("--device-id", default="1FT8W3ED5LFB0D19", required=False, help="Device ID for all rows. Defaults to sample Vin: 1FT8W3ED5LFB0D19")
    parser.add_argument("--dry-run", action="store_true", help="Transform but don't write")
    parser.add_argument("--battery-only", action="store_true", help="Only seed battery status")
    parser.add_argument("--sessions-only", action="store_true", help="Only seed charging sessions")
    args = parser.parse_args()
    asyncio.run(seed(args))


if __name__ == "__main__":
    main()
