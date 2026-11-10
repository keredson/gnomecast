import logging
import os
import re
import signal
import sys
import threading
import time
import typing
import urllib.parse
from collections.abc import Callable
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .ffmpeg import check_ffmpeg_installed, get_media_duration
from .gui import (
    QueueRow,
    ask_text,
    choose_media_files,
    choose_subtitles_file,
    show_error_dialog,
    show_text_dialog,
)
from .log import setup_logging
from .metadata import AudioMetadata, FileMetadata, StreamMetadata
from .models import ChoiceItem, QueueItem, find_item
from .player import (
    CastPlayer,
    ChromecastConnectionError,
    cast_from_host,
    discover_casts,
)
from .subtitles import (
    convert_subtitles_to_webvtt,
    extract_single_subtitle,
    find_sidecar_subtitles,
    get_preferred_language,
)
from .transcoder import AUDIO_EXTS, Transcoder
from .utils import (
    MainLoopStopped,
    humanize_seconds,
    is_pid_running,
    run_on_main_thread,
    start_thread,
    stop_main_thread_calls,
    throttle,
)
from .version import __version__
from .webserver import GnomecastWebServer

logger = logging.getLogger(__name__)

APP_ID = "io.github.julienc91.Gnomecast"
ASSETS_DIR = Path(__file__).parent / "assets"
LOGO_SVG = ASSETS_DIR / "gnomecast.svg"
# thumbnails are extracted 600px wide (see ffmpeg.extract_thumbnail)
THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT = 600, 338


