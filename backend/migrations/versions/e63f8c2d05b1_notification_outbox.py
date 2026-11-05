"""Recipient-specific durable notification outbox."""
from alembic import op
import sqlalchemy as sa

revision='e63f8c2d05b1'
down_revision='d52e7b1c94a0'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('notices',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('dedupe_key',sa.String(160),nullable=False,unique=True),
        sa.Column('recipient_id',sa.String(36),nullable=False),
        sa.Column('batch_id',sa.String(36),sa.ForeignKey('batches.id'),nullable=True),
        sa.Column('item_ids',sa.JSON(),nullable=False),
        sa.Column('summary',sa.JSON(),nullable=False),
        sa.Column('delivery_evidence',sa.JSON(),nullable=False),
        sa.Column('delivery_state',sa.String(24),nullable=False),
        sa.Column('synthetic',sa.Boolean(),nullable=False),
        sa.Column('task_id',sa.String(255),nullable=True),
        sa.Column('request_id',sa.String(255),nullable=True),
        sa.Column('recipient_userid',sa.String(255),nullable=True),
        sa.Column('ack_at',sa.String(40),nullable=True),
        sa.Column('ack_by',sa.String(36),nullable=True),
        sa.Column('escalated_at',sa.String(40),nullable=True),
        sa.Column('parent_id',sa.String(36),nullable=True),
        sa.Column('created_at',sa.String(40),nullable=False),
        sa.Column('updated_at',sa.String(40),nullable=False),
        sa.Column('last_error',sa.Text(),nullable=True))
    op.create_index('ix_notices_recipient_id','notices',['recipient_id'])
    op.create_index('ix_notices_delivery_state','notices',['delivery_state'])


def downgrade():
    op.drop_table('notices')
