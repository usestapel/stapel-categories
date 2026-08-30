"""An in-memory ``VocabularyResolver`` so ref-typed features can be tested here.

stapel-attributes 0.5.0 makes a ``ref_select`` / ``ref_hierarchical_select``
config LOUD without a registered resolver — it raises ``INVALID_CONFIG`` at
save time rather than at first submitted value. That is the right upstream
behaviour and it means this module cannot exercise a ref feature at all
without one. The real implementation lives in stapel-vocabularies; this is
the two-level slice of a phone catalogue the ref types were designed against.

Local rather than imported from stapel-attributes' own test package: that
package is not shipped in the wheel, so importing it would make this suite
pass only against a source checkout.
"""
from typing import Dict, Optional, Sequence, Tuple

from stapel_attributes.vocabularies import VocabularyInfo, VocabularyLevel

VOCABULARY = "phones"

LEVELS = (
    VocabularyLevel(name="Vendor"),
    VocabularyLevel(name="Model", parent="Vendor"),
)

TERMS: Dict[str, Dict[str, str]] = {
    "Vendor": {"apple": "Apple", "samsung": "Samsung"},
    "Model": {"iphone-15": "iPhone 15", "galaxy-s24": "Galaxy S24"},
}

#: (parent_level, parent_code, child_level, child_code)
EDGES: Tuple[Tuple[str, str, str, str], ...] = (
    ("Vendor", "apple", "Model", "iphone-15"),
    ("Vendor", "samsung", "Model", "galaxy-s24"),
)


class FakeVocabularyResolver:
    """Reads the tables above."""

    def describe(self, vocabulary: str) -> Optional[VocabularyInfo]:
        if vocabulary != VOCABULARY:
            return None
        return VocabularyInfo(slug=VOCABULARY, levels=LEVELS)

    def exists(self, vocabulary: str, level: str, code: str) -> bool:
        return vocabulary == VOCABULARY and code in TERMS.get(level, {})

    def is_child(
        self, vocabulary: str, level: str, code: str, parent_level: str, parent_code: str
    ) -> bool:
        return (
            vocabulary == VOCABULARY
            and (parent_level, parent_code, level, code) in EDGES
        )

    def labels(self, vocabulary: str, level: str, codes: Sequence[str]) -> Dict[str, str]:
        if vocabulary != VOCABULARY:
            return {}
        known = TERMS.get(level, {})
        return {code: known[code] for code in codes if code in known}


__all__ = ["EDGES", "LEVELS", "TERMS", "VOCABULARY", "FakeVocabularyResolver"]
