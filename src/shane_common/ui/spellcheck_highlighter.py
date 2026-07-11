# shane_common/ui/spellcheck_highlighter.py
"""Reusable spell-check syntax highlighter for QTextEdit / QPlainTextEdit.

Requires: shane_common[qt]  (PySide6>=6.5, pyspellchecker>=0.8)

Usage
-----
Attach to any document-based editor::

    from shane_common.ui.spellcheck_highlighter import enable_spellcheck

    self._note_editor = QTextEdit()
    enable_spellcheck(self._note_editor, ignored_words={"MyBrand", "Qt"})

Keep the returned highlighter (or use ``enable_spellcheck`` which stores it on
the editor as ``_spellcheck_highlighter``) so it is not garbage-collected.
"""

from __future__ import annotations

import re
from typing import Iterable

from PySide6.QtGui import QColor, QSyntaxHighlighter, QTextCharFormat


class SpellCheckHighlighter(QSyntaxHighlighter):
    """Underlines misspelled words with a red squiggly (SpellCheckUnderline).

    Parameters
    ----------
    document:
        The ``QTextDocument`` to attach to (``editor.document()``).
    language:
        BCP-47 language code understood by *pyspellchecker* (default ``"en"``).
    ignored_words:
        Extra words to treat as correctly spelled (case-insensitive).
    """

    WORD_RE = re.compile(r"\b[A-Za-z][A-Za-z']*\b")

    def __init__(
        self,
        document,
        *,
        language: str = "en",
        ignored_words: Iterable[str] | None = None,
    ) -> None:
        super().__init__(document)

        # Import lazily so the rest of shane_common still loads without
        # pyspellchecker when the `qt` extra is not installed.
        from spellchecker import SpellChecker  # type: ignore[import]

        self._spell = SpellChecker(language=language)
        self._ignored: set[str] = {w.lower() for w in (ignored_words or [])}

        self._fmt = QTextCharFormat()
        self._fmt.setUnderlineColor(QColor("red"))
        self._fmt.setUnderlineStyle(QTextCharFormat.UnderlineStyle.SpellCheckUnderline)

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def add_ignored_word(self, word: str) -> None:
        """Add a single word to the ignore list and re-highlight."""
        self._ignored.add(word.lower())
        self.rehighlight()

    def set_ignored_words(self, words: Iterable[str]) -> None:
        """Replace the ignore list and re-highlight."""
        self._ignored = {w.lower() for w in words}
        self.rehighlight()

    # ------------------------------------------------------------------
    # QSyntaxHighlighter interface
    # ------------------------------------------------------------------

    def highlightBlock(self, text: str) -> None:  # noqa: N802  (Qt naming)
        for match in self.WORD_RE.finditer(text):
            raw = match.group(0)
            word = raw.lower().strip("'")

            if not word or len(word) < 2:
                continue

            if self._should_skip(word):
                continue

            if word in self._ignored:
                continue

            if self._spell.unknown([word]):
                self.setFormat(match.start(), len(raw), self._fmt)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    @staticmethod
    def _should_skip(word: str) -> bool:
        """Return True for tokens that should never be spell-checked."""
        # All-caps acronyms (API, URL, …)
        if word.isupper() and len(word) > 1:
            return True
        # Snake-case identifiers
        if "_" in word:
            return True
        # Anything containing a digit
        if any(c.isdigit() for c in word):
            return True
        return False


# ---------------------------------------------------------------------------
# Convenience helper
# ---------------------------------------------------------------------------

def enable_spellcheck(
    editor,
    *,
    language: str = "en",
    ignored_words: Iterable[str] | None = None,
) -> "SpellCheckHighlighter | None":
    """Attach a :class:`SpellCheckHighlighter` to *editor* and return it.

    Returns ``None`` silently if *pyspellchecker* is not installed, so callers
    do not need to guard the call.

    The highlighter is stored as ``editor._spellcheck_highlighter`` so it
    survives garbage collection without the caller needing to hold a reference.

    Parameters
    ----------
    editor:
        Any ``QTextEdit`` or ``QPlainTextEdit`` instance.
    language:
        Spell-checker language (default ``"en"``).
    ignored_words:
        Extra words considered correctly spelled.
    """
    try:
        highlighter = SpellCheckHighlighter(
            editor.document(),
            language=language,
            ignored_words=ignored_words,
        )
    except ImportError:
        return None
    editor._spellcheck_highlighter = highlighter  # keep alive
    return highlighter
