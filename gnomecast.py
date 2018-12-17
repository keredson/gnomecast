import base64
import codecs
import contextlib
import io
import mimetypes
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib

DEPS_MET = True
try:
  import pychromecast
  import bottle
  import html5lib.treebuilders
  
  # hack fixing pycaption needing an old version of html5lib
  if not hasattr(html5lib.treebuilders, '_base'):
    html5lib.treebuilders._base = html5lib.treebuilders.base

  import pycaption
except Exception as e:
  traceback.print_exc()
  print(e)
  DEPS_MET = False

DBUS_AVAILABLE = False
try:
  import dbus
  DBUS_AVAILABLE = True
except Exception as e:
  print(e)

  
try:
  import gi
  gi.require_version('Gtk', '3.0')
  from gi.repository import Gtk, Gdk, GLib, GdkPixbuf, Gio
except ImportError:
  line = "-"*70
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
  print(ERROR_MESSAGE.format(line,line))
  sys.exit(1)

__version__ = '1.7.0'

if DEPS_MET:
  pycaption.WebVTTWriter._encode = lambda self, s: s


def throttle(seconds=2):
  def decorator(f):
    timer = None
    lastest_args, latest_kwargs = None, None
    def run_f():
      nonlocal timer, lastest_args, latest_kwargs
      ret = f(*lastest_args, **latest_kwargs)
      timer = None
      return ret
    def wrapper(*args, **kwargs):
      nonlocal timer, lastest_args, latest_kwargs
      lastest_args, latest_kwargs = args, kwargs
      if timer == None:
        timer = threading.Timer(seconds, run_f)
        timer.start()
    return wrapper
  return decorator


AUDIO_EXTS = ('aac','mp3','wav')

class Transcoder(object):

  def __init__(self, cast, fn, done_callback, prev_transcoder, force_audio=False, force_video=False):
    self.cast = cast
    self.source_fn = fn
    self.p = None
    self.show_save_button = False
    
    if prev_transcoder and prev_transcoder.source_fn == self.source_fn:
      self.transcode_video = prev_transcoder.transcode_video
      self.transcode_audio = prev_transcoder.transcode_audio
      self.transcode = False
      self.trans_fn = prev_transcoder.trans_fn
    else:
      if prev_transcoder:
        prev_transcoder.destroy()
      output = subprocess.check_output(['ffmpeg', '-i', fn, '-f', 'ffmetadata', '-'], stderr=subprocess.STDOUT).decode().split('\n')
      container = fn.lower().split(".")[-1]
      video_codec = None
      transcode_audio = container not in AUDIO_EXTS
      for line in output:
        line = line.strip()
        if line.startswith('Stream') and 'Video' in line and not video_codec:
          video_codec = line.split()[3]
        elif line.startswith('Stream') and 'Audio' in line and ('aac (LC)' in line or 'aac (HE)' in line or 'mp3' in line):
          transcode_audio = False
      transcode_audio |= force_audio
      print('Transcoder', fn, container, video_codec, transcode_audio)
      transcode_container = container not in ('mp4','aac','mp3','wav')
      self.transcode_video = force_video or not self.can_play_video_codec(video_codec)
      self.transcode_audio = transcode_audio
      self.transcode = transcode_container or self.transcode_video or self.transcode_audio
      self.trans_fn = None

    self.progress_bytes = 0
    self.progress_seconds = 0
    self.done_callback = done_callback
    print ('transcode, transcode_video, transcode_audio', self.transcode, self.transcode_video, self.transcode_audio)
    if self.transcode:
      self.done = False
      dir = '/var/tmp' if os.path.isdir('/var/tmp') else None
      self.trans_fn = tempfile.mkstemp(suffix='.mp4', prefix='gnomecast_', dir=dir)[1]
      os.remove(self.trans_fn)
      # flags = '''-c:v libx264 -profile:v high -level 5 -crf 18 -maxrate 10M -bufsize 16M -pix_fmt yuv420p -x264opts bframes=3:cabac=1 -movflags faststart -c:a libfdk_aac -b:a 320k''' # -vf "scale=iw*sar:ih, scale='if(gt(iw,ih),min(1920,iw),-1)':'if(gt(iw,ih),-1,min(1080,ih))'"
      args = ['ffmpeg', '-i', self.source_fn, '-c:v', 'h264' if self.transcode_video else 'copy', '-c:a', 'mp3' if self.transcode_audio else 'copy'] + (['-b:a','256k'] if self.transcode_audio else []) + [self.trans_fn] # '-movflags', 'faststart'
      # args = ['ffmpeg', '-i', self.source_fn, '-c:v', 'libvpx', '-b:v', '5M', '-c:a', 'libvorbis', '-deadline','realtime', self.trans_fn]
      # args = ['ffmpeg', '-i', self.source_fn] + flags.split() + [self.trans_fn]
      print(args)
      self.p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
      t = threading.Thread(target=self.monitor)
      t.daemon = True
      t.start()
    else:
      self.done = True
      self.done_callback()
  
  @property
  def fn(self):
    return self.trans_fn if self.transcode else self.source_fn
    
  def can_play_video_codec(self, video_codec):
    if self.cast.device.model_name=='Chromecast Ultra':
      return video_codec in ('h264','h265','hevc')
    else:
      return video_codec in ('h264',)
    
  def wait_for_byte(self, offset, buffer=128*1024*1024):
    if self.done: return
    if self.source_fn.lower().split(".")[-1]=='mp4':
      while offset > self.progress_bytes + buffer:
        print('waiting for', offset, 'at', self.progress_bytes + buffer)
        time.sleep(2)
    else:
      while not self.done:
        print('waiting for transcode to finish')
        time.sleep(2)
    print('done waiting')
  
  def monitor(self):
    line = b''
    r = re.compile(r'=\s+')
    while True:
      byte = self.p.stdout.read(1)
      if byte == b'' and self.p.poll() != None:
        break
      if byte != b'':
        line += byte
        if byte == b'\r':
          # frame=92578 fps=3937 q=-1.0 size= 1142542kB time=01:04:21.14 bitrate=2424.1kbits/s speed= 164x 
          line = line.decode()
          line = r.sub('=', line)
          items = [s.split('=') for s in line.split()]
          d = dict([x for x in items if len(x)==2])
          print(d)
          self.progress_bytes = int(d.get('size','0kb')[:-2])*1024
          self.progress_seconds = parse_ffmpeg_time(d.get('time','00:00:00'))
          line = b''
    self.p.stdout.close()
    self.done = True
    self.show_save_button = True
    self.done_callback(did_transcode=True)
  
  def destroy(self):
    # self.cast.media_controller.stop()
    if self.p and self.p.poll() is None:
      self.p.terminate()
    if self.trans_fn and os.path.isfile(self.trans_fn):
      os.remove(self.trans_fn)

