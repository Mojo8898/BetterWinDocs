from pathlib import Path

from binaryninjaui import Sidebar, SidebarWidgetType, UIContext, UIContextNotification
from PySide6.QtGui import QImage

from .sidebar import WinDocSidebar, dispatch_xref_selection

root = Path(__file__).parent


class _WinDocSelectionNotification(UIContextNotification):
    def OnNewSelectionForXref(self, context, frame, view, selection):
        dispatch_xref_selection(frame, selection)


class MSFTDocWidget(SidebarWidgetType):
    def __init__(self):
        icon = QImage(str(root.joinpath("icon.png")))
        SidebarWidgetType.__init__(self, icon, "Docs")

    def createWidget(self, frame, data):
        return WinDocSidebar("Function doc", frame, data)


Sidebar.addSidebarWidgetType(MSFTDocWidget())

# Register a single notification instance across reloads by storing it on UIContext
_KEY = "_mojo_betterwindocs_notification"
if not hasattr(UIContext, _KEY):
    setattr(UIContext, _KEY, _WinDocSelectionNotification())
    UIContext.registerNotification(getattr(UIContext, _KEY))
