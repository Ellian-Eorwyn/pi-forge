"""HTTP for the media provider registry: spacing, bounded retries, honest refusals.

A port of the rules `lib/http-fetch.mjs` arrived at, kept in Python because the
providers are Python and skills do not import across skill boundaries.

Three of those rules are load-bearing here and none of them is obvious:

**A 429 must not trip the circuit breaker.** "Slow down" is not "go away".
MusicBrainz answers 429 to anyone exceeding one request a second, and treating
that as a refusal would take the only free music provider out of the run after
three albums. A 429 defers the host and is retried; 401/402/403 are decisions
about *us* and are what the breaker counts.

**Rate-limit headers are read, never guessed.** MusicBrainz reports
``x-ratelimit-remaining`` on every response. Recording what the service said is
the difference between backing off because we are actually near the limit and
backing off because we assumed a number from its documentation.

**A real User-Agent is not politeness, it is a requirement.** MusicBrainz refuses
a generic client outright, and Open Library degrades one. The contact address in
it is what lets an operator complain to a human instead of blocking the host.
"""

import email.utils
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_TIMEOUT = 20.0
DEFAULT_BREAKER_THRESHOLD = 3
MAX_ATTEMPTS = 3
BASE_BACKOFF = 1.0
MAX_BACKOFF = 20.0

# A refusal is a decision about the caller and counts toward the breaker. 429 is
# deliberately absent: see the module docstring.
REFUSAL_STATUSES = frozenset({401, 402, 403})
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

USER_AGENT = "pi-forge-vault-media/0.1 (+https://github.com/Ellian-Eorwyn/pi-forge)"


_SSL_CONTEXT = None
_SSL_SOURCE = None


def ssl_context():
    """A verifying SSL context, with the trust store located rather than assumed.

    A python.org framework build on macOS ships with ``openssl_cafile`` pointing
    at a bundle that only exists once someone has run its
    ``Install Certificates.command``. On a machine where nobody has, every HTTPS
    call fails with CERTIFICATE_VERIFY_FAILED — which reads like the provider
    being unreachable, and sent this skill chasing four "down" hosts that were
    all answering fine to curl.

    So: use the default trust store when it actually resolves to something, fall
    back to certifi's bundle when it does not, and let ``doctor`` report which
    one is in play. Verification is never disabled — an unverified fetch would
    turn a proxy or a captive portal into note content.
    """
    global _SSL_CONTEXT, _SSL_SOURCE
    if _SSL_CONTEXT is not None:
        return _SSL_CONTEXT

    override = os.environ.get("SSL_CERT_FILE")
    if override and Path(override).exists():
        _SSL_CONTEXT, _SSL_SOURCE = ssl.create_default_context(cafile=override), f"SSL_CERT_FILE={override}"
        return _SSL_CONTEXT

    paths = ssl.get_default_verify_paths()
    if (paths.cafile and Path(paths.cafile).exists()) or (paths.capath and Path(paths.capath).is_dir()):
        _SSL_CONTEXT, _SSL_SOURCE = ssl.create_default_context(), "system trust store"
        return _SSL_CONTEXT

    try:
        import certifi

        _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
        _SSL_SOURCE = f"certifi ({certifi.where()})"
    except ImportError:
        _SSL_CONTEXT, _SSL_SOURCE = ssl.create_default_context(), "system default (no bundle found; HTTPS will likely fail)"
    return _SSL_CONTEXT


def ssl_source():
    """Which trust store ``ssl_context`` settled on. For ``doctor`` output."""
    ssl_context()
    return _SSL_SOURCE