def find_screensaver_dbus_iface(bus):
  """ Searches the DBus names for Screensaver and returns correct Interface"""
  if not DBUS_AVAILABLE: return None
  for path, name in [('org.freedesktop.ScreenSaver', '/ScreenSaver'), ('org.mate.ScreenSaver', '/ScreenSaver')]:
    try:
      saver = bus.get_object(path, name)
      return dbus.Interface(saver, dbus_interface=path)
    except dbus.exceptions.DBusException as e:
      # wrong path, try next one
      print(e)
  return None

class Gnomecast(object):

  def __init__(self):
    self.ip = (([ip for ip in socket.gethostbyname_ex(socket.gethostname())[2] if not ip.startswith("127.")] or [[(s.connect(("8.8.8.8", 53)), s.getsockname()[0], s.close()) for s in [socket.socket(socket.AF_INET, socket.SOCK_DGRAM)]][0][1]]) + [None])[0]
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
      s.bind(('0.0.0.0', 0))
      self.port = s.getsockname()[1]
    self.app = bottle.Bottle()
    self.cast = None
    self.last_known_player_state = None
    self.last_known_current_time = None
    self.last_time_current_time = None
    self.fn = None
    self.last_fn_played = None
    self.transcoder = None
    self.duration = None
    self.subtitles = None
    self.seeking = False
    self.last_known_volume_level = None
    bus = dbus.SessionBus() if DBUS_AVAILABLE else None
    self.saver_interface = find_screensaver_dbus_iface(bus)
    self.inhibit_screensaver_cookie = None
    self.autoplay = False

  def run(self, fn=None, device=None, subtitles=None):
    self.build_gui()
    self.init_casts(device=device)
    threading.Thread(target=self.check_ffmpeg).start()
    t = threading.Thread(target=self.start_server)
    t.daemon = True
    t.start()
    t = threading.Thread(target=self.monitor_cast)
    t.daemon = True
    t.start()
    if fn:
      self.select_file(fn)
    if subtitles:
      self.select_subtitles_file(subtitles)
    if fn and subtitles:
      self.autoplay = True
    Gtk.main()
    
  def check_ffmpeg(self):
    time.sleep(1)
    ffmpeg_available = True
    print('check_ffmpeg')
    try:
      print(subprocess.check_output(['which', 'ffmpeg']))
    except Exception as e:
      print(e, e.output)
      ffmpeg_available = False
    if not ffmpeg_available:
      def f():
        dialog = Gtk.MessageDialog(self.win, 0, Gtk.MessageType.ERROR, Gtk.ButtonsType.CLOSE, "FFMPEG not Found")
        dialog.format_secondary_text("Could not find ffmpeg.  Please run 'sudo apt-get install ffmpeg'.")
        dialog.run()
        dialog.destroy()
        # TODO: there's a weird pause here closing the dialog.  why?
        sys.exit(1)
      GLib.idle_add(f)
    
  def start_server(self):
    app = self.app

    @app.route('/subtitles.vtt')
    def subtitles():
      # response = bottle.static_file(self.subtitles_fn, root='/', mimetype='text/vtt')
      response = bottle.response
      response.headers['Access-Control-Allow-Origin'] = '*'
      response.headers['Access-Control-Allow-Methods'] = 'GET, HEAD'
      response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
      response.headers['Content-Type'] = 'text/vtt'
      return self.subtitles
    
    @app.get('/media/<id>.<ext>')
    def video(id, ext):
      print(list(bottle.request.headers.items()))
      ranges = list(bottle.parse_range_header(bottle.request.environ['HTTP_RANGE'], 1000000000000))
      print('ranges', ranges)
      offset, end = ranges[0]
      self.transcoder.wait_for_byte(offset)
      response = bottle.static_file(self.transcoder.fn, root='/')
      if 'Last-Modified' in response.headers:
        del response.headers['Last-Modified']
      response.headers['Access-Control-Allow-Origin'] = '*'
      response.headers['Access-Control-Allow-Methods'] = 'GET, HEAD'
      response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
      return response

    # app.run(host=self.ip, port=self.port, server='paste', daemon=True)
    from paste import httpserver
    from paste.translogger import TransLogger
    handler = TransLogger(app, setup_console_handler=True)
    httpserver.serve(handler, host=self.ip, port=str(self.port), daemon_threads=True)

  def update_status(self, did_transcode=False):
    if did_transcode:
      self.update_button_visible()
      self.prep_next_transcode()
