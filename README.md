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

* `1.7`: Drag and drop files into the main UI.
* `1.6`: Mutiple file / queuing support.

Install
-------
Please run:

```
$ sudo pip3 install gnomecast
```

If installing in a `mkvirtualenv` built virtual environment, make sure you include the `--system-site-packages` parameter to get the GTK bindings.

Run
---

After installing, log out and log back in.  It will be in your launcher:

![alt text](https://raw.githubusercontent.com/keredson/gnomecast/master/launcher.png)

You can also run it from the command line:

```
$ gnomecast
```

If you ran `pip3` without `sudo` when installing, and `$ gnomecast` doesn't work due to your local path setup, you can also run it as:

```
$ python3 -m gnomecast
```

*Please report bugs, including video files that don't work for you!*

My File Won't Play!!!
---------------------

Chromecasts are picky, and the built in media receiver doesn't give any feedback regarding why it won't play something.  (It just flashes and quits on the main TV.)  So while this program can detect and auto-transcode files using unsupported codecs, that doesn't cover everything.

Usually I've found re-encoding a file will appease the Chromecast file format gods.  See:

![image](https://user-images.githubusercontent.com/2049665/50061428-31270700-0155-11e9-9ff5-39075db0bcfd.png)

I recommend transcoding just the audio first, as this is enough to fix most files in my experience, and it's ~20x faster than transcoding the video.

If you think there's a bug beyond this, please open an issue (and link to the offending file if possible).


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

Running on Windows
------------------

In order to run on Windows you will need to install the PyGObject for Windows from here:  https://sourceforge.net/projects/pygobjectwin32/files/

First get Python 3.4.4 - later versions are not supported by PyGOobject for Windows.

Start the installation and point the installer to the Python 3.4 folder.

Select the following libraries to be installed: Dbus-GLib, GDA, Gdk-Pixbuf, GTK+, and GLib

After this, you should install gnomecast's dependencies and everything should be ready to go.
