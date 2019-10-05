import contextlib, os, socket, subprocess, threading
import bottle
from paste import httpserver
from paste.translogger import TransLogger

base = 'http://qthttp.apple.com.edgesuite.net/1010qwoeiuryfg/'
m3u8 = '0640_vod.m3u8'

subprocess.call(['wget', '--directory-prefix=m3u8', '-nc', base + m3u8])

with open(os.path.join('m3u8',m3u8)) as f:
  for line in f.readlines()[:20]:
    line = line.strip()
    if line.startswith('#'): continue
    subprocess.call(['wget', '--directory-prefix=m3u8/0640', '-nc', base + line])
    

app = bottle.Bottle()


@app.get('/<fn:path>')
def video(fn):
  response = bottle.static_file(fn, root='m3u8')
  if 'Last-Modified' in response.headers:
    del response.headers['Last-Modified']
  response.headers['Access-Control-Allow-Origin'] = '*'
  response.headers['Access-Control-Allow-Methods'] = 'GET, HEAD'
  response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
  return response

ip = (([ip for ip in socket.gethostbyname_ex(socket.gethostname())[2] if not ip.startswith("127.")] or [[(s.connect(("8.8.8.8", 53)), s.getsockname()[0], s.close()) for s in [socket.socket(socket.AF_INET, socket.SOCK_DGRAM)]][0][1]]) + [None])[0]
with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
  s.bind(('0.0.0.0', 0))
  port = s.getsockname()[1]
handler = TransLogger(app, setup_console_handler=True)
def f():
  httpserver.serve(handler, host=ip, port=str(port), daemon_threads=True)
t = threading.Thread(target=f)
t.daemon = True
t.start()

url = 'http://%s:%d/0640_vod.m3u8' % (ip, port)

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
while True:
  time.sleep(5)
  print(cast.status)



