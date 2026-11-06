"""The hosts that the service may reach, enforced in the process.

The service declares the network `*` (open egress), for two reasons that nodo gives in
docs/NETWORKS.md ("Hostname tags"):

- The LLM endpoint is the user's choice (`OPENMONTAGE_LLM_BASE_URL`), so it cannot be a
  hostname tag fixed at pack time.
- A hostname tag grants addresses, not name resolution: nodo serves no DNS in the guest,
  so an HTTP client that calls getaddrinfo() fails before it connects. Archive.org also
  sends its files from hosts that change for each item (ia800000.us.archive.org).

So the service narrows the egress itself, as celaut-basics/yt-transcript does: every name
lookup of the Python process goes through `guarded_getaddrinfo`, which refuses a host that
is not in the list below. Python is the only program of the image that opens a socket:
ffmpeg, Piper and the Remotion renderer (Chromium) read local files only.
"""

import ipaddress
import logging
import os
import socket
from urllib.parse import urlsplit

log = logging.getLogger("egress")

# The free media sources of the default pipeline (OpenMontage tools/video/stock_sources):
# Wikimedia Commons, NASA Image and Video Library, Internet Archive. No key.
FREE_HOSTS = {
    "commons.wikimedia.org", "upload.wikimedia.org", "thumb.wikimedia.org",
    "images-api.nasa.gov", "images-assets.nasa.gov",
    "archive.org",
}
FREE_SUFFIXES = (".archive.org",)  # ia800000.us.archive.org, the file servers of an item
# Sources with a free key (OpenMontage tools/video/stock_sources/pexels.py and
# pixabay_video.py): allowed only when the key is set.
KEY_HOSTS = {
    "PEXELS_API_KEY": {"api.pexels.com", "videos.pexels.com", "images.pexels.com"},
    "PIXABAY_API_KEY": {"pixabay.com", "cdn.pixabay.com"},
}

_original = socket.getaddrinfo
_allowed = set()
_suffixes = ()


def llm_host(env=None):
    env = os.environ if env is None else env
    url = (env.get("OPENMONTAGE_LLM_BASE_URL") or "").strip()
    return (urlsplit(url).hostname or "").lower() if url else ""


def allowed_hosts(env=None):
    env = os.environ if env is None else env
    hosts = set(FREE_HOSTS)
    for key, names in KEY_HOSTS.items():
        if env.get(key):
            hosts |= names
    llm = llm_host(env)
    if llm:
        hosts.add(llm)
    return hosts


def is_allowed(host):
    host = (host or "").lower().rstrip(".")
    if not host:
        return False
    if host in _allowed:
        return True
    try:
        ipaddress.ip_address(host)
        return False  # an address is allowed only if it is in the list (the LLM host)
    except ValueError:
        return host.endswith(_suffixes)


def guarded_getaddrinfo(host, *args, **kwargs):
    name = host.decode() if isinstance(host, bytes) else host
    if name is not None and not is_allowed(name):
        log.warning("egress refused: %s", name)
        raise socket.gaierror(socket.EAI_NONAME, "egress to %s is not allowed by the service" % name)
    return _original(host, *args, **kwargs)


def install(env=None):
    """Guard the name lookups of this process. Return the allowed hosts."""
    global _allowed, _suffixes
    _allowed = allowed_hosts(env)
    _suffixes = FREE_SUFFIXES
    socket.getaddrinfo = guarded_getaddrinfo
    return sorted(_allowed) + ["*" + s for s in _suffixes]