#    if self.last_known_player_state and self.last_known_player_state!='UNKNOWN':
#      notes.append('Cast: %s' % self.last_known_player_state)
    def f():
      for row in self.files_store:
        duration = row[2]
        transcoder = row[7]
        if transcoder:
          if duration:
            if transcoder.done:
              row[5] = 100
            else:
              row[5] = transcoder.progress_seconds*100 // duration
    GLib.idle_add(f)
    
  def monitor_cast(self):
    while True:
      time.sleep(1)
      if not self.cast: continue
      seeking = self.seeking
      cast = self.cast
      mc = cast.media_controller
      if mc.status.player_state != self.last_known_player_state:
        if mc.status.player_state=='PLAYING' and self.last_known_player_state=='BUFFERING' and seeking:
          self.seeking = False
        if mc.status.player_state=='IDLE' and self.last_known_player_state=='PLAYING':
          self.check_for_next_in_queue()
        if mc.status.player_state=='PLAYING':
          self.inhibit_screensaver()
        else:
          self.restore_screensaver()
        self.last_known_player_state = mc.status.player_state
        def f():
          self.update_media_button_states()
          self.update_status()
        GLib.idle_add(f)
      elif self.transcoder and not self.transcoder.done:
        def f():
          self.update_status()
        GLib.idle_add(f)
      if self.last_known_current_time != mc.status.current_time:
        self.last_known_current_time = mc.status.current_time
        self.last_time_current_time = time.time()
      if not seeking and mc.status.player_state=='PLAYING':
        GLib.idle_add(lambda: self.scrubber_adj.set_value(mc.status.current_time + time.time() - self.last_time_current_time))

  def init_casts(self, widget=None, device=None):
    self.cast_store.clear()
    self.cast_store.append([None, "Searching local network - please wait..."])
    self.cast_combo.set_active(0)
    threading.Thread(target=self.load_casts, kwargs={'device':device}).start()
    
  def inhibit_screensaver(self):
    if not self.saver_interface or self.inhibit_screensaver_cookie: return
    self.inhibit_screensaver_cookie = self.saver_interface.Inhibit("Gnomecast", "Player is playing...")
    print('disabled screensaver')

  def restore_screensaver(self):
    if self.saver_interface and self.inhibit_screensaver_cookie:
      self.saver_interface.UnInhibit(self.inhibit_screensaver_cookie)
      self.inhibit_screensaver_cookie = None
      print('restored screensaver')

  def load_casts(self, device=None):
    chromecasts = pychromecast.get_chromecasts()
    def f():
      self.cast_store.clear()
      self.cast_store.append([None, "Select a cast device..."])
      self.cast_store.append([-1, 'Add a non-local Chromecast...'])
      for cc in chromecasts:
        friendly_name = cc.device.friendly_name
        if cc.cast_type!='cast':
          friendly_name = '%s (%s)' % (friendly_name, cc.cast_type)
        self.cast_store.append([cc, friendly_name])
      if device:
        found = False
        for i, cc in enumerate(chromecasts):
          if device == cc.device.friendly_name:
            self.cast_combo.set_active(i+1)
            found = True
        if not found:
          self.cast_combo.set_active(0)
          dialog = Gtk.MessageDialog(self.win, 0, Gtk.MessageType.ERROR, Gtk.ButtonsType.CLOSE, "Chromecast Not Found")
          dialog.format_secondary_text("The Chromecast '%s' wasn't found." % device)
          dialog.run()
          dialog.destroy()
      else:
        self.cast_combo.set_active(2 if len(chromecasts) == 1 else 0)
    GLib.idle_add(f)
  
  def update_media_button_states(self):
    mc = self.cast.media_controller if self.cast else None
    self.play_button.set_sensitive(bool(self.transcoder and self.cast and mc.status.player_state in ('BUFFERING','PLAYING','PAUSED','IDLE','UNKNOWN') and self.fn))
    self.volume_button.set_sensitive(bool(self.cast))
    self.stop_button.set_sensitive(bool(self.transcoder and self.cast and mc.status.player_state in ('BUFFERING','PLAYING','PAUSED')))
    self.rewind_button.set_sensitive(bool(self.transcoder and self.cast and mc.status.player_state in ('BUFFERING','PLAYING','PAUSED')))
    self.forward_button.set_sensitive(bool(self.transcoder and self.cast and mc.status.player_state in ('BUFFERING','PLAYING','PAUSED')))
    self.play_button.set_image(Gtk.Image(stock=Gtk.STOCK_MEDIA_PAUSE) if self.cast and mc.status.player_state=='PLAYING' else Gtk.Image(stock=Gtk.STOCK_MEDIA_PLAY))
    if self.transcoder and self.duration:
      self.scrubber_adj.set_upper(self.duration)
      self.scrubber.set_sensitive(True)
    else:
      self.scrubber.set_sensitive(False)
    self.update_button_visible()


  def build_gui(self):
    self.win = win = Gtk.ApplicationWindow(title='Gnomecast v%s' % __version__)
    win.set_border_width(0)
    win.set_icon(self.get_logo_pixbuf(color='#000000'))
    enforce_target = Gtk.TargetEntry.new('text/plain', Gtk.TargetFlags(4), 129)
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
    refresh_button = Gtk.Button(None, image=Gtk.Image(stock=Gtk.STOCK_REFRESH))
    refresh_button.connect("clicked", self.init_casts)
    hbox.pack_start(refresh_button, False, False, 0)

    win.add(vbox_outer)
    
    # list of queued files
    self.files_store = Gtk.ListStore(str, str, int, str, str, int, str, object) # name, path, duration, duration_str, thumbnail_fn, transcode_progress, status_icon, transcoder
    self.files_store.connect("row-inserted", self.update_button_visible)
    self.files_store.connect("row-deleted", self.update_button_visible)
    self.files_view = Gtk.TreeView(self.files_store)
    self.files_view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
    self.files_view.set_headers_visible(False)
    self.files_view.set_rules_hint(True)
    column = Gtk.TreeViewColumn("Name", Gtk.CellRendererText(), text=0)
    column.set_expand(True)
    self.files_view.append_column(column)
    self.file_view_column_renderer = r = Gtk.CellRendererText()
    r.props.xalign = 1.0
    self.files_view.append_column(Gtk.TreeViewColumn("Duration", r, text=3))
    self.files_view_progress_column = column_progress = Gtk.TreeViewColumn("Progress", Gtk.CellRendererProgress(), value=5)
    self.files_view.append_column(column_progress)

    column_pixbuf = Gtk.TreeViewColumn("Playing", Gtk.CellRendererPixbuf(), icon_name=6)
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

    self.btn_vbox = btn_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    hbox.pack_start(btn_vbox, True, True, 0)
    self.file_button = Gtk.Button(None, image=Gtk.Image(stock=Gtk.STOCK_ADD))
    self.file_button.set_tooltip_text('Add one or more audio or video files...')
    self.file_button.set_always_show_image(True)
    self.file_button.connect("clicked", self.on_file_clicked)
    btn_vbox.pack_start(self.file_button, True, True, 0)
    self.remove_button = Gtk.Button(None, image=Gtk.Image(stock=Gtk.STOCK_REMOVE))
    self.remove_button.set_tooltip_text('Overwrite original file with transcoded version.')
    self.remove_button.connect("clicked", self.remove_files)
    self.remove_button.set_sensitive(False)
    btn_vbox.pack_start(self.remove_button, False, False, 0)

    self.file_detail_row = hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    vbox.pack_start(hbox, False, False, 0)
    self.subtitle_store = subtitle_store = Gtk.ListStore(str, int, str)
    subtitle_store.append(["No subtitles.", -1, None])
    subtitle_store.append(["Add subtitle file...", -2, None])
    self.subtitle_combo = Gtk.ComboBox.new_with_model(subtitle_store)
    self.subtitle_combo.connect("changed", self.on_subtitle_combo_changed)
    self.subtitle_combo.set_entry_text_column(0)
    renderer_text = Gtk.CellRendererText()
    self.subtitle_combo.pack_start(renderer_text, True)
    self.subtitle_combo.add_attribute(renderer_text, "text", 0)
    self.subtitle_combo.set_active(0)
    hbox.pack_start(self.subtitle_combo, True, True, 0)
    self.save_button = Gtk.Button(None, image=Gtk.Image(stock=Gtk.STOCK_SAVE))
    self.save_button.set_tooltip_text('Overwrite original file with transcoded version.')
    self.save_button.connect("clicked", self.save_transcoded_file)
    hbox.pack_start(self.save_button, False, False, 0)

    # force transcode button
    self.transcode_button = Gtk.MenuButton()
    self.transcode_button.set_tooltip_text("Force transcode (if your Chromecast won't play a file)...")
    menumodel = Gio.Menu()
    menumodel.append("Transcode Audio Only (fast)", 'win.transcode-audio')
    menumodel.append("Transcode Audio and Video (slow)", "win.transcode-all")
    self.transcode_button.set_menu_model(menumodel)
    self.transcode_button.set_image(Gtk.Image(stock=Gtk.STOCK_CONVERT))
    action = Gio.SimpleAction.new("transcode-audio", None)
    action.connect("activate", lambda a,b: self.force_transcode(audio=True, video=False))
    self.win.add_action(action)
    action = Gio.SimpleAction.new("transcode-all", None)
    action.connect("activate", lambda a,b: self.force_transcode(audio=True, video=True))
    self.win.add_action(action)
    hbox.pack_start(self.transcode_button, False, False, 0)
    
    self.scrubber_adj = Gtk.Adjustment(0, 0, 100, 15, 60, 0)
    self.scrubber = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.scrubber_adj)
    self.scrubber.set_digits(0)
    def f(scale, s):
      notes = [self.humanize_seconds(s)]
      return ''.join(notes)
    self.scrubber.connect("format-value", f)
    self.scrubber.connect("change-value", self.scrubber_move_started)
    self.scrubber.connect("change-value", self.scrubber_moved)
    self.scrubber.set_sensitive(False)
    vbox.pack_start(self.scrubber, False, False, 0)

    hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
    self.rewind_button = Gtk.Button(None, image=Gtk.Image(stock=Gtk.STOCK_MEDIA_REWIND))
    self.rewind_button.connect("clicked", self.rewind_clicked)
    self.rewind_button.set_sensitive(False)
    self.rewind_button.set_relief(Gtk.ReliefStyle.NONE)
    hbox.pack_start(self.rewind_button, True, False, 0)
    self.play_button = Gtk.Button(None, image=Gtk.Image(stock=Gtk.STOCK_MEDIA_PLAY))
    self.play_button.connect("clicked", self.play_clicked)
    self.play_button.set_sensitive(False)
    self.play_button.set_relief(Gtk.ReliefStyle.NONE)
    hbox.pack_start(self.play_button, True, False, 0)
    self.forward_button = Gtk.Button(None, image=Gtk.Image(stock=Gtk.STOCK_MEDIA_FORWARD))
    self.forward_button.connect("clicked", self.forward_clicked)
    self.forward_button.set_sensitive(False)
    self.forward_button.set_relief(Gtk.ReliefStyle.NONE)
    hbox.pack_start(self.forward_button, True, False, 0)
    self.stop_button = Gtk.Button(None, image=Gtk.Image(stock=Gtk.STOCK_MEDIA_STOP))
    self.stop_button.connect("clicked", self.stop_clicked)
    self.stop_button.set_sensitive(False)
    self.stop_button.set_relief(Gtk.ReliefStyle.NONE)
    hbox.pack_start(self.stop_button, True, False, 0)
    self.volume_button = Gtk.VolumeButton()
    self.volume_button.set_value(1)
    self.volume_button.connect("value-changed", self.volume_moved)
    self.volume_button.set_sensitive(False)
    hbox.pack_start(self.volume_button, True, False, 0)
    vbox.pack_start(hbox, False, False, 0)
    
    cast_combo.connect("changed", self.on_cast_combo_changed)

    win.connect("delete-event", self.quit)
    win.connect("key_press_event", self.on_key_press)
    win.show_all()

    self.update_button_visible()

    win.resize(1,1)

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self.quit)


  def on_drag_data_received(self, widget, drag_context, x,y, data,info, time):
    fn = data.get_text()
    if fn.startswith('file://'):
      fn = urllib.parse.unquote(fn[len('file://'):]).strip()
      self.queue_files([fn])
    
  def force_transcode(self, audio=True, video=True):
    for row in self.files_store:
      if row[1]!=self.fn: continue
      transcoder = row[7]
      transcoder.destroy()
      self.transcoder = Transcoder(self.cast, self.fn, lambda did_transcode=None: GLib.idle_add(self.update_status, did_transcode), None, force_audio=audio, force_video=video)
      row[7] = self.transcoder
  
  def update_button_visible(self, x=None, y=None, z=None):
    print('update_button_visible')
    count = len(self.files_store)
    self.scrolled_window.set_visible(count)
    self.remove_button.set_visible(count)
    self.file_button.set_label('' if count else '  Add one or more audio or video files...')
    self.file_button.get_child().set_padding(1,0,2,0) # w/ an empty label the + icon isn't quite centered
    self.hbox.set_child_packing(self.btn_vbox, not count, not count, 0, Gtk.PackType.START)
    self.save_button.set_visible(bool(self.transcoder and self.transcoder.show_save_button))
    self.file_detail_row.set_visible(bool(self.fn and self.cast))

  def scrubber_move_started(self, scale, scroll_type, seconds):
    print('scrubber_move_started', seconds)
    self.seeking = True
  
  def on_files_view_selection_changed(self, selection):
    model, treeiter = selection.get_selected_rows()
    self.remove_button.set_sensitive(bool(treeiter))
   
  def remove_files(self, w):
    store, paths = self.files_view.get_selection().get_selected_rows()
    for path in reversed(paths):
      print('remove', path)
      iterx = store.get_iter(path)
      transcoder = store.get_value(iterx, 7)
      if transcoder:
        transcoder.destroy()
      fn = store.get_value(iterx, 1)
      store.remove(iterx)
      if self.fn == fn:
        self.unselect_file()
      
        
  def on_files_view_row_activated(self, widget, row, col):
    model = widget.get_model()
    print('double-clicked', model[row][:])
    fn = model[row][1]
    self.unselect_file()
    self.fn = fn
    self.transcoder = model[row][7]
    self.duration = model[row][2]
    thumbnail_fn = model[row][4]
    if thumbnail_fn and os.path.isfile(thumbnail_fn):
      self.thumbnail_image.set_from_file(thumbnail_fn)
    if self.cast:
      self.cast.media_controller.stop()
    def f():
      self.win.resize(1,1)
      self.scrubber_adj.set_value(0)
      for row in self.files_store:
        if self.fn == row[1]:
          row[6] = 'video-x-generic'
        else:
          row[6] = None
      self.update_button_visible()
      self.update_media_button_states()
    GLib.idle_add(f)


    return True
          
  def queue_files(self, files):
    existing_files = set([row[1] for row in self.files_store])
    files = [f for f in files if f not in existing_files]
    for fn in files:
      display = os.path.basename(fn)
      MAX_LEN = 40
      if len(display) > MAX_LEN:
        display = display[:MAX_LEN-10] + '...' + display[-10:]
      self.files_store.append([display, fn, None, '...', None, None, None, None])
      threading.Thread(target=self.get_info, args=[fn]).start()
    def gen_thumbnails():
      for fn in files:
        self.gen_thumbnail(fn)
    threading.Thread(target=gen_thumbnails).start()
    self.scrolled_window.set_visible(True)
    if len(files) and self.fn is None:
      self.select_file(files[0])
    path = Gtk.TreePath().new_first()
    _1, _2, width, height = self.files_view_progress_column.cell_get_size()
    height += self.file_view_column_renderer.get_padding().ypad*2
    height += 2 # measured - row lines?
    self.scrolled_window.set_min_content_height(height*min(len(self.files_store),6))
    
  
  @throttle(seconds=1)
  def volume_moved(self, button, volume):
    if self.last_known_volume_level != volume:
      self.last_known_volume_level = volume
      self.cast.set_volume(volume)
      print('setting volume', volume)
      
  def save_transcoded_file(self, x):
    print('save_transcoded_file')
    if not self.transcoder or not self.transcoder.transcode:
      return
    fn = self.transcoder.fn
    display_name = os.path.basename(self.fn)
    path = os.path.dirname(self.fn)
    display_name = os.path.splitext(display_name)[0]+'.mp4'
    new_fn = os.path.join(path, display_name)
    print(fn, '=>', new_fn)
    os.rename(fn, new_fn)
    os.remove(self.fn)
    self.transcoder.source_fn = new_fn
    self.transcoder.transcode = False
    self.transcoder.show_save_button = False
    self.fn = new_fn
    def f():
      self.update_button_visible()
      self.update_status()
    GLib.idle_add(f)

  @throttle()
  def scrubber_moved(self, scale, scroll_type, seconds):
    print('scrubber_moved', seconds)
    self.seeking = True
    self.cast.media_controller.seek(seconds)

  def humanize_seconds(self, s):
    s = int(s)
    hours = s // (60*60)
    minutes = (s // 60) % 60
    seconds = s % 60
    if hours:
      return '%ih %im %is' % (hours, minutes, seconds)
    if minutes:
      return '%im %is' % (minutes, seconds)
    else:
      return '%is' % (seconds)
    

  def stop_clicked(self, widget):
    if not self.cast: return
    self.cast.media_controller.stop()
    
  def get_logo_pixbuf(self, width=200, color=None):
    svg = LOGO_SVG
    if color:
      svg = svg.replace('#aaaaaa', color)
    f = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(svg.encode()))
    preserve_aspect_ratio = True
    pixbuf = GdkPixbuf.Pixbuf.new_from_stream(f, None)
    return pixbuf

  
  def quit(self, a=0, b=0):
    for row in self.files_store:
      transcoder = row[7]
      if transcoder:
        transcoder.destroy()
    if self.cast:
      self.cast.media_controller.stop()
    self.restore_screensaver()
    for row in self.files_store:
      if row[4] and os.path.isfile(row[4]):
        os.remove(row[4])
    Gtk.main_quit()

  def forward_clicked(self, widget):
    self.seek_delta(30)
    
  def rewind_clicked(self, widget):
    self.seek_delta(-10)
    
  def seek_delta(self, delta):
    seconds = self.cast.media_controller.status.current_time + time.time() - self.last_time_current_time + delta
    self.last_time_current_time = time.time()
    self.cast.media_controller.status.current_time = seconds
    self.scrubber_adj.set_value(seconds)
    self.seeking = True
    self.cast.media_controller.seek(seconds)
    
  def play_clicked(self, widget):
    if not self.cast:
      print('no cast selected')
      return
    cast = self.cast
    mc = cast.media_controller
    
    print('mc.status.player_state', mc.status.player_state, self.fn, hash(self.fn))
    if mc.status.player_state in ('IDLE','UNKNOWN') or self.last_fn_played != self.fn:
      self.last_fn_played = self.fn
      cast.wait()
      mc = cast.media_controller
      kwargs = {}
      if self.subtitles:
        kwargs['subtitles'] = 'http://%s:%s/subtitles.vtt' % (self.ip, self.port)
      current_time = self.scrubber_adj.get_value()
      if current_time:
        kwargs['current_time'] = current_time
      ext = self.fn.split('.')[-1]
      ext = ''.join(ch for ch in ext if ch.isalnum()).lower()
      mc.play_media('http://%s:%s/media/%s.%s' % (self.ip, self.port, hash(self.fn), ext), 'audio/%s'%ext if ext in AUDIO_EXTS else 'video/mp4', **kwargs)
      print(cast.status)
      print(mc.status)
      self.prep_next_transcode()
    elif mc.status.player_state=='PLAYING':
      mc.pause()
    elif mc.status.player_state=='PAUSED':
      mc.play()

  def on_file_clicked(self, widget):
      dialog = Gtk.FileChooserDialog("Please choose an audio or video file...", self.win,
          Gtk.FileChooserAction.OPEN,
          (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
           Gtk.STOCK_OPEN, Gtk.ResponseType.OK))
      dialog.set_select_multiple(True)

      downloads_dir = os.path.expanduser('~/Downloads')
      if os.path.isdir(downloads_dir):
        dialog.set_current_folder(downloads_dir)

      filter_py = Gtk.FileFilter()
      filter_py.set_name("Videos")
      filter_py.add_mime_type("video/*")
      filter_py.add_mime_type("audio/*")
      dialog.add_filter(filter_py)
        
      response = dialog.run()
      if response == Gtk.ResponseType.OK:
          print("Open clicked")
          print("File selected:", dialog.get_filenames())
          self.queue_files(dialog.get_filenames())
          #self.select_file(dialog.get_filename())
      elif response == Gtk.ResponseType.CANCEL:
          print("Cancel clicked")

      dialog.destroy()
      
  def on_new_subtitle_clicked(self):
      dialog = Gtk.FileChooserDialog("Please choose a subtitle file...", self.win,
          Gtk.FileChooserAction.OPEN,
          (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
           Gtk.STOCK_OPEN, Gtk.ResponseType.OK))

      if self.fn:
        dialog.set_current_folder(os.path.dirname(self.fn))
      
      filter_py = Gtk.FileFilter()
      filter_py.set_name("Subtitles")
      filter_py.add_pattern("*.srt")
      filter_py.add_pattern("*.vtt")
      dialog.add_filter(filter_py)
        
      response = dialog.run()
      if response == Gtk.ResponseType.OK:
          print("Open clicked")
          print("File selected: " + dialog.get_filename())
          self.select_subtitles_file(dialog.get_filename())
      elif response == Gtk.ResponseType.CANCEL:
          print("Cancel clicked")
          self.subtitle_combo.set_active(0)

      dialog.destroy()
      
  def select_subtitles_file(self, fn):
    if not os.path.isfile(fn):
      def f():
        dialog = Gtk.MessageDialog(self.win, 0, Gtk.MessageType.ERROR, Gtk.ButtonsType.CLOSE, "File Not Found")
        dialog.format_secondary_text("Could not find subtitles file: %s" % fn)
        dialog.run()
        dialog.destroy()
      GLib.idle_add(f)
      return
    fn = os.path.abspath(fn)
    ext = fn.split('.')[-1]
    display_name = os.path.basename(fn)
    if ext=='vtt':
      with open(fn) as f:
        self.subtitles = f.read()
    else:
      with open(fn,'rb') as f:
        caps = f.read()
        try: caps = caps.decode()
        except UnicodeDecodeError: caps = caps.decode('latin-1')
      if caps.startswith('\ufeff'): # BOM
        caps = caps[1:]
      converter = pycaption.CaptionConverter()
      converter.read(caps, pycaption.detect_format(caps)())
      self.subtitles = converter.write(pycaption.WebVTTWriter())
    pos = len(self.subtitle_store)
    self.subtitle_store.append([display_name, pos-2, self.subtitles])
    self.subtitle_combo.set_active(pos)
    
  def unselect_file(self):
    self.thumbnail_image.set_from_pixbuf(self.get_logo_pixbuf())
    self.fn = None
    self.subtitle_store.clear()
    self.subtitle_store.append(["No subtitles.", -1, None])
    self.subtitle_combo.set_active(0)
    self.transcoder = None
    self.duration = None
    if self.cast:
      self.cast.media_controller.stop()
    def f():
      self.scrubber_adj.set_value(0)
      for row in self.files_store:
          row[6] = None
      self.win.resize(1,1)
      self.update_button_visible()
    GLib.idle_add(f)
  
  def select_file(self, fn):
    self.unselect_file()
    if not os.path.isfile(fn):
      def f():
        dialog = Gtk.MessageDialog(self.win, 0, Gtk.MessageType.ERROR, Gtk.ButtonsType.CLOSE, "File Not Found")
        dialog.format_secondary_text("Could not find media file: %s" % fn)
        dialog.run()
        dialog.destroy()
      GLib.idle_add(f)
      return
    fn = os.path.abspath(fn)
    self.thumbnail_image.set_from_pixbuf(self.get_logo_pixbuf())
    self.fn = fn
    self.subtitle_store.clear()
    self.subtitle_store.append(["Checking for subtitles...", -1, None])
    self.subtitle_store.append(["Add subtitle file...", -2, None])
    self.subtitle_combo.set_active(0)
    if self.cast:
      self.cast.media_controller.stop()
    def f():
      self.scrubber_adj.set_value(0)
      for row in self.files_store:
        thumbnail_fn = row[4]
        if self.fn == row[1]:
          if thumbnail_fn:
            self.thumbnail_image.set_from_file(thumbnail_fn)
            self.win.resize(1,1)
          row[6] = 'video-x-generic'
          self.duration = row[2]
        else:
          row[6] = None
      threading.Thread(target=self.update_transcoders).start()
      threading.Thread(target=self.update_subtitles).start()
      self.update_button_visible()
      self.update_media_button_states()
    GLib.idle_add(f)
  
  def update_transcoders(self):
    if self.cast and self.fn:
      transcoder = None
      for row in self.files_store:
        if row[1]!=self.fn: continue
        transcoder = row[7]
        if not transcoder or self.cast != transcoder.cast or self.fn != transcoder.source_fn:
          self.transcoder = Transcoder(self.cast, self.fn, lambda did_transcode=None: GLib.idle_add(self.update_status, did_transcode), transcoder)
          row[7] = self.transcoder
      if self.autoplay:
        self.autoplay = False
        self.play_clicked(None)
    if not self.cast:
      for row in self.files_store:
        transcoder = row[7]
        if transcoder:
          transcoder.destroy()
          row[7] = None
    GLib.idle_add(self.update_media_button_states)
  
  def check_for_next_in_queue(self):
    next = False
    for row in self.files_store:
      fn = row[1]
      if next:
        print('check_for_next_in_queue', fn)
        self.autoplay = True
        self.select_file(fn)
        next = False
      if self.cast and self.fn and self.fn == fn:
        next = True
  
  def prep_next_transcode(self):
    transcode_next = False
    for row in self.files_store:
      fn = row[1]
      transcoder = row[7]
      if transcode_next and not transcoder:
        print('prep_next_transcode', fn)
        transcoder = Transcoder(self.cast, fn, lambda did_transcode=None: GLib.idle_add(self.update_status, did_transcode), transcoder)
        row[7] = transcoder
        transcode_next = False
      if self.cast and self.fn and self.fn == fn and transcoder and transcoder.done:
        transcode_next = True
        
  def gen_thumbnail(self, fn):
    container = fn.lower().split(".")[-1]
    thumbnail_fn = None
    if container in ('aac','mp3','wav'):
      cmd = ['ffmpeg', '-i', fn, '-f', 'ffmetadata', '-']
    else:
      thumbnail_fn = tempfile.mkstemp(suffix='.jpg', prefix='gnomecast_thumbnail_')[1]
      os.remove(thumbnail_fn)
      cmd = ['ffmpeg', '-y', '-i', fn, '-f', 'mjpeg', '-vframes', '1', '-ss', '27', '-vf', 'scale=600:-1', thumbnail_fn]
    self.ffmpeg_desc = output = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
    if os.path.isfile(thumbnail_fn):
      for row in self.files_store:
        if row[1]==fn:
          row[4] = thumbnail_fn
    def f():
      if self.fn == fn and thumbnail_fn:
        self.thumbnail_image.set_from_file(thumbnail_fn)
        self.win.resize(1,1)
      self.update_status()
    GLib.idle_add(f)

  def get_info(self, fn):
    cmd = ['ffprobe', '-i', fn]
    output = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
    for line in output.decode().split('\n'):
      line = line.strip()
      if line.startswith('Duration:'):
        duration = parse_ffmpeg_time(line.split()[1].strip(','))
        if fn == self.fn:
          self.duration = duration
        for row in self.files_store:
          if row[1]==fn:
            row[2] = duration
            row[3] = self.humanize_seconds(duration)

  def update_subtitles(self):
    subtitle_ids = []
    cmd = ['ffprobe', '-i', self.fn]
    output = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
    for line in output.decode().split('\n'):
      line = line.strip()
      if line.startswith('Stream') and 'Subtitle' in line:
        id = line.split()[1].strip('#').replace(':','.')
        id = id[:id.index('(')]
        subtitle_ids.append(id)
    print('subtitle_ids', subtitle_ids)
    new_subtitles = []
    for subtitle_id in subtitle_ids:
      srt_fn = tempfile.mkstemp(suffix='.srt', prefix='gnomecast_subtitles_')[1]
      output = subprocess.check_output(['ffmpeg', '-y', '-i', self.fn, '-vn', '-an', '-codec:s:%s' % subtitle_id, 'srt', srt_fn], stderr=subprocess.STDOUT)
      with open(srt_fn) as f:
        caps = f.read()
      #print('caps', caps)
      converter = pycaption.CaptionConverter()
      converter.read(caps, pycaption.detect_format(caps)())
      subtitles = converter.write(pycaption.WebVTTWriter())
      new_subtitles.append((subtitle_id, subtitles))
      os.remove(srt_fn)
    def f():
      self.subtitle_store.clear()
      self.subtitle_store.append(["No subtitles.", -1, None])
      self.subtitle_store.append(["Add subtitle file...", -2, None])
      self.subtitle_combo.set_active(0)
      pos = len(self.subtitle_store)
      for id, subs in new_subtitles:
        self.subtitle_store.append([id, pos-2, subs])
        pos += 1
    GLib.idle_add(f)
    ext = self.fn.split('.')[-1]
    sexts = ['vtt', 'srt']
    for sext in sexts:
      if os.path.isfile(self.fn[:-len(ext)] + sext):
        self.select_subtitles_file(self.fn[:-len(ext)] + sext) 
        break

  def on_key_press(self, widget, event, user_data=None):
    key = Gdk.keyval_name(event.keyval)
    ctrl = (event.state & Gdk.ModifierType.CONTROL_MASK)
    if key=='q' and ctrl:
      self.quit()
      return True
    return False
    
  def select_cast(self, cast):
    self.cast = cast
    if cast:
