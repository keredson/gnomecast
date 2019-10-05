import os, shutil, subprocess, tempfile, threading, time, re

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

  def __init__(self, cast, fn, force_audio=False, force_video=False):
    self.cast = cast
    self.source_fn = fn
    self.p = None

    output = subprocess.check_output(['ffmpeg', '-i', fn, '-f', 'ffmetadata', '-'], stderr=subprocess.STDOUT).decode().split('\n')
    container = fn.lower().split(".")[-1]
    video_codec = None
    transcode_audio = container not in AUDIO_EXTS
    for line in output:
      line = line.strip()
      if line.startswith('Stream') and 'Video' in line and not video_codec:
        video_codec = line.split()[3]
      elif line.startswith('Stream') and 'Audio' in line and (
        'aac (LC)' in line or 'aac (HE)' in line or 'mp3' in line):
        transcode_audio = False
    transcode_audio |= force_audio
    print('Transcoder', fn, container, video_codec, transcode_audio)
    transcode_container = container not in ('mp4', 'aac', 'mp3', 'wav')
    self.transcode_video = force_video or not self.can_play_video_codec(video_codec)
    self.transcode_audio = transcode_audio
    self.transcode = transcode_container or self.transcode_video or self.transcode_audio
    self.trans_fn = None

    print('transcode, transcode_video, transcode_audio', self.transcode, self.transcode_video, self.transcode_audio)
    if self.transcode:
      self.done = False
      self.trans_dir = tempfile.mkdtemp(prefix='gnomecast_')
      self.trans_fn = os.path.join(self.trans_dir, 'output.m3u8')

      with open(self.trans_fn,'w', encoding='utf-8') as f:
        f.write('#EXTM3U\n')
        f.write('#EXT-X-PLAYLIST-TYPE:EVENT\n')
        f.write('#EXT-X-TARGETDURATION:10\n')
        f.write('#EXT-X-ALLOW-CACHE:YES\n')
        f.write('#EXT-X-VERSION:3\n')

      print('self.trans_dir', self.trans_dir)
    else:
      self.done = True
  
  def start(self):
    if self.done: return
    self.transcode_audio = True
    t = threading.Thread(target=self.do_transcode)
    t.daemon = True
    t.start()
    time.sleep(5)

  @property
  def fn(self):
    return self.trans_fn if self.transcode else self.source_fn

  def can_play_video_codec(self, video_codec):
    if self.cast.device.model_name == 'Chromecast Ultra' or self.cast.device.manufacturer == 'VIZIO':
      return video_codec in ('h264', 'h265', 'hevc')
    else:
      return video_codec in ('h264',)

  def do_transcode(self):
    print('transcoding...')
    cmd = ['ffmpeg', '-i', self.source_fn, '-c:v', 'h264' if self.transcode_video else 'copy', '-c:a',
            'aac' if self.transcode_audio else 'copy'] + (['-b:a', '256k'] if self.transcode_audio else []) + [
             '-f', 'segment', '-segment_time', '10', '%s/output_%%04d.ts' % self.trans_dir]  # '-movflags', 'faststart'
    print(' '.join(['"%s"'%arg if ' ' in arg else arg for arg in cmd]))
    self.p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    currently_writing = None
    def done_writing(fn):
      print('transcode: done', fn)
      with open(self.trans_fn,'a', encoding='utf-8') as f:
        f.write('#EXTINF:10.000,\n%s\n' % fn)
    for s in iter(self.p.stdout.readline, ""):
        s = s.strip()
        print('transcode:', s)
        m = re.search(r"Opening '/tmp/gnomecast_\w+/(output_\d+.ts)' for writing", s)
        if m:
          if currently_writing:
            done_writing(currently_writing)
          currently_writing = m.group(1)
    if currently_writing:
      done_writing(currently_writing)
    self.p.stdout.close()
    return_code = self.p.wait()
    print('transcode done:', return_code)
    if return_code:
        raise subprocess.CalledProcessError(return_code, cmd)
    self.done = True

  def destroy(self):
    # self.cast.media_controller.stop()
    if self.p and self.p.poll() is None:
      self.p.terminate()
    if hasattr(self, 'trans_dir'):
      shutil.rmtree(self.trans_dir)
    if self.trans_fn and os.path.isfile(self.trans_fn):
      os.remove(self.trans_fn)
