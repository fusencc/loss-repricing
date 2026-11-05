"""Local users, explicit ownership, target controls and correction intents."""
from alembic import op
import sqlalchemy as sa
revision = 'd52e7b1c94a0'
down_revision = 'c41a62f0b931'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('user_accounts',
        sa.Column('id', sa.String(36), nullable=False, primary_key=True),
        sa.Column('username', sa.String(100), nullable=False, unique=True),
        sa.Column('display_name', sa.String(100), nullable=False),
        sa.Column('role', sa.String(16), nullable=False),
        sa.Column('can_correct', sa.Boolean(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('password_hash', sa.Text(), nullable=False),
        sa.Column('session_version', sa.Integer(), nullable=False),
        sa.Column('dingtalk_userid', sa.String(128), nullable=True, unique=True),
        sa.Column('created_at', sa.String(40), nullable=False),
        sa.Column('updated_at', sa.String(40), nullable=False),
    )
    op.create_table('ownerships',
        sa.Column('id', sa.String(36), nullable=False, primary_key=True),
        sa.Column('scope_key', sa.String(64), nullable=False, unique=True),
        sa.Column('platform', sa.String(64), nullable=False),
        sa.Column('shop_id', sa.String(100), nullable=False),
        sa.Column('site', sa.String(4), nullable=False),
        sa.Column('target_id', sa.String(64), nullable=True),
        sa.Column('owner_id', sa.String(36), nullable=False),
        sa.Column('supervisor_id', sa.String(36), nullable=True),
        sa.Column('autonomous', sa.Boolean(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
    )
    op.create_table('target_controls',
        sa.Column('target_id', sa.String(64), nullable=False, primary_key=True),
        sa.Column('key', sa.JSON(), nullable=False),
        sa.Column('mode', sa.String(16), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('changed_by', sa.String(36), nullable=False),
        sa.Column('updated_at', sa.String(40), nullable=False),
        sa.Column('baseline', sa.JSON(), nullable=False),
        sa.Column('handback_pending', sa.Boolean(), nullable=False),
    )
    op.create_table('corrections',
        sa.Column('id', sa.String(36), nullable=False, primary_key=True),
        sa.Column('target_id', sa.String(64), nullable=False),
        sa.Column('item_id', sa.String(36), nullable=False),
        sa.Column('desired_actual', sa.String(100), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('requested_by', sa.String(36), nullable=False),
        sa.Column('state', sa.String(20), nullable=False),
        sa.Column('control_version', sa.Integer(), nullable=False),
        sa.Column('baseline', sa.JSON(), nullable=False),
        sa.Column('job_id', sa.String(36), nullable=True),
        sa.Column('execution_id', sa.String(36), nullable=True),
        sa.Column('created_at', sa.String(40), nullable=False),
        sa.Column('updated_at', sa.String(40), nullable=False),
        sa.Column('result', sa.JSON(), nullable=False),
        sa.Column('approved_by', sa.String(36), nullable=True),
    )
    op.create_index('ix_corrections_target_id', 'corrections', ['target_id'], unique=False)

def downgrade():
    op.drop_table('corrections')
    op.drop_table('target_controls')
    op.drop_table('ownerships')
    op.drop_table('user_accounts')
