"""Product catalog business logic.

Routes stay thin: they parse input and translate the result into HTTP. All
rules -- SKU uniqueness, category resolution, soft delete, search semantics --
live here, so they are testable without an HTTP client and reusable from the
Kafka consumer path.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Sequence

from sqlalchemy import Select, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Category, Product, ProductStatus
from app.schemas import ProductCreate, ProductUpdate
from retailpulse_common.errors import ConflictError, NotFoundError, ValidationError
from retailpulse_common.pagination import PageParams

logger = logging.getLogger("product-service")

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
# Characters that mean "wildcard" to SQL LIKE. Left unescaped, a search for
# "50%" would match everything -- and "_" would match any character.
_LIKE_SPECIALS = re.compile(r"([%_\\])")


def slugify(value: str) -> str:
    return _SLUG_STRIP.sub("-", value.strip().lower()).strip("-")


def _escape_like(term: str) -> str:
    return _LIKE_SPECIALS.sub(r"\\\1", term)


class ProductService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------
    def get_or_create_category(self, name: str) -> Category:
        """Resolve a category by name, creating it on first use.

        The unique constraint on ``slug`` is what guarantees correctness if two
        requests create the same category at once; the SELECT is only a
        fast path, and the IntegrityError branch handles the race.
        """
        slug = slugify(name)
        if not slug:
            raise ValidationError("Category name must contain alphanumeric characters.")

        existing = self.session.scalar(select(Category).where(Category.slug == slug))
        if existing is not None:
            return existing

        category = Category(name=name.strip(), slug=slug)
        self.session.add(category)
        try:
            self.session.flush()
        except IntegrityError:
            self.session.rollback()
            found = self.session.scalar(select(Category).where(Category.slug == slug))
            if found is None:  # pragma: no cover - only on a genuine DB fault
                raise
            return found
        return category

    def list_categories(self) -> Sequence[Category]:
        return self.session.scalars(select(Category).order_by(Category.name)).all()

    # ------------------------------------------------------------------
    # Products
    # ------------------------------------------------------------------
    def create(self, payload: ProductCreate) -> Product:
        category = self.get_or_create_category(payload.category)
        product = Product(
            sku=payload.sku,
            name=payload.name,
            description=payload.description,
            category_id=category.category_id,
            brand=payload.brand,
            price=payload.price,
            currency=payload.currency,
            weight_grams=payload.weight_grams,
            status=payload.status.value,
        )
        self.session.add(product)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            # 409, not 400: the payload is well-formed, it just collides with
            # state that already exists.
            raise ConflictError(
                f"A product with SKU {payload.sku} already exists.",
                details={"sku": payload.sku},
            ) from exc

        self.session.refresh(product)
        logger.info("product created", extra={"sku": product.sku})
        return product

    def get(self, product_id: uuid.UUID) -> Product:
        product = self.session.get(Product, product_id)
        if product is None:
            raise NotFoundError(
                f"Product {product_id} was not found.",
                details={"product_id": str(product_id)},
            )
        return product

    def get_by_sku(self, sku: str) -> Product:
        product = self.session.scalar(select(Product).where(Product.sku == sku.upper()))
        if product is None:
            raise NotFoundError(f"Product with SKU {sku} was not found.", details={"sku": sku})
        return product

    def get_many(self, product_ids: Sequence[uuid.UUID]) -> list[Product]:
        """Bulk fetch for the order service. Missing IDs are simply absent."""
        if not product_ids:
            return []
        stmt = select(Product).where(Product.product_id.in_(list(product_ids)))
        return list(self.session.scalars(stmt).unique().all())

    def update(self, product_id: uuid.UUID, payload: ProductUpdate) -> Product:
        product = self.get(product_id)
        data = payload.model_dump(exclude_unset=True)

        if "category" in data and data["category"] is not None:
            product.category_id = self.get_or_create_category(data.pop("category")).category_id
        data.pop("category", None)

        if "status" in data and data["status"] is not None:
            data["status"] = ProductStatus(data["status"]).value

        for field, value in data.items():
            setattr(product, field, value)

        self.session.flush()
        self.session.refresh(product)
        logger.info("product updated", extra={"sku": product.sku, "fields": list(data)})
        return product

    def discontinue(self, product_id: uuid.UUID) -> Product:
        """Soft delete.

        Orders and analytics reference products indefinitely, so removing the
        row would orphan historical data. DISCONTINUED hides it from the
        catalog while keeping every foreign key valid.
        """
        product = self.get(product_id)
        if product.status == ProductStatus.DISCONTINUED.value:
            raise ConflictError(
                f"Product {product_id} is already discontinued.",
                details={"product_id": str(product_id)},
            )
        product.status = ProductStatus.DISCONTINUED.value
        self.session.flush()
        logger.info("product discontinued", extra={"sku": product.sku})
        return product

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def _paginate(
        self, stmt: Select, params: PageParams
    ) -> tuple[list[Product], int]:
        """Run a count and a page against the same filtered statement.

        The count uses a subquery over the *same* WHERE clause so the total can
        never disagree with the rows returned.
        """
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = self.session.scalar(count_stmt) or 0
        rows = self.session.scalars(
            stmt.order_by(Product.created_at.desc(), Product.product_id)
            .offset(params.offset)
            .limit(params.limit)
        ).unique().all()
        return list(rows), total

    def list_products(
        self,
        params: PageParams,
        *,
        status: ProductStatus | None = None,
        category: str | None = None,
        brand: str | None = None,
        include_discontinued: bool = False,
    ) -> tuple[list[Product], int]:
        stmt = select(Product)

        if status is not None:
            stmt = stmt.where(Product.status == status.value)
        elif not include_discontinued:
            # Default view is the shoppable catalog.
            stmt = stmt.where(Product.status != ProductStatus.DISCONTINUED.value)

        if category:
            stmt = stmt.join(Category).where(Category.slug == slugify(category))
        if brand:
            stmt = stmt.where(Product.brand == brand)

        return self._paginate(stmt, params)

    def search(
        self, query: str, params: PageParams, *, include_discontinued: bool = False
    ) -> tuple[list[Product], int]:
        """Substring search across SKU, name, brand and description.

        This is an honest LIKE scan, which is the right call at catalog sizes in
        the thousands. It does not scale to millions of rows -- that is when a
        Postgres GIN/trigram index or a dedicated search engine earns its
        operational cost, and the README says so rather than pretending
        otherwise.
        """
        term = query.strip()
        if len(term) < 2:
            raise ValidationError("Search query must be at least 2 characters.")

        pattern = f"%{_escape_like(term)}%"
        stmt = select(Product).where(
            or_(
                Product.name.ilike(pattern, escape="\\"),
                Product.sku.ilike(pattern, escape="\\"),
                Product.brand.ilike(pattern, escape="\\"),
                Product.description.ilike(pattern, escape="\\"),
            )
        )
        if not include_discontinued:
            stmt = stmt.where(Product.status != ProductStatus.DISCONTINUED.value)

        return self._paginate(stmt, params)
