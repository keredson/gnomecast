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

      with open(self.trans_fn,'w') as f:
        f.write('#EXTM3U\n')
        f.write('#EXT-X-PLAYLIST-TYPE:EVENT\n')
        f.write('#EXT-X-TARGETDURATION:10\n')
        f.write('#EXT-X-ALLOW-CACHE:YES\n')
        f.write('#EXT-X-VERSION:3\n')
        # chromecast hangs w/o at least one file
        f.write('#EXTINF:10.000,\noutput_0000.ts\n')

      print('self.trans_dir', self.trans_dir)
    else:
      self.done = True
  
  def start(self):
    if self.done: return
    # flags = '''-c:v libx264 -profile:v high -level 5 -crf 18 -maxrate 10M -bufsize 16M -pix_fmt yuv420p -x264opts bframes=3:cabac=1 -movflags faststart -c:a libfdk_aac -b:a 320k''' # -vf "scale=iw*sar:ih, scale='if(gt(iw,ih),min(1920,iw),-1)':'if(gt(iw,ih),-1,min(1080,ih))'"
    args = ['ffmpeg', '-i', self.source_fn, '-c:v', 'h264' if self.transcode_video else 'copy', '-c:a',
            'aac' if self.transcode_audio else 'copy'] + (['-b:a', '256k'] if self.transcode_audio else []) + [
             '-f', 'segment', '-segment_time', '10', '%s/output_%%04d.ts' % self.trans_dir]  # '-movflags', 'faststart'
    # args = ['ffmpeg', '-i', self.source_fn, '-c:v', 'libvpx', '-b:v', '5M', '-c:a', 'libvorbis', '-deadline','realtime', self.trans_fn]
    # args = ['ffmpeg', '-i', self.source_fn] + flags.split() + [self.trans_fn]
    print(args)
    self.p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    t = threading.Thread(target=self.monitor)
    t.daemon = True
    t.start()
    time.sleep(.5)

  @property
  def fn(self):
    return self.trans_fn if self.transcode else self.source_fn

  def can_play_video_codec(self, video_codec):
    if self.cast.device.model_name == 'Chromecast Ultra' or self.cast.device.manufacturer == 'VIZIO':
      return video_codec in ('h264', 'h265', 'hevc')
    else:
      return video_codec in ('h264',)

  def monitor(self):
    print('monitoring...')
    seen_files = set(['output_0000.ts'])
    while True:
      if self.p.poll() != None:
        break
      files = [fn for fn in os.listdir(self.trans_dir) if fn.endswith('.ts')]
      new_files = set(files) - seen_files
      seen_files.update(new_files)
      new_files = sorted(new_files)
      print('available:', new_files)
      with open(self.trans_fn,'a') as f:
        for fn in new_files:
          f.write('#EXTINF:10.000,\n%s\n' % fn)
      time.sleep(2)
    self.p.stdout.close()
    self.done = True

  def destroy(self):
    # self.cast.media_controller.stop()
    if self.p and self.p.poll() is None:
      self.p.terminate()
    shutil.rmtree(self.trans_dir)
    if self.trans_fn and os.path.isfile(self.trans_fn):
      os.remove(self.trans_fn)
