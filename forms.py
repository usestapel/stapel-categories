"""Admin forms for Category and Feature.

The Feature ``config`` field renders through stapel-attributes'
schema-driven ``ConfigEditorWidget`` (resolved via the ``ADMIN_WIDGETS`` seam
so a host can swap it). Config validation itself is delegated to
stapel-attributes — the form only marshals JSON and surfaces errors.
"""
import json

from django import forms
from django.core.exceptions import ValidationError
from treenode.forms import TreeNodeForm

from stapel_attributes import get_config_editor_widget

from .models import Category, Feature


class FeatureAdminForm(TreeNodeForm):
    """Admin form for Feature with the schema-driven config editor."""

    class Meta:
        model = Feature
        fields = [
            "tn_parent", "name", "slug", "icon", "comment", "config",
            "mandatory", "show_as_badge", "show_at_title", "tn_priority",
        ]
        widgets = {
            "comment": forms.Textarea(attrs={"rows": 2, "cols": 50}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "config" in self.fields:
            # Resolve the config editor widget through the attributes seam.
            self.fields["config"].widget = get_config_editor_widget("config")()

    def clean_config(self):
        """Marshal + validate config JSON via stapel-attributes."""
        from stapel_attributes import validate_feature_config

        config = self.cleaned_data.get("config")
        if not config:
            config = {}

        if isinstance(config, str):
            try:
                config = json.loads(config)
            except json.JSONDecodeError as e:
                raise ValidationError(f"Invalid JSON: {e}")

        if not isinstance(config, dict):
            raise ValidationError("Config must be a JSON object")

        if "type" not in config:
            raise ValidationError("Config must include 'type' field")

        try:
            validate_feature_config(config)
        except (ValidationError, ValueError) as e:
            raise ValidationError(str(e))

        return config


class CategoryAdminForm(TreeNodeForm):
    class Meta:
        model = Category
        exclude = ["features"]
        widgets = {
            "name": forms.TextInput(attrs={"size": 50, "placeholder": "Category name"}),
            "draft": forms.Textarea(
                attrs={"rows": 4, "cols": 80, "style": "font-family: monospace; white-space: pre;"}
            ),
        }
