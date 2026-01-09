from __future__ import annotations

import json
import os
import tempfile
import threading
import weakref
from pathlib import Path
from typing import Optional

from binaryninja import BackgroundTaskThread, BinaryView, execute_on_main_thread
from binaryninja.enums import SymbolType
from binaryninjaui import SidebarWidget, UIActionHandler, getMonospaceFont
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout

from .api import MSFTLearnScrapper

# ---- Frame -> Sidebar routing (used by UIContextNotification in __init__.py) ----

_FRAME_TO_SIDEBAR: "weakref.WeakKeyDictionary[object, WinDocSidebar]" = weakref.WeakKeyDictionary()


def dispatch_xref_selection(frame, selection) -> None:
    w = _FRAME_TO_SIDEBAR.get(frame)
    if w is not None:
        w.on_xref_selection(frame, selection)


# ---- UI helpers ----

def make_hline():
    out = QFrame()
    out.setFrameShape(QFrame.HLine)
    out.setFrameShadow(QFrame.Sunken)
    return out


def _normalize_symbol_name(name: str) -> str:
    """
    Normalize common PE import decorations / UI prefixes:
      - "KERNEL32.dll!GetProcAddress" -> "GetProcAddress"
      - "__imp_GetProcAddress" -> "GetProcAddress"
      - "_GetProcAddress@8" -> "GetProcAddress"
    """
    if not name:
        return ""

    s = name.strip()

    if "!" in s:
        s = s.split("!", 1)[1].strip()

    if s.startswith("__imp_"):
        s = s[len("__imp_"):]

    if s.startswith("_"):
        s = s[1:]

    # stdcall decoration
    if "@" in s:
        base, maybe = s.rsplit("@", 1)
        if maybe.isdigit():
            s = base

    return s.strip()


def _looks_like_local(name: str) -> bool:
    return (not name) or name.startswith("sub_") or name.startswith("j_sub_")


def _is_imported_function_symbol(sym) -> bool:
    if sym is None:
        return False
    return sym.type in (SymbolType.ImportedFunctionSymbol, SymbolType.ImportAddressSymbol)


# ---- Cache (thread-safe, atomic writes) ----

_CACHE_LOCK = threading.Lock()


