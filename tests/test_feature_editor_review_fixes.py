"""Repro-driven tests for the catalog feature-editor review findings.

Each test reproduces the minimal tree scenario from the review report
(tasks/fable/done/review-catalog-feature-editor.md) and asserts the fixed
behaviour: H-1 (slug dedup / override wins), H-2 (edit fans out revision +
category.changed), H-3/L-8 (draft save emits no phantom revision), M-4
(server-side available_actions), M-5 (optimistic base_revision -> 409 +
lock), L-9 (replace same-tree), L-11 (inherit slug matches source).
"""
import pytest

from stapel_categories.feature_editor import (
    FeatureEditorConflict,
    FeatureEditorError,
    FeatureEditorItem,
    apply_feature_editor_changes,
    build_editor_state,
)
from stapel_categories.models import Category, CategoryFeature, Feature


# ---------------------------------------------------------------------------
# Fixtures (self-contained; mirrors of the report's minimal trees)
# ---------------------------------------------------------------------------


@pytest.fixture
def root_cat():
    return Category.objects.create(name="Root", slug="root", draft="")


@pytest.fixture
def child_cat(root_cat):
    return Category.objects.create(name="Child", slug="child", tn_parent=root_cat, draft="")


def _color_feature():
    return Feature.objects.create(slug="color", name="Color", config={"type": "string"})


# ---------------------------------------------------------------------------
# H-1: override closest to the category wins; slug appears once
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_h1_inherit_override_wins_and_slug_appears_once(root_cat, child_cat):
    """R->C, C inherits color; C's resolved schema uses the child override and
    slug 'color' appears exactly once (not the parent version, not a dup)."""
    color = _color_feature()
    root_cat.features.add(color)
    # Mirror copy_parent_features: the child links the same root feature (the
    # copy runs at child-creation time, before this test adds color to root).
    CategoryFeature.objects.create(category=child_cat, feature=color, order=0)

    # Child overrides color via inherit -> new Feature row, same slug.
    apply_feature_editor_changes(
        child_cat,
        [FeatureEditorItem(
            action="inherit", order=0,
            feature={"id": color.pk, "slug": "color", "name": "Color (child)",
                     "config": {"type": "string"}},
        )],
    )

    resolved = list(child_cat.get_all_features())
    slugs = [f.slug for f in resolved]
    assert slugs.count("color") == 1, f"slug must appear once, got {slugs}"

    override = resolved[0]
    assert override.pk != color.pk, "child must resolve to its own override row"
    assert override.tn_parent_id == color.pk

    defs = child_cat.feature_defs()
    assert [d["slug"] for d in defs].count("color") == 1


@pytest.mark.django_db
def test_h1_slugless_features_not_collapsed(root_cat):
    """Two slug-less header features must both survive dedup."""
    h1 = Feature.objects.create(slug="", name="H1", config={"type": "header"})
    h2 = Feature.objects.create(slug="", name="H2", config={"type": "header"})
    CategoryFeature.objects.create(category=root_cat, feature=h1, order=0)
    CategoryFeature.objects.create(category=root_cat, feature=h2, order=1)
    assert list(root_cat.get_all_features().values_list("pk", flat=True)) == [h1.pk, h2.pk]


# ---------------------------------------------------------------------------
# H-2: edit fans out revision bump + category.changed to all categories
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_h2_edit_bumps_feature_revision_and_fans_out(root_cat):
    """edit via Feature.save(): the feature revision bumps and category.changed
    fans out to EVERY category carrying the feature (root + a sibling)."""
    from stapel_core.comm import subscribe_action

    size = Feature.objects.create(slug="size", name="Size", config={"type": "string"})
    sibling = Category.objects.create(name="Sib", slug="sib", draft="")
    root_cat.features.add(size)
    sibling.features.add(size)

    feat_rev_before = size.revision

    received = []
    subscribe_action("category.changed", lambda event: received.append(event.payload))

    apply_feature_editor_changes(
        root_cat,
        [FeatureEditorItem(
            action="edit", order=0,
            feature={"id": size.pk, "name": "Size!", "slug": "size",
                     "config": {"type": "string"}, "mandatory": True},
        )],
    )

    size.refresh_from_db()
    assert size.name == "Size!" and size.mandatory is True
    assert size.revision > feat_rev_before, "edit must bump the feature revision"
    # The .update() path emitted for nothing; save() fans out to both categories.
    changed_ids = {p["category_id"] for p in received}
    assert root_cat.pk in changed_ids
    assert sibling.pk in changed_ids, "sibling carrying the feature must be invalidated"


