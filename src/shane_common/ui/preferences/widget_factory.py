"""Build Qt field editors from shared preference schema definitions."""

from __future__ import annotations

import json
from typing import Any, Callable

from PySide6 import QtWidgets

from shane_common.preferences.models import SettingDefinition


class FieldWidget:
    __slots__ = ("widget", "_getter", "_setter")

    def __init__(
        self,
        widget: QtWidgets.QWidget,
        getter: Callable[[], Any],
        setter: Callable[[Any], None],
    ) -> None:
        self.widget = widget
        self._getter = getter
        self._setter = setter

    def get_value(self) -> Any:
        return self._getter()

    def set_value(self, value: Any) -> None:
        self._setter(value)


def build_field_widget(defn: SettingDefinition, current_value: Any) -> FieldWidget:
    field_type = defn.type

    if field_type == "bool":
        checkbox = QtWidgets.QCheckBox()
        checkbox.setChecked(bool(current_value))
        return FieldWidget(checkbox, checkbox.isChecked, checkbox.setChecked)

    if field_type == "int":
        spin_box = QtWidgets.QSpinBox()
        spin_box.setRange(
            int(defn.min) if defn.min is not None else -2_000_000_000,
            int(defn.max) if defn.max is not None else 2_000_000_000,
        )
        spin_box.setValue(int(current_value))
        return FieldWidget(spin_box, spin_box.value, spin_box.setValue)

    if field_type == "float":
        double_spin_box = QtWidgets.QDoubleSpinBox()
        double_spin_box.setDecimals(4)
        double_spin_box.setSingleStep(1.0)
        double_spin_box.setRange(
            float(defn.min) if defn.min is not None else -1e12,
            float(defn.max) if defn.max is not None else 1e12,
        )
        double_spin_box.setValue(float(current_value))
        return FieldWidget(
            double_spin_box,
            double_spin_box.value,
            double_spin_box.setValue,
        )

    if field_type == "str":
        line_edit = QtWidgets.QLineEdit()
        line_edit.setText(str(current_value))
        return FieldWidget(line_edit, line_edit.text, line_edit.setText)

    if field_type == "enum":
        combo_box = QtWidgets.QComboBox()
        for choice in defn.choices:
            combo_box.addItem(str(choice))
        index = combo_box.findText(str(current_value))
        if index >= 0:
            combo_box.setCurrentIndex(index)

        def _enum_getter() -> Any:
            return combo_box.currentText()

        def _enum_setter(value: Any) -> None:
            option_index = combo_box.findText(str(value))
            if option_index >= 0:
                combo_box.setCurrentIndex(option_index)

        return FieldWidget(combo_box, _enum_getter, _enum_setter)

    if field_type == "list":
        plain_text_edit = QtWidgets.QPlainTextEdit()
        plain_text_edit.setFixedHeight(90)
        plain_text_edit.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        if isinstance(current_value, list):
            plain_text_edit.setPlainText("\n".join(str(value) for value in current_value))
        else:
            plain_text_edit.setPlainText("")

        def _list_getter() -> list:
            raw = plain_text_edit.toPlainText()
            return [line for line in (item.strip() for item in raw.splitlines()) if line]

        def _list_setter(value: Any) -> None:
            if isinstance(value, list):
                plain_text_edit.setPlainText("\n".join(str(item) for item in value))
            else:
                plain_text_edit.setPlainText("")

        return FieldWidget(plain_text_edit, _list_getter, _list_setter)

    if field_type == "dict":
        plain_text_edit = QtWidgets.QPlainTextEdit()
        plain_text_edit.setFixedHeight(90)
        plain_text_edit.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        if isinstance(current_value, dict):
            plain_text_edit.setPlainText(json.dumps(current_value, indent=2))
        else:
            plain_text_edit.setPlainText("{}")

        def _dict_getter() -> dict:
            raw = plain_text_edit.toPlainText().strip()
            try:
                value = json.loads(raw or "{}")
                return value if isinstance(value, dict) else {}
            except (json.JSONDecodeError, ValueError):
                return {}

        def _dict_setter(value: Any) -> None:
            if isinstance(value, dict):
                plain_text_edit.setPlainText(json.dumps(value, indent=2))
            else:
                plain_text_edit.setPlainText("{}")

        return FieldWidget(plain_text_edit, _dict_getter, _dict_setter)

    raise ValueError(
        f"build_field_widget: unsupported schema type {field_type!r} for key {defn.key!r}. "
        "Extend widget_factory.py to support this type."
    )