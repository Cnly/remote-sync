"""Makes API requests without following redirects.

Retries HTTP 429 indefinitely. Retries HTTP 502, 503, and 504 on GET requests at most twice.
"""

from email.utils import parsedate_to_datetime
import math
import time

import requests

from .utils import progress


def retry_delay(headers, default):
    delays = []
    now = time.time()
    retry_after = headers.get("Retry-After", "")
    try:
        delays.append(float(retry_after))
    except ValueError:
        try:
            delays.append(parsedate_to_datetime(retry_after).timestamp() - now)
        except (ValueError, TypeError, OverflowError):
            pass
    # HetrixTools reports Unix reset times for both account and endpoint limits.
    # A limit with requests remaining must not delay an exhausted limit's retry.
    for scope in ("user", "endpoint"):
        try:
            if int(headers.get(f"ratelimit-remaining-{scope}", "0")) <= 0:
                delays.append(float(headers[f"ratelimit-reset-{scope}"]) - now)
        except (KeyError, ValueError):
            pass
    return math.ceil(max((delay for delay in delays if math.isfinite(delay) and delay >= 0), default=default))


class API:
    def __init__(self, base_url, token=None, *, session=None, interval=0, headers=None):
        self.base_url = base_url
        self.token = token
        self.headers = headers or {}
        self.session = session or requests.Session()
        self.interval = interval
        self.last_request = 0

    def request(self, method, path, *, bearer=True, expect_json=True, **kwargs):
        read_retries = 0
        while True:
            time.sleep(max(0, self.interval - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            headers = {"Accept": "application/json"} | self.headers
            if bearer and self.token is not None:
                # A token supplier can renew credentials after a long rate-limit wait.
                token = self.token() if callable(self.token) else self.token
                headers["Authorization"] = f"Bearer {token}"
            try:
                response = self.session.request(
                    method, self.base_url + path, headers=headers,
                    timeout=30, allow_redirects=False, **kwargs,
                )
            except requests.RequestException:
                # HetrixTools v2 embeds the token in the URL. Never print the
                # requests exception (or chain it into a traceback).
                raise RuntimeError("API connection failed; preview again before retrying.") from None
            if response.status_code in (429, 502, 503, 504):
                limited = response.status_code == 429
                delay = retry_delay(response.headers, 60 if limited else 2 ** read_retries)
                # An explicit 429 rejects the request. Other write failures may
                # have committed, so retry those only for reads.
                if limited or (method == "GET" and read_retries < 2 and delay <= 30):
                    reason = "rate limited" if limited else "temporarily unavailable"
                    operation = "read" if method == "GET" else "write"
                    progress(f"API {reason} (HTTP {response.status_code}); retrying {operation} in {delay} seconds.")
                    if not limited:
                        read_retries += 1
                    time.sleep(delay)
                    continue
            assert response.status_code != 409, "API returned HTTP 409 (conflict); preview again before retrying."
            assert 200 <= response.status_code < 300, (
                f"API returned HTTP {response.status_code}; check permissions/limits and preview again."
            )
            if not expect_json:
                return None
            try:
                result = response.json()
            except ValueError:
                raise RuntimeError("API returned invalid JSON; refusing to continue.") from None
            return result
