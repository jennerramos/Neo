"""Shared Pydantic schemas."""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, create_model


def pick(name: str, base: type[BaseModel], fields: tuple[str, ...]) -> type[BaseModel]:
    """Pydantic equivalent of TypeScript's ``Pick<T, K>``.

    Build a new model whose fields are a strict subset of ``base``'s, copying
    each field's annotation and default.  Use this for projection schemas
    (e.g. ``VoteSummary`` is a projection of ``VoteRow``) so the row schema
    remains the single source of truth — renaming a field on the row will
    fail-fast at import time if the projection still references the old name.

    See refactor_candidates.md #2.
    """
    missing = [f for f in fields if f not in base.model_fields]
    if missing:
        raise ValueError(
            f"pick({name}, {base.__name__}): fields {missing} not in {base.__name__}"
        )
    return create_model(  # type: ignore[call-overload]
        name,
        **{
            f: (base.model_fields[f].annotation, base.model_fields[f].default)
            for f in fields
        },
    )


class School(BaseModel):
    school_id: int
    slug:      str
    name:      str
    website:   Optional[str] = None
    state:     Optional[str] = None

    model_config = {"from_attributes": True}


class Pagination(BaseModel):
    total:    int
    limit:    int
    offset:   int
    has_more: bool = False

    model_config = {"from_attributes": True}

    @classmethod
    def build(cls, total: int, limit: int, offset: int) -> "Pagination":
        return cls(
            total=total,
            limit=limit,
            offset=offset,
            has_more=(offset + limit) < total,
        )
