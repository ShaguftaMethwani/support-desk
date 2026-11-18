"""loop connector: support-queue themes, explainer chunk ids, canonical archive and reuse,
draft canonical link

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-18 14:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('theme', sa.Column('cluster_id', sa.Uuid(), nullable=True))
    op.create_index(op.f('ix_theme_cluster_id'), 'theme', ['cluster_id'], unique=False)
    op.create_foreign_key(
        op.f('fk_theme_cluster_id_question_cluster'), 'theme', 'question_cluster',
        ['cluster_id'], ['id'], ondelete='SET NULL',
    )
    op.add_column(
        'fee_explainer',
        sa.Column('chunk_ids', postgresql.JSONB(astext_type=sa.Text()), server_default='[]',
                  nullable=False),
    )
    op.add_column(
        'canonical_explainer',
        sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'canonical_explainer',
        sa.Column('bullet_chunk_ids', postgresql.JSONB(astext_type=sa.Text()),
                  server_default='[]', nullable=False),
    )
    op.add_column('draft_version', sa.Column('canonical_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f('fk_draft_version_canonical_id_canonical_explainer'), 'draft_version',
        'canonical_explainer', ['canonical_id'], ['id'], ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f('fk_draft_version_canonical_id_canonical_explainer'), 'draft_version',
        type_='foreignkey',
    )
    op.drop_column('draft_version', 'canonical_id')
    op.drop_column('canonical_explainer', 'bullet_chunk_ids')
    op.drop_column('canonical_explainer', 'archived_at')
    op.drop_column('fee_explainer', 'chunk_ids')
    op.drop_constraint(
        op.f('fk_theme_cluster_id_question_cluster'), 'theme', type_='foreignkey'
    )
    op.drop_index(op.f('ix_theme_cluster_id'), table_name='theme')
    op.drop_column('theme', 'cluster_id')
