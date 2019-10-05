#url = 'http://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4'
url = 'https://bitdash-a.akamaihd.net/content/MI201109210084_1/m3u8s/f08e80da-bf1d-4e3d-8899-f0f6155f6efa.m3u8'
url = 'http://qthttp.apple.com.edgesuite.net/1010qwoeiuryfg/sl.m3u8'
url = 'http://qthttp.apple.com.edgesuite.net/1010qwoeiuryfg/0640_vod.m3u8'

import time
import pychromecast

print('getting devices...')
chromecasts = pychromecast.get_chromecasts()
[cc.device.friendly_name for cc in chromecasts]
cast = next(cc for cc in chromecasts if cc.device.friendly_name == "Family Room TV")
# Start worker thread and wait for cast device to be ready
cast.wait()
print(cast.device)
print(cast.status)
mc = cast.media_controller
mc.play_media(url, 'video/mp4')
mc.block_until_active()
print(mc.status)
#mc.pause()
while True:
  time.sleep(5)
  print(cast.status)

#mc.play()
