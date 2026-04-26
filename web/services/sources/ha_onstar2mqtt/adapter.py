"""ha-onstar2mqtt source adapter.

Ingestion adapter for the BigThunderSR/onstar2mqtt Home Assistant add-on.
Handles GM vehicles (Chevrolet Bolt, Blazer EV, etc.) connected to HA via
onstar2mqtt's MQTT auto-discovery.

### Entity naming convention
onstar2mqtt publishes entities with the pattern:

    sensor.{ha_entity_prefix}_{slug}

where `ha_entity_prefix` is derived from the VEHICLE_NAME config option
(e.g. "2017 Chevrolet Bolt EV" -> "2017_chevrolet_bolt_ev").

This prefix is stored per-vehicle in ev_vehicles.ha_entity_prefix and used
at runtime to match incoming HA state_changed events.

### Unit strategy: always use km entities
onstar2mqtt publishes dual-unit distance sensors — both km and mi variants.
For example:
  - sensor.*_ev_range          (km, unit_of_measurement="km")
  - sensor.*_ev_range_mi       (mi, unit_of_measurement="mi")
  - sensor.*_odo_read          (km)
  - sensor.*_odo_read_mi       (mi)

This adapter always reads the km variants directly. No unit detection or
per-event UoM resolution is needed for distance fields.

Temperature is always °C (onstar2mqtt publishes _f suffix variants for °F).
Energy is always kWh. Battery SoC is always %.

### Data sources mapped to DB tables

ev_battery_status:
  - sensor.*_ev_range           -> hv_battery_range (km)
  - sensor.*_ev_max_range       -> hv_battery_max_range (km)
  - sensor.*_charge_state       -> hv_battery_soc (%)
  - sensor.*_hybrid_battery_minimum_temperature -> hv_battery_temperature (°C)

ev_vehicle_status (odometer):
  - sensor.*_odo_read           -> odometer (km)

ev_location:
  - device_tracker.*            -> latitude, longitude

Charging session tracking requires a charge start/end event pair. The
binary_sensor.*_ev_charge_state is used to detect session boundaries.
Charging sessions are approximated from SoC delta at plug events.

### What is NOT available from onstar2mqtt (vs FordPass)
- Real-time power (kW) during charging — not exposed
- Per-trip distance/energy from onstar API (no events entity equivalent)
- Charging energy per session (kWh) — not directly available
- Charger type (L1/L2/DC) — not exposed by onstar2mqtt diagnostics

These gaps mean ev_trip_metrics and ev_charging_session will be partially
populated for onstar2mqtt vehicles compared to FordPass vehicles.
"""

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from web.services.units.contracts import FieldContract
from web.services.units.to_metric import UnknownSourceUnit, to_metric

logger = logging.getLogger("lightningrod.sources.ha_onstar2mqtt")

INGEST_SCHEMA_VERSION = 2

# ---------------------------------------------------------------------------
# FIELD_CONTRACTS registry
# ---------------------------------------------------------------------------
# onstar2mqtt always uses km for distance and °C for temperature.
# We read the km/°C entity variants directly — source_unit is always declared,
# never resolved at read-time.

FIELD_CONTRACTS: list[FieldContract] = [
    # --- ev_battery_status: hv_battery_range ---
    # sensor.*_ev_range state value (km)
    FieldContract(
        source_entity_pattern="sensor.{prefix}_ev_range",
        source_attribute="__state__",
        source_unit="km",
        target_db_table="ev_battery_status",
        target_db_column="hv_battery_range",
        target_unit="km",
        notes="onstar2mqtt ev_range entity state, always km",
    ),
    # --- ev_battery_status: hv_battery_max_range ---
    # sensor.*_ev_max_range state value (km)
    FieldContract(
        source_entity_pattern="sensor.{prefix}_ev_max_range",
        source_attribute="__state__",
        source_unit="km",
        target_db_table="ev_battery_status",
        target_db_column="hv_battery_max_range",
        target_unit="km",
        notes="onstar2mqtt ev_max_range entity state, always km",
    ),
    # --- ev_battery_status: hv_battery_soc ---
    # sensor.*_charge_state state value (%)
    FieldContract(
        source_entity_pattern="sensor.{prefix}_charge_state",
        source_attribute="__state__",
        source_unit="%",
        target_db_table="ev_battery_status",
        target_db_column="hv_battery_soc",
        target_unit="%",
        notes="onstar2mqtt charge_state (SoC %) entity state",
    ),
    # --- ev_battery_status: hv_battery_temperature ---
    # sensor.*_hybrid_battery_minimum_temperature (°C)
    FieldContract(
        source_entity_pattern="sensor.{prefix}_hybrid_battery_minimum_temperature",
        source_attribute="__state__",
        source_unit="degC",
        target_db_table="ev_battery_status",
        target_db_column="hv_battery_temperature",
        target_unit="degC",
        notes="onstar2mqtt hybrid_battery_minimum_temperature, always °C",
    ),
    # --- ev_battery_status: lv_battery_voltage ---
    # sensor.*_charge_voltage state value (V) — AC charge voltage, not 12V
    # Omitted: charge_voltage is AC supply voltage (120/240V), not 12V aux.
    # lv_battery_voltage has no direct equivalent in onstar2mqtt diagnostics.

    # --- Odometer (written to ev_vehicle_status.odometer) ---
    # sensor.*_odo_read state value (km)
    FieldContract(
        source_entity_pattern="sensor.{prefix}_odo_read",
        source_attribute="__state__",
        source_unit="km",
        target_db_table="ev_vehicle_status",
        target_db_column="odometer",
        target_unit="km",
        notes="onstar2mqtt odo_read entity state, always km",
    ),
]


