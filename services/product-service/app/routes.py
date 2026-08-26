"""HTTP routes for the product catalog.

Authorization model:

* Reading the catalog is public -- customers browse before signing in.
* Every mutation requires the ADMIN role, enforced here on the server. This is
  the only enforcement point that matters; the frontend hiding an edit button
  is convenience, not security.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.caching import (
    invalidate_categories,
    invalidate_product,
    read_categories,
    read_product,
    read_product_by_sku,
)
from app.config import get_settings
from app.deps import get_cache, get_db_session, require_roles
from app.models import ProductStatus
from app.schemas import (
    CategoryCreate,
    CategoryRead,
    ProductBulkLookup,
    ProductCreate,
    ProductRead,
    ProductUpdate,
)
from app.service import ProductService
from retailpulse_common.auth import Role, TokenPayload
from retailpulse_common.cache import CacheBackend
from retailpulse_common.pagination import Page, PageParams, page_params

router = APIRouter(prefix="/products", tags=["products"])
category_router = APIRouter(prefix="/categories", tags=["categories"])

settings = get_settings()

SessionDep = Annotated[Session, Depends(get_db_session)]
CacheDep = Annotated[CacheBackend, Depends(get_cache)]
PageDep = Annotated[PageParams, Depends(page_params)]
AdminDep = Annotated[TokenPayload, Depends(require_roles(Role.ADMIN))]

COMMON_ERRORS = {
    401: {"description": "Missing or invalid bearer token."},
    403: {"description": "Authenticated but the role is not permitted."},
}


@router.post(
    "",
    response_model=ProductRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a product",
    responses={**COMMON_ERRORS, 409: {"description": "SKU already exists."}},
)
def create_product(
    payload: ProductCreate, session: SessionDep, cache: CacheDep, _: AdminDep
) -> ProductRead:
    """Add a catalog entry. Requires ADMIN. SKU must be globally unique."""
    product = ProductService(session).create(payload)
    result = ProductRead.from_model(product)
    # A new product changes the category listing and the category set.
    invalidate_product(cache, product.product_id, product.sku, result.category)
    invalidate_categories(cache)
    return result


@router.get(
    "",
    response_model=Page[ProductRead],
    summary="List products (paginated)",
)
def list_products(
    session: SessionDep,
    params: PageDep,
    status_filter: Annotated[
        ProductStatus | None, Query(alias="status", description="Filter by lifecycle status.")
    ] = None,
    category: Annotated[str | None, Query(description="Filter by category name or slug.")] = None,
    brand: Annotated[str | None, Query(description="Exact brand match.")] = None,
    include_discontinued: Annotated[bool, Query()] = False,
) -> Page[ProductRead]:
    """Public catalog listing. Discontinued products are hidden by default."""
    items, total = ProductService(session).list_products(
        params,
        status=status_filter,
        category=category,
        brand=brand,
        include_discontinued=include_discontinued,
    )
    return Page.build([ProductRead.from_model(p) for p in items], total, params)


@router.get(
    "/search",
    response_model=Page[ProductRead],
    summary="Search products",
    responses={400: {"description": "Query shorter than 2 characters."}},
)
def search_products(
    session: SessionDep,
    params: PageDep,
    q: Annotated[str, Query(min_length=2, description="Substring matched against SKU, name, brand, description.")],
) -> Page[ProductRead]:
    """Substring search across the catalog."""
    items, total = ProductService(session).search(q, params)
    return Page.build([ProductRead.from_model(p) for p in items], total, params)


@router.get(
    "/category/{category}",
    response_model=Page[ProductRead],
    summary="List products in a category",
)
def products_by_category(
    category: str, session: SessionDep, params: PageDep
) -> Page[ProductRead]:
    """Category browse. Accepts either the display name or the slug."""
    items, total = ProductService(session).list_products(params, category=category)
    return Page.build([ProductRead.from_model(p) for p in items], total, params)


@router.get(
    "/sku/{sku}",
    response_model=ProductRead,
    summary="Look up a product by SKU",
    responses={404: {"description": "No product with that SKU."}},
)
def get_product_by_sku(sku: str, session: SessionDep, cache: CacheDep) -> ProductRead:
    """Warehouse-facing lookup: SKU is what is printed on the label. Cached."""
    return ProductRead.model_validate(
        read_product_by_sku(
            cache,
            sku,
            lambda: ProductRead.from_model(ProductService(session).get_by_sku(sku)).model_dump(
                mode="json"
            ),
            settings.cache_ttl_seconds,
        )
    )


@router.post(
    "/bulk",
    response_model=list[ProductRead],
    summary="Fetch many products by ID",
)
def bulk_lookup(payload: ProductBulkLookup, session: SessionDep) -> list[ProductRead]:
    """Used by the order service to price a whole cart in one call.

    Unknown IDs are omitted rather than raising, so the caller can decide how to
    report a partially-invalid cart.
    """
    products = ProductService(session).get_many(payload.product_ids)
    return [ProductRead.from_model(p) for p in products]


@router.get(
    "/{product_id}",
    response_model=ProductRead,
    summary="Get a product by ID",
    responses={404: {"description": "No product with that ID."}},
)
def get_product(product_id: uuid.UUID, session: SessionDep, cache: CacheDep) -> ProductRead:
    """Product detail. Public, and served from Redis on a cache hit.

    The hottest read in the catalog, so it is the one that most benefits from
    not touching Postgres.
    """
    return ProductRead.model_validate(
        read_product(
            cache,
            product_id,
            lambda: ProductRead.from_model(ProductService(session).get(product_id)).model_dump(
                mode="json"
            ),
            settings.cache_ttl_seconds,
        )
    )


@router.put(
    "/{product_id}",
    response_model=ProductRead,
    summary="Update a product",
    responses={**COMMON_ERRORS, 404: {"description": "No product with that ID."}},
)
def update_product(
    product_id: uuid.UUID,
    payload: ProductUpdate,
    session: SessionDep,
    cache: CacheDep,
    _: AdminDep,
) -> ProductRead:
    """Partial update. Requires ADMIN. SKU is immutable and cannot be sent.

    The cache entry is deleted rather than overwritten -- see cache.py for why
    overwriting races under concurrent updates.
    """
    service = ProductService(session)
    previous_category = service.get(product_id).category.name
    product = service.update(product_id, payload)
    result = ProductRead.from_model(product)

    invalidate_product(cache, product.product_id, product.sku, result.category)
    if previous_category != result.category:
        # It left one category and joined another; both listings are stale.
        invalidate_product(cache, product.product_id, product.sku, previous_category)
        invalidate_categories(cache)
    return result


@router.delete(
    "/{product_id}",
    response_model=ProductRead,
    summary="Discontinue a product (soft delete)",
    responses={
        **COMMON_ERRORS,
        404: {"description": "No product with that ID."},
        409: {"description": "Already discontinued."},
    },
)
def discontinue_product(
    product_id: uuid.UUID, session: SessionDep, cache: CacheDep, _: AdminDep
) -> ProductRead:
    """Soft delete: sets status to DISCONTINUED so order history stays intact."""
    product = ProductService(session).discontinue(product_id)
    result = ProductRead.from_model(product)
    # Critical to invalidate: a stale cache would keep selling a withdrawn item.
    invalidate_product(cache, product.product_id, product.sku, result.category)
    return result


@category_router.get("", response_model=list[CategoryRead], summary="List categories")
def list_categories(session: SessionDep, cache: CacheDep) -> list[CategoryRead]:
    """Categories currently in use by the catalog. Cached.

    Rendered in the storefront navigation on every page load and changes only
    when a category is first used, which makes it close to an ideal cache
    candidate.
    """
    rows = read_categories(
        cache,
        lambda: [
            CategoryRead.model_validate(c).model_dump(mode="json")
            for c in ProductService(session).list_categories()
        ],
        settings.cache_ttl_seconds,
    )
    return [CategoryRead.model_validate(row) for row in rows]


@category_router.post(
    "",
    response_model=CategoryRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a category",
    responses=COMMON_ERRORS,
)
def create_category(
    payload: CategoryCreate, session: SessionDep, cache: CacheDep, _: AdminDep
) -> CategoryRead:
    """Requires ADMIN. Idempotent: an existing slug is returned as-is."""
    category = ProductService(session).get_or_create_category(payload.name)
    if payload.description and not category.description:
        category.description = payload.description
        session.flush()
    invalidate_categories(cache)
    return CategoryRead.model_validate(category)
