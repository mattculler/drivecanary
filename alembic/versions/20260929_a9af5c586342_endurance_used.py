"""endurance used

Revision ID: a9af5c586342
Revises: 8726e88dfb7c
Create Date: 2026-09-29 03:40:22.485425+00:00

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a9af5c586342"
down_revision = "8726e88dfb7c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("smart_run", schema=None) as batch_op:
        batch_op.add_column(sa.Column("endurance_used", sa.Integer(), nullable=True))
    # Every reading kept what smartctl said, so the readings from before today can be given what they
    # already held: the endurance figure, and the power-on hours of the drives that report them as 0.
    import json
    import re
    import zlib

    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, power_on_hours, raw_json FROM smart_run WHERE raw_json IS NOT NULL")
    ).fetchall()
    for run_id, hours, raw in rows:
        try:
            doc = json.loads(zlib.decompress(raw))
        except (ValueError, zlib.error):
            continue
        used = None
        for page in (doc.get("ata_device_statistics") or {}).get("pages") or []:
            for row in page.get("table") or []:
                if row.get("name") == "Percentage Used Endurance Indicator" and (row.get("flags") or {}).get(
                    "valid", True
                ):
                    used = row.get("value")
        if not hours:
            for attr in (doc.get("ata_smart_attributes") or {}).get("table") or []:
                found = (
                    re.match(r"\s*(\d+)", str((attr.get("raw") or {}).get("string") or ""))
                    if attr.get("id") == 9
                    else None
                )
                if found and int(found.group(1)):
                    hours = int(found.group(1))
        conn.execute(
            sa.text("UPDATE smart_run SET endurance_used = :used, power_on_hours = :hours WHERE id = :id"),
            {"used": used if isinstance(used, int) else None, "hours": hours, "id": run_id},
        )


def downgrade() -> None:
    with op.batch_alter_table("smart_run", schema=None) as batch_op:
        batch_op.drop_column("endurance_used")
