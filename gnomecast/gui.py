"""Dialogs and widgets. Everything here must be called from the main thread,
except show_error_dialog which can be called from any thread."""

import logging
import os
from collections.abc import Callable
from typing import Any, cast

from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango

from .models import ChoiceItem, QueueItem
from .transcoder import AUDIO_EXTS

logger = logging.getLogger(__name__)


def show_error_dialog(
    window: Gtk.Window,
    title: str,
    message: str,
    on_close: Callable[[], Any] | None = None,
) -> None:
    logger.error("%s: %s", title, message)

    def inner() -> None:
        dialog = Adw.AlertDialog(heading=title, body=message)
        dialog.add_response("close", "_Close")
        if on_close is not None:
            dialog.connect("response", lambda *_: on_close())
        dialog.present(window)

    GLib.idle_add(inner)


def show_text_dialog(
    window: Gtk.Window,
    heading: str,
    body: str,
    text: str,
    link: tuple[str, str] | None = None,
) -> None:
    """Alert with a scrollable read-only text, and an optional (label, uri)
    response that opens `uri`."""
    dialog = Adw.AlertDialog(heading=heading, body=body, prefer_wide_layout=True)

    buffer = Gtk.TextBuffer()
    buffer.set_text(text)
    view = Gtk.TextView(
        buffer=buffer, editable=False, cursor_visible=False, monospace=True
    )
    scrolled = Gtk.ScrolledWindow(
        child=view, min_content_height=300, hexpand=True, vexpand=True
    )
    dialog.set_extra_child(scrolled)

    dialog.add_response("close", "_Close")
    dialog.set_default_response("close")
    dialog.set_close_response("close")
    if link is not None:
        label, uri = link
        dialog.add_response("link", label)

        def on_response(_dialog: Adw.AlertDialog, response: str) -> None:
            if response == "link":
                open_uri(window, uri)

        dialog.connect("response", on_response)
    dialog.present(window)


def open_uri(window: Gtk.Window, uri: str) -> None:
    def done(launcher: Gtk.UriLauncher, result: Gio.AsyncResult, *_: Any) -> None:
        try:
            launcher.launch_finish(result)
        except GLib.Error as e:
            logger.error("Could not open %s: %s", uri, e.message)

    Gtk.UriLauncher.new(uri).launch(window, None, done)


def ask_text(
    window: Gtk.Window,
    heading: str,
    body: str,
    on_done: Callable[[str | None], Any],
) -> None:
    """Ask for a line of text; `on_done` gets None on cancel or empty text."""
    dialog = Adw.AlertDialog(heading=heading, body=body)
    entry = Gtk.Entry(activates_default=True)
    dialog.set_extra_child(entry)
    dialog.add_response("cancel", "_Cancel")
    dialog.add_response("ok", "_OK")
    dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("ok")
    dialog.set_close_response("cancel")

    def on_response(_dialog: Adw.AlertDialog, response: str) -> None:
        text = entry.get_text().strip()
        on_done(text if response == "ok" and text else None)

    dialog.connect("response", on_response)
    dialog.present(window)


def _file_dialog(
    title: str, file_filter: Gtk.FileFilter, folder: str | None
) -> Gtk.FileDialog:
    filters = Gio.ListStore(item_type=Gtk.FileFilter)
    filters.append(file_filter)
    dialog = Gtk.FileDialog(title=title, modal=True)
    dialog.set_filters(filters)
    dialog.set_default_filter(file_filter)
    if folder is not None and os.path.isdir(folder):
        dialog.set_initial_folder(Gio.File.new_for_path(folder))
    return dialog


def choose_media_files(window: Gtk.Window, on_done: Callable[[list[str]], Any]) -> None:
    """Pick one or more audio/video files; `on_done` gets [] on cancel."""
    media_filter = Gtk.FileFilter(name="Videos")
    media_filter.add_mime_type("video/*")
    media_filter.add_mime_type("audio/*")
    dialog = _file_dialog(
        "Please choose an audio or video file...",
        media_filter,
        os.path.expanduser("~/Downloads"),
    )

    def done(dialog: Gtk.FileDialog, result: Gio.AsyncResult, *_: Any) -> None:
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error as e:
            logger.debug("Media file selection cancelled: %s", e.message)
            on_done([])
            return
        paths = []
        for file in files or []:
            path = cast(Gio.File, file).get_path()
            if path is not None:
                paths.append(path)
        on_done(paths)

    dialog.open_multiple(window, None, done)


def choose_subtitles_file(
    window: Gtk.Window, folder: str | None, on_done: Callable[[str | None], Any]
) -> None:
    """Pick a .srt/.vtt file; `on_done` gets None on cancel."""
    subtitles_filter = Gtk.FileFilter(name="Subtitles")
    subtitles_filter.add_pattern("*.srt")
    subtitles_filter.add_pattern("*.vtt")
    dialog = _file_dialog("Please choose a subtitle file...", subtitles_filter, folder)

    def done(dialog: Gtk.FileDialog, result: Gio.AsyncResult, *_: Any) -> None:
        try:
            file = dialog.open_finish(result)
        except GLib.Error as e:
            logger.debug("Subtitles file selection cancelled: %s", e.message)
            on_done(None)
            return
        on_done(file.get_path())

    dialog.open(window, None, done)


