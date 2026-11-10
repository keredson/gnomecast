import logging
import os
import re
import signal
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from .ffmpeg import check_ffmpeg_installed, get_media_duration
from .gui import show_error_dialog
from .log import setup_logging
from .metadata import AudioMetadata, FileMetadata, StreamMetadata
from .player import (
    CastPlayer,
    ChromecastConnectionError,
    cast_from_host,
    discover_casts,
)
from .screensaver import ScreenSaverInhibitor
from .subtitles import (
    convert_subtitles_to_webvtt,
    extract_single_subtitle,
    find_sidecar_subtitles,
    get_preferred_language,
)
from .transcoder import AUDIO_EXTS, Transcoder
from .utils import (
    humanize_seconds,
    is_pid_running,
    run_on_main_thread,
    start_thread,
    throttle,
)
from .version import __version__
from .webserver import GnomecastWebServer

try:
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk
except ImportError:
    line = "-" * 70
    ERROR_MESSAGE = """
{}
Python package "gi" (for building the GU not found.\n
If on Debian or Ubuntu, please run:
$ sudo apt-get install python3-gi\n
For other distributions please look up the equivalent package.\n
If this doesn't work, please report the error here:
https://github.com/keredson/gnomecast\n
Thanks! - Gnomecast
{}
"""
    print(ERROR_MESSAGE.format(line, line), file=sys.stderr)
    sys.exit(1)

logger = logging.getLogger(__name__)


