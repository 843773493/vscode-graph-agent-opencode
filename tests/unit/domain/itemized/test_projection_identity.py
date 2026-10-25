from __future__ import annotations

import pytest

from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
    ProjectionPartRef,
)


@pytest.mark.parametrize("indices", [(), (0,), (0, 2, 5)])
def test_content_part_refs_accept_strictly_increasing_indices(
    indices: tuple[int, ...],
) -> None:
    identity = ItemProjectionIdentity(
        content_part_refs=tuple(
            ProjectionPartRef(id=f"part-{index}", index=index)
            for index in indices
        )
    )

    assert tuple(ref.index for ref in identity.content_part_refs) == indices


@pytest.mark.parametrize("indices", [(1, 0), (0, 0), (0, 2, 1)])
def test_content_part_refs_reject_unsorted_or_duplicate_indices(
    indices: tuple[int, ...],
) -> None:
    with pytest.raises(ItemSchemaError, match="有序且唯一"):
        ItemProjectionIdentity(
            content_part_refs=tuple(
                ProjectionPartRef(id=f"part-{ordinal}", index=index)
                for ordinal, index in enumerate(indices)
            )
        )


def test_content_part_refs_type_error_precedes_order_error() -> None:
    with pytest.raises(ItemSchemaError, match="typed ProjectionPartRef"):
        ItemProjectionIdentity(
            content_part_refs=(
                ProjectionPartRef(id="part-2", index=2),
                ProjectionPartRef(id="part-1", index=1),
                object(),  # type: ignore[arg-type]
            )
        )