@pytest.mark.django_db
def test_h2_edit_keeps_icon(root_cat):
    """L-10: edit no longer drops the icon field."""
    f = Feature.objects.create(slug="mat", name="Material", config={"type": "string"},
                               icon="icons/mat")
    root_cat.features.add(f)
    apply_feature_editor_changes(
        root_cat,
        [FeatureEditorItem(
            action="edit", order=0,
            feature={"id": f.pk, "name": "Material", "slug": "mat",
                     "config": {"type": "string"}, "icon": "icons/mat2"},
        )],
    )
    f.refresh_from_db()
    assert f.icon == "icons/mat2"


@pytest.mark.django_db
def test_h2_edit_invalid_config_rejected(root_cat):
    """L-10: an invalid config is now rejected (clean) instead of written."""
    f = Feature.objects.create(slug="c", name="C", config={"type": "string"})
    root_cat.features.add(f)
    with pytest.raises(Exception):
        apply_feature_editor_changes(
            root_cat,
            [FeatureEditorItem(action="edit", order=0,
                               feature={"id": f.pk, "slug": "c", "config": {}})],
        )


# ---------------------------------------------------------------------------
# M-4: server enforces available_actions (edit/remove of inherited slug -> error)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_m4_edit_inherited_slug_rejected(root_cat, child_cat):
    weight = Feature.objects.create(slug="weight", name="Weight", config={"type": "string"})
    root_cat.features.add(weight)  # inherited by child via copy_parent_features
    with pytest.raises(FeatureEditorError):
        apply_feature_editor_changes(
            child_cat,
            [FeatureEditorItem(
                action="edit", order=0,
                feature={"id": weight.pk, "slug": "weight", "name": "HACK",
                         "config": {"type": "string"}},
            )],
        )
    weight.refresh_from_db()
    assert weight.name == "Weight", "parent feature must be untouched"


@pytest.mark.django_db
def test_m4_remove_inherited_slug_rejected(root_cat, child_cat):
    weight = Feature.objects.create(slug="weight", name="Weight", config={"type": "string"})
    root_cat.features.add(weight)
    with pytest.raises(FeatureEditorError):
        apply_feature_editor_changes(
            child_cat,
            [FeatureEditorItem(action="remove", order=0,
                               feature={"id": weight.pk, "slug": "weight"})],
        )


# ---------------------------------------------------------------------------
# M-5: optimistic base_revision -> conflict; build_editor_state exposes revision
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_m5_stale_base_revision_conflicts(root_cat):
    color = _color_feature()
    root_cat.features.add(color)
    stale = build_editor_state(root_cat)["revision"]

    # Another editor commits, bumping the revision.
    apply_feature_editor_changes(
        root_cat,
        [FeatureEditorItem(action="create", order=1,
                           feature={"slug": "size", "name": "Size",
                                    "config": {"type": "string"}})],
    )

    with pytest.raises(FeatureEditorConflict):
        apply_feature_editor_changes(
            root_cat,
            [FeatureEditorItem(action="keep", order=0,
                               feature={"id": color.pk, "slug": "color"})],
            base_revision=stale,
        )


@pytest.mark.django_db
def test_m5_matching_base_revision_applies(root_cat):
    color = _color_feature()
    root_cat.features.add(color)
    rev = build_editor_state(root_cat)["revision"]
    apply_feature_editor_changes(
        root_cat,
        [FeatureEditorItem(action="keep", order=0,
                           feature={"id": color.pk, "slug": "color"})],
        base_revision=rev,
    )  # no raise