# max natural widths (in characters) of dropdown labels, ellipsized beyond
DROPDOWN_BUTTON_MAX_CHARS = 25
DROPDOWN_LIST_MAX_CHARS = 60


def _choice_label(max_width_chars: int) -> Gtk.Label:
    return Gtk.Label(
        xalign=0,
        ellipsize=Pango.EllipsizeMode.MIDDLE,
        max_width_chars=max_width_chars,
    )


class _ChoiceListRow(Gtk.Box):
    """An entry of an open dropdown: label, and a checkmark when selected."""

    __gtype_name__ = "GnomecastChoiceListRow"

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.label = _choice_label(DROPDOWN_LIST_MAX_CHARS)
        self.label.set_hexpand(True)
        self.check = Gtk.Image(icon_name="object-select-symbolic")
        self.append(self.label)
        self.append(self.check)
        self.handler_id = 0


def set_ellipsized_factories(dropdown: Gtk.DropDown) -> None:
    """Make a dropdown of ChoiceItem ellipsize long labels.

    Gtk.DropDown's default factory never ellipsizes, so the longest entry
    (e.g. a subtitle file name) sets the width of the window.
    """

    def setup_button(_factory, list_item: Gtk.ListItem) -> None:
        list_item.set_child(_choice_label(DROPDOWN_BUTTON_MAX_CHARS))

    def bind_button(_factory, list_item: Gtk.ListItem) -> None:
        label = cast(Gtk.Label, list_item.get_child())
        item = cast(ChoiceItem, list_item.get_item())
        label.set_label(item.label)
        label.set_tooltip_text(item.label)

    def setup_list(_factory, list_item: Gtk.ListItem) -> None:
        list_item.set_child(_ChoiceListRow())

    def bind_list(_factory, list_item: Gtk.ListItem) -> None:
        row = cast(_ChoiceListRow, list_item.get_child())
        item = cast(ChoiceItem, list_item.get_item())
        row.label.set_label(item.label)
        row.label.set_tooltip_text(item.label)

        def update_check(*_args: Any) -> None:
            selected = dropdown.get_selected_item() is item
            row.check.set_opacity(1.0 if selected else 0.0)

        row.handler_id = dropdown.connect("notify::selected-item", update_check)
        update_check()

    def unbind_list(_factory, list_item: Gtk.ListItem) -> None:
        row = cast(_ChoiceListRow, list_item.get_child())
        if row.handler_id:
            dropdown.disconnect(row.handler_id)
            row.handler_id = 0

    button_factory = Gtk.SignalListItemFactory()
    button_factory.connect("setup", setup_button)
    button_factory.connect("bind", bind_button)
    dropdown.set_factory(button_factory)

    list_factory = Gtk.SignalListItemFactory()
    list_factory.connect("setup", setup_list)
    list_factory.connect("bind", bind_list)
    list_factory.connect("unbind", unbind_list)
    dropdown.set_list_factory(list_factory)


class QueueRow(Gtk.Box):
    """A row of the play queue: selected-file icon, name, duration, and
    transcode progress (bar and percentage), bound to a QueueItem."""

    __gtype_name__ = "GnomecastQueueRow"

    def __init__(self, item: QueueItem) -> None:
        super().__init__(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        # marks the selected file (not a button)
        ext = os.path.splitext(item.path)[1].lstrip(".").lower()
        playing_icon = Gtk.Image(
            icon_name="audio-x-generic-symbolic"
            if ext in AUDIO_EXTS
            else "video-x-generic-symbolic",
            tooltip_text="Selected file",
        )
        name_label = Gtk.Label(
            xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE
        )
        duration_label = Gtk.Label(xalign=1)
        progress_bar = Gtk.ProgressBar(valign=Gtk.Align.CENTER)
        progress_bar.set_size_request(80, -1)
        # fixed width and tabular digits, so the row doesn't move as it changes
        progress_label = Gtk.Label(xalign=1, width_chars=5)
        progress_label.add_css_class("numeric")
        for widget in (
            playing_icon,
            name_label,
            duration_label,
            progress_bar,
            progress_label,
        ):
            self.append(widget)

        # bindings are dropped automatically when the item or row is finalized
        flags = GObject.BindingFlags.SYNC_CREATE
        item.bind_property("name", name_label, "label", flags)
        item.bind_property("duration_str", duration_label, "label", flags)
        item.bind_property("progress", progress_bar, "fraction", flags)
        item.bind_property(
            "progress",
            progress_label,
            "label",
            flags,
            lambda _binding, progress: f"{int(progress * 100)} %",
        )
        # transparent rather than hidden, so names stay aligned across rows
        item.bind_property(
            "playing",
            playing_icon,
            "opacity",
            flags,
            lambda _binding, playing: 1.0 if playing else 0.0,
        )
