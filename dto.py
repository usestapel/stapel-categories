"""Dataclass DTOs — the API models of stapel-categories (never ORM instances)."""
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class UndeleteResponse:
    """Restored soft-deleted categories.

    Attributes:
        restored: List of restored category IDs. Example: [1, 2, 3]
    """

    restored: List[int]


@dataclass
class FeatureEditorDraftResponse:
    """Feature editor draft state.

    Attributes:
        draft: JSON-encoded draft string, null if no draft saved. Example: {"features": []}
    """

    draft: Optional[str]
