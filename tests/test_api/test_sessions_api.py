"""API integration tests for charging session endpoints."""

import pytest

from tests.factories.sessions import ChargingSessionFactory
from tests.factories.vehicles import VehicleFactory


@pytest.mark.db
async def test_sessions_list_returns_200(client, db_session):
    """GET /charging/sessions returns 200."""
    response = await client.get("/charging/sessions")
    assert response.status_code == 200
    assert "Charging Sessions" in response.text


@pytest.mark.db
async def test_sessions_list_contains_table_structure(client, db_session):
    """GET /charging/sessions renders the sessions page with expected structure."""
    vehicle = await VehicleFactory.create(db_session)
    await ChargingSessionFactory.create(
        db_session,
        device_id=vehicle.device_id,
        energy_kwh=42.5,
    )
    response = await client.get("/charging/sessions")
    assert response.status_code == 200
    # Page structure assertions
    assert "sessions-table-region" in response.text
    assert "Add Session" in response.text


@pytest.mark.db
async def test_session_detail_returns_200(client, db_session):
    """GET /charging/sessions/{id}/detail returns 200 for existing session."""
    vehicle = await VehicleFactory.create(db_session)
    session = await ChargingSessionFactory.create(
        db_session,
        device_id=vehicle.device_id,
    )
    response = await client.get(f"/charging/sessions/{session.id}/detail")
    assert response.status_code == 200


@pytest.mark.db
async def test_session_new_modal_returns_200(client, db_session):
    """GET /charging/sessions/new/modal returns the new session form."""
    response = await client.get("/charging/sessions/new/modal")
    assert response.status_code == 200