class MediaHTTPError(Exception):
    """A fetch that failed in a way the caller should report rather than retry."""

    def __init__(self, code, message, status=None, retry_after=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retry_after = retry_after


def _host(url):
    return (urlsplit(url).hostname or "").lower()


def _parse_retry_after(value):
    """``Retry-After`` is either a delta in seconds or an HTTP-date. Accept both."""
    if not value:
        return 0.0
    value = value.strip()
    try:
        return max(0.0, float(int(value)))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return 0.0
    if parsed is None:
        return 0.0
    return max(0.0, parsed.timestamp() - time.time())


def _backoff(attempt):
    return min(MAX_BACKOFF, BASE_BACKOFF * (2**attempt))


class HostLimiter:
    """Per-host spacing, refusal tracking and circuit breaking.

    An instance rather than module state, so a test can exercise a tripped
    breaker without poisoning the rest of the suite.
    """

    def __init__(self, spacing_ms=0, breaker_threshold=DEFAULT_BREAKER_THRESHOLD, sleep=time.sleep):
        self.spacing = spacing_ms / 1000.0
        self.breaker_threshold = breaker_threshold
        self._sleep = sleep
        self._state = {}

    def _for(self, host):
        return self._state.setdefault(host, {"next_at": 0.0, "refusals": 0, "tripped": False, "budget": None})

    def tripped(self, host):
        return self._for(host)["tripped"]

    def wait(self, host):
        state = self._for(host)
        delay = state["next_at"] - time.monotonic()
        if delay > 0:
            self._sleep(delay)

    def defer(self, host, seconds):
        state = self._for(host)
        state["next_at"] = max(state["next_at"], time.monotonic() + seconds)

    def succeeded(self, host):
        state = self._for(host)
        state["refusals"] = 0
        state["next_at"] = time.monotonic() + self.spacing

    def refused(self, host):
        state = self._for(host)
        state["refusals"] += 1
        if state["refusals"] >= self.breaker_threshold:
            state["tripped"] = True
        return state["tripped"]

    def record_budget(self, host, headers):
        """Remember what the service said about its own limit, if it says anything."""
        remaining = headers.get("x-ratelimit-remaining")
        limit = headers.get("x-ratelimit-limit")
        if remaining is None and limit is None:
            return
        state = self._for(host)
        state["budget"] = {
            "remaining": _maybe_number(remaining),
            "limit": _maybe_number(limit),
            "at": time.time(),
        }

    def budget(self, host):
        return self._for(host).get("budget")

    def budgets(self):
        return {host: state["budget"] for host, state in self._state.items() if state.get("budget")}


def _maybe_number(value):
    if value is None:
        return None
    try:
        return float(value) if "." in str(value) else int(value)
    except (TypeError, ValueError):
        return value


def fetch_json(url, limiter=None, headers=None, timeout=DEFAULT_TIMEOUT, accept="application/json"):
    """GET a JSON document, or raise ``MediaHTTPError``.

    Returns ``(payload, meta)`` where meta carries the status and whatever the
    host published about its own rate limit.
    """
    payload, meta = fetch_text(url, limiter=limiter, headers=headers, timeout=timeout, accept=accept)
    try:
        return json.loads(payload), meta
    except json.JSONDecodeError as exc:
        raise MediaHTTPError("bad_json", f"{_host(url)} returned a body that is not JSON: {exc}") from exc


def fetch_text(url, limiter=None, headers=None, timeout=DEFAULT_TIMEOUT, accept="*/*", method="GET", data=None):
    limiter = limiter or HostLimiter()
    host = _host(url)
    if limiter.tripped(host):
        raise MediaHTTPError("host_tripped", f"{host} tripped the circuit breaker earlier in this run")

    request_headers = {"User-Agent": USER_AGENT, "Accept": accept}
    request_headers.update(headers or {})

    last = None
    for attempt in range(MAX_ATTEMPTS):
        limiter.wait(host)
        request = urllib.request.Request(url, headers=request_headers, method=method, data=data)
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=ssl_context()) as response:
                body = response.read().decode("utf-8", errors="replace")
                limiter.record_budget(host, {k.lower(): v for k, v in response.headers.items()})
                limiter.succeeded(host)
                return body, {"status": response.status, "url": response.url, "budget": limiter.budget(host)}
        except urllib.error.HTTPError as exc:
            status = exc.code
            retry_after = _parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
            if status == 429:
                # A 429 defers the host and is retried, but must never count
                # toward the breaker. See the module docstring.
                delay = max(retry_after, _backoff(attempt), limiter.spacing)
                limiter.defer(host, delay)
                last = MediaHTTPError("http_429", f"{host} returned HTTP 429", status, delay)
            elif status in REFUSAL_STATUSES:
                detail = _refusal_detail(exc)
                if limiter.refused(host):
                    raise MediaHTTPError("host_tripped", f"{host} refused {limiter.breaker_threshold} times: {detail}", status)
                raise MediaHTTPError("refused", f"{host} returned HTTP {status}: {detail}", status, retry_after)
            elif status in RETRYABLE_STATUSES:
                last = MediaHTTPError("http_error", f"{host} returned HTTP {status}", status, retry_after)
            else:
                raise MediaHTTPError("http_error", f"{host} returned HTTP {status}", status, retry_after)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = MediaHTTPError("network", f"{host} unreachable: {exc}")

        if attempt < MAX_ATTEMPTS - 1:
            time.sleep(max(getattr(last, "retry_after", 0) or 0, _backoff(attempt)))

    raise last or MediaHTTPError("network", f"{host} failed for an unrecorded reason")


def _refusal_detail(exc):
    """The body of a refusal usually says which credential is wrong. Keep it short."""
    try:
        body = exc.read().decode("utf-8", errors="replace").strip()
    except Exception:  # noqa: BLE001 - a refusal with an unreadable body is still a refusal
        return exc.reason or "no detail"
    if not body:
        return exc.reason or "no detail"
    return body[:200].replace("\n", " ")
