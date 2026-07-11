"""shane_common.ui — shared PySide6 GUI components (requires shane_common[qt])."""

from shane_common.ui.collapsible_section import CollapsibleSection
from shane_common.ui.floating_status_pill import FloatingStatusPill
from shane_common.ui.spellcheck_highlighter import SpellCheckHighlighter, enable_spellcheck

__all__ = ["CollapsibleSection", "FloatingStatusPill", "SpellCheckHighlighter", "enable_spellcheck"]