# ---------------------------------------------------------------------------
# L-9 / L-11: replace same-tree; inherit slug must match source
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_l9_replace_cross_tree_rejected(root_cat):
    color = _color_feature()
    unrelated = Feature.objects.create(slug="onoff", name="OnOff", config={"type": "bool"})
    root_cat.features.add(color)
    with pytest.raises(FeatureEditorError):
        apply_feature_editor_changes(
            root_cat,
            [FeatureEditorItem(action="replace", order=0,
                               feature={"id": color.pk, "slug": "color"},
                               replace_with=unrelated.pk)],
        )


@pytest.mark.django_db
def test_l11_inherit_slug_mismatch_rejected(root_cat, child_cat):
    color = _color_feature()
    root_cat.features.add(color)
    with pytest.raises(FeatureEditorError):
        apply_feature_editor_changes(
            child_cat,
            [FeatureEditorItem(
                action="inherit", order=0,
                feature={"id": color.pk, "slug": "colour",  # wrong slug
                         "name": "Colour", "config": {"type": "string"}},
            )],
        )


# ---------------------------------------------------------------------------
# H-3 / L-8: draft save/clear must not bump revision (no phantom revision)
# ---------------------------------------------------------------------------


from django.test import TestCase  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from stapel_core.django.users.models import User  # noqa: E402


class DraftRevisionTests(TestCase):
    """H-3/L-8: draft is editor scratch state — persisting it must never bump
    the category revision nor emit a phantom category.changed."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_superuser(
            username="admin", email="admin@test.com", password="pw"
        )
        self.client.force_authenticate(user=self.user)
        self.cat = Category.objects.create(name="Cat", slug="cat", draft="")
        self.base = f"/catalog/api/categories/{self.cat.pk}"

    def test_l8_draft_save_does_not_bump_revision(self):
        rev = Category.objects.get(pk=self.cat.pk).revision
        resp = self.client.post(
            f"{self.base}/feature-editor/draft/", {"draft": "hello"}, format="json"
        )
        self.assertEqual(resp.status_code, 200)
        row = Category.objects.get(pk=self.cat.pk)
        self.assertEqual(row.revision, rev, "draft save must not bump revision")
        self.assertEqual(row.draft, "hello", "draft must be persisted")

    def test_h3_apply_clear_draft_no_phantom_revision(self):
        """After apply, the revision the client sees equals the DB revision and
        is stable across a reload — no phantom (skipped) revision number."""
        self.cat.draft = "scratch"
        Category.objects.filter(pk=self.cat.pk).update(draft="scratch")
        color = _color_feature()
        resp = self.client.post(
            f"{self.base}/feature-editor/apply/",
            {"features": [{"action": "add", "order": 0,
                           "feature": {"id": color.pk, "slug": "color"}}]},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["draft"], "")  # cleared
        applied_rev = resp.data["revision"]
        db_rev = Category.objects.get(pk=self.cat.pk).revision
        self.assertEqual(applied_rev, db_rev)
        # Reload editor: revision unchanged (draft-clear emitted no phantom).
        state = self.client.get(f"{self.base}/feature-editor/")
        self.assertEqual(state.data["revision"], db_rev)

    def test_m5_apply_stale_base_revision_returns_409(self):
        color = _color_feature()
        self.cat.features.add(color)
        stale = self.client.get(f"{self.base}/feature-editor/").data["revision"]
        # Bump revision behind the editor's back.
        self.cat.name = "renamed"
        self.cat.save()
        resp = self.client.post(
            f"{self.base}/feature-editor/apply/",
            {"base_revision": stale,
             "features": [{"action": "keep", "order": 0,
                           "feature": {"id": color.pk, "slug": "color"}}]},
            format="json",
        )
        self.assertEqual(resp.status_code, 409)
