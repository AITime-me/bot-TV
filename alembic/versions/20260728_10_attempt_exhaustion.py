"""Persist ingress attempt limits for exhausted-lease recovery (BOT-TV-10).

Revision ID: 20260728_10_attempt_exhaustion
Revises: 20260728_09_amocrm_mirror
Create Date: 2026-07-28

The other durable queues already persist max_attempts per row. Ingress needs
the same source of truth so an expired final lease can be terminalized after a
worker restart without relying on the next worker's process configuration.

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260728_10_attempt_exhaustion"
down_revision: Union[str, Sequence[str], None] = "20260728_09_amocrm_mirror"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ingress_events",
        sa.Column(
            "max_attempts",
            sa.Integer(),
            server_default=sa.text("5"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_ingress_max_attempts_positive",
        "ingress_events",
        "max_attempts > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_ingress_max_attempts_positive",
        "ingress_events",
        type_="check",
    )
    op.drop_column("ingress_events", "max_attempts")
