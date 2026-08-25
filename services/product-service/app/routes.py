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

from app.deps import get_db_session, require_roles
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
from retailpulse_common.pagination import Page, PageParams, page_params

router = APIRouter(prefix="/products", tags=["products"])
category_router = APIRouter(prefix="/categories", tags=["categories"])

SessionDep = Annotated[Session, Depends(get_db_session)]
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
def create_product(payload: ProductCreate, session: SessionDep, _: AdminDep) -> ProductRead:
    """Add a catalog entry. Requires ADMIN. SKU must be globally unique."""
    product = ProductService(session).create(payload)
    return ProductRead.from_model(product)


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
def get_product_by_sku(sku: str, session: SessionDep) -> ProductRead:
    """Warehouse-facing lookup: SKU is what is printed on the label."""
    return ProductRead.from_model(ProductService(session).get_by_sku(sku))


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
def get_product(product_id: uuid.UUID, session: SessionDep) -> ProductRead:
    """Product detail. Public."""
    return ProductRead.from_model(ProductService(session).get(product_id))


@router.put(
    "/{product_id}",
    response_model=ProductRead,
    summary="Update a product",
    responses={**COMMON_ERRORS, 404: {"description": "No product with that ID."}},
)
def update_product(
    product_id: uuid.UUID, payload: ProductUpdate, session: SessionDep, _: AdminDep
) -> ProductRead:
    """Partial update. Requires ADMIN. SKU is immutable and cannot be sent."""
    return ProductRead.from_model(ProductService(session).update(product_id, payload))


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
    product_id: uuid.UUID, session: SessionDep, _: AdminDep
) -> ProductRead:
    """Soft delete: sets status to DISCONTINUED so order history stays intact."""
    return ProductRead.from_model(ProductService(session).discontinue(product_id))


@category_router.get("", response_model=list[CategoryRead], summary="List categories")
def list_categories(session: SessionDep) -> list[CategoryRead]:
    """Categories currently in use by the catalog."""
    return [CategoryRead.model_validate(c) for c in ProductService(session).list_categories()]


@category_router.post(
    "",
    response_model=CategoryRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a category",
    responses=COMMON_ERRORS,
)
def create_category(
    payload: CategoryCreate, session: SessionDep, _: AdminDep
) -> CategoryRead:
    """Requires ADMIN. Idempotent: an existing slug is returned as-is."""
    category = ProductService(session).get_or_create_category(payload.name)
    if payload.description and not category.description:
        category.description = payload.description
        session.flush()
    return CategoryRead.model_validate(category)
