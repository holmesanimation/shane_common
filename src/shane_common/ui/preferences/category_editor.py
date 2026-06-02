"""Schema-driven editor widget for a single settings category."""

from __future__ import annotations

from typing import Any

from PySide6 import QtCore, QtWidgets

from shane_common.preferences.models import SettingsCategory
from shane_common.ui.preferences.widget_factory import FieldWidget, build_field_widget


class CategoryEditor(QtWidgets.QWidget):
    def __init__(
        self,
        category: SettingsCategory,
        initial_values: dict[str, Any],
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        self._category = category
        self._initial_values: dict[str, Any] = dict(initial_values)
        self._field_widgets: dict[str, FieldWidget] = {}

        scroll = QtWidgets.QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)

        container = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(container)
        form.setContentsMargins(12, 16, 12, 16)
        form.setSpacing(12)
        form.setLabelAlignment(
            QtCore.Qt.AlignmentFlag.AlignRight
            | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        form.setFieldGrowthPolicy(
            QtWidgets.QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )

        for definition in category.definitions:
            current = initial_values.get(definition.key, definition.default)
            field_widget = build_field_widget(definition, current)
            self._field_widgets[definition.key] = field_widget

            label_text = definition.label if definition.label else definition.key
            label = QtWidgets.QLabel(label_text + ":", container)

            if definition.description:
                label.setToolTip(definition.description)
                field_widget.widget.setToolTip(definition.description)

            form.addRow(label, field_widget.widget)

        scroll.setWidget(container)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

    @property
    def category_id(self) -> str:
        return self._category.category_id

    def get_draft(self) -> dict[str, Any]:
        return {key: field_widget.get_value() for key, field_widget in self._field_widgets.items()}

    def reset_to_defaults(self) -> None:
        for definition in self._category.definitions:
            field_widget = self._field_widgets.get(definition.key)
            if field_widget is not None:
                field_widget.set_value(definition.default)

    def is_dirty(self) -> bool:
        for key, field_widget in self._field_widgets.items():
            if field_widget.get_value() != self._initial_values.get(key):
                return True
        return False