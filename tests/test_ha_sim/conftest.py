"""Fixtures for HA simulator tests."""

import pytest
import pytest_asyncio

from tests.test_ha_sim.simulator import HASimulator


@pytest_asyncio.fixture
async def ha_simulator():
    """Create and start an HA simulator, yield it, then stop."""
    sim = HASimulator()
    await sim.start()
    yield sim
    await sim.stop()


@pytest.fixture(autouse=True)
def clear_processor_state():
    """Clear hass_processor module-level state dicts before each test.

    Prevents cross-test contamination from accumulated pending status
    fields and last-seen trip values (Pitfall 6 from RESEARCH.md).
    """
    from web.services import hass_processor

    hass_processor._last_trip_values.clear()
    hass_processor._pending_vehicle_status.clear()
    hass_processor._pending_vehicle_status_ts.clear()
    hass_processor._pending_battery_status.clear()
    hass_processor._pending_battery_status_ts.clear()
    yield
    # Also clear after test for good measure
    hass_processor._last_trip_values.clear()
    hass_processor._pending_vehicle_status.clear()
    hass_processor._pending_vehicle_status_ts.clear()
    hass_processor._pending_battery_status.clear()
    hass_processor._pending_battery_status_ts.clear()
