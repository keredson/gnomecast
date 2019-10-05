import subprocess
import os
import tempfile
import threading
import time
import re

AUDIO_EXTS = ('aac', 'mp3', 'wav')


def parse_ffmpeg_time(time_s):
  """
  Converts ffmpeg's time string to number of seconds
  :param time_s:
  :return: number of seconds
  """
  hours, minutes, seconds = (float(s) for s in time_s.split(':'))
  return hours * 60 * 60 + minutes * 60 + seconds


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
      output = subprocess.check_output(['ffmpeg', '-i', fn, '-f', 'ffmetadata', '-'],
                                       stderr=subprocess.STDOUT).decode().split('\n')
      container = fn.lower().split(".")[-1]
      video_codec = None
      audio_codec = None
      for line in output:
        line = line.strip()
        if line.startswith('Stream') and 'Video' in line and not video_codec:
          video_codec = line.split()[3]
        elif line.startswith('Stream') and 'Audio' in line and not audio_codec:
          audio_codec = line.split()[3]
      print('Transcoder', fn, container, video_codec, audio_codec)
      transcode_container = container not in ('mp4', 'aac', 'mp3', 'wav')
      self.transcode_video = force_video or not self.can_play_video_codec(video_codec)
      self.transcode_audio = force_audio or container not in AUDIO_EXTS or not self.can_play_audio_codec(audio_codec)
      self.transcode = transcode_container or self.transcode_video or self.transcode_audio
      self.trans_fn = None

    self.progress_bytes = 0
    self.progress_seconds = 0
    self.done_callback = done_callback
    print('transcode, transcode_video, transcode_audio', self.transcode, self.transcode_video, self.transcode_audio)
    if self.transcode:
      self.done = False
      dir = '/var/tmp' if os.path.isdir('/var/tmp') else None
      self.trans_fn = tempfile.mkstemp(suffix='.mp4', prefix='gnomecast_', dir=dir)[1]
      os.remove(self.trans_fn)
      # flags = '''-c:v libx264 -profile:v high -level 5 -crf 18 -maxrate 10M -bufsize 16M -pix_fmt yuv420p -x264opts bframes=3:cabac=1 -movflags faststart -c:a libfdk_aac -b:a 320k''' # -vf "scale=iw*sar:ih, scale='if(gt(iw,ih),min(1920,iw),-1)':'if(gt(iw,ih),-1,min(1080,ih))'"
      args = ['ffmpeg', '-i', self.source_fn, '-c:v', 'h264' if self.transcode_video else 'copy', '-c:a',
              'ac3' if self.transcode_audio else 'copy'] + (['-b:a', '256k'] if self.transcode_audio else []) + [
               self.trans_fn]  # '-movflags', 'faststart'
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
    if self.cast.device.model_name == 'Chromecast Ultra' or self.cast.device.manufacturer == 'VIZIO':
      return video_codec in ('h264', 'h265', 'hevc')
    else:
      return video_codec in ('h264',)

  def can_play_audio_codec(self, codec):
    return codec in ('aac', 'mp3', 'ac3', 'eac3')

  def wait_for_byte(self, offset, buffer=128 * 1024 * 1024):
    if self.done: return
    if self.source_fn.lower().split(".")[-1] == 'mp4':
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
          d = dict([x for x in items if len(x) == 2])
          print(d)
          self.progress_bytes = int(d.get('size', '0kb')[:-2]) * 1024
          self.progress_seconds = parse_ffmpeg_time(d.get('time', '00:00:00'))
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
