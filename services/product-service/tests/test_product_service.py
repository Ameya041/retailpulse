"""Unit tests for the product domain layer and shared auth helpers.

These bypass HTTP entirely -- the rules should hold regardless of transport.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.schemas import ProductCreate, ProductUpdate
from app.service import ProductService, slugify
from retailpulse_common.auth import (
    Role,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from retailpulse_common.errors import ConflictError, NotFoundError, ValidationError
from retailpulse_common.pagination import Page, PageParams

# At least 32 bytes: PyJWT warns below that for HS256, and the warning would be
# a real finding if it appeared against a production key.
SECRET = "unit-test-secret-key-long-enough-for-hs256"


def _payload(**overrides) -> ProductCreate:
    data = {
        "sku": "SKU-UNIT-001",
        "name": "Unit product",
        "category": "Electronics",
        "brand": "Acme",
        "price": Decimal("100.50"),
        "currency": "inr",
    }
    data.update(overrides)
    return ProductCreate(**data)


# ---------------------------------------------------------------------------
# Slugs
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Electronics", "electronics"),
        ("Home Appliances", "home-appliances"),
        ("  Sports & Outdoors  ", "sports-outdoors"),
        ("Baby/Kids", "baby-kids"),
    ],
)
def test_slugify_normalises_category_names(raw, expected):
    assert slugify(raw) == expected


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------
def test_price_round_trips_as_exact_decimal(session):
    """Money must not go through binary float."""
    product = ProductService(session).create(_payload(price=Decimal("19.99")))
    assert Decimal(str(product.price)) == Decimal("19.99")


def test_currency_is_normalised_to_uppercase(session):
    product = ProductService(session).create(_payload(currency="inr"))
    assert product.currency == "INR"


def test_zero_price_is_rejected_by_the_schema():
    with pytest.raises(PydanticValidationError):
        _payload(price=Decimal("0.00"))


# ---------------------------------------------------------------------------
# Service rules
# ---------------------------------------------------------------------------
def test_duplicate_sku_raises_conflict(session):
    service = ProductService(session)
    service.create(_payload())

    with pytest.raises(ConflictError):
        service.create(_payload(name="Different name"))


def test_get_unknown_id_raises_not_found(session):
    with pytest.raises(NotFoundError):
        ProductService(session).get(uuid.uuid4())


def test_categories_are_deduplicated_by_slug(session):
    service = ProductService(session)
    first = service.get_or_create_category("Home Appliances")
    second = service.get_or_create_category("home appliances")

    assert first.category_id == second.category_id


def test_blank_category_name_is_rejected(session):
    with pytest.raises(ValidationError):
        ProductService(session).get_or_create_category("!!!")


def test_discontinue_is_idempotent_only_once(session):
    service = ProductService(session)
    product = service.create(_payload())

    service.discontinue(product.product_id)

    with pytest.raises(ConflictError):
        service.discontinue(product.product_id)


def test_update_leaves_unsupplied_fields_untouched(session):
    service = ProductService(session)
    product = service.create(_payload(name="Original", brand="Acme"))

    updated = service.update(product.product_id, ProductUpdate(name="Renamed"))

    assert updated.name == "Renamed"
    assert updated.brand == "Acme"


def test_short_search_term_is_rejected(session):
    with pytest.raises(ValidationError):
        ProductService(session).search("a", PageParams())


def test_search_is_scoped_to_active_products_by_default(session):
    service = ProductService(session)
    product = service.create(_payload(name="Findable widget"))
    service.discontinue(product.product_id)

    items, total = service.search("Findable", PageParams())

    assert total == 0
    items, total = service.search("Findable", PageParams(), include_discontinued=True)
    assert total == 1


def test_get_many_returns_empty_for_empty_input(session):
    assert ProductService(session).get_many([]) == []


# ---------------------------------------------------------------------------
# Pagination maths
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("total", "page_size", "expected_pages"),
    [(0, 20, 0), (1, 20, 1), (20, 20, 1), (21, 20, 2), (100, 7, 15)],
)
def test_total_pages_rounds_up(total, page_size, expected_pages):
    page = Page.build([], total, PageParams(page=1, page_size=page_size))
    assert page.total_pages == expected_pages


def test_offset_is_derived_from_one_indexed_page():
    assert PageParams(page=1, page_size=20).offset == 0
    assert PageParams(page=3, page_size=20).offset == 40


# ---------------------------------------------------------------------------
# Auth primitives
# ---------------------------------------------------------------------------
def test_password_hash_is_not_the_plaintext():
    hashed = hash_password("correct horse battery staple")
    assert hashed != "correct horse battery staple"
    assert hashed.startswith("$2")  # bcrypt


def test_password_verification_round_trip():
    hashed = hash_password("s3cret-pass")
    assert verify_password("s3cret-pass", hashed) is True
    assert verify_password("wrong-pass", hashed) is False


def test_same_password_hashes_differently_each_time():
    """Per-password salt: identical passwords must not share a hash."""
    assert hash_password("same") != hash_password("same")


def test_token_round_trip_preserves_identity_and_role():
    user_id = uuid.uuid4()
    token = create_access_token(
        user_id=user_id, email="a@b.com", role=Role.ADMIN, secret_key=SECRET
    )

    payload = decode_access_token(token, secret_key=SECRET)

    assert payload.user_id == user_id
    assert payload.role is Role.ADMIN


def test_token_signed_with_another_key_is_rejected():
    from retailpulse_common.errors import UnauthorizedError

    token = create_access_token(
        user_id=uuid.uuid4(),
        email="a@b.com",
        role=Role.ADMIN,
        secret_key="attacker-controlled-key-also-32-bytes-plus",
    )

    with pytest.raises(UnauthorizedError):
        decode_access_token(token, secret_key=SECRET)


def test_expired_token_is_rejected():
    from retailpulse_common.errors import UnauthorizedError

    token = create_access_token(
        user_id=uuid.uuid4(),
        email="a@b.com",
        role=Role.CUSTOMER,
        secret_key=SECRET,
        expires_minutes=-1,
    )

    with pytest.raises(UnauthorizedError):
        decode_access_token(token, secret_key=SECRET)