class Gnomecast:
    def __init__(self):
        self.main_loop = GLib.MainLoop()
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
        self.screen_saver_inhibitor = ScreenSaverInhibitor()
        self.autoplay = False
        self.exit_code = 0

    def run(self, fn=None, device=None, subtitles=None) -> int:
        self.build_gui()
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
        self.main_loop.run()
        return self.exit_code

    @property
    def cast(self):
        return self.player.cast

    def check_ffmpeg(self):
        time.sleep(1)

        if not check_ffmpeg_installed():
            show_error_dialog(
                self.win,
                "fmpeg not found",
                "Could not find ffmpeg. Please run 'sudo apt-get install ffmpeg'.",
            )
            self.exit_code = 1
            GLib.idle_add(self.quit)

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
            for row in self.files_store:
                duration = row[2]
                transcoder = row[7]
                if transcoder and duration:
                    if transcoder.done:
                        row[5] = 100
                    else:
                        row[5] = transcoder.progress_seconds * 100 // duration

        GLib.idle_add(f)

    def on_player_state_changed(self, state):
        if state == "PLAYING":
            self.screen_saver_inhibitor.start()
        else:
            self.screen_saver_inhibitor.stop()

        def f():
            self.update_media_button_states()
            self.update_status()

        GLib.idle_add(f)

    def on_player_position(self, seconds):
        GLib.idle_add(self.scrubber_adj.set_value, seconds)

    def on_player_tick(self):
        if self.transcoder and not self.transcoder.done:
            GLib.idle_add(self.update_status)

    def init_casts(self, widget=None, device=None):
        self.cast_store.clear()
        self.cast_store.append([None, "Searching local network - please wait..."])
        self.cast_combo.set_active(0)
        start_thread(self.load_casts, kwargs={"device": device})

    def load_casts(self, device=None):
        chromecasts = discover_casts()

        def update_ui():
            self.cast_store.clear()
            self.cast_store.append([None, "Select a cast device..."])
            self.cast_store.append([-1, "Add a non-local Chromecast..."])
            for cc in chromecasts:
                friendly_name = cc.cast_info.friendly_name
                if cc.cast_type != "cast":
                    friendly_name = f"{friendly_name} ({cc.cast_type})"
                self.cast_store.append([cc, friendly_name])
            if device:
                found = False
                for i, cc in enumerate(chromecasts):
                    if device == cc.cast_info.friendly_name:
                        self.cast_combo.set_active(i + 1)
                        found = True
                if not found:
                    self.cast_combo.set_active(0)
                    show_error_dialog(
                        self.win,
                        "Chromecast not found",
                        f"The Chromecast {device} wasn't found.",
                    )
            else:
                self.cast_combo.set_active(2 if len(chromecasts) == 1 else 0)

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
        self.play_button.set_image(
            Gtk.Image(stock=Gtk.STOCK_MEDIA_PAUSE)
            if state == "PLAYING"
            else Gtk.Image(stock=Gtk.STOCK_MEDIA_PLAY)
        )
        if self.transcoder and self.duration is not None and self.duration > 0:
            self.scrubber_adj.set_upper(self.duration)
            self.scrubber.set_sensitive(True)
        else:
            self.scrubber.set_sensitive(False)
        self.update_button_visible()

    def build_gui(self):
        self.win = win = Gtk.ApplicationWindow(title=f"Gnomecast v{__version__}")
        win.set_border_width(0)
        win.set_icon(self.get_logo_pixbuf(color="#000000"))
        enforce_target = Gtk.TargetEntry.new("text/plain", Gtk.TargetFlags(4), 129)
        win.drag_dest_set(Gtk.DestDefaults.ALL, [enforce_target], Gdk.DragAction.COPY)
        win.connect("drag-data-received", self.on_drag_data_received)
        self.cast_store = cast_store = Gtk.ListStore(object, str)

        vbox_outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)

        self.thumbnail_image = Gtk.Image()
        self.thumbnail_image.set_from_pixbuf(self.get_logo_pixbuf())
        vbox_outer.pack_start(self.thumbnail_image, True, False, 0)
        alignment = Gtk.Alignment(xscale=1, yscale=1)
        alignment.add(vbox)
        alignment.set_padding(16, 20, 16, 16)
        vbox_outer.pack_start(alignment, False, False, 0)

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        vbox.pack_start(hbox, False, False, 0)
        self.cast_combo = cast_combo = Gtk.ComboBox.new_with_model(cast_store)
        cast_combo.set_entry_text_column(1)
        renderer_text = Gtk.CellRendererText()
        cast_combo.pack_start(renderer_text, True)
        cast_combo.add_attribute(renderer_text, "text", 1)
        hbox.pack_start(cast_combo, True, True, 0)
        refresh_button = Gtk.Button(image=Gtk.Image(stock=Gtk.STOCK_REFRESH))
        refresh_button.connect("clicked", self.init_casts)
        hbox.pack_start(refresh_button, False, False, 0)

        win.add(vbox_outer)

        # list of queued files
        self.files_store = Gtk.ListStore(
            str, str, int, str, str, int, str, object, object
        )  # name, path, duration, duration_str, thumbnail_fn, transcode_progress, status_icon, transcoder, file_metadata
        self.files_store.connect("row-inserted", self.update_button_visible)
        self.files_store.connect("row-deleted", self.update_button_visible)
        self.files_view = Gtk.TreeView(model=self.files_store)
        self.files_view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self.files_view.set_headers_visible(False)
        self.files_view.set_rules_hint(True)
        r = Gtk.CellRendererText()
        column = Gtk.TreeViewColumn("Name", r)
        column.add_attribute(r, "text", 0)
        column.set_expand(True)
        self.files_view.append_column(column)
        self.file_view_column_renderer = r = Gtk.CellRendererText()
        r.props.xalign = 1.0
        column_duration = Gtk.TreeViewColumn("Duration", r)
        column_duration.add_attribute(r, "text", 3)
        self.files_view.append_column(column_duration)
        r = Gtk.CellRendererProgress()
        self.files_view_progress_column = column_progress = Gtk.TreeViewColumn(
            "Progress", r
        )
        column_progress.add_attribute(r, "value", 5)
        self.files_view.append_column(column_progress)

        r = Gtk.CellRendererPixbuf()
        column_pixbuf = Gtk.TreeViewColumn("Playing", r)
        column_pixbuf.add_attribute(r, "icon_name", 6)
        self.files_view.append_column(column_pixbuf)

        select = self.files_view.get_selection()
        select.connect("changed", self.on_files_view_selection_changed)
        self.files_view.connect("row-activated", self.on_files_view_row_activated)

        # contains the files list and the buttons to add/del
        self.hbox = hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        vbox.pack_start(hbox, False, False, 0)

        self.scrolled_window = Gtk.ScrolledWindow()
        self.scrolled_window.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scrolled_window.add(self.files_view)
        hbox.pack_start(self.scrolled_window, True, True, 0)

        self.btn_vbox = btn_vbox = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8
        )
        hbox.pack_start(btn_vbox, True, True, 0)
        self.file_button = Gtk.Button(image=Gtk.Image(stock=Gtk.STOCK_ADD))
        self.file_button.set_tooltip_text("Add one or more audio or video files...")
        self.file_button.set_always_show_image(True)
        self.file_button.connect("clicked", self.on_file_clicked)
        btn_vbox.pack_start(self.file_button, True, True, 0)
        self.remove_button = Gtk.Button(image=Gtk.Image(stock=Gtk.STOCK_REMOVE))
        self.remove_button.set_tooltip_text(
            "Overwrite original file with transcoded version."
        )
        self.remove_button.connect("clicked", self.remove_files)
        self.remove_button.set_sensitive(False)
        btn_vbox.pack_start(self.remove_button, False, False, 0)

        self.file_detail_row = hbox = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8
        )
        vbox.pack_start(self.file_detail_row, False, False, 0)

        # audio/video track selection
        self.stream_store = Gtk.ListStore(str, object, object)
        self.audio_combo = Gtk.ComboBox.new_with_model(self.stream_store)
        self.audio_combo.connect("changed", self.on_audio_combo_changed)
        self.audio_combo.set_entry_text_column(0)
        renderer_text = Gtk.CellRendererText()
        self.audio_combo.pack_start(renderer_text, True)
        self.audio_combo.add_attribute(renderer_text, "text", 0)
        self.file_detail_row.pack_start(self.audio_combo, True, True, 0)

        # subtitle selection
        self.subtitle_store = Gtk.ListStore(
            str, object, object
        )  # title, stream, callback
        self.subtitle_combo = Gtk.ComboBox.new_with_model(self.subtitle_store)
        self.subtitle_combo.connect("changed", self.on_subtitle_combo_changed)
        self.subtitle_combo.set_entry_text_column(0)
        renderer_text = Gtk.CellRendererText()
        self.subtitle_combo.pack_start(renderer_text, True)
        self.subtitle_combo.add_attribute(renderer_text, "text", 0)
        self.subtitle_combo.set_active(0)
        self.file_detail_row.pack_start(self.subtitle_combo, True, True, 0)

        file_info_button = Gtk.Button(image=Gtk.Image(stock=Gtk.STOCK_DIALOG_INFO))
        file_info_button.connect("clicked", self.show_file_info)
        self.file_detail_row.pack_start(file_info_button, False, False, 0)

        self.scrubber_adj = Gtk.Adjustment(
            value=0, lower=0, upper=100, step_increment=15, page_increment=60
        )
        self.scrubber = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.scrubber_adj
        )
        self.scrubber.set_digits(0)

        def f(scale, s):
            notes = [humanize_seconds(s)]
            return "".join(notes)

        self.scrubber.connect("format-value", f)
        self.scrubber.connect("change-value", self.scrubber_move_started)
        self.scrubber.connect("change-value", self.scrubber_moved)
        self.scrubber.set_sensitive(False)
        vbox.pack_start(self.scrubber, False, False, 0)

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        self.rewind_button = Gtk.Button(image=Gtk.Image(stock=Gtk.STOCK_MEDIA_REWIND))
        self.rewind_button.connect("clicked", self.rewind_clicked)
        self.rewind_button.set_sensitive(False)
        self.rewind_button.set_relief(Gtk.ReliefStyle.NONE)
        hbox.pack_start(self.rewind_button, True, False, 0)
        self.play_button = Gtk.Button(image=Gtk.Image(stock=Gtk.STOCK_MEDIA_PLAY))
        self.play_button.connect("clicked", self.play_clicked)
        self.play_button.set_sensitive(False)
        self.play_button.set_relief(Gtk.ReliefStyle.NONE)
        hbox.pack_start(self.play_button, True, False, 0)
        self.forward_button = Gtk.Button(image=Gtk.Image(stock=Gtk.STOCK_MEDIA_FORWARD))
        self.forward_button.connect("clicked", self.forward_clicked)
        self.forward_button.set_sensitive(False)
        self.forward_button.set_relief(Gtk.ReliefStyle.NONE)
        hbox.pack_start(self.forward_button, True, False, 0)
        self.stop_button = Gtk.Button(image=Gtk.Image(stock=Gtk.STOCK_MEDIA_STOP))
        self.stop_button.connect("clicked", self.stop_clicked)
        self.stop_button.set_sensitive(False)
        self.stop_button.set_relief(Gtk.ReliefStyle.NONE)
        hbox.pack_start(self.stop_button, True, False, 0)
        self.volume_button = Gtk.VolumeButton()
        self.volume_button.set_value(1)
        self.volume_button.connect("value-changed", self.volume_moved)
        self.volume_button.set_sensitive(False)
        hbox.pack_start(self.volume_button, True, False, 0)
        speed_label = Gtk.Label(label="Speed:")
        speed_label.set_tooltip_text("Playback speed")
        hbox.pack_start(speed_label, False, False, 0)
        self.speed_button = Gtk.SpinButton()
        self.speed_button.set_adjustment(
            Gtk.Adjustment(
                value=1.0,
                lower=0.5,
                upper=2.0,
                step_increment=0.05,
                page_increment=0.25,
            )
        )
        self.speed_button.set_digits(2)
        self.speed_button.set_width_chars(5)
        self.speed_button.set_tooltip_text("Playback speed")
        self.speed_button.connect("value-changed", self.speed_moved)
        self.speed_button.set_sensitive(False)
        hbox.pack_start(self.speed_button, True, False, 0)
        vbox.pack_start(hbox, False, False, 0)

        cast_combo.connect("changed", self.on_cast_combo_changed)

        win.connect("delete-event", self.quit)
        win.connect("key_press_event", self.on_key_press)
        win.show_all()

        self.update_button_visible()

        win.resize(1, 1)

        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self.quit)

    def add_extra_subtitle_options(self):
        self.subtitle_store.prepend(["No subtitles.", None, None])
        self.subtitle_store.append(
            ["Add subtitle file...", None, self.on_new_subtitle_clicked]
        )
        self.subtitle_combo.set_active(0)

    def on_drag_data_received(self, widget, drag_context, x, y, data, info, time):
        fn = data.get_text()
        if fn.startswith("file://"):
            fn = urllib.parse.unquote(fn[len("file://") :]).strip()
            self.queue_files([fn])

    def update_button_visible(self, x=None, y=None, z=None):
        count = len(self.files_store)
        self.scrolled_window.set_visible(bool(count))
        self.remove_button.set_visible(bool(count))
        self.file_button.set_label(
            "" if count else "  Add one or more audio or video files..."
        )
        file_button_child = self.file_button.get_child()
        assert isinstance(file_button_child, Gtk.Alignment)
        file_button_child.set_padding(
            1, 0, 2, 0
        )  # w/ an empty label the + icon isn't quite centered
        self.hbox.set_child_packing(
            self.btn_vbox, not count, not count, 0, Gtk.PackType.START
        )
        self.file_detail_row.set_visible(bool(self.fn))

    def scrubber_move_started(self, scale, scroll_type, seconds):
        self.player.begin_seek()

    def on_files_view_selection_changed(self, selection):
        _model, treeiter = selection.get_selected_rows()
        self.remove_button.set_sensitive(bool(treeiter))

    def remove_files(self, w):
        store, paths = self.files_view.get_selection().get_selected_rows()
        for path in reversed(paths):
            iterx = store.get_iter(path)
            transcoder = store.get_value(iterx, 7)
            if transcoder:
                transcoder.destroy()
            fn = store.get_value(iterx, 1)
            logger.info("Removed from queue: %s", fn)
            self.files_store.remove(iterx)
            if self.fn == fn:
                self.unselect_file()

    def on_files_view_row_activated(self, widget, row, col):
        model = widget.get_model()
        fn = model[row][1]
        logger.info("Selected file: %s", fn)
        self.unselect_file()
        self.fn = fn
        self.transcoder = model[row][7]
        self.duration = model[row][2]
        thumbnail_fn = model[row][4]
        if thumbnail_fn and os.path.isfile(thumbnail_fn):
            self.thumbnail_image.set_from_file(thumbnail_fn)
        self.player.stop()

        def f():
            self.win.resize(1, 1)
            self.scrubber_adj.set_value(0)
            for r in self.files_store:
                if self.fn == r[1]:
                    r[6] = "video-x-generic"
                else:
                    r[6] = None
            self.update_button_visible()
            self.update_media_button_states()

        GLib.idle_add(f)

        return True

    def queue_files(self, files):
        existing_files = {row[1] for row in self.files_store}
        files = [f for f in files if f not in existing_files]
        for fn in files:
            display = os.path.basename(fn)
            MAX_LEN = 40
            if len(display) > MAX_LEN:
                display = display[: MAX_LEN - 10] + "..." + display[-10:]

            def callback(fmd):
                def f():
                    if os.path.isfile(fmd.thumbnail_fn):
                        for row in self.files_store:
                            if row[1] == fmd.fn:
                                row[4] = fmd.thumbnail_fn
                    if self.fn == fmd.fn and fmd.thumbnail_fn:
                        self.thumbnail_image.set_from_file(fmd.thumbnail_fn)
                        self.win.resize(1, 1)
                    self.update_status()

                GLib.idle_add(f)

            logger.info("Queued: %s", fn)
            fmd = FileMetadata(fn, callback)
            self.files_store.append(
                [display, fn, None, "...", None, None, None, None, fmd]
            )
            start_thread(self.get_duration, args=[fn])
        self.scrolled_window.set_visible(True)
        if len(files) and self.fn is None:
            self.select_file(files[0])
        Gtk.TreePath().new_first()
        _1, _2, _width, height = self.files_view_progress_column.cell_get_size()
        height += self.file_view_column_renderer.get_padding()[1] * 2
        height += 2  # measured - row lines?
        self.scrolled_window.set_min_content_height(
            height * min(len(self.files_store), 6)
        )

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

    def get_logo_pixbuf(self, width=200, color=None):
        svg = (Path(__file__) / ".." / "assets" / "gnomecast.svg").resolve().read_text()
        if color:
            svg = svg.replace("#aaaaaa", color)
        f = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(svg.encode()))
        pixbuf = GdkPixbuf.Pixbuf.new_from_stream(f, None)
        return pixbuf

    def quit(self, *args) -> bool:
        for row in self.files_store:
            transcoder = row[7]
            if transcoder:
                transcoder.destroy()
            thumbnail_fn = row[4]
            if thumbnail_fn and os.path.isfile(thumbnail_fn):
                os.remove(thumbnail_fn)
        self.screen_saver_inhibitor.stop()
        self.main_loop.quit()
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
        dialog = Gtk.FileChooserDialog(
            title="Please choose an audio or video file...",
            transient_for=self.win,
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL,
            Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN,
            Gtk.ResponseType.OK,
        )
        dialog.set_select_multiple(True)

        downloads_dir = os.path.expanduser("~/Downloads")
        if os.path.isdir(downloads_dir):
            dialog.set_current_folder(downloads_dir)

        filter_py = Gtk.FileFilter()
        filter_py.set_name("Videos")
        filter_py.add_mime_type("video/*")
        filter_py.add_mime_type("audio/*")
        dialog.add_filter(filter_py)

        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            self.queue_files(dialog.get_filenames())
            # self.select_file(dialog.get_filename())

        dialog.destroy()

    def on_new_subtitle_clicked(self):
        dialog = Gtk.FileChooserDialog(
            title="Please choose a subtitle file...",
            transient_for=self.win,
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL,
            Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN,
            Gtk.ResponseType.OK,
        )

        if self.fn is not None:
            dialog.set_current_folder(os.path.dirname(self.fn))

        filter_py = Gtk.FileFilter()
        filter_py.set_name("Subtitles")
        filter_py.add_pattern("*.srt")
        filter_py.add_pattern("*.vtt")
        dialog.add_filter(filter_py)

        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            filename = dialog.get_filename()
            if filename is not None:
                self.select_subtitles_file(filename)
        elif response == Gtk.ResponseType.CANCEL:
            self.subtitle_combo.set_active(0)

        dialog.destroy()

    def select_subtitles_file(self, fn: str | Path, select: bool = True):
        substitles_path = Path(fn)
        if not substitles_path.is_file():
            show_error_dialog(
                self.win, "File not found", f"Could not find subtitles file: {fn}."
            )
            return

        subtitles_path = substitles_path.resolve()
        display_name = subtitles_path.name
        pos = len(self.subtitle_store)
        stream = StreamMetadata(None, None, display_name)
        stream._subtitles = convert_subtitles_to_webvtt(subtitles_path)
        logger.info("Loaded subtitles file: %s", subtitles_path)
        self.subtitle_store.append([display_name, stream, None])
        if select:
            self.subtitles = stream._subtitles
            self.subtitle_combo.set_active(pos)

    def unselect_file(self):
        self.thumbnail_image.set_from_pixbuf(self.get_logo_pixbuf())
        self.fn = None
        self.stream_store.clear()
        self.subtitle_store.clear()
        self.subtitle_combo.set_active(0)
        self.transcoder = None
        self.duration = None
        self.player.stop()

        def f():
            self.scrubber_adj.set_value(0)
            for row in self.files_store:
                row[6] = None
            self.win.resize(1, 1)
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
        self.thumbnail_image.set_from_pixbuf(self.get_logo_pixbuf())
        self.fn = fn
        self.stream_store.clear()
        self.subtitle_store.clear()
        self.player.stop()

        def f():
            self.scrubber_adj.set_value(0)
            for row in self.files_store:
                thumbnail_fn = row[4]
                if self.fn == row[1]:
                    if thumbnail_fn:
                        self.thumbnail_image.set_from_file(thumbnail_fn)
                        self.win.resize(1, 1)
                    row[6] = "video-x-generic"
                    self.duration = row[2]
                else:
                    row[6] = None
            start_thread(self.update_transcoders)
            start_thread(self.update_audio_tracks)
            start_thread(self.update_subtitles)
            self.update_button_visible()
            self.update_media_button_states()

        GLib.idle_add(f)

    update_transcoders_lock = threading.Lock()

    def update_transcoders(self):
        with self.update_transcoders_lock:
            if self.cast and self.fn is not None:
                rows = run_on_main_thread(
                    lambda: [(row[1], row[7], row[8]) for row in self.files_store]
                )
                for fn, transcoder, fmd in rows:
                    if fn != self.fn:
                        continue
                    fmd.wait()
                    if not self.video_stream:
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
                            lambda fn=fn, t=self.transcoder: self.set_row_transcoder(
                                fn, t
                            )
                        )
                if self.autoplay:
                    self.autoplay = False
                    GLib.idle_add(self.play_clicked, None)
            if not self.cast:
                GLib.idle_add(self.destroy_all_transcoders)
            GLib.idle_add(self.update_media_button_states)

    def set_row_transcoder(self, fn: str, transcoder: Transcoder | None) -> None:
        for row in self.files_store:
            if row[1] == fn:
                row[7] = transcoder

    def destroy_all_transcoders(self) -> None:
        for row in self.files_store:
            transcoder = row[7]
            if transcoder:
                transcoder.destroy()
                row[7] = None

    def check_for_next_in_queue(self):
        # called from the player monitor thread
        GLib.idle_add(self._play_next_in_queue)

    def _play_next_in_queue(self) -> None:
        next = False
        for row in self.files_store:
            fn = row[1]
            if next:
                logger.info("Autoplaying next in queue: %s", fn)
                self.autoplay = True
                self.select_file(fn)
                return
            if self.cast and self.fn is not None and self.fn == fn:
                next = True

    def prep_next_transcode(self):
        transcode_next = False
        for row in self.files_store:
            fn = row[1]
            transcoder = row[7]
            fmd = row[8]
            if transcode_next and not transcoder:
                logger.debug("Preparing next in queue: %s", fn)
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
                row[7] = transcoder
                transcode_next = False
            if (
                self.cast
                and self.fn is not None
                and self.fn == fn
                and transcoder
                and transcoder.done
            ):
                transcode_next = True

    def get_duration(self, fn: str) -> None:
        duration = get_media_duration(Path(fn))

        def f():
            if fn == self.fn:
                self.duration = duration
            for row in self.files_store:
                if row[1] == fn:
                    row[2] = duration
                    row[3] = humanize_seconds(duration)

        GLib.idle_add(f)

    def get_fmd(self):
        for row in self.files_store:
            fn = row[1]
            fmd = row[8]
            if self.fn == fn:
                return fmd

    def update_subtitles(self):
        fmd = self.get_fmd()
        fmd.wait()

        sidecars = (
            find_sidecar_subtitles(Path(self.fn), get_preferred_language())
            if self.fn is not None
            else []
        )

        def f():
            self.subtitle_store.clear()
            for stream in fmd.subtitles:
                self.subtitle_store.append([stream.label, stream, None])
            self.add_extra_subtitle_options()
            for i, sidecar in enumerate(sidecars):
                self.select_subtitles_file(sidecar, select=i == 0)

        GLib.idle_add(f)

    def update_audio_tracks(self):
        fmd = self.get_fmd()
        fmd.wait()

        def f():
            self.stream_store.clear()
            for video_stream in fmd.video_streams:
                for audio_stream in fmd.audio_streams:
                    self.stream_store.append(
                        [
                            f"{video_stream.label} - {audio_stream.label}",
                            video_stream,
                            audio_stream,
                        ]
                    )
            self.audio_combo.set_active(0)

        GLib.idle_add(f)

    def on_key_press(self, widget, event, user_data=None):
        key = Gdk.keyval_name(event.keyval)
        ctrl = event.state & Gdk.ModifierType.CONTROL_MASK
        if key == "q" and ctrl:
            self.quit()
            return True
        return False

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
        def f():
            dialogWindow = Gtk.MessageDialog(
                transient_for=self.win,
                modal=True,
                destroy_with_parent=True,
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.OK,
                text="\nGnomecast encountered an error converting your file.",
            )
            dialogWindow.set_title("Transcoding Error")
            dialogWindow.set_default_size(1, 400)

            dialogBox = dialogWindow.get_content_area()
            buffer1 = Gtk.TextBuffer()
            buffer1.set_text(msg)
            text_view = Gtk.TextView(buffer=buffer1)
            text_view.set_editable(False)
            scrolled_window = Gtk.ScrolledWindow()
            scrolled_window.set_border_width(5)
            # we scroll only if needed
            scrolled_window.set_policy(
                Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC
            )
            scrolled_window.add(text_view)
            dialogBox.pack_end(scrolled_window, True, True, 0)
            dialogWindow.show_all()
            dialogWindow.run()
            dialogWindow.destroy()

        GLib.idle_add(f)

    def show_file_info(self, b=None):
        fmd = self.get_fmd()
        msg = "\n" + fmd.details()
        if self.cast:
            msg += f"\nDevice: {self.cast.cast_info.model_name} ({self.cast.cast_info.manufacturer})"
        msg += f"\nChromecast: v{__version__}"
        dialogWindow = Gtk.MessageDialog(
            transient_for=self.win,
            modal=True,
            destroy_with_parent=True,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text=msg,
        )
        dialogWindow.set_title("File Info")
        dialogWindow.set_default_size(1, 400)

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
            dialogWindow.add_action_widget(
                Gtk.LinkButton(uri=url, label="Report File Doesn't Play"), 10
            )

        dialogBox = dialogWindow.get_content_area()
        buffer1 = Gtk.TextBuffer()
        buffer1.set_text(fmd._ffmpeg_output)
        text_view = Gtk.TextView(buffer=buffer1)
        text_view.set_editable(False)
        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.set_border_width(5)
        # we scroll only if needed
        scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled_window.add(text_view)
        dialogBox.pack_end(scrolled_window, True, True, 0)

        dialogWindow.show_all()
        dialogWindow.run()
        dialogWindow.destroy()

    def get_nonlocal_cast(self):
        dialogWindow = Gtk.MessageDialog(
            transient_for=self.win,
            modal=True,
            destroy_with_parent=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="\nPlease specify the IP address or hostname of a Chromecast device:",
        )

        dialogWindow.set_title("Add a non-local Chromecast")

        dialogBox = dialogWindow.get_content_area()
        userEntry = Gtk.Entry()
        #    userEntry.set_size_request(250,0)
        dialogBox.pack_end(userEntry, False, False, 0)

        dialogWindow.show_all()
        response = dialogWindow.run()
        text = userEntry.get_text()
        dialogWindow.destroy()
        if (response == Gtk.ResponseType.OK) and (text != ""):
            logger.info("Connecting to Chromecast at %s", text)
            try:
                cast = cast_from_host(text)
                self.cast_store.append([cast, text])
                self.cast_combo.set_active(len(self.cast_store) - 1)
            except ChromecastConnectionError as e:
                logger.error("Could not connect to Chromecast at %s: %s", text, e)
                dialog = Gtk.MessageDialog(
                    transient_for=self.win,
                    message_type=Gtk.MessageType.ERROR,
                    buttons=Gtk.ButtonsType.CLOSE,
                    text="Chromecast Not Found",
                )
                dialog.format_secondary_text(f"The Chromecast '{text}' wasn't found.")
                dialog.run()
                dialog.destroy()

    def on_cast_combo_changed(self, combo):
        tree_iter = combo.get_active_iter()
        if tree_iter is not None:
            model = combo.get_model()
            cast, _name = model[tree_iter][:2]
            if cast == -1:
                self.get_nonlocal_cast()
            else:
                self.select_cast(cast)
        else:
            combo.get_child()

    def on_subtitle_combo_changed(self, combo):
        tree_iter = combo.get_active_iter()
        if tree_iter is not None:
            model = combo.get_model()
            text, stream, callback = model[tree_iter]
            if callback:
                callback()
            else:
                logger.info("Subtitles: %s", text)
                if stream and stream._subtitles is None:
                    fmd = self.get_fmd()
                    stream._subtitles = extract_single_subtitle(fmd.fn, stream.index)
                self.subtitles = stream._subtitles if stream else None
                if self.player.is_active:
                    self.player.stop()
                    self.player.wait()

                    def f():
                        self.play_clicked(None)

                    start_thread(GLib.idle_add, args=(f,), delay=1)
        else:
            combo.get_child()

    def on_audio_combo_changed(self, combo):
        tree_iter = combo.get_active_iter()
        if tree_iter is not None:
            model = combo.get_model()
            _text, video_stream, audio_stream = model[tree_iter]
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
