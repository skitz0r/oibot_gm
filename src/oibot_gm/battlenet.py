"""What every Blizzard client of the process shares (armory link, spec §5.3): ONE OAuth token (client credentials from
.env) and ONE rate limiter (≤ MAX_PER_SECOND requests in any second, across every guild and every client — the recipe
importer in importers/blizzard.py and the armory in armory.py). Both are thread-safe: callers run in worker threads.
The token value is never logged, put in an exception, or returned by anything but Token.get."""
from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from typing import Callable, Mapping

MAX_PER_SECOND = 10
TOKEN_URL = "https://oauth.battle.net/token"
ID_VAR, SECRET_VAR = "BLIZZARD_CLIENT_ID", "BLIZZARD_CLIENT_SECRET"


class TokenRefused(RuntimeError):
    """The credentials are missing or Blizzard refused them: retrying will not help."""


class TokenUnavailable(RuntimeError):
    """The token service could not be reached or answered badly: try again later."""


def credentials_set(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return bool(env.get(ID_VAR)) and bool(env.get(SECRET_VAR))


def _post_token(cid: str, secret: str) -> dict:
    """The client-credentials request (network). Errors carry the status only, never a header."""
    req = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode())
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{cid}:{secret}".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            raise TokenRefused(f"the token request was refused ({e.code})") from None
        raise TokenUnavailable(f"the token service answered {e.code}") from None
    except (OSError, ValueError) as e:
        raise TokenUnavailable(f"the token service could not be reached ({type(e).__name__})") from None


class Token:
    """The process's access token, renewed a minute before it expires. `post(cid, secret) -> {access_token, expires_in}`
    and `env` stand in for the network and .env in tests; the credentials are read at each renewal (load_dotenv may
    run after import)."""

    def __init__(self, post: Callable[[str, str], dict] | None = None, clock: Callable[[], float] = time.time,
                 env: Mapping[str, str] | None = None):
        self._post, self._clock, self._env = post or _post_token, clock, env
        self._lock = threading.Lock()
        self._value: str | None = None
        self._expires = 0.0
        self._margin = 60.0

    def get(self) -> str:
        with self._lock:
            if self._value and self._clock() < self._expires - self._margin:
                return self._value
            env = os.environ if self._env is None else self._env
            if not credentials_set(env):
                raise TokenRefused(f"{ID_VAR} / {SECRET_VAR} missing from .env")
            d = self._post(env[ID_VAR], env[SECRET_VAR])
            if not isinstance(d, dict) or not isinstance(d.get("access_token"), str):
                raise TokenUnavailable("the token service answered without a token")
            try:
                ttl = int(d.get("expires_in", 3600))
            except (TypeError, ValueError):
                ttl = 3600
            # renewed a minute early, or at half its life when it lives two minutes or less
            self._value, self._expires, self._margin = d["access_token"], self._clock() + ttl, min(60.0, ttl / 2)
            return self._value

    def drop(self, value: str) -> None:
        """Forget `value` (an answer 401'd it); a newer token taken meanwhile by another thread is kept."""
        with self._lock:
            if self._value == value:
                self._value, self._expires = None, 0.0


class Limiter:
    """At most `per_second` acquisitions in any one-second window (a sliding window: a burst of `per_second` goes at
    once, the next waits for the oldest to age out — stricter than a token bucket, which allows twice the rate across a
    window boundary). Blocking; waiting callers queue on the lock."""

    def __init__(self, per_second: int = MAX_PER_SECOND, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.per_second, self._clock, self._sleep = per_second, clock, sleep
        self._lock = threading.Lock()
        self._times: deque[float] = deque()

    def acquire(self) -> None:
        with self._lock:
            while True:
                now = self._clock()
                while self._times and now - self._times[0] >= 1.0:
                    self._times.popleft()
                if len(self._times) < self.per_second:
                    self._times.append(now)
                    return
                self._sleep(self._times[0] + 1.0 - now)


TOKEN = Token()  # THE token of the process
LIMITER = Limiter(MAX_PER_SECOND)  # THE limiter of the process
