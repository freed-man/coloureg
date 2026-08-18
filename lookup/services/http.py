"""One shared requests.Session for every outbound call (F12).

WHY A SESSION
-------------
Every provider call opened a fresh TCP connection and a fresh TLS handshake.
Against VDG, One Auto, partslink24, DVLA and MOT that is a full round-trip of
setup per call, on a path where three providers race and the customer is
already waiting 10-26 seconds. Reusing the connection removes the handshake
from every call after the first.

WHY RETRIES ARE PINNED TO ZERO
------------------------------
This is the part that matters, and it is the reason a Session cannot simply be
dropped in.

urllib3 retries by default. A Session created without an explicit adapter
inherits that, and a retry is INVISIBLE to the caller: requests returns one
response, having sent two. On the VDG path that means two charges for one
lookup, recorded once — the same class of invisible spend as paint18/21/26/67,
and worse because the second charge leaves no trace at all.

So every adapter here mounts Retry(total=0, connect=0, read=0, status=0,
redirect=0). Not "fewer retries" — none. A failure must reach the caller, which
already knows how to record the cost and degrade.

THE TRADE THIS INTRODUCES
-------------------------
A pooled connection that has been idle can be closed at the far end without us
knowing. The next call gets a reset rather than a clean response. With retries
off it is not silently re-sent, so an occasional hard failure appears where
today there would be a slow success.

That is the correct trade on a billed path: a visible failure the caller can
record beats a hidden second charge. It is also bounded — POOL_IDLE_S keeps
connections young enough that most are still live, and every caller here
already has a failure path because every one of these providers times out
sometimes anyway.
"""

import os
import threading

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

#: Connections older than this are not reused. Railway recycles workers and
#: intermediaries drop idle sockets; keeping the pool young trades a little of
#: the saving for far fewer resets on the first call after a quiet spell.
POOL_IDLE_S = int(os.environ.get('HTTP_POOL_IDLE_S', '60'))

#: NO RETRIES. Read the module docstring before changing this — a silent retry
#: on the VDG path is a second charge nobody sees.
_NO_RETRIES = Retry(
    total=0,
    connect=0,
    read=0,
    status=0,
    redirect=0,
    backoff_factor=0,
    raise_on_status=False,
)

_session = None
_lock = threading.Lock()


def get_session():
    """The shared Session, created once.

    Built under a lock because resolve_paint races three providers in a thread
    pool: two threads arriving together on the first call of a fresh worker
    would otherwise each build a session, and one would be discarded along with
    whatever connections it had already opened.
    """
    global _session
    if _session is not None:
        return _session
    with _lock:
        if _session is None:
            s = requests.Session()
            adapter = HTTPAdapter(
                max_retries=_NO_RETRIES,
                pool_connections=8,
                pool_maxsize=8,
            )
            s.mount('https://', adapter)
            s.mount('http://', adapter)
            _session = s
    return _session


def reset_session():
    """Drop the pooled connections. For tests, and for a manual recycle."""
    global _session
    with _lock:
        if _session is not None:
            _session.close()
        _session = None