#      cast.media_controller.app_id = 'FF0F6B72'
      self.last_known_volume_level = cast.media_controller.status.volume_level
      self.volume_button.set_value(cast.media_controller.status.volume_level)
    self.last_known_player_state = None
    self.update_media_button_states()
    threading.Thread(target=self.update_transcoders).start()
    

  def get_nonlocal_cast(self):
    dialogWindow = Gtk.MessageDialog(self.win,
                          Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
                          Gtk.MessageType.QUESTION,
                          Gtk.ButtonsType.OK_CANCEL,
                          '\nPlease specify the IP address or hostname of a Chromecast device:')

    dialogWindow.set_title('Add a non-local Chromecast')

    dialogBox = dialogWindow.get_content_area()
    userEntry = Gtk.Entry()
#    userEntry.set_size_request(250,0)
    dialogBox.pack_end(userEntry, False, False, 0)

    dialogWindow.show_all()
    response = dialogWindow.run()
    text = userEntry.get_text() 
    dialogWindow.destroy()
    if (response == Gtk.ResponseType.OK) and (text != ''):
      print(text)
      try:
        cast = pychromecast.Chromecast(text)
        self.cast_store.append([cast, text])
        self.cast_combo.set_active(len(self.cast_store)-1)
      except pychromecast.error.ChromecastConnectionError:
        dialog = Gtk.MessageDialog(self.win, 0, Gtk.MessageType.ERROR, Gtk.ButtonsType.CLOSE, "Chromecast Not Found")
        dialog.format_secondary_text("The Chromecast '%s' wasn't found." % text)
        dialog.run()
        dialog.destroy()
        
  def on_cast_combo_changed(self, combo):
      tree_iter = combo.get_active_iter()
      if tree_iter is not None:
          model = combo.get_model()
          cast, name = model[tree_iter][:2]
          if cast==-1:
            self.get_nonlocal_cast()
          else:
            print(cast)
            self.select_cast(cast)
      else:
          entry = combo.get_child()

  def on_subtitle_combo_changed(self, combo):
      tree_iter = combo.get_active_iter()
      if tree_iter is not None:
          model = combo.get_model()
          text, position, subs = model[tree_iter]
          print(text, position, subs)
          if position==-1: self.subtitles = None
          elif position==-2: self.on_new_subtitle_clicked()
          else:
            self.subtitles = subs
            mc = self.cast.media_controller if self.cast else None
            if mc and mc.status.player_state in ('BUFFERING','PLAYING','PAUSED'):
              self.stop_clicked(None)
              self.cast.wait()
              def f(): self.play_clicked(None)
              threading.Timer(1, lambda: GLib.idle_add(f)).start()
      else:
          entry = combo.get_child()

