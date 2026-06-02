"""Shared schema-driven preferences UI widgets."""

from shane_common.ui.preferences.category_editor import CategoryEditor
from shane_common.ui.preferences.preferences_dialog import PreferencesDialog
from shane_common.ui.preferences.widget_factory import FieldWidget, build_field_widget

__all__ = [
    "CategoryEditor",
    "FieldWidget",
    "PreferencesDialog",
    "build_field_widget",
]