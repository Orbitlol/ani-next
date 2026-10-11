"""aninext - a zero-dependency anime / movie / series scraper & media kit for Termux.

Termux runs Android's Bionic libc, so PyPI's manylinux wheels are unusable:
anything with a C extension has to be compiled locally (and often fails).
aninext is therefore written against the standard library only, and treats
ffmpeg / aria2c / yt-dlp as optional accelerators.

Quick start
-----------
    from aninext import api

    for result in api.search("frieren"):
        print(result.title, result.score)

    title = api.info("frieren")
    print(title.synopsis)

    streams = api.streams("frieren", episode=1)
    print(streams[0].url)
"""

__version__ = "1.0.0"
__appname__ = "aninext"
__author__ = "ani-next"
__license__ = "MIT"
__url__ = "https://github.com/Orbitlol/ani-next"

__all__ = ["__version__", "__appname__", "api"]