def _atomic_write_json(path: str, obj: dict) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)

    fd, tmp = tempfile.mkstemp(prefix="windoc_", suffix=".json", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


# ---- Scraper worker ----

class ScrapperThread(BackgroundTaskThread):
    """
    Fetch docs for ONE candidate function name.

    finish() is invoked on the UI thread by Binary Ninja, so we do UI-safe dispatch there.
    """

    def __init__(self, function_name: str, req_id: int, cache_file: str, on_done):
        super().__init__(f"Getting docs for {function_name} ...", can_cancel=True)

        self.function_name = function_name
        self.req_id = req_id
        self.cache_file = cache_file
        self._on_done = on_done

        self.success: bool = False
        self.syntax: str = ""
        self.description: str = ""
        self.return_value: str = ""

    def run(self):
        if not self.function_name:
            return

        # Read cache
        with _CACHE_LOCK:
            cache = {}
            if os.path.exists(self.cache_file):
                try:
                    with open(self.cache_file, "r", encoding="utf-8") as f:
                        cache = json.load(f) or {}
                except Exception:
                    cache = {}

            entry = cache.get(self.function_name)
            if isinstance(entry, dict):
                if entry.get("found") is False:
                    self.success = False
                    return
                if entry.get("found") is True:
                    self.success = True
                    self.syntax = entry.get("syntax", "") or ""
                    self.description = entry.get("description", "") or ""
                    self.return_value = entry.get("return_value", "") or ""
                    return

        # Not cached -> query MS docs
        try:
            scr = MSFTLearnScrapper(self.function_name)
            if scr.soup is None or not scr.found_function_docs():
                self.success = False
            else:
                self.success = True
                self.syntax = scr.get_syntax() or ""
                self.description = scr.get_description(check=True) or ""
                rv = scr.get_return_value() or []
                self.return_value = "\n".join(rv).strip()
        except Exception:
            self.success = False

        # Write cache (including negative cache)
        with _CACHE_LOCK:
            cache = {}
            if os.path.exists(self.cache_file):
                try:
                    with open(self.cache_file, "r", encoding="utf-8") as f:
                        cache = json.load(f) or {}
                except Exception:
                    cache = {}

            cache[self.function_name] = {
                "found": bool(self.success),
                "syntax": self.syntax,
                "description": self.description,
                "return_value": self.return_value,
            }
            _atomic_write_json(self.cache_file, cache)

    def finish(self):
        # Called on UI thread by BN.
        try:
            super().finish()
        finally:
            try:
                self._on_done(
                    self.req_id,
                    self.function_name,
                    self.success,
                    self.syntax,
                    self.description,
                    self.return_value,
                )
            except Exception:
                pass


# ---- Sidebar widget ----

class WinDocSidebar(SidebarWidget):
    timer_ms = 4000

    def __init__(self, name, frame, bv: Optional[BinaryView] = None):
        SidebarWidget.__init__(self, name)

        self._frame = frame
        _FRAME_TO_SIDEBAR[self._frame] = self

        self.actionHandler = UIActionHandler()
        self.actionHandler.setupActionHandler(self)

        self._bv: Optional[BinaryView] = None
        self.bv = bv

        self._req_id = 0
        self._worker: Optional[ScrapperThread] = None

        self._timeout: QTimer = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._on_timeout)

        self._layout = QVBoxLayout(self)
        self._layout.setAlignment(Qt.AlignTop)

        self._label_font: QFont = QFont()
        self._mono_font: QFont = getMonospaceFont(self)

        def make_label(text):
            lbl = QLabel(text)
            return lbl

        self._function = QLabel()
        self._function.setTextFormat(Qt.RichText)
        self._function.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._function.setWordWrap(True)
        self._layout.addWidget(self._function)

        self._layout.addWidget(make_hline())

        self._syntax_label = make_label("Syntax:")
        self._layout.addWidget(self._syntax_label)
        self._syntax = QLabel()
        self._syntax.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._syntax.setWordWrap(True)
        self._layout.addWidget(self._syntax)

        self._layout.addWidget(make_hline())

        self._description_label = make_label("Description:")
        self._layout.addWidget(self._description_label)
        self._description = QLabel()
        self._description.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._description.setWordWrap(True)
        self._layout.addWidget(self._description)

        self._layout.addWidget(make_hline())

        self._retval_label = make_label("Return value:")
        self._layout.addWidget(self._retval_label)
        self._retval = QLabel()
        self._retval.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._retval.setWordWrap(True)
        self._layout.addWidget(self._retval)

        self.notifyFontChanged()

        root = Path(__file__).parent
        self._cache_file = str(root / "cache.json")

    def __del__(self):
        try:
            if self._frame in _FRAME_TO_SIDEBAR:
                del _FRAME_TO_SIDEBAR[self._frame]
        except Exception:
            pass

    @property
    def bv(self):
        return self._bv

    @bv.setter
    def bv(self, new_bv: Optional[BinaryView]):
        self._bv = new_bv

    def notifyViewChanged(self, view_frame):
        if view_frame is None:
            self.bv = None
        else:
            view = view_frame.getCurrentViewInterface()
            self.bv = view.getData()

    def notifyOffsetChanged(self, offset):
        # Intentionally do nothing.
        # Updates are driven by token selection (OnNewSelectionForXref), not cursor movement.
        return

    def notifyFontChanged(self, *args, **kwargs):
        self._label_font = QFont()
        self._mono_font = getMonospaceFont(self)

        self._syntax_label.setFont(self._label_font)
        self._description_label.setFont(self._label_font)
        self._retval_label.setFont(self._label_font)

        self._syntax.setFont(self._mono_font)
        self._description.setFont(self._label_font)
        self._retval.setFont(self._label_font)

    def _cancel_worker(self):
        if self._worker is not None:
            try:
                self._worker.cancel()
            except Exception:
                pass
            self._worker = None
        self._timeout.stop()

    def _on_timeout(self):
        if self._worker is not None:
            try:
                self._worker.cancel()
            except Exception:
                pass
            self._worker = None

        # Timeout is only visible when we started a docs request.
        self._syntax.setText("Timed out while fetching docs.")

    def on_xref_selection(self, frame, selection) -> None:
        """
        Called by the global UIContextNotification when the user clicks something that BN
        considers xref-relevant.

        Requirement:
          - Only fetch/update when the clicked token corresponds to a WinAPI function.
          - If it is not WinAPI (or docs not found), do not update UI at all.
        """
        self._cancel_worker()
        self._req_id += 1
        req_id = self._req_id

        # Need an active BinaryView to resolve symbols.
        try:
            view_iface = frame.getCurrentViewInterface()
            bv = view_iface.getData()
        except Exception:
            return

        if bv is None:
            return

        candidate_name = ""

        # 1) If selection.func looks like an imported function, prefer that.
        try:
            sel_func = getattr(selection, "func", None)
            sym = getattr(sel_func, "symbol", None) if sel_func is not None else None
            if sym is not None and _is_imported_function_symbol(sym):
                candidate_name = _normalize_symbol_name(sym.name)
        except Exception:
            pass

        # 2) Otherwise, use selection.offset/start to look up a symbol at that address.
        if not candidate_name:
            try:
                addr_valid = bool(getattr(selection, "addrValid", False))
                if addr_valid:
                    addr = int(getattr(selection, "offset", 0)) or int(getattr(selection, "start", 0))
                    if addr:
                        sym = bv.get_symbol_at(addr)
                        if _is_imported_function_symbol(sym):
                            candidate_name = _normalize_symbol_name(sym.name)
            except Exception:
                pass

        candidate_name = candidate_name.strip()
        if _looks_like_local(candidate_name):
            return

        def on_done(got_req_id, func_name, success, syntax, desc, rv):
            # Ensure stale results can't update the UI and always stop timeout for the matching req.
            if got_req_id != self._req_id:
                return

            def _apply():
                self._timeout.stop()
                self._worker = None

                if not success:
                    # Not WinAPI function docs -> do not update UI at all.
                    return

                self._function.setText(func_name)
                self._syntax.setText(syntax or "")
                self._description.setText(desc or "")
                self._retval.setText(rv or "")

            execute_on_main_thread(_apply)

        self._worker = ScrapperThread(candidate_name, req_id, self._cache_file, on_done)
        self._timeout.start(self.timer_ms)
        self._worker.start()
