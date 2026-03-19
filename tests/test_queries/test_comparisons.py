"""Comparisons query layer validation tests.

Tests gas comparison and network rate comparison calculations.
"""

import pytest
from datetime import datetime, timedelta, timezone

from db.models.charging_session import EVChargingSession
from db.models.reference import EVChargingNetwork, GasPriceHistory
from db.models.vehicle import EVVehicle
from web.queries.comparisons import query_gas_comparison, query_network_comparison
from web.queries.costs import compute_session_cost, get_networks_by_name


pytestmark = [pytest.mark.query, pytest.mark.db]


async def _setup_comparison_data(db):
    """Create vehicle, network, gas prices, and sessions with known values."""
    # Vehicle with ICE comparison fields set
    vehicle = EVVehicle(
        device_id="COMP_VIN",
        display_name="Comparison Vehicle",
        year=2024,
        make="Ford",
        model="Mustang Mach-E",
        trim="Premium AWD",
        battery_capacity_kwh=91.0,
        ice_mpg=25.0,
        ice_fuel_tank_gal=15.0,
        ice_label="2024 Ford Explorer 25 MPG",
    )
    db.add(vehicle)
    await db.flush()

    net = EVChargingNetwork(
        network_name="Comparison Net",
        cost_per_kwh=0.35,
        is_free=False,
        is_verified=True,
    )
    db.add(net)
    await db.flush()

    # Gas price for June 2025 — station $4.00, average $4.20
    gas_price = GasPriceHistory(
        year=2025,
        month=6,
        station_price=4.00,
        average_price=4.20,
        source="manual",
    )
    db.add(gas_price)
    await db.flush()

    # 3 sessions with known energy, miles, and costs
    sessions = []
    for i, (kwh, miles) in enumerate([(40.0, 120.0), (30.0, 90.0), (50.0, 150.0)]):
        s = EVChargingSession(
            device_id="COMP_VIN",
            energy_kwh=kwh,
            miles_added=miles,
            network_id=net.id,
            location_name="Comparison Net",
            session_start_utc=datetime(2025, 6, 1, tzinfo=timezone.utc) + timedelta(days=i),
            is_complete=True,
            source_system="test",
        )
        sessions.append(s)

    db.add_all(sessions)
    await db.flush()

    return {
        "vehicle": vehicle,
        "network": net,
        "sessions": sessions,
        "total_kwh": 120.0,
        "total_miles": 360.0,
    }


async def test_gas_comparison(db_session):
    """Verify gas comparison calculates EV vs gas costs correctly."""
    db = db_session
    data = await _setup_comparison_data(db)
    vehicle = data["vehicle"]

    result = await query_gas_comparison(db, vehicle=vehicle, time_range="all")

    # EV costs: each session = kwh * 0.35 -> 14.00 + 10.50 + 17.50 = 42.00
    assert result["ev_total"] == pytest.approx(42.00, abs=0.01)
    assert result["session_count"] == 3
    assert result["total_miles"] == pytest.approx(data["total_miles"], abs=0.01)

    # Gas costs using miles-based path: 360 miles / 25 mpg = 14.4 gallons
    # Station track: 14.4 * $4.00 = $57.60
    # Average track: 14.4 * $4.20 = $60.48
    assert result["gas_total_low"] == pytest.approx(57.60, abs=0.01)
    assert result["gas_total_high"] == pytest.approx(60.48, abs=0.01)

    # Savings: gas - ev
    assert result["savings_low"] == pytest.approx(57.60 - 42.00, abs=0.01)
    assert result["savings_high"] == pytest.approx(60.48 - 42.00, abs=0.01)
    assert result["has_range"] is True
    assert result["ice_label"] == "2024 Ford Explorer 25 MPG"


async def test_gas_comparison_no_vehicle(db_session):
    """No vehicle -> returns zeros gracefully."""
    result = await query_gas_comparison(db_session, vehicle=None, time_range="all")

    assert result["ev_total"] == 0.0
    assert result["gas_total_low"] == 0.0
    assert result["gas_total_high"] == 0.0
    assert result["savings_low"] == 0.0
    assert result["session_count"] == 0


async def test_network_comparison(db_session):
    """Verify network rate comparison against a reference rate."""
    db = db_session
    data = await _setup_comparison_data(db)

    # Compare actual cost (0.35/kWh) to hypothetical rate of 0.50/kWh
    result = await query_network_comparison(db, reference_rate=0.50, time_range="all")

    # EV costs: 42.00 (same as gas comparison)
    assert result["ev_total"] == pytest.approx(42.00, abs=0.01)
    # Hypothetical: 120 kWh * 0.50 = 60.00
    assert result["hypothetical_total"] == pytest.approx(60.00, abs=0.01)
    # Difference: 60 - 42 = 18.00
    assert result["difference"] == pytest.approx(18.00, abs=0.01)
    assert result["session_count"] == 3


async def test_gas_comparison_empty(db_session):
    """Vehicle with ICE config but no sessions -> returns zeros."""
    vehicle = EVVehicle(
        device_id="EMPTY_VIN",
        display_name="Empty Vehicle",
        year=2024,
        make="Ford",
        model="Mustang Mach-E",
        battery_capacity_kwh=91.0,
        ice_mpg=25.0,
        ice_fuel_tank_gal=15.0,
    )
    db_session.add(vehicle)
    await db_session.flush()

    result = await query_gas_comparison(db_session, vehicle=vehicle, time_range="all")

    assert result["ev_total"] == 0.0
    assert result["gas_total_low"] == 0.0
    assert result["session_count"] == 0