class Gnomecast:
    def __init__(self):
        self.app: Adw.Application | None = None
        self.webserver: GnomecastWebServer | None = None
        self.player = CastPlayer(
            on_state_changed=self.on_player_state_changed,
            on_tick=self.on_player_tick,
            on_position=self.on_player_position,
            on_finished=self.check_for_next_in_queue,
        )
        self.fn: str | None = None
        self.video_stream: StreamMetadata | None = None
        self.audio_stream: AudioMetadata | None = None
        self.last_fn_played: str | None = None
        self.transcoder: Transcoder | None = None
        self.duration: float | None = None
        self.subtitles: str | None = None
        self.inhibit_cookie = 0
        self.autoplay = False
        self.exit_code = 0
        self.quitting = False
        # last item applied by each dropdown (see on_dropdown_changed)
        self.applied_choices: dict[Gtk.DropDown, ChoiceItem] = {}

    def run(self, fn=None, device=None, subtitles=None) -> int:
        self.app = app = Adw.Application(
            application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE
        )
        app.connect("activate", self.on_activate, fn, device, subtitles)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self.quit)
        status = app.run(None)
        return self.exit_code or status

    def on_activate(self, app: Adw.Application, fn, device, subtitles) -> None:
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        app.add_action(quit_action)
        app.set_accels_for_action("app.quit", ["<Control>q"])

        self.build_gui(app)
        self.init_casts(device=device)
        start_thread(self.check_ffmpeg)
        start_thread(self.start_server, daemon=True)
        self.player.start()
        if fn:
            self.queue_files([fn])
        if subtitles:
            self.select_subtitles_file(subtitles)
        if fn and subtitles:
            self.autoplay = True

    @property
    def cast(self):
        return self.player.cast

    def items(self) -> list[QueueItem]:
        return list(self.files_store)

    def check_ffmpeg(self):
        time.sleep(1)

        if not check_ffmpeg_installed():
            self.exit_code = 1
            show_error_dialog(
                self.win,
                "ffmpeg not found",
                "Could not find ffmpeg. Please run 'sudo apt-get install ffmpeg'.",
                on_close=self.quit,
            )

    def start_server(self):
        self.webserver = GnomecastWebServer(
            get_subtitles=lambda: self.subtitles,
            get_transcoder=lambda: self.transcoder,
        )
        self.webserver.start()

    def update_status(self, did_transcode=False):
        if did_transcode:
            self.update_button_visible()
            self.prep_next_transcode()

        def f():
            for item in self.items():
                transcoder = item.transcoder
                if transcoder and item.duration is not None and item.duration > 0:
                    if transcoder.done:
                        item.progress = 1.0
                    else:
                        progress = transcoder.progress_seconds / item.duration
                        item.progress = max(0.0, min(progress, 1.0))

        GLib.idle_add(f)

    def on_player_state_changed(self, state):
        # called from the player monitor thread
        def f():
            if state == "PLAYING":
                self.inhibit_idle()
            else:
                self.uninhibit_idle()
            self.update_media_button_states()
            self.update_status()

        GLib.idle_add(f)

    def inhibit_idle(self) -> None:
        """Keep the computer awake while casting, since it serves the media.

        IDLE alone is implemented with the Wayland idle-inhibit protocol, which
        only works while the window is visible; adding SUSPEND makes GTK go
        through the session manager / portal instead.
        """
        if self.app is not None and not self.inhibit_cookie:
            self.inhibit_cookie = self.app.inhibit(
                self.win,
                Gtk.ApplicationInhibitFlags.IDLE | Gtk.ApplicationInhibitFlags.SUSPEND,
                "Casting media",
            )

    def uninhibit_idle(self) -> None:
        if self.app is not None and self.inhibit_cookie:
            self.app.uninhibit(self.inhibit_cookie)
            self.inhibit_cookie = 0

    def on_player_position(self, seconds):
        GLib.idle_add(self.scrubber_adj.set_value, seconds)

    def on_player_tick(self):
        if self.transcoder and not self.transcoder.done:
            GLib.idle_add(self.update_status)

    def init_casts(self, widget=None, device=None):
        self.cast_store.splice(
            0,
            len(self.cast_store),
            [ChoiceItem("Searching local network - please wait...")],
        )
        self.cast_combo.set_selected(0)
        start_thread(self.load_casts, kwargs={"device": device})

    def load_casts(self, device=None):
        chromecasts = discover_casts()

        def update_ui():
            items = [
                ChoiceItem("Select a cast device..."),
                ChoiceItem(
                    "Add a non-local Chromecast...", action=self.get_nonlocal_cast
                ),
            ]
            for cc in chromecasts:
                friendly_name = cc.cast_info.friendly_name or str(cc.cast_info.host)
                if cc.cast_type != "cast":
                    friendly_name = f"{friendly_name} ({cc.cast_type})"
                items.append(ChoiceItem(friendly_name, cc))
            self.cast_store.splice(0, len(self.cast_store), items)
            first_cast = 2  # position of the first Chromecast in the dropdown
            if device:
                for i, cc in enumerate(chromecasts):
                    if device == cc.cast_info.friendly_name:
                        self.cast_combo.set_selected(first_cast + i)
                        break
                else:
                    self.cast_combo.set_selected(0)
                    show_error_dialog(
                        self.win,
                        "Chromecast not found",
                        f"The Chromecast {device} wasn't found.",
                    )
            else:
                self.cast_combo.set_selected(first_cast if len(chromecasts) == 1 else 0)

        GLib.idle_add(update_ui)

    def update_media_button_states(self):
        state = self.player.state
        active = bool(self.transcoder and self.player.is_active)
        self.play_button.set_sensitive(
            bool(
                self.transcoder
                and state in ("BUFFERING", "PLAYING", "PAUSED", "IDLE", "UNKNOWN")
                and self.fn
            )
        )
        self.volume_button.set_sensitive(bool(self.cast))
        self.speed_button.set_sensitive(bool(self.cast))
        self.stop_button.set_sensitive(active)
        self.rewind_button.set_sensitive(active)
        self.forward_button.set_sensitive(active)
        self.play_button.set_icon_name(
            "media-playback-pause-symbolic"
            if state == "PLAYING"
            else "media-playback-start-symbolic"
        )
        if self.transcoder and self.duration is not None and self.duration > 0:
            self.scrubber_adj.set_upper(self.duration)
            self.scrubber.set_sensitive(True)
        else:
            self.scrubber.set_sensitive(False)
        self.update_button_visible()

    def make_dropdown(
        self, store: Gio.ListStore, on_choice: Callable[[ChoiceItem], None]
    ) -> Gtk.DropDown:
        expression = Gtk.PropertyExpression.new(ChoiceItem, None, "label")
        dropdown = Gtk.DropDown.new(store, expression)
        dropdown.connect("notify::selected-item", self.on_dropdown_changed, on_choice)
        return dropdown

    def on_dropdown_changed(
        self,
        dropdown: Gtk.DropDown,
        _pspec,
        on_choice: Callable[[ChoiceItem], None],
    ) -> None:
        item = dropdown.get_selected_item()
        # None when the store is emptied; the same item when the selection
        # only moved because items were added or removed before it
        if not isinstance(item, ChoiceItem) or item is self.applied_choices.get(
            dropdown
        ):
            return
        if item.action is not None:
            # e.g. "Add subtitle file...": the action ends by selecting
            # another item, or calls revert_dropdown()
            item.action()
            return
        self.applied_choices[dropdown] = item
        on_choice(item)

    def revert_dropdown(self, dropdown: Gtk.DropDown) -> None:
        """Select the last applied item again (or the first one)."""
        previous = self.applied_choices.get(dropdown)
        model = typing.cast(Gio.ListStore, dropdown.get_model())
        found, position = model.find(previous) if previous else (False, 0)
        dropdown.set_selected(position if found else 0)

    def media_button(
        self, icon_name: str, on_clicked: Callable[[Gtk.Button], None]
    ) -> Gtk.Button:
        button = Gtk.Button(
            icon_name=icon_name,
            sensitive=False,
            hexpand=True,
            halign=Gtk.Align.CENTER,
        )
        button.add_css_class("flat")
        button.connect("clicked", on_clicked)
        return button

    def build_gui(self, app: Adw.Application) -> None:
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.IconTheme.get_for_display(display).add_search_path(str(ASSETS_DIR))
        Gtk.Window.set_default_icon_name("gnomecast")

        self.win = win = Adw.ApplicationWindow(application=app, title="Gnomecast")
        win.set_default_size(THUMBNAIL_WIDTH + 32, -1)
        drop_target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop_target.connect("drop", self.on_drop)
        win.add_controller(drop_target)

        header = Adw.HeaderBar()
        header.set_title_widget(
            Adw.WindowTitle(title="Gnomecast", subtitle=f"v{__version__}")
        )
        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(header)
        win.set_content(toolbar_view)

        vbox_outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        toolbar_view.set_content(vbox_outer)
        vbox = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
            margin_top=16,
            margin_bottom=20,
            margin_start=16,
            margin_end=16,
        )

        self.thumbnail_image = Gtk.Picture(
            content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True
        )
        self.thumbnail_image.set_size_request(THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT)
        self.show_logo()
        # a Gtk.Picture asks for a height proportional to its width (537px for
        # the logo in a 632px wide window): clamp it, and let it take any extra
        # height since GTK4 windows can't be shrunk programmatically
        vbox_outer.append(
            Adw.Clamp(
                child=self.thumbnail_image,
                orientation=Gtk.Orientation.VERTICAL,
                maximum_size=THUMBNAIL_HEIGHT,
                tightening_threshold=THUMBNAIL_HEIGHT,
                vexpand=True,
            )
        )
        vbox_outer.append(vbox)

        # cast device selection
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        vbox.append(hbox)
        self.cast_store = Gio.ListStore(item_type=ChoiceItem)
        self.cast_combo = self.make_dropdown(self.cast_store, self.on_cast_selected)
        self.cast_combo.set_hexpand(True)
        hbox.append(self.cast_combo)
        refresh_button = Gtk.Button(
            icon_name="view-refresh-symbolic",
            tooltip_text="Search for cast devices again",
        )
        refresh_button.connect("clicked", self.init_casts)
        hbox.append(refresh_button)

        # list of queued files
        self.files_store = Gio.ListStore(item_type=QueueItem)
        self.files_store.connect("items-changed", self.update_button_visible)
        self.files_view = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.MULTIPLE,
            activate_on_single_click=False,
            show_separators=True,
        )
        self.files_view.bind_model(
            self.files_store, lambda item: QueueRow(typing.cast(QueueItem, item))
        )
        self.files_view.connect(
            "selected-rows-changed", self.on_files_selection_changed
        )
        self.files_view.connect("row-activated", self.on_files_view_activate)

        # contains the files list and the buttons to add/del
        self.hbox = hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        vbox.append(hbox)

        self.scrolled_window = Gtk.ScrolledWindow(
            child=self.files_view,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            propagate_natural_height=True,
            max_content_height=220,
            hexpand=True,
        )
        hbox.append(self.scrolled_window)

        # vexpand=False: don't let the add button's vexpand propagate upwards
        self.btn_vbox = btn_vbox = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8, vexpand=False
        )
        hbox.append(btn_vbox)
        self.file_button_content = Adw.ButtonContent(icon_name="list-add-symbolic")
        self.file_button = Gtk.Button(
            child=self.file_button_content,
            tooltip_text="Add one or more audio or video files...",
            vexpand=True,
        )
        self.file_button.connect("clicked", self.on_file_clicked)
        btn_vbox.append(self.file_button)
        self.remove_button = Gtk.Button(
            icon_name="list-remove-symbolic",
            tooltip_text="Remove the selected files from the queue.",
            sensitive=False,
        )
        self.remove_button.connect("clicked", self.remove_files)
        btn_vbox.append(self.remove_button)

        self.file_detail_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8
        )
        vbox.append(self.file_detail_row)

        # audio/video track selection
        self.stream_store = Gio.ListStore(item_type=ChoiceItem)
        self.audio_combo = self.make_dropdown(
            self.stream_store, self.on_stream_selected
        )
        self.audio_combo.set_hexpand(True)
        self.file_detail_row.append(self.audio_combo)

        # subtitle selection
        self.subtitle_store = Gio.ListStore(item_type=ChoiceItem)
        self.subtitle_combo = self.make_dropdown(
            self.subtitle_store, self.on_subtitles_selected
        )
        self.subtitle_combo.set_hexpand(True)
        self.file_detail_row.append(self.subtitle_combo)

        file_info_button = Gtk.Button(
            icon_name="dialog-information-symbolic", tooltip_text="File info"
        )
        file_info_button.connect("clicked", self.show_file_info)
        self.file_detail_row.append(file_info_button)

        self.scrubber_adj = Gtk.Adjustment(
            value=0, lower=0, upper=100, step_increment=15, page_increment=60
        )
        self.scrubber = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL,
            adjustment=self.scrubber_adj,
            digits=0,
            draw_value=True,
            sensitive=False,
        )
        self.scrubber.set_format_value_func(
            lambda _scale, value, *_: humanize_seconds(value)
        )
        self.scrubber.connect("change-value", self.scrubber_move_started)
        self.scrubber.connect("change-value", self.scrubber_moved)
        vbox.append(self.scrubber)

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        self.rewind_button = self.media_button(
            "media-seek-backward-symbolic", self.rewind_clicked
        )
        hbox.append(self.rewind_button)
        self.play_button = self.media_button(
            "media-playback-start-symbolic", self.play_clicked
        )
        hbox.append(self.play_button)
        self.forward_button = self.media_button(
            "media-seek-forward-symbolic", self.forward_clicked
        )
        hbox.append(self.forward_button)
        self.stop_button = self.media_button(
            "media-playback-stop-symbolic", self.stop_clicked
        )
        hbox.append(self.stop_button)
        self.volume_button = Gtk.ScaleButton.new(
            0.0,
            1.0,
            0.05,
            [
                "audio-volume-muted-symbolic",
                "audio-volume-high-symbolic",
                "audio-volume-low-symbolic",
                "audio-volume-medium-symbolic",
            ],
        )
        self.volume_button.set_value(1)
        self.volume_button.set_hexpand(True)
        self.volume_button.set_halign(Gtk.Align.CENTER)
        self.volume_button.connect("value-changed", self.volume_moved)
        self.volume_button.set_sensitive(False)
        hbox.append(self.volume_button)
        speed_label = Gtk.Label(label="Speed:", tooltip_text="Playback speed")
        hbox.append(speed_label)
        self.speed_button = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(
                value=1.0,
                lower=0.5,
                upper=2.0,
                step_increment=0.05,
                page_increment=0.25,
            ),
            digits=2,
            width_chars=5,
            tooltip_text="Playback speed",
            sensitive=False,
            hexpand=True,
            halign=Gtk.Align.START,
        )
        self.speed_button.connect("value-changed", self.speed_moved)
        hbox.append(self.speed_button)
        vbox.append(hbox)

        win.connect("close-request", self.quit)
        self.update_button_visible()
        win.present()

    def show_logo(self) -> None:
        self.thumbnail_image.set_filename(str(LOGO_SVG))

    def show_thumbnail(self, thumbnail_fn: str) -> None:
        if os.path.isfile(thumbnail_fn):
            self.thumbnail_image.set_filename(thumbnail_fn)

    def on_drop(self, _target, value, _x, _y) -> bool:
        if not isinstance(value, Gdk.FileList):
            return False
        paths = []
        for file in value.get_files():
            path = file.get_path()
            if path is not None and os.path.isfile(path):
                paths.append(path)
        if paths:
            self.queue_files(paths)
        return bool(paths)

    def update_button_visible(self, *_args) -> None:
        count = len(self.files_store)
        self.scrolled_window.set_visible(bool(count))
        self.remove_button.set_visible(bool(count))
        self.file_button_content.set_label(
            "" if count else "Add one or more audio or video files..."
        )
        self.btn_vbox.set_hexpand(not count)
        self.file_detail_row.set_visible(bool(self.fn))

    def scrubber_move_started(self, scale, scroll_type, seconds):
        self.player.begin_seek()

    def on_files_selection_changed(self, listbox: Gtk.ListBox) -> None:
        self.remove_button.set_sensitive(bool(listbox.get_selected_rows()))

    def remove_files(self, _button) -> None:
        rows = self.files_view.get_selected_rows()
        for position in sorted((row.get_index() for row in rows), reverse=True):
            item = typing.cast(QueueItem, self.files_store.get_item(position))
            if item.transcoder:
                item.transcoder.destroy()
            logger.info("Removed from queue: %s", item.path)
            self.files_store.remove(position)
            if self.fn == item.path:
                self.unselect_file()

    def on_files_view_activate(self, _listbox, row: Gtk.ListBoxRow) -> None:
        item = typing.cast(QueueItem, self.files_store.get_item(row.get_index()))
        logger.info("Selected file: %s", item.path)
        self.select_file(item.path)

    def queue_files(self, files: list[str]) -> None:
        existing_files = {item.path for item in self.items()}
        files = [os.path.abspath(f) for f in files]
        files = [f for f in files if f not in existing_files]
        for fn in files:

            def callback(fmd):
                def f():
                    found = find_item(self.files_store, fmd.fn)
                    if found and os.path.isfile(fmd.thumbnail_fn):
                        found[1].thumbnail_fn = fmd.thumbnail_fn
                    if self.fn == fmd.fn and fmd.thumbnail_fn:
                        self.show_thumbnail(fmd.thumbnail_fn)
                    self.update_status()

                GLib.idle_add(f)

            logger.info("Queued: %s", fn)
            self.files_store.append(QueueItem(fn, FileMetadata(fn, callback)))
            start_thread(self.get_duration, args=[fn])
        if files and self.fn is None:
            self.select_file(files[0])

    @throttle(seconds=1)
    def volume_moved(self, button, volume):
        self.player.set_volume(volume)

    @throttle(seconds=1)
    def speed_moved(self, spin_button):
        rate = spin_button.get_adjustment().get_value()
        self.player.set_playback_rate(rate)

    @throttle(seconds=2)
    def scrubber_moved(self, scale, scroll_type, seconds):
        if not self.cast:
            return
        self.player.seek(seconds)

    def stop_clicked(self, widget):
        self.player.stop()

    def quit(self, *args) -> bool:
        if self.quitting:
            return False
        self.quitting = True
        for item in self.items():
            if item.transcoder:
                item.transcoder.destroy()
            if item.thumbnail_fn is not None and os.path.isfile(item.thumbnail_fn):
                os.remove(item.thumbnail_fn)
        self.uninhibit_idle()
        stop_main_thread_calls()
        if self.app is not None:
            self.app.quit()
        return False

    def forward_clicked(self, widget):
        self.seek_delta(30)

    def rewind_clicked(self, widget):
        self.seek_delta(-10)

    def seek_delta(self, delta):
        seconds = self.player.seek_delta(delta)
        if seconds is not None:
            self.scrubber_adj.set_value(seconds)

    def play_clicked(self, widget):
        if not self.cast:
            logger.debug("Play clicked with no Chromecast selected")
            return
        if self.fn is None or not self.webserver:
            return

        state = self.player.state
        if state in ("IDLE", "UNKNOWN") or self.last_fn_played != self.fn:
            self.last_fn_played = self.fn
            subtitles_url = (
                self.webserver.get_subtitles_url() if bool(self.subtitles) else None
            )

            current_time = self.scrubber_adj.get_value()
            logger.info(
                "Casting %s to '%s'%s",
                self.fn,
                self.cast.cast_info.friendly_name,
                f" from {humanize_seconds(current_time)}" if current_time else "",
            )
            self.speed_button.set_value(1.0)
            ext = self.fn.split(".")[-1]
            ext = "".join(ch for ch in ext if ch.isalnum()).lower()
            self.player.play(
                f"{self.webserver.get_media_base_url()}/{hash(self.fn)}.{ext}",
                f"audio/{ext}" if ext in AUDIO_EXTS else "video/mp4",
                subtitles_url=subtitles_url,
                current_time=current_time if current_time else None,
            )
            self.prep_next_transcode()
        else:
            self.player.toggle_pause()

    def on_file_clicked(self, widget):
        choose_media_files(self.win, self.queue_files)

    def on_new_subtitle_clicked(self):
        def done(path: str | None) -> None:
            if path is None:
                self.revert_dropdown(self.subtitle_combo)
            else:
                self.select_subtitles_file(path)

        folder = os.path.dirname(self.fn) if self.fn is not None else None
        choose_subtitles_file(self.win, folder, done)

    def select_subtitles_file(self, fn: str | Path, select: bool = True):
        substitles_path = Path(fn)
        if not substitles_path.is_file():
            show_error_dialog(
                self.win, "File not found", f"Could not find subtitles file: {fn}."
            )
            self.revert_dropdown(self.subtitle_combo)
            return

        subtitles_path = substitles_path.resolve()
        display_name = subtitles_path.name
        pos = len(self.subtitle_store)
        stream = StreamMetadata(None, None, display_name)
        stream._subtitles = convert_subtitles_to_webvtt(subtitles_path)
        logger.info("Loaded subtitles file: %s", subtitles_path)
        self.subtitle_store.append(ChoiceItem(display_name, stream))
        if select:
            self.subtitle_combo.set_selected(pos)

    def unselect_file(self):
        self.show_logo()
        self.fn = None
        # streams belong to the file: the next one picks its own defaults
        self.video_stream = None
        self.audio_stream = None
        self.stream_store.remove_all()
        self.subtitle_store.remove_all()
        self.transcoder = None
        self.duration = None
        self.player.stop()

        def f():
            self.scrubber_adj.set_value(0)
            for item in self.items():
                item.playing = False
            self.update_button_visible()

        GLib.idle_add(f)

    def select_file(self, fn):
        self.unselect_file()
        if not os.path.isfile(fn):
            show_error_dialog(
                self.win, "File not found", f"Could not find media file: {fn}."
            )
            return
        fn = os.path.abspath(fn)
        self.fn = fn
        self.player.stop()

        def f():
            self.scrubber_adj.set_value(0)
            for item in self.items():
                if self.fn == item.path:
                    if item.thumbnail_fn is not None:
                        self.show_thumbnail(item.thumbnail_fn)
                    item.playing = True
                    self.duration = item.duration
                else:
                    item.playing = False
            start_thread(self.update_transcoders)
            fmd = self.get_fmd()
            if fmd is not None:
                start_thread(self.update_audio_tracks, args=[fmd])
                start_thread(self.update_subtitles, args=[fmd])
            self.update_button_visible()
            self.update_media_button_states()

        GLib.idle_add(f)

    update_transcoders_lock = threading.Lock()

    def update_transcoders(self):
        try:
            self._update_transcoders()
        except MainLoopStopped:
            logger.debug("App quit while updating transcoders")

    def _update_transcoders(self):
        with self.update_transcoders_lock:
            if self.cast and self.fn is not None:
                rows = run_on_main_thread(
                    lambda: [
                        (item.path, item.transcoder, item.metadata)
                        for item in self.items()
                    ]
                )
                for fn, transcoder, fmd in rows:
                    if fn != self.fn:
                        continue
                    fmd.wait()
                    if self.quitting:  # don't start ffmpeg after quit()'s cleanup
                        return
                    if not self.video_stream and fmd.video_streams:
                        self.video_stream = fmd.video_streams[0]
                    if not self.audio_stream and fmd.audio_streams:
                        self.audio_stream = fmd.audio_streams[0]
                    if (
                        not transcoder
                        or self.cast != transcoder.cast
                        or self.fn != transcoder.source_fn
                        or self.audio_stream != transcoder.audio_stream
                    ):
                        self.transcoder = Transcoder(
                            self.cast,
                            fmd,
                            self.video_stream,
                            self.audio_stream,
                            lambda did_transcode=None: GLib.idle_add(
                                self.update_status, did_transcode
                            ),
                            self.error_callback,
                            transcoder,
                        )
                        # synchronous, so the next update_transcoders call
                        # sees this transcoder and destroys it if replaced
                        run_on_main_thread(
                            lambda fn=fn, t=self.transcoder: self.set_item_transcoder(
                                fn, t
                            )
                        )
                    else:
                        # still matches (e.g. the same file selected again)
                        self.transcoder = transcoder
                if self.autoplay:
                    self.autoplay = False
                    GLib.idle_add(self.play_clicked, None)
            if not self.cast:
                GLib.idle_add(self.destroy_all_transcoders)
            GLib.idle_add(self.update_media_button_states)

    def set_item_transcoder(self, fn: str, transcoder: Transcoder | None) -> None:
        found = find_item(self.files_store, fn)
        if found:
            found[1].transcoder = transcoder
            self.update_status()

    def destroy_all_transcoders(self) -> None:
        for item in self.items():
            if item.transcoder:
                item.transcoder.destroy()
                item.transcoder = None

    def check_for_next_in_queue(self):
        # called from the player monitor thread
        GLib.idle_add(self.play_next_in_queue)

    def play_next_in_queue(self) -> None:
        next = False
        for item in self.items():
            if next:
                logger.info("Autoplaying next in queue: %s", item.path)
                self.autoplay = True
                self.select_file(item.path)
                return
            if self.cast and self.fn is not None and self.fn == item.path:
                next = True

    def prep_next_transcode(self):
        transcode_next = False
        for item in self.items():
            transcoder = item.transcoder
            fmd = item.metadata
            if transcode_next and not transcoder:
                logger.debug("Preparing next in queue: %s", item.path)
                transcoder = Transcoder(
                    self.cast,
                    fmd,
                    fmd.video_streams[0] if fmd.video_streams else None,
                    fmd.audio_streams[0] if fmd.audio_streams else None,
                    lambda did_transcode=None: GLib.idle_add(
                        self.update_status, did_transcode
                    ),
                    self.error_callback,
                    transcoder,
                )
                item.transcoder = transcoder
                transcode_next = False
            if (
                self.cast
                and self.fn is not None
                and self.fn == item.path
                and transcoder
                and transcoder.done
            ):
                transcode_next = True

    def get_duration(self, fn: str) -> None:
        duration = get_media_duration(Path(fn))

        def f():
            if fn == self.fn:
                self.duration = duration
            found = find_item(self.files_store, fn)
            if found:
                found[1].duration = duration
                found[1].duration_str = humanize_seconds(duration)

        GLib.idle_add(f)

    def get_fmd(self) -> FileMetadata | None:
        if self.fn is None:
            return None
        found = find_item(self.files_store, self.fn)
        return found[1].metadata if found else None

    def update_subtitles(self, fmd: FileMetadata) -> None:
        fmd.wait()
        sidecars = find_sidecar_subtitles(Path(fmd.fn), get_preferred_language())

        items = [
            ChoiceItem("No subtitles."),
            *(ChoiceItem(stream.label, stream) for stream in fmd.subtitles),
            ChoiceItem("Add subtitle file...", action=self.on_new_subtitle_clicked),
        ]

        def f():
            if self.fn != fmd.fn:  # another file was selected meanwhile
                return
            self.subtitle_store.splice(0, len(self.subtitle_store), items)
            self.subtitle_combo.set_selected(0)
            for i, sidecar in enumerate(sidecars):
                self.select_subtitles_file(sidecar, select=i == 0)

        GLib.idle_add(f)

    def update_audio_tracks(self, fmd: FileMetadata) -> None:
        fmd.wait()

        items = [
            ChoiceItem(
                f"{video_stream.label} - {audio_stream.label}",
                (video_stream, audio_stream),
            )
            for video_stream in fmd.video_streams
            for audio_stream in fmd.audio_streams
        ]

        def f():
            if self.fn != fmd.fn:  # another file was selected meanwhile
                return
            self.stream_store.splice(0, len(self.stream_store), items)
            self.audio_combo.set_selected(0)

        GLib.idle_add(f)

    def select_cast(self, cast):
        if cast:
            info = cast.cast_info
            logger.info(
                "Selected Chromecast '%s' (%s %s at %s)",
                info.friendly_name,
                info.manufacturer,
                info.model_name,
                info.host,
            )
        self.player.select(cast)
        volume = self.player.volume_level
        if volume is not None:
            self.volume_button.set_value(volume)
        self.update_media_button_states()
        start_thread(self.update_transcoders)

    def error_callback(self, msg):
        GLib.idle_add(
            show_text_dialog,
            self.win,
            "Transcoding Error",
            "Gnomecast encountered an error converting your file.",
            msg,
        )

    def show_file_info(self, b=None):
        fmd = self.get_fmd()
        if fmd is None:
            return
        msg = fmd.details()
        if self.cast:
            msg += f"\nDevice: {self.cast.cast_info.model_name} ({self.cast.cast_info.manufacturer})"
        msg += f"\nChromecast: v{__version__}"

        link = None
        if self.cast and self.fn is not None:
            title = f"Error playing {os.path.basename(self.fn)}"
            body = f"""
[Please describe what happened here...]

[Please link to the download here...]

```
[If possible, please run `ffprobe -i <fn>` and paste the output here...]
```

------------------------------------------------------------

{msg}

{fmd}

```{fmd._important_ffmpeg}``` """
            url = f"https://github.com/keredson/gnomecast/issues/new?title={urllib.parse.quote(title)}&body={urllib.parse.quote(body)}"
            link = ("Report File Doesn't Play", url)

        show_text_dialog(self.win, "File Info", msg, fmd._ffmpeg_output, link)

    def get_nonlocal_cast(self):
        def done(text: str | None) -> None:
            if text is None:
                self.revert_dropdown(self.cast_combo)
                return
            logger.info("Connecting to Chromecast at %s", text)
            start_thread(self.connect_nonlocal_cast, args=[text])

        ask_text(
            self.win,
            "Add a non-local Chromecast",
            "Please specify the IP address or hostname of a Chromecast device:",
            done,
        )

    def connect_nonlocal_cast(self, host: str) -> None:
        try:
            cast = cast_from_host(host)
        except ChromecastConnectionError as e:
            logger.error("Could not connect to Chromecast at %s: %s", host, e)

            def failed():
                self.revert_dropdown(self.cast_combo)
                show_error_dialog(
                    self.win,
                    "Chromecast Not Found",
                    f"The Chromecast '{host}' wasn't found.",
                )

            GLib.idle_add(failed)
            return

        def connected():
            self.cast_store.append(ChoiceItem(host, cast))
            self.cast_combo.set_selected(len(self.cast_store) - 1)

        GLib.idle_add(connected)

    def on_cast_selected(self, item: ChoiceItem) -> None:
        self.select_cast(item.value)

    def on_subtitles_selected(self, item: ChoiceItem) -> None:
        logger.info("Subtitles: %s", item.label)
        stream = item.value
        if stream and stream._subtitles is None:
            fmd = self.get_fmd()
            if fmd is not None:
                stream._subtitles = extract_single_subtitle(fmd.fn, stream.index)
        self.subtitles = stream._subtitles if stream else None
        if self.player.is_active:
            self.player.stop()
            self.player.wait()

            def f():
                self.play_clicked(None)

            start_thread(GLib.idle_add, args=(f,), delay=1)

    def on_stream_selected(self, item: ChoiceItem) -> None:
        video_stream, audio_stream = item.value
        logger.info(
            "Audio track: %s",
            audio_stream.details() if audio_stream else "none",
        )
        self.video_stream = video_stream
        self.audio_stream = audio_stream
        start_thread(self.update_transcoders)


