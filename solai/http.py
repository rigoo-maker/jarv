"""Minimal stdlib HTTP/JSON client with retries and honest error messages.

Honors HTTPS_PROXY (urllib does this natively). Distinguishes three failures
that people constantly confuse:

  BlockedError   — the network/proxy refused to reach the host at all. Nothing
                   about your code is wrong; run it somewhere with egress.
  RateLimited    — 429. Back off; the free endpoints are strict.
  HttpError      — the API answered and said no.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

UA = "solai/0.1 (+https://github.com/rigoo-maker/jarv)"


class HttpError(RuntimeError):
    def __init__(self, status, url, body=""):
        self.status, self.url, self.body = status, url, (body or "")[:400]
        super().__init__(f"HTTP {status} for {url}: {self.body}")


class RateLimited(HttpError):
    pass


class BlockedError(RuntimeError):
    """Host unreachable — DNS, firewall, or an egress policy said no."""


def _request(url, *, method="GET", payload=None, headers=None, timeout=20.0):
    body = None
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode()
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        text = ""
        try:
            text = e.read().decode()
        except Exception:
            pass
        if e.code == 429:
            raise RateLimited(429, url, text)
        raise HttpError(e.code, url, text)
    except urllib.error.URLError as e:
        raise BlockedError(f"cannot reach {url} ({e.reason}). If this is a proxy "
                           f"403/CONNECT failure the host is blocked by network "
                           f"policy — run where it is reachable.")
    except json.JSONDecodeError as e:
        raise HttpError(200, url, f"non-JSON response: {e}")


def get_json(url, *, params=None, headers=None, timeout=20.0, retries=3):
    if params:
        from urllib.parse import urlencode
        url = f"{url}?{urlencode(params, doseq=True)}"
    return _retry(lambda: _request(url, headers=headers, timeout=timeout), retries, url)


def post_json(url, payload, *, headers=None, timeout=20.0, retries=3):
    return _retry(lambda: _request(url, method="POST", payload=payload,
                                   headers=headers, timeout=timeout), retries, url)


def _retry(fn, retries, url):
    delay, last = 1.0, None
    for attempt in range(max(1, retries)):
        try:
            return fn()
        except RateLimited as e:
            last = e
        except HttpError as e:
            if e.status < 500:
                raise            # 4xx is our fault; retrying repeats the mistake
            last = e
        except BlockedError:
            raise                # never retry a policy denial
        if attempt < retries - 1:
            time.sleep(delay)
            delay *= 2
    raise last
