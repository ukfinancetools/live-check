#!/usr/bin/env python3
"""live_fetch.py - one home for fetching the live or staging site in a check (S99, I170, 2026-10-06).

WHY. The site sits behind Cloudflare on shared hosting, and a share of origin fetches come back as a
Cloudflare origin error (HTTP 520 to 527, 530) or a dropped connection. On 2026-10-05 deploy run
37362359648 failed OVERLAY-001 live on one HTTP 525, and the staging smoke, the data-vintage live axis
and the weekly sitemap fetch each made one fetch with no retry. A check that reads one transient error as
a finding marks a good deploy as failed. A fail-open check skips its comparison without a word.

WHAT. fetch() retries ONLY that transient class (RETRY_CODES, which adds the gateway errors 502 to 504,
and connection, timeout or TLS errors),
waiting WAITS seconds between attempts, and prints one line per retry so a CI log records the rate.
Any other status (200, 301, 404, 500) is returned at once for the caller to judge. When every attempt
fails, it raises LiveFetchError, and the caller must treat that as an error, never as a pass.

Usage as a module:  status, headers, body = live_fetch.fetch(url)
Usage from a shell: live_fetch.py --head URL   prints "HTTP/1.1 <code>" and "name: value" lines
                    live_fetch.py --body URL   prints the body
                    exit 0 when a final status arrived (any code), 2 when every attempt failed.
"""
import argparse, socket, ssl, sys, time, urllib.error, urllib.request
import http.client

# The edge could not get a valid answer from the origin: Cloudflare's 52x class and the gateway
# errors 502 to 504. A 500 (the page's own error) and every 4xx are real answers and are not retried.
RETRY_CODES = frozenset({502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 527, 530})
WAITS = (5, 15, 30)  # seconds before attempts 2, 3 and 4
UA = "Mozilla/5.0 (X11; Linux x86_64) Chrome/128 ukft-ci live-fetch"


class LiveFetchError(OSError):
    """Every attempt ended in a transient error. The caller must not read this as a pass.
    An OSError, as the urllib errors it replaces are, so a caller's existing except clause still holds."""


def _once(url, headers, timeout, opener):
    req = urllib.request.Request(url, headers=headers)
    try:
        with (opener.open if opener else urllib.request.urlopen)(req, timeout=timeout) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def fetch(url, headers=None, waits=None, timeout=30, sleep=time.sleep, log=None, opener=None):
    """Return (status, headers, body_bytes) for the first non-transient answer.
    Raise LiveFetchError when every attempt (1 + len(waits)) gives a transient error.
    waits=None reads the module's WAITS at call time, so a test can set it to zeros.
    opener: a urllib opener, for example one that does not follow redirects (the /go/ probe)."""
    waits = WAITS if waits is None else waits
    log = log or (lambda m: print(m, flush=True))
    h = {"User-Agent": UA, "Cache-Control": "no-cache"}
    h.update(headers or {})
    last = None
    for attempt in range(len(waits) + 1):
        if attempt:
            log("live_fetch: RETRY %d/%d in %ss after %s on %s" % (attempt, len(waits), waits[attempt - 1], last, url))
            sleep(waits[attempt - 1])
        try:
            status, hdrs, body = _once(url, h, timeout, opener)
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, ssl.SSLError,
                http.client.HTTPException, OSError) as e:
            last = "%s: %s" % (type(e).__name__, getattr(e, "reason", e))
            continue
        if status in RETRY_CODES:
            last = "HTTP %d (cf-ray %s)" % (status, hdrs.get("cf-ray") if hdrs else None)
            continue
        if attempt:
            log("live_fetch: HTTP %d on attempt %d of %s" % (status, attempt + 1, url))
        return status, hdrs, body
    raise LiveFetchError("%d attempts, last %s, on %s" % (len(waits) + 1, last, url))


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--head", metavar="URL")
    g.add_argument("--body", metavar="URL")
    ap.add_argument("--user-agent")
    a = ap.parse_args()
    url = a.head or a.body
    extra = {"User-Agent": a.user_agent} if a.user_agent else None
    try:
        status, hdrs, body = fetch(url, extra, log=lambda m: print(m, file=sys.stderr, flush=True))
    except LiveFetchError as e:
        print("live_fetch: ERROR %s" % e, file=sys.stderr)
        return 2
    if a.head:
        print("HTTP/1.1 %d" % status)
        for k, v in hdrs.items():
            print("%s: %s" % (k.lower(), v))
    else:
        sys.stdout.write(body.decode("utf-8", "replace"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