def parse_ffmpeg_time(time_s):
  hours, minutes, seconds = (float(s) for s in time_s.split(':'))
  return hours*60*60 + minutes*60 + seconds
  
  
# this is embedded here because i gave up trying to get pip to handle a non-python file
LOGO_SVG = '''<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<!-- Created with Inkscape (http://www.inkscape.org/) -->

<svg
   xmlns:dc="http://purl.org/dc/elements/1.1/"
   xmlns:cc="http://creativecommons.org/ns#"
   xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
   xmlns:svg="http://www.w3.org/2000/svg"
   xmlns="http://www.w3.org/2000/svg"
   xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"
   xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
   version="1.0"
   width="400"
   height="340"
   id="svg1903"
   sodipodi:docname="logo2.svg"
   inkscape:version="0.92.1 r15371">
  <metadata
     id="metadata18">
    <rdf:RDF>
      <cc:Work
         rdf:about="">
        <dc:format>image/svg+xml</dc:format>
        <dc:type
           rdf:resource="http://purl.org/dc/dcmitype/StillImage" />
        <dc:title></dc:title>
      </cc:Work>
    </rdf:RDF>
  </metadata>
  <sodipodi:namedview
     pagecolor="#ffffff"
     bordercolor="#666666"
     borderopacity="1"
     objecttolerance="10"
     gridtolerance="10"
     guidetolerance="10"
     inkscape:pageopacity="0"
     inkscape:pageshadow="2"
     inkscape:window-width="1227"
     inkscape:window-height="926"
     id="namedview16"
     showgrid="false"
     fit-margin-top="100"
     fit-margin-left="100"
     fit-margin-right="100"
     fit-margin-bottom="100"
     inkscape:zoom="0.48337649"
     inkscape:cx="272.94153"
     inkscape:cy="365.88387"
     inkscape:window-x="1506"
     inkscape:window-y="557"
     inkscape:window-maximized="0"
     inkscape:current-layer="svg1903" />
  <defs
     id="defs1905" />
  <g
     transform="matrix(0.32014843,0,0,0.32014843,123.60933,34.982023)"
     id="layer1">
    <g
       transform="translate(925.8326,120.8762)"
       id="g3963">
      <g
         transform="matrix(2.914897,0,0,2.914897,-717.5904,128.5015)"
         style="fill:#aaaaaa;fill-opacity:1;fill-rule:nonzero;stroke:none;stroke-miterlimit:4"
         id="g3771">
        <g
           style="fill:#aaaaaa;fill-opacity:1"
           id="g3773">
          <path
             d="M 86.068,0 C 61.466,0 56.851,35.041 70.691,35.041 84.529,35.041 110.671,0 86.068,0 Z"
             style="fill:#aaaaaa;fill-opacity:1"
             id="path3775"
             inkscape:connector-curvature="0" />
          <path
             d="M 45.217,30.699 C 52.586,31.149 60.671,2.577 46.821,4.374 32.976,6.171 37.845,30.249 45.217,30.699 Z"
             style="fill:#aaaaaa;fill-opacity:1"
             id="path3777"
             inkscape:connector-curvature="0" />
          <path
             d="M 11.445,48.453 C 16.686,46.146 12.12,23.581 3.208,29.735 -5.7,35.89 6.204,50.759 11.445,48.453 Z"
             style="fill:#aaaaaa;fill-opacity:1"
             id="path3779"
             inkscape:connector-curvature="0" />
          <path
             d="M 26.212,36.642 C 32.451,35.37 32.793,9.778 21.667,14.369 10.539,18.961 19.978,37.916 26.212,36.642 Z"
             style="fill:#aaaaaa;fill-opacity:1"
             id="path3781"
             inkscape:connector-curvature="0" />
          <path
             d="m 58.791,93.913 c 1.107,8.454 -6.202,12.629 -13.36,7.179 C 22.644,83.743 83.16,75.088 79.171,51.386 75.86,31.712 15.495,37.769 8.621,68.553 3.968,89.374 27.774,118.26 52.614,118.26 c 12.22,0 26.315,-11.034 28.952,-25.012 C 83.58,82.589 57.867,86.86 58.791,93.913 Z"
             style="fill:#aaaaaa;fill-opacity:1"
             id="path3783"
             inkscape:connector-curvature="0" />
        </g>
      </g>
    </g>
  </g>
  <g
     id="cast"
     transform="matrix(0.53475936,0,0,0.53475936,50,20)">
    <path
       d="M 510,51 H 51 C 22.95,51 0,73.95 0,102 v 76.5 H 51 V 102 H 510 V 459 H 331.5 v 51 H 510 c 28.05,0 51,-22.95 51,-51 V 102 C 561,73.95 538.05,51 510,51 Z M 0,433.5 V 510 H 76.5 C 76.5,466.65 43.35,433.5 0,433.5 Z m 0,-102 v 51 c 71.4,0 127.5,56.1 127.5,127.5 h 51 C 178.5,410.55 99.45,331.5 0,331.5 Z m 0,-102 v 51 c 127.5,0 229.5,102 229.5,229.5 h 51 C 280.5,354.45 155.55,229.5 0,229.5 Z"
       id="path12"
       style="fill:#aaaaaa;fill-opacity:1"
       inkscape:connector-curvature="0" />
  </g>
</svg>'''

def arg_parse(args, kw_synonyms, f, usage):
  kw = None
  f_args = []
  f_kwargs = {}
  for arg in args:
    if arg.startswith('-'):
      if kw:
        f_kwargs[kw] = True
      arg = arg.lstrip('-')
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
    msg = str(e).split('()',1)[1].strip()
    print('ERROR:', msg)
    print(usage)
    sys.exit(1)

USAGE = '''
python gnomecast.py [<media_filename>] [-d|--device <chromecast_name>] [-s|--subtitles <subtitles_filename>]
'''.strip()

def main():
  caster = Gnomecast()
  arg_parse(sys.argv[1:], {'s':'subtitles', 'd':'device'}, caster.run, USAGE)

if DEPS_MET and __name__=='__main__':
  main()
  

