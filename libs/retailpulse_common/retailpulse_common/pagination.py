"""Pagination primitives.

Every list endpoint is paginated. An unbounded ``GET /products`` is fine with
50 rows and fatal with 5 million: it pins a worker, balloons memory, and pushes
a huge payload through the gateway. ``page_size`` is capped server-side so a
client cannot opt out.
"""

from __future__ import annotations

from typing import Annotated, Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel, Field

T = TypeVar("T")

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20


class PageParams(BaseModel):
    """Validated page/page_size pair."""

    page: int = Field(default=1, ge=1, description="1-indexed page number.")
    page_size: int = Field(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Rows per page (max {MAX_PAGE_SIZE}).",
    )

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size


def page_params(
    page: Annotated[int, Query(ge=1, description="1-indexed page number.")] = 1,
    page_size: Annotated[
        int, Query(ge=1, le=MAX_PAGE_SIZE, description=f"Rows per page (max {MAX_PAGE_SIZE}).")
    ] = DEFAULT_PAGE_SIZE,
) -> PageParams:
    """FastAPI dependency yielding :class:`PageParams`."""
    return PageParams(page=page, page_size=page_size)


class Page(BaseModel, Generic[T]):
    """Envelope returned by every list endpoint."""

    items: list[T]
    page: int
    page_size: int
    total: int
    total_pages: int
    has_next: bool
    has_previous: bool

    @classmethod
    def build(cls, items: list[T], total: int, params: PageParams) -> Page[T]:
        total_pages = (total + params.page_size - 1) // params.page_size if total else 0
        return cls(
            items=items,
            page=params.page,
            page_size=params.page_size,
            total=total,
            total_pages=total_pages,
            has_next=params.page < total_pages,
            has_previous=params.page > 1,
        )
