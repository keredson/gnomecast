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

Running on Windows
------------------

Follow the PyGObject installation guide for Windows from https://pygobject.readthedocs.io/en/latest/getting_started.html or below: 

Covered in the guide above:

1. Install Msys2 and run `mingw64.exe` from the installation folder to install Mingw64 Python and its dependencies. **Note**: The Mingw64 terminal will only be used for installing the prerequisites and not for actually running the program.
1. Run the following commands: 
   1. Run `pacman -Suy` once and it will prompt to exit and run it again. Close terminal, re-open and run `pacman -Suy` once more.
   1. Execute `pacman -S mingw-w64-x86_64-gtk3 mingw-w64-x86_64-python3 mingw-w64-x86_64-python3-gobject`

Not included in link but needed in order to be able to start Gnomecast: 

1. Execute following: `pacman -S mingw-w64-x86_64-python3-pip mingw-w64-x86_64-python3-lxml` to get `pip` and `lxml` needed for one of the dependencies. 
1. Install Python package dependencies: `pip install bottle pychromecast pycaption paste`

After this, for running Gnomecast you will need to **start the Mingw64 Python installation from a regular Windows commandline**, not from the Mingw64 or MSYS terminal (*I believe it has to do with the way paths are handled between UNIX and Windows, but not sure*). 
So just run the Mingw64 Python installation from Windows, for example, `D:\msys64\mingw64\bin\python.exe gnomecast.py` 
Also, make sure you have ffmpeg available on your regular Windows environment!

Make sure to properly set up the Mingw64 Python environment correctly in Visual Studio as well, if wishing to use it for debugging purposes.


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
