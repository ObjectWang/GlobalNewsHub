"""Search bar with debounced FTS5 live filtering (PRD task P4.5).

Every keystroke restarts a single-shot timer; when it fires, the
stripped query is emitted on ``search_submitted``. An empty (or
whitespace-only) field emits ``""`` which downstream code must treat as
"clear the search filter". Enter flushes immediately.

Signal-slot contract (consumed by ``main.py`` P4.8):

- ``search_submitted(str)``: current query or "" — wired to a worker
  that runs :func:`core.storage.crud.search_articles` off the UI thread.
"""

from __future__ import annotations

from typing import Final

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QLineEdit, QVBoxLayout, QWidget

_PLACEHOLDER: Final = "搜索标题 / 摘要 / 正文（FTS）…"


class SearchBar(QWidget):
    """Debounced full-text search input."""

    search_submitted = Signal(str)

    def __init__(self, debounce_ms: int = 300) -> None:
        super().__init__()
        self._last_emitted: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._edit = QLineEdit(self)
        self._edit.setPlaceholderText(_PLACEHOLDER)
        self._edit.setClearButtonEnabled(True)
        layout.addWidget(self._edit)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(debounce_ms)
        self._timer.timeout.connect(self._flush)

        self._edit.textChanged.connect(lambda _: self._timer.start())
        self._edit.returnPressed.connect(self._timer.stop)
        self._edit.returnPressed.connect(self._flush)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def line_edit(self) -> QLineEdit:
        """The underlying input (tests and programmatic queries)."""
        return self._edit

    def text(self) -> str:
        """Current stripped query text."""
        return self._edit.text().strip()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _flush(self) -> None:
        """Emit the current query unless it was already emitted."""
        query = self._edit.text().strip()
        if query == self._last_emitted:
            return
        self._last_emitted = query
        self.search_submitted.emit(query)
