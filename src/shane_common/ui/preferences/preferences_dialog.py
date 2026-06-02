"""Modal schema-driven preferences dialog."""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from shane_common.preferences.manager import SettingsManager
from shane_common.ui.preferences.category_editor import CategoryEditor


class PreferencesDialog(QtWidgets.QDialog):
    def __init__(
        self,
        settings_manager: SettingsManager,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        self._manager = settings_manager
        self._editors: dict[str, CategoryEditor] = {}

        self.setWindowTitle("Preferences")
        self.resize(800, 520)
        self.setModal(True)

        self._category_list = QtWidgets.QListWidget(self)
        self._category_list.setMinimumWidth(160)
        self._category_list.setMaximumWidth(240)
        self._category_list.setAlternatingRowColors(True)

        self._stack = QtWidgets.QStackedWidget(self)

        for category in settings_manager.categories():
            initial_values = {
                key: settings_manager.get(category.category_id, key)
                for key in category.keys
            }
            editor = CategoryEditor(category, initial_values, parent=self._stack)
            self._editors[category.category_id] = editor
            self._stack.addWidget(editor)

            item = QtWidgets.QListWidgetItem(category.label or category.category_id)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, category.category_id)
            self._category_list.addItem(item)

        if self._category_list.count() > 0:
            self._category_list.setCurrentRow(0)
            self._stack.setCurrentIndex(0)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._category_list)
        splitter.addWidget(self._stack)
        splitter.setSizes([200, 600])

        self._btn_reset = QtWidgets.QPushButton("Reset to Defaults")
        self._btn_cancel = QtWidgets.QPushButton("Cancel")
        self._btn_save = QtWidgets.QPushButton("Save")
        self._btn_save.setDefault(True)

        self._btn_reset.setToolTip(
            "Reset the current category's fields to their registered defaults.\n"
            "Does not affect other categories and does not save to disk."
        )
        self._btn_cancel.setToolTip("Close without saving any changes.")
        self._btn_save.setToolTip("Save all changes to disk and close.")

        button_bar = QtWidgets.QHBoxLayout()
        button_bar.addStretch(1)
        button_bar.addWidget(self._btn_reset)
        button_bar.addWidget(self._btn_cancel)
        button_bar.addWidget(self._btn_save)

        root = QtWidgets.QVBoxLayout(self)
        root.addWidget(splitter, 1)
        root.addLayout(button_bar)

        self._category_list.currentRowChanged.connect(self._on_category_row_changed)
        self._btn_cancel.clicked.connect(self.reject)
        self._btn_save.clicked.connect(self._on_save)
        self._btn_reset.clicked.connect(self._on_reset_current)

    @QtCore.Slot(int)
    def _on_category_row_changed(self, row: int) -> None:
        if row >= 0:
            self._stack.setCurrentIndex(row)

    @QtCore.Slot()
    def _on_save(self) -> None:
        errors: list[str] = []

        for category_id, editor in self._editors.items():
            draft = editor.get_draft()
            for key, value in draft.items():
                try:
                    self._manager.set(category_id, key, value)
                except (KeyError, ValueError) as exc:
                    errors.append(f"  {category_id}.{key}: {exc}")

        if errors:
            QtWidgets.QMessageBox.critical(
                self,
                "Validation Error",
                "One or more settings could not be applied:\n\n" + "\n".join(errors),
            )
            return

        try:
            self._manager.save()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(
                self,
                "Save Error",
                f"Could not write settings file:\n\n{exc}",
            )
            return

        self.accept()

    @QtCore.Slot()
    def _on_reset_current(self) -> None:
        row = self._category_list.currentRow()
        item = self._category_list.item(row)
        if item is None:
            return
        category_id: str = item.data(QtCore.Qt.ItemDataRole.UserRole)
        editor = self._editors.get(category_id)
        if editor is not None:
            editor.reset_to_defaults()