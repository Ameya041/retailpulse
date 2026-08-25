"""Initial product catalog schema.

Creates `categories` and `products` with the constraints and indexes the
catalog access patterns need.

Revision ID: 0001_initial_catalog
Revises:
Create Date: 2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial_catalog"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "categories",
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("category_id", name="pk_categories"),
        sa.UniqueConstraint("name", name="uq_categories_name"),
    )
    # A unique index, not a separate UNIQUE constraint plus a plain index.
    # In Postgres the two are equivalent (a UNIQUE constraint is implemented as
    # a unique index), but the ORM declares `unique=True, index=True` which
    # emits exactly one unique index -- and `alembic check` in CI fails on any
    # divergence between the models and the migrations.
    op.create_index("ix_categories_slug", "categories", ["slug"], unique=True)

    op.create_table(
        "products",
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("brand", sa.String(length=120), nullable=True),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("weight_grams", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("product_id", name="pk_products"),
        # RESTRICT, not CASCADE: deleting a category must not silently delete
        # the products in it.
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.category_id"],
            name="fk_products_category_id",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("price >= 0", name="ck_products_price_non_negative"),
        sa.CheckConstraint(
            "weight_grams IS NULL OR weight_grams >= 0",
            name="ck_products_weight_non_negative",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'INACTIVE', 'DISCONTINUED')",
            name="ck_products_status_valid",
        ),
        sa.CheckConstraint("length(currency) = 3", name="ck_products_currency_iso"),
    )
    op.create_index("ix_products_sku", "products", ["sku"], unique=True)
    op.create_index("ix_products_brand", "products", ["brand"])
    # Composite indexes match the two real access paths: browsing a category,
    # and listing the active catalog newest-first.
    op.create_index("ix_products_category_status", "products", ["category_id", "status"])
    op.create_index("ix_products_status_created_at", "products", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_products_status_created_at", table_name="products")
    op.drop_index("ix_products_category_status", table_name="products")
    op.drop_index("ix_products_brand", table_name="products")
    op.drop_index("ix_products_sku", table_name="products")
    op.drop_table("products")
    op.drop_index("ix_categories_slug", table_name="categories")
    op.drop_table("categories")
