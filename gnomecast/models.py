"""GObject models backing the queue list and the dropdown pickers."""

import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gi.repository import Gio, GObject

if TYPE_CHECKING:
    from .metadata import FileMetadata
    from .transcoder import Transcoder

MAX_NAME_LEN = 40


def display_name(path: str) -> str:
    name = os.path.basename(path)
    if len(name) > MAX_NAME_LEN:
        name = name[: MAX_NAME_LEN - 10] + "..." + name[-10:]
    return name


class QueueItem(GObject.Object):
    """A file in the play queue.

    GObject properties are the values displayed in the list (they emit
    `notify::<name>` when changed); the rest are plain attributes.
    """

    __gtype_name__ = "GnomecastQueueItem"

    name = GObject.Property(type=str, default="")
    duration_str = GObject.Property(type=str, default="...")
    progress = GObject.Property(type=float, default=0.0, minimum=0.0, maximum=1.0)
    playing = GObject.Property(type=bool, default=False)

    def __init__(self, path: str, metadata: "FileMetadata") -> None:
        super().__init__()
        self.name = display_name(path)
        self.path = path
        self.metadata = metadata
        self.duration: float | None = None
        self.thumbnail_fn: str | None = None
        self.transcoder: Transcoder | None = None


def find_item(store: Gio.ListStore, path: str) -> tuple[int, QueueItem] | None:
    for position, item in enumerate(store):
        if isinstance(item, QueueItem) and item.path == path:
            return position, item
    return None


class ChoiceItem(GObject.Object):
    """An entry of a Gtk.DropDown: a label, an arbitrary value, and an optional
    action run instead of selecting the value (e.g. "Add subtitle file...")."""

    __gtype_name__ = "GnomecastChoiceItem"

    label = GObject.Property(type=str, default="")

    def __init__(
        self,
        label: str,
        value: Any = None,
        action: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self.label = label
        self.value = value
        self.action = action


# ChoiceItem.value of the "Add a non-local Chromecast..." entry
ADD_HOST = object()
