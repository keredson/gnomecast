![alt text](https://raw.githubusercontent.com/keredson/gnomecast/master/screenshot.png)

Gnomecast ![logo](https://github.com/keredson/gnomecast/raw/master/icons/gnomecast_16.png)
=========

This is a native Linux GUI for casting local files to Chromecast devices.  It supports:

- Both audio and video files (anything `ffmpeg` can read)
- Realtime transcoding (only when needed)
- Subtitles (embedded and external SRT files)
- Fast scrubbing (waiting 20s for buffering to skip 30s ahead is wrong!)
- 4K videos on the Chromecast Ultra!

What's New
----------

* `1.9`: Multi video/audio stream support.
* `1.8`: 5.1/7.1 surround sound E/AC3 support.
* `1.7`: Drag and drop files into the main UI.
* `1.6`: Mutiple file / queuing support.

Install
-------

Please run:

```
$ sudo apt install ffmpeg python3-pip python3-gi
$ pip3 install gnomecast
```

If installing in a `mkvirtualenv` built virtual environment, make sure you include the `--system-site-packages` parameter to get the GTK bindings.

Fedora
~~~~~~

This section describes how to install this application on Fedora inside a virtual environment
without relying on ``python3-gobject`` system level package and ``--system-site-packages``
virtualenv flag.

1. Install OS level dependencies

```bash
sudo dnf install ffmpeg cairo-gobject-devel gobject-introspection-devel dbus-devel cairo-devel
```

2. Install Python dependencies

NOTE: ``dbus-python`` is an optional dependency.

```bash
pip3 install pygobject dbus-python
```

3. Install the application itself

```bash
pip3 install gnomecast
```

Run
---

After installing, log out and log back in.  It will be in your launcher:

![alt text](https://raw.githubusercontent.com/keredson/gnomecast/master/launcher.png)

You can also run it from the command line:

```
$ gnomecast
```

Or:

```
$ python3 -m gnomecast
```

You can also configure the port used for the HTTP server via the environment variable `GNOMECAST_HTTP_PORT`:

```
$ GNOMECAST_HTTP_PORT=8010 python3 -m gnomecast
```

*Please report bugs, including video files that don't work for you!*

Tests
-----

Run the tests from the commandline:
```
$ python3 test_gnomecast.py
```

My File Won't Play!
-------------------

Chromecasts are picky, and the built in media receiver doesn't give any feedback regarding why it won't play something.  (It just flashes and quits on the main TV.)  If your file won't play, please click the info button:

![image](https://user-images.githubusercontent.com/2049665/66446007-978b5780-e9fd-11e9-87cc-c01f07c67271.png)

And then the "Report File Doesn't Play" button:

![image](https://user-images.githubusercontent.com/2049665/66446040-b12c9f00-e9fd-11e9-8acf-b3bc0d28c971.png)

So I can fix it!

Thanks To...
------------

- https://github.com/balloob/pychromecast
- https://github.com/pbs/pycaption
- https://www.ffmpeg.org/

And everyone who made this project hit [HN's front page](https://news.ycombinator.com/item?id=16386173) and #2 on GitHub's trending list!  That's so awesome!!!

![alt text](https://raw.githubusercontent.com/keredson/gnomecast/master/trending.png)


Transcoding
-----------
Chromecasts only support a handful of media formats.  See: https://developers.google.com/cast/docs/media

So some amount of transcoding is necessary if your video files don't conform.  But we're smart about it.  If you have an `.mkv` file with `h264` video and `AAC` audio, we use `ffmpeg` to simply rewrite the container (to `.mp4`) without touching the underlying streams, which my XPS 13 can at around 100x realtime (it's fully IO bound).

Now if you have that same `.mkv` file with and `A3C` audio stream (which Chromecast doesn't support) we'll rewrite the container, copy the `h264` stream as is and only transcode the audio (at about 20x).

If neither your file's audio or video streams are supported, then it'll do a full transcode (at around 5x).

We write the entire transcoded file to your `/tmp` directory in order to make scrubbing fast and glitch-free, a good trade-off IMO.  Hopefully you're not running your drive at less than one video's worth of free space!

Subtitles
---------
Chromecast only supports a handful of subtitle formats, `.srt` not included.  But it does support [WebVTT](https://w3c.github.io/webvtt/).  So we extract whatever subtitles are in your video, convert them to WebVTT, and then reattach them to the video through Chomecast's API.