def arg_parse(args, kw_synonyms, f, usage):
    kw = None
    f_args = []
    f_kwargs = {}
    for arg in args:
        if arg.startswith("-"):
            if kw:
                f_kwargs[kw] = True
            arg = arg.lstrip("-")
            kw = kw_synonyms.get(arg, arg)
        else:
            if kw:
                f_kwargs[kw] = arg
            else:
                f_args.append(arg)
            kw = None
    if kw:
        f_kwargs[kw] = True
    try:
        f(*f_args, **f_kwargs)
    except TypeError as e:
        msg = str(e).split("()", 1)[1].strip()
        print("ERROR:", msg, file=sys.stderr)
        print(usage, file=sys.stderr)
        sys.exit(1)


USAGE = """
python gnomecast.py [<media_filename>] [-d|--device <chromecast_name>] [-s|--subtitles <subtitles_filename>] [-v|--verbose]
""".strip()


def delete_old_transcodes():
    # if process is killed old transcoded files can be left around
    # delete if found
    for tmpdir in ["/tmp", "/var/tmp"]:
        for fn in os.listdir(tmpdir):
            if not fn.startswith("gnomecast_"):
                continue
            if fn.startswith("gnomecast_transcode_cache"):
                continue
            fn = os.path.join(tmpdir, fn)
            match = re.search(r"gnomecast_pid(\d+)_", fn)
            if match:
                pid = int(match.group(1))
                if not is_pid_running(pid):
                    logger.debug("Deleting leftover file from dead pid %d: %s", pid, fn)
                    os.remove(fn)
            else:
                logger.debug("Deleting old-style leftover file: %s", fn)
                os.remove(fn)


def start(fn=None, device=None, subtitles=None, verbose=False):
    setup_logging(verbose=verbose)
    logger.info("Gnomecast v%s starting", __version__)
    delete_old_transcodes()
    sys.exit(Gnomecast().run(fn, device=device, subtitles=subtitles))


def main():
    arg_parse(
        sys.argv[1:],
        {"s": "subtitles", "d": "device", "v": "verbose"},
        start,
        USAGE,
    )
