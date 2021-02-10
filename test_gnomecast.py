import unittest
import gnomecast


class FakeCast:
  def __init__(self, cast_type=None, manufacturer=None, model_name=None):
    self.device = FakeDevice(cast_type=cast_type, manufacturer=manufacturer, model_name=model_name)

class FakeDevice:
  def __init__(self, **kwargs):
    self.__dict__.update(kwargs)

def error_callback_test(msg):
  print(msg)


class TestGnomecast(unittest.TestCase):

  def test_1(self):
    fmd = gnomecast.FileMetadata(
      'pCU2GE07KW4.mkv',
      _ffmpeg_output = '''
ffprobe version 4.1.4 Copyright (c) 2007-2019 the FFmpeg developers
  built with gcc 9 (GCC)
  configuration: --prefix=/usr --bindir=/usr/bin --datadir=/usr/share/ffmpeg --docdir=/usr/share/doc/ffmpeg --incdir=/usr/include/ffmpeg --libdir=/usr/lib64 --mandir=/usr/share/man --arch=x86_64 --optflags='-O2 -g -pipe -Wall -Werror=format-security -Wp,-D_FORTIFY_SOURCE=2 -Wp,-D_GLIBCXX_ASSERTIONS -fexceptions -fstack-protector-strong -grecord-gcc-switches -specs=/usr/lib/rpm/redhat/redhat-hardened-cc1 -specs=/usr/lib/rpm/redhat/redhat-annobin-cc1 -m64 -mtune=generic -fasynchronous-unwind-tables -fstack-clash-protection -fcf-protection' --extra-ldflags='-Wl,-z,relro -Wl,--as-needed -Wl,-z,now -specs=/usr/lib/rpm/redhat/redhat-hardened-ld ' --extra-cflags=' ' --enable-libopencore-amrnb --enable-libopencore-amrwb --enable-libvo-amrwbenc --enable-version3 --enable-bzlib --disable-crystalhd --enable-fontconfig --enable-frei0r --enable-gcrypt --enable-gnutls --enable-ladspa --enable-libaom --enable-libass --enable-libbluray --enable-libcdio --enable-libdrm --enable-libjack --enable-libfreetype --enable-libfribidi --enable-libgsm --enable-libmp3lame --enable-nvenc --enable-openal --enable-opencl --enable-opengl --enable-libopenjpeg --enable-libopus --enable-libpulse --enable-librsvg --enable-libsoxr --enable-libspeex --enable-libssh --enable-libtheora --enable-libvorbis --enable-libv4l2 --enable-libvidstab --enable-libvmaf --enable-libvpx --enable-libx264 --enable-libx265 --enable-libxvid --enable-libzvbi --enable-avfilter --enable-avresample --enable-postproc --enable-pthreads --disable-static --enable-shared --enable-gpl --disable-debug --disable-stripping --shlibdir=/usr/lib64 --enable-libmfx --enable-runtime-cpudetect
  libavutil      56. 22.100 / 56. 22.100
  libavcodec     58. 35.100 / 58. 35.100
  libavformat    58. 20.100 / 58. 20.100
  libavdevice    58.  5.100 / 58.  5.100
  libavfilter     7. 40.101 /  7. 40.101
  libavresample   4.  0.  0 /  4.  0.  0
  libswscale      5.  3.100 /  5.  3.100
  libswresample   3.  3.100 /  3.  3.100
  libpostproc    55.  3.100 / 55.  3.100
Input #0, matroska,webm, from 'pCU2GE07KW4.mkv':
  Metadata:
    COMPATIBLE_BRANDS: iso6avc1mp41
    MAJOR_BRAND     : dash
    MINOR_VERSION   : 0
    ENCODER         : Lavf58.20.100
  Duration: 00:41:45.28, start: -0.007000, bitrate: 1303 kb/s
    Stream #0:0: Video: h264 (High), yuv420p(tv, bt709, progressive), 1920x1080 [SAR 1:1 DAR 16:9], 29.97 fps, 29.97 tbr, 1k tbn, 59.94 tbc (default)
    Metadata:
      HANDLER_NAME    : ISO Media file produced by Google Inc.
      DURATION        : 00:41:45.269000000
    Stream #0:1(eng): Audio: opus, 48000 Hz, stereo, fltp (default)
    Metadata:
      DURATION        : 00:41:45.281000000
    ''')
    fmd.wait()

    self.assertEqual(fmd.container, 'mkv')
    self.assertEqual(len(fmd.video_streams), 1)
    self.assertEqual(fmd.video_streams[0].index, '0:0')
    self.assertEqual(fmd.video_streams[0].codec, 'h264')
    self.assertEqual(len(fmd.audio_streams), 1)
    self.assertEqual(fmd.audio_streams[0].index, '0:1')
    self.assertEqual(fmd.audio_streams[0].codec, 'opus')
    self.assertEqual(fmd.audio_streams[0].title, 'eng')
    self.assertEqual(fmd.audio_streams[0].channels, 2)
    self.assertEqual(len(fmd.subtitles), 0)

    cast = FakeCast(cast_type='video', manufacturer='Unknown manufacturer', model_name='Chromecast')
    transcoder = gnomecast.Transcoder(cast, fmd, fmd.video_streams[0], fmd.audio_streams[0], None, error_callback_test, fake=True)

    self.assertCountEqual(transcoder.transcode_cmd[:-1], ['ffmpeg', '-i', 'pCU2GE07KW4.mkv', '-map', '0:0', '-map', '0:1', '-c:v', 'copy', '-c:a', 'mp3', '-b:a', '256k'])

  def test_2(self):
    fmd = gnomecast.FileMetadata(
      'Godzilla - King of the Monsters (2019) (2160p BluRay x265 10bit HDR Tigole).mkv',
      _ffmpeg_output = '''
Input #0, matroska,webm, from 'Godzilla - King of the Monsters (2019) (2160p BluRay x265 10bit HDR Tigole).mkv':
  Metadata:
    title           : Godzilla: King of the Monsters
    encoder         : libebml v1.3.7 + libmatroska v1.5.0
    creation_time   : 2019-08-23T10:49:27.000000Z
  Duration: 02:11:43.19, start: 0.000000, bitrate: 18112 kb/s
    Chapter #0:0: start 0.000000, end 653.694708
    Metadata:
      title           : Chapter 01
    Chapter #0:1: start 653.694708, end 1086.585500
    Metadata:
      title           : Chapter 02
    Chapter #0:2: start 1086.585500, end 1827.325500
    Metadata:
      title           : Chapter 03
    Chapter #0:3: start 1827.325500, end 2718.632583
    Metadata:
      title           : Chapter 04
    Chapter #0:4: start 2718.632583, end 3250.830917
    Metadata:
      title           : Chapter 05
    Chapter #0:5: start 3250.830917, end 3941.646042
    Metadata:
      title           : Chapter 06
    Chapter #0:6: start 3941.646042, end 4171.292125
    Metadata:
      title           : Chapter 07
    Chapter #0:7: start 4171.292125, end 4771.308208
    Metadata:
      title           : Chapter 08
    Chapter #0:8: start 4771.308208, end 5406.067333
    Metadata:
      title           : Chapter 09
    Chapter #0:9: start 5406.067333, end 5916.785875
    Metadata:
      title           : Chapter 10
    Chapter #0:10: start 5916.785875, end 6598.508583
    Metadata:
      title           : Chapter 11
    Chapter #0:11: start 6598.508583, end 7168.536375
    Metadata:
      title           : Chapter 12
    Chapter #0:12: start 7168.536375, end 7903.104000
    Metadata:
      title           : Chapter 13
    Stream #0:0: Video: hevc (Main 10), yuv420p10le(tv, bt2020nc/bt2020/smpte2084), 3840x1600, SAR 1:1 DAR 12:5, 23.98 fps, 23.98 tbr, 1k tbn, 23.98 tbc (default)
    Metadata:
      BPS-eng         : 17115721
      DURATION-eng    : 02:11:43.104000000
      NUMBER_OF_FRAMES-eng: 189485
      NUMBER_OF_BYTES-eng: 16908415995
      _STATISTICS_WRITING_APP-eng: mkvmerge v32.0.0 ('Astral Progressions') 64-bit
      _STATISTICS_WRITING_DATE_UTC-eng: 2019-08-23 10:49:27
      _STATISTICS_TAGS-eng: BPS DURATION NUMBER_OF_FRAMES NUMBER_OF_BYTES
    Stream #0:1(eng): Audio: aac (LC), 48000 Hz, 7.1, fltp (default)
    Metadata:
      BPS-eng         : 901426
      DURATION-eng    : 02:11:43.104000000
      NUMBER_OF_FRAMES-eng: 370458
      NUMBER_OF_BYTES-eng: 890507991
      _STATISTICS_WRITING_APP-eng: mkvmerge v32.0.0 ('Astral Progressions') 64-bit
      _STATISTICS_WRITING_DATE_UTC-eng: 2019-08-23 10:49:27
      _STATISTICS_TAGS-eng: BPS DURATION NUMBER_OF_FRAMES NUMBER_OF_BYTES
    Stream #0:2(eng): Audio: aac (HE-AAC), 48000 Hz, stereo, fltp
    Metadata:
      title           : Commentary
      BPS-eng         : 65862
      DURATION-eng    : 02:11:43.147000000
      NUMBER_OF_FRAMES-eng: 185230
      NUMBER_OF_BYTES-eng: 65064850
      _STATISTICS_WRITING_APP-eng: mkvmerge v32.0.0 ('Astral Progressions') 64-bit
      _STATISTICS_WRITING_DATE_UTC-eng: 2019-08-23 10:49:27
      _STATISTICS_TAGS-eng: BPS DURATION NUMBER_OF_FRAMES NUMBER_OF_BYTES
    Stream #0:3(eng): Subtitle: dvd_subtitle, 1920x1080
    Metadata:
      BPS-eng         : 8966
      DURATION-eng    : 02:11:18.819000000
      NUMBER_OF_FRAMES-eng: 1661
      NUMBER_OF_BYTES-eng: 8830829
      _STATISTICS_WRITING_APP-eng: mkvmerge v32.0.0 ('Astral Progressions') 64-bit
      _STATISTICS_WRITING_DATE_UTC-eng: 2019-08-23 10:49:27
      _STATISTICS_TAGS-eng: BPS DURATION NUMBER_OF_FRAMES NUMBER_OF_BYTES
    Stream #0:4(ara): Subtitle: dvd_subtitle, 1920x1080
    Metadata:
      BPS-eng         : 4132
      DURATION-eng    : 02:10:49.414000000
      NUMBER_OF_FRAMES-eng: 1373
      NUMBER_OF_BYTES-eng: 4055035
      _STATISTICS_WRITING_APP-eng: mkvmerge v32.0.0 ('Astral Progressions') 64-bit
      _STATISTICS_WRITING_DATE_UTC-eng: 2019-08-23 10:49:27
      _STATISTICS_TAGS-eng: BPS DURATION NUMBER_OF_FRAMES NUMBER_OF_BYTES
    Stream #0:5(chi): Subtitle: dvd_subtitle, 1920x1080
    Metadata:
      BPS-eng         : 6291
      DURATION-eng    : 02:10:44.329000000
      NUMBER_OF_FRAMES-eng: 1246
      NUMBER_OF_BYTES-eng: 6169396
      _STATISTICS_WRITING_APP-eng: mkvmerge v32.0.0 ('Astral Progressions') 64-bit
      _STATISTICS_WRITING_DATE_UTC-eng: 2019-08-23 10:49:27
      _STATISTICS_TAGS-eng: BPS DURATION NUMBER_OF_FRAMES NUMBER_OF_BYTES
    Stream #0:6(fre): Subtitle: dvd_subtitle, 1920x1080
    Metadata:
      BPS-eng         : 6451
      DURATION-eng    : 02:10:49.539000000
      NUMBER_OF_FRAMES-eng: 1276
      NUMBER_OF_BYTES-eng: 6330309
      _STATISTICS_WRITING_APP-eng: mkvmerge v32.0.0 ('Astral Progressions') 64-bit
      _STATISTICS_WRITING_DATE_UTC-eng: 2019-08-23 10:49:27
      _STATISTICS_TAGS-eng: BPS DURATION NUMBER_OF_FRAMES NUMBER_OF_BYTES
    Stream #0:7(kor): Subtitle: dvd_subtitle, 1920x1080
    Metadata:
      BPS-eng         : 4593
      DURATION-eng    : 02:10:44.333000000
      NUMBER_OF_FRAMES-eng: 1359
      NUMBER_OF_BYTES-eng: 4504269
      _STATISTICS_WRITING_APP-eng: mkvmerge v32.0.0 ('Astral Progressions') 64-bit
      _STATISTICS_WRITING_DATE_UTC-eng: 2019-08-23 10:49:27
      _STATISTICS_TAGS-eng: BPS DURATION NUMBER_OF_FRAMES NUMBER_OF_BYTES
    Stream #0:8(spa): Subtitle: dvd_subtitle, 1920x1080
    Metadata:
      BPS-eng         : 7522
      DURATION-eng    : 02:10:44.409000000
      NUMBER_OF_FRAMES-eng: 1392
      NUMBER_OF_BYTES-eng: 7376389
      _STATISTICS_WRITING_APP-eng: mkvmerge v32.0.0 ('Astral Progressions') 64-bit
      _STATISTICS_WRITING_DATE_UTC-eng: 2019-08-23 10:49:27
      _STATISTICS_TAGS-eng: BPS DURATION NUMBER_OF_FRAMES NUMBER_OF_BYTES
    ''')
    fmd.wait()
    self.assertEqual(fmd.container, 'mkv')
    self.assertEqual(len(fmd.video_streams), 1)
    self.assertEqual(fmd.video_streams[0].index, '0:0')
    self.assertEqual(fmd.video_streams[0].codec, 'hevc')
    self.assertEqual(len(fmd.audio_streams), 2)
    self.assertEqual(fmd.audio_streams[0].index, '0:1')
    self.assertEqual(fmd.audio_streams[0].codec, 'aac')
    self.assertEqual(fmd.audio_streams[0].title, 'eng')
    self.assertEqual(fmd.audio_streams[0].channels, 8)
    self.assertEqual(fmd.audio_streams[1].index, '0:2')
    self.assertEqual(fmd.audio_streams[1].codec, 'aac')
    self.assertEqual(fmd.audio_streams[1].title, 'Commentary')
    self.assertEqual(fmd.audio_streams[1].channels, 2)
    self.assertEqual(len(fmd.subtitles), 6)
    self.assertEqual([s.title for s in fmd.subtitles], ['eng', 'ara', 'chi', 'fre', 'kor', 'spa'])
    
    print(fmd)

    cast = FakeCast(cast_type='video', manufacturer='Unknown manufacturer', model_name='Chromecast Ultra')

    transcoder = gnomecast.Transcoder(cast, fmd, fmd.video_streams[0], fmd.audio_streams[0], None, error_callback_test, fake=True)
    self.assertCountEqual(transcoder.transcode_cmd[:-1], ['ffmpeg', '-i', 'Godzilla - King of the Monsters (2019) (2160p BluRay x265 10bit HDR Tigole).mkv', '-map', '0:0', '-map', '0:1', '-c:v', 'copy', '-c:a', 'ac3', '-b:a', '256k'])

    transcoder = gnomecast.Transcoder(cast, fmd, fmd.video_streams[0], fmd.audio_streams[1], None, error_callback_test, fake=True)
    self.assertCountEqual(transcoder.transcode_cmd[:-1], ['ffmpeg', '-i', 'Godzilla - King of the Monsters (2019) (2160p BluRay x265 10bit HDR Tigole).mkv', '-map', '0:0', '-map', '0:2', '-c:v', 'copy', '-c:a', 'mp3', '-b:a', '256k'])

    cast = FakeCast(cast_type='video', manufacturer='Unknown manufacturer', model_name='Chromecast')
    transcoder = gnomecast.Transcoder(cast, fmd, fmd.video_streams[0], fmd.audio_streams[0], None, error_callback_test, fake=True)
    self.assertCountEqual(transcoder.transcode_cmd[:-1], ['ffmpeg', '-i', 'Godzilla - King of the Monsters (2019) (2160p BluRay x265 10bit HDR Tigole).mkv', '-map', '0:0', '-map', '0:1', '-c:v', 'h264', '-c:a', 'mp3', '-b:a', '256k'])

    cast = FakeCast(cast_type='video', manufacturer='VIZIO', model_name='P75-F1')
    transcoder = gnomecast.Transcoder(cast, fmd, fmd.video_streams[0], fmd.audio_streams[0], None, error_callback_test, fake=True)
    self.assertCountEqual(transcoder.transcode_cmd[:-1], ['ffmpeg', '-i', 'Godzilla - King of the Monsters (2019) (2160p BluRay x265 10bit HDR Tigole).mkv', '-map', '0:0', '-map', '0:1', '-c:v', 'copy', '-c:a', 'ac3', '-b:a', '256k'])

    cast = FakeCast(cast_type='video', manufacturer='UNK', model_name='UNK')
    transcoder = gnomecast.Transcoder(cast, fmd, fmd.video_streams[0], fmd.audio_streams[0], None, error_callback_test, fake=True)
    self.assertCountEqual(transcoder.transcode_cmd[:-1], ['ffmpeg', '-i', 'Godzilla - King of the Monsters (2019) (2160p BluRay x265 10bit HDR Tigole).mkv', '-map', '0:0', '-map', '0:1', '-c:v', 'copy', '-c:a', 'ac3', '-b:a', '256k'])


if __name__ == '__main__':
    unittest.main()
