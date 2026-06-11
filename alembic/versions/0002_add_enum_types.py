"""add enum types to existing columns

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-11

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(sa.text("""
        DO $$ BEGIN
            CREATE TYPE offerstatus AS ENUM ('pending_create','draft','active','paused','error');
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
    """))
    conn.execute(sa.text("""
        DO $$ BEGIN
            CREATE TYPE deliverystatus AS ENUM ('pending','dispatched','done','finalized','failed','needs_attention');
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
    """))
    conn.execute(sa.text("""
        DO $$ BEGIN
            CREATE TYPE taskkind AS ENUM ('CREATE_OFFER','UPDATE_PRICE_BATCH','TOGGLE_STATUS_BATCH','DELIVER','MONITOR_DELIVERY','MARK_DELIVERED','TRADE_WATCH');
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
    """))
    conn.execute(sa.text("""
        DO $$ BEGIN
            CREATE TYPE taskstatus AS ENUM ('pending','processing','done','failed');
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
    """))
    conn.execute(sa.text("""
        DO $$ BEGIN
            CREATE TYPE webhookkind AS ENUM ('precheck','notification');
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
    """))

    conn.execute(sa.text(
        "ALTER TABLE offers ALTER COLUMN status TYPE offerstatus USING status::offerstatus"
    ))
    conn.execute(sa.text(
        "ALTER TABLE orders ALTER COLUMN delivery_status TYPE deliverystatus USING delivery_status::deliverystatus"
    ))
    conn.execute(sa.text(
        "ALTER TABLE tasks ALTER COLUMN kind TYPE taskkind USING kind::taskkind"
    ))
    conn.execute(sa.text(
        "ALTER TABLE tasks ALTER COLUMN status TYPE taskstatus USING status::taskstatus"
    ))
    conn.execute(sa.text(
        "ALTER TABLE webhook_events ALTER COLUMN kind TYPE webhookkind USING kind::webhookkind"
    ))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("ALTER TABLE offers ALTER COLUMN status TYPE varchar USING status::varchar"))
    conn.execute(sa.text("ALTER TABLE orders ALTER COLUMN delivery_status TYPE varchar USING delivery_status::varchar"))
    conn.execute(sa.text("ALTER TABLE tasks ALTER COLUMN kind TYPE varchar USING kind::varchar"))
    conn.execute(sa.text("ALTER TABLE tasks ALTER COLUMN status TYPE varchar USING status::varchar"))
    conn.execute(sa.text("ALTER TABLE webhook_events ALTER COLUMN kind TYPE varchar USING kind::varchar"))
    conn.execute(sa.text("DROP TYPE IF EXISTS offerstatus"))
    conn.execute(sa.text("DROP TYPE IF EXISTS deliverystatus"))
    conn.execute(sa.text("DROP TYPE IF EXISTS taskkind"))
    conn.execute(sa.text("DROP TYPE IF EXISTS taskstatus"))
    conn.execute(sa.text("DROP TYPE IF EXISTS webhookkind"))
