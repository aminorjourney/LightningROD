"""Add ha_entity_prefix to ev_vehicles for onstar2mqtt support.

Revision ID: s35_onstar2mqtt_prefix
Revises: s34_phase29_ingest_schema_version
Create Date: 2026-04-26

Adds:
  - ev_vehicles.ha_entity_prefix  (nullable string)
      The HA entity ID prefix for this vehicle, e.g. "2017_chevrolet_bolt_ev".
      Used by the ha_onstar2mqtt adapter to match incoming HA state_changed
      events to the correct vehicle/device_id. For FordPass vehicles this
      is null (VIN auto-detection continues to work as before).
"""

import sqlalchemy as sa
from alembic import op

revision = "s35_onstar2mqtt_prefix"
down_revision = "s34_phase29_schema_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ev_vehicles",
        sa.Column("ha_entity_prefix", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ev_vehicles", "ha_entity_prefix")