# ---------------------------------------------------------------------------
# Per-device state caches (keyed by device_id / ha_entity_prefix)
# ---------------------------------------------------------------------------

# Last-seen SoC, used to detect charging session boundaries
_last_soc: dict[str, float | None] = {}

# Last-seen plug state (True = plugged)
_last_plug_state: dict[str, bool | None] = {}

# Last-seen charge state (True = charging)
_last_charge_state: dict[str, bool | None] = {}

# Pending charging session start info keyed by device_id
_pending_charge_session: dict[str, dict] = {}

# Last-seen raw cache for diagnostic display (mirrors ha_fordpass pattern)
_last_seen_raw: dict[str, dict[str, Any]] = {}


def _record_last_seen(key: str, raw_value: Any, converted: Any, unit: str) -> None:
    _last_seen_raw[key] = {
        "value": raw_value,
        "unit": unit,
        "seen_at": datetime.now(UTC).isoformat(),
        "converted": converted,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _parse_iso(val: Any) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    if not isinstance(val, str):
        return None
    try:
        if val.endswith("Z"):
            val = val[:-1] + "+00:00"
        return datetime.fromisoformat(val)
    except (ValueError, TypeError):
        return None


def _parse_event_ts(new_state: dict) -> datetime | None:
    for key in ("last_changed", "last_updated"):
        val = new_state.get(key)
        if not val:
            continue
        parsed = _parse_iso(val)
        if parsed is not None:
            return parsed
    return None


def _entity_suffix(entity_id: str, prefix: str) -> str | None:
    """Return the trailing slug after the ha_entity_prefix, or None.

    Example: sensor.2017_chevrolet_bolt_ev_ev_range, prefix=2017_chevrolet_bolt_ev
    -> "ev_range"

    Handles both sensor.* and binary_sensor.* and device_tracker.* prefixes.
    """
    for domain in ("sensor.", "binary_sensor.", "device_tracker."):
        if entity_id.startswith(domain):
            remainder = entity_id[len(domain):]
            full_prefix = prefix + "_"
            if remainder.startswith(full_prefix):
                return remainder[len(full_prefix):]
    return None


def _convert_state(raw_state: str | None, source_unit: str) -> float | None:
    """Convert a state string value to metric float."""
    val = _safe_float(raw_state)
    if val is None:
        return None
    if source_unit == "km" or source_unit == "%" or source_unit == "degC":
        # Already in target unit — passthrough
        return val
    try:
        return to_metric(val, source_unit)
    except UnknownSourceUnit as exc:
        logger.warning("UnknownSourceUnit converting state %r (%s): %s", raw_state, source_unit, exc)
        return None


# ---------------------------------------------------------------------------
# process_event — main entry point
# ---------------------------------------------------------------------------

async def process_event(
    entity_id: str,
    new_state: dict,
    db: AsyncSession,
    ha_entity_prefix: str,
    device_id: str,
    ha_config: dict | None = None,
) -> None:
    """Route an HA state_changed event for an onstar2mqtt vehicle.

    Unlike ha_fordpass.process_event, this adapter receives ha_entity_prefix
    and device_id explicitly from hass_processor, since the entity IDs contain
    no VIN — only the vehicle name slug configured in onstar2mqtt.

    Dispatches by entity suffix (slug after the prefix):
      ev_range / ev_max_range   -> ev_battery_status (range fields)
      charge_state              -> ev_battery_status (SoC)
      hybrid_battery_minimum_temperature -> ev_battery_status (temp)
      odo_read                  -> ev_vehicle_status (odometer)
      ev_plug_state             -> session boundary detection
      ev_charge_state (binary)  -> session boundary detection
    """
    if not entity_id or not isinstance(new_state, dict):
        return

    suffix = _entity_suffix(entity_id, ha_entity_prefix)
    if suffix is None:
        return

    state_val = new_state.get("state")
    # Skip unknown/unavailable states
    if state_val in (None, "unknown", "unavailable", ""):
        return

    try:
        if suffix == "ev_range":
            await _handle_ev_range(entity_id, new_state, device_id, db)
        elif suffix == "ev_max_range":
            await _handle_ev_max_range(entity_id, new_state, device_id, db)
        elif suffix == "charge_state":
            await _handle_charge_state(entity_id, new_state, device_id, db)
        elif suffix == "hybrid_battery_minimum_temperature":
            await _handle_battery_temp(entity_id, new_state, device_id, db)
        elif suffix == "odo_read":
            await _handle_odometer(entity_id, new_state, device_id, db)
        elif suffix == "ev_plug_state":
            await _handle_plug_state(entity_id, new_state, device_id, db)
        elif suffix == "ev_charge_state":
            # binary_sensor — state is "on"/"off"
            await _handle_binary_charge_state(entity_id, new_state, device_id, db)
        else:
            # Not an adapter-owned entity — silent return
            return
    except Exception:
        logger.exception("ha_onstar2mqtt.process_event failed for %s", entity_id)


# ---------------------------------------------------------------------------
# Per-entity handlers
# ---------------------------------------------------------------------------

async def _handle_ev_range(
    entity_id: str,
    new_state: dict,
    device_id: str,
    db: AsyncSession,
) -> None:
    """sensor.*_ev_range -> ev_battery_status.hv_battery_range (km)."""
    from db.models.battery_status import EVBatteryStatus

    raw = new_state.get("state")
    hv_range = _convert_state(raw, "km")
    if hv_range is None:
        return

    _record_last_seen(f"{entity_id}|state", raw, hv_range, "km")

    recorded_at = _parse_event_ts(new_state) or datetime.now(UTC)
    record = EVBatteryStatus(
        device_id=device_id,
        recorded_at=recorded_at,
        source_system="ha_onstar2mqtt",
        hv_battery_range=hv_range,
        original_timestamp=recorded_at,
        ingest_schema_version=INGEST_SCHEMA_VERSION,
    )
    db.add(record)
    logger.debug("ha_onstar2mqtt: ev_range=%s km for %s", hv_range, device_id)


async def _handle_ev_max_range(
    entity_id: str,
    new_state: dict,
    device_id: str,
    db: AsyncSession,
) -> None:
    """sensor.*_ev_max_range -> ev_battery_status.hv_battery_max_range (km)."""
    from db.models.battery_status import EVBatteryStatus

    raw = new_state.get("state")
    hv_max_range = _convert_state(raw, "km")
    if hv_max_range is None:
        return

    _record_last_seen(f"{entity_id}|state", raw, hv_max_range, "km")

    recorded_at = _parse_event_ts(new_state) or datetime.now(UTC)
    record = EVBatteryStatus(
        device_id=device_id,
        recorded_at=recorded_at,
        source_system="ha_onstar2mqtt",
        hv_battery_max_range=hv_max_range,
        original_timestamp=recorded_at,
        ingest_schema_version=INGEST_SCHEMA_VERSION,
    )
    db.add(record)
    logger.debug("ha_onstar2mqtt: ev_max_range=%s km for %s", hv_max_range, device_id)


async def _handle_charge_state(
    entity_id: str,
    new_state: dict,
    device_id: str,
    db: AsyncSession,
) -> None:
    """sensor.*_charge_state -> ev_battery_status.hv_battery_soc (%)."""
    from db.models.battery_status import EVBatteryStatus

    raw = new_state.get("state")
    soc = _safe_float(raw)
    if soc is None:
        return

    _record_last_seen(f"{entity_id}|state", raw, soc, "%")

    # Cache for session boundary detection
    _last_soc[device_id] = soc

    recorded_at = _parse_event_ts(new_state) or datetime.now(UTC)
    record = EVBatteryStatus(
        device_id=device_id,
        recorded_at=recorded_at,
        source_system="ha_onstar2mqtt",
        hv_battery_soc=soc,
        original_timestamp=recorded_at,
        ingest_schema_version=INGEST_SCHEMA_VERSION,
    )
    db.add(record)
    logger.debug("ha_onstar2mqtt: charge_state=%s%% for %s", soc, device_id)


async def _handle_battery_temp(
    entity_id: str,
    new_state: dict,
    device_id: str,
    db: AsyncSession,
) -> None:
    """sensor.*_hybrid_battery_minimum_temperature -> ev_battery_status.hv_battery_temperature (°C)."""
    from db.models.battery_status import EVBatteryStatus

    raw = new_state.get("state")
    temp = _safe_float(raw)
    if temp is None:
        return

    # Sanity check: -40 is the onstar2mqtt default/unavailable sentinel
    if temp == -40.0:
        logger.debug("ha_onstar2mqtt: skipping sentinel battery temp -40°C for %s", device_id)
        return

    _record_last_seen(f"{entity_id}|state", raw, temp, "degC")

    recorded_at = _parse_event_ts(new_state) or datetime.now(UTC)
    record = EVBatteryStatus(
        device_id=device_id,
        recorded_at=recorded_at,
        source_system="ha_onstar2mqtt",
        hv_battery_temperature=temp,
        original_timestamp=recorded_at,
        ingest_schema_version=INGEST_SCHEMA_VERSION,
    )
    db.add(record)
    logger.debug("ha_onstar2mqtt: battery_temp=%s°C for %s", temp, device_id)


async def _handle_odometer(
    entity_id: str,
    new_state: dict,
    device_id: str,
    db: AsyncSession,
) -> None:
    """sensor.*_odo_read -> ev_vehicle_status.odometer (km)."""
    from db.models.vehicle_status import EVVehicleStatus

    raw = new_state.get("state")
    odometer = _convert_state(raw, "km")
    if odometer is None:
        return

    _record_last_seen(f"{entity_id}|state", raw, odometer, "km")

    recorded_at = _parse_event_ts(new_state) or datetime.now(UTC)
    record = EVVehicleStatus(
        device_id=device_id,
        recorded_at=recorded_at,
        source_system="ha_onstar2mqtt",
        odometer=odometer,
        original_timestamp=recorded_at,
        ingest_schema_version=INGEST_SCHEMA_VERSION,
    )
    db.add(record)
    logger.debug("ha_onstar2mqtt: odometer=%s km for %s", odometer, device_id)


async def _handle_plug_state(
    entity_id: str,
    new_state: dict,
    device_id: str,
    db: AsyncSession,
) -> None:
    """binary_sensor.*_ev_plug_state -> session boundary detection.

    Tracks plug-in / plug-out transitions. On plug-in, caches the current SoC
    as session start. On plug-out, writes an ev_charging_session record using
    the SoC delta as a proxy for energy added.
    """
    from db.models.charging_session import EVChargingSession

    state_val = new_state.get("state")
    plugged = state_val == "on"
    prev = _last_plug_state.get(device_id)
    _last_plug_state[device_id] = plugged

    recorded_at = _parse_event_ts(new_state) or datetime.now(UTC)

    if plugged and not prev:
        # Plug-in event: cache session start
        _pending_charge_session[device_id] = {
            "start_at": recorded_at,
            "soc_start": _last_soc.get(device_id),
        }
        logger.info("ha_onstar2mqtt: plug-in detected for %s, SoC=%s%%", device_id, _last_soc.get(device_id))

    elif not plugged and prev:
        # Plug-out event: close session
        session_start = _pending_charge_session.pop(device_id, None)
        if session_start:
            soc_start = session_start.get("soc_start")
            soc_end = _last_soc.get(device_id)
            start_at = session_start.get("start_at", recorded_at)

            duration_s = (recorded_at - start_at).total_seconds() if start_at else None

            record = EVChargingSession(
                device_id=device_id,
                session_start=start_at,
                session_end=recorded_at,
                source_system="ha_onstar2mqtt",
                soc_start=soc_start,
                soc_end=soc_end,
                duration_seconds=int(duration_s) if duration_s is not None else None,
            )
            db.add(record)
            logger.info(
                "ha_onstar2mqtt: plug-out, wrote charging session for %s "
                "SoC %s%%->%s%% duration=%ss",
                device_id, soc_start, soc_end, duration_s,
            )


async def _handle_binary_charge_state(
    entity_id: str,
    new_state: dict,
    device_id: str,
    db: AsyncSession,
) -> None:
    """binary_sensor.*_ev_charge_state -> cache charge state.

    Used for future session-level logic (e.g. distinguishing AC vs DC based
    on sensor.*_charge_voltage). Currently just caches the state.
    """
    state_val = new_state.get("state")
    charging = state_val == "on"
    _last_charge_state[device_id] = charging
    logger.debug("ha_onstar2mqtt: charge_state=%s for %s", charging, device_id)
