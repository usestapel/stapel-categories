"""Enum components in docs/schema.json are named, and follow the models.

drf-spectacular names a choice set after its property; when one property
name carries several sets it falls back to collision names (``TypeE3eEnum``,
``VisibilityD99Enum``) that change with unrelated edits and generator
versions, renaming the react pair's generated types. The harness pins the
shared sets (``_codegen_settings.enum_name_overrides``), and the editor's
payload serializer reads the model's own choices instead of a typed copy.
"""
import json
import re
from pathlib import Path

import pytest

SCHEMA = Path(__file__).resolve().parent.parent / "docs" / "schema.json"
COLLISION = re.compile(r"[0-9A-F][0-9a-f]{2}Enum$")


def _schemas():
    return json.loads(SCHEMA.read_text())["components"]["schemas"]


def test_no_enum_component_carries_a_collision_name():
    assert [name for name in _schemas() if COLLISION.search(name)
            and not name.endswith("TypeEnum")] == []


def test_the_type_discriminators_are_named_after_their_slugs():
    schemas = _schemas()
    assert schemas["SelectTypeEnum"]["enum"] == ["select"]
    assert schemas["BoolTypeEnum"]["enum"] == ["bool"]


@pytest.mark.parametrize("component,choices", [
    ("FeatureAxisRoleEnum", "AxisRole"),
    ("FeatureVisibilityEnum", "Visibility"),
    ("FeatureTranslateModeEnum", "TranslateMode"),
])
def test_the_pinned_enums_are_the_model_choices(component, choices):
    from stapel_categories.models import Feature

    assert _schemas()[component]["enum"] == list(getattr(Feature, choices).values)


@pytest.mark.parametrize("field,choices", [
    ("axis_role_authored", "AxisRole"),
    ("visibility", "Visibility"),
    ("translate", "TranslateMode"),
])
def test_the_editor_payload_uses_the_model_choices(field, choices):
    from stapel_categories.models import Feature
    from stapel_categories.serializers import FeatureEditorFeatureSerializer

    declared = FeatureEditorFeatureSerializer().fields[field].choices
    assert dict(declared) == dict(getattr(Feature, choices).choices)
