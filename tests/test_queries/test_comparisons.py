"""Comparisons query layer validation tests.

Tests gas comparison and network rate comparison calculations.
"""

import pytest
from datetime import datetime, timedelta, timezone

from db.models.charging_session import EVChargingSession
from db.models.reference import EVChargingNetwork
from web.queries.comparisons import query_gas_comparison, query_network_comparison
from web.queries.settings import set_app_setting


pytestmark = [pytest.mark.query, pytest.mark.db]


async def _setup_comparison_data(db):
    """Create sessions with known energy, cost, and miles for comparison tests."""
    net = EVChargingNetwork(
        network_name="Comparison Net",
        cost_per_kwh=0.35,
        is_free=False,
        is_verified=True,
    )
    db.add(net)
    await db.flush()

    # 3 sessions with known values
    sessions = []
    for i, (kwh, miles) in enumerate([(40.0, 120.0), (30.0, 90.0), (50.0, 150.0)]):
        s = EVChargingSession(
            device_id="COMP_VIN",
            energy_kwh=kwh,
            miles_added=miles,
            network_id=net.id,
            location_name="Comparison Net",  # for old-style networks_by_name lookup
            session_start_utc=datetime(2025, 6, 1, tzinfo=timezone.utc) + timedelta(days=i),
            is_complete=True,
            source_system="test",
        )
        sessions.append(s)

    db.add_all(sessions)
    await db.flush()

    return {
        "network": net,
        "sessions": sessions,
        "total_kwh": 120.0,
        "total_miles": 360.0,
    }


async def test_gas_comparison(db_session):
    """Verify gas comparison calculates EV vs gas costs correctly."""
    db = db_session
    data = await _setup_comparison_data(db)

    # Set gas price and mpg settings (upsert to handle pre-existing seed data)
    await set_app_setting(db, "gas_price_per_gallon", "4.00")
    await set_app_setting(db, "vehicle_mpg", "25.0")

    result = await query_gas_comparison(db, time_range="all")

    # EV costs: each session = kwh * 0.35 -> 14.00 + 10.50 + 17.50 = 42.00
    assert result["ev_total"] == pytest.approx(42.00, abs=0.01)
    assert result["session_count"] == 3
    assert result["total_miles"] == pytest.approx(data["total_miles"], abs=0.01)

    # Gas costs: 360 miles / 25 mpg * $4.00/gal = 57.60
    assert result["gas_total"] == pytest.approx(57.60, abs=0.01)

    # Savings: 57.60 - 42.00 = 15.60
    assert result["savings"] == pytest.approx(15.60, abs=0.01)


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
    """No sessions -> returns zeros gracefully."""
    result = await query_gas_comparison(db_session, time_range="all")

    assert result["ev_total"] == 0.0
    assert result["gas_total"] == 0.0
    assert result["savings"] == 0.0
    assert result["session_count"] == 0
