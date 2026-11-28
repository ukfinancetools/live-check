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

FIRST ATTEMPTS (I744, 2026-11-25). A retry that recovers hides an edge fault: from about 16:50 to 22:20 GMT on
25 Nov 2026, 25 to 60% of requests from LHR failed, and every hourly live check passed because one retry fixed
each fetch. So fetch() records EVERY attempt in ATTEMPTS (time UTC, duration, status or error, cf-ray), and
first_attempt_verdict() judges the first attempts against one rule (EDGE-001): trip when more than
FIRST_FAIL_SHARE of first attempts failed, or when FETCHES_52X_MIN or more fetches met a Cloudflare 52x on any
attempt. The live check and the laptop edge sampler both call it, so the rule has one home.

Usage as a module:  status, headers, body = live_fetch.fetch(url)
Usage from a shell: live_fetch.py --head URL   prints "HTTP/1.1 <code>" and "name: value" lines
                    live_fetch.py --body URL   prints the body
                    exit 0 when a final status arrived (any code), 2 when every attempt failed.
"""
import argparse, datetime, itertools, socket, ssl, sys, time, urllib.error, urllib.request
import http.client

# The edge could not get a valid answer from the origin: Cloudflare's 52x class and the gateway
# errors 502 to 504. A 500 (the page's own error) and every 4xx are real answers and are not retried.
RETRY_CODES = frozenset({502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 527, 530})
WAITS = (5, 15, 30)  # seconds before attempts 2, 3 and 4
UA = "Mozilla/5.0 (X11; Linux x86_64) Chrome/128 ukft-ci live-fetch"

# EDGE-001 (I744): the first-attempt rule. Trip when the share of failed first attempts is MORE than this, or
# when this many fetches or more met a 52x (any attempt).
FIRST_FAIL_SHARE = 0.10
FETCHES_52X_MIN = 2
ATTEMPTS = []          # one dict per attempt of every fetch in this process (see _note)
ATTEMPTS_CAP = 50000   # a long process keeps the first records; the verdict counts the rest in 'dropped'
_STATE = {"dropped": 0}
_FETCH_IDS = itertools.count(1)  # next() on a count is atomic in CPython, so threaded callers get distinct ids


def is_52x(status):
    """Cloudflare's own origin errors: 520 to 527 and 530."""
    return status is not None and (520 <= status <= 527 or status == 530)


def reset_attempts():
    del ATTEMPTS[:]
    _STATE["dropped"] = 0


def _note(fetch_id, attempt, url, started, t0, status=None, hdrs=None, error=None):
    rec = {"fetch": fetch_id, "attempt": attempt, "url": url,
           "t": started.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (started.microsecond // 1000),
           "ms": int(round((time.monotonic() - t0) * 1000)), "status": status,
           "ray": (hdrs.get("cf-ray") if hdrs is not None else None), "error": error,
           "transient": bool(error) or status in RETRY_CODES}
    if len(ATTEMPTS) < ATTEMPTS_CAP:
        ATTEMPTS.append(rec)
    else:
        _STATE["dropped"] += 1
    return rec


def first_attempt_verdict(records=None, share=None, min_52x=None):
    """EDGE-001 over attempt records (default: ATTEMPTS). Each record needs fetch, attempt, status, transient.
    Returns a dict: fetches, first_failed, share, fetches_52x, tripped, reason, failed (the failed first
    attempts), recovered (fetches whose first attempt failed and whose last attempt answered under 500)."""
    records = ATTEMPTS if records is None else records
    share = FIRST_FAIL_SHARE if share is None else share
    min_52x = FETCHES_52X_MIN if min_52x is None else min_52x
    firsts = [r for r in records if r["attempt"] == 1]
    failed = [r for r in firsts if r["transient"]]
    with52 = sorted({r["fetch"] for r in records if is_52x(r["status"])})
    last = {}
    for r in records:
        last[r["fetch"]] = r
    recovered = [r for r in failed if last.get(r["fetch"]) is not None and not last[r["fetch"]]["transient"]
                 and (last[r["fetch"]]["status"] or 999) < 500]
    sh = (len(failed) / float(len(firsts))) if firsts else 0.0
    why = []
    if firsts and sh > share:
        why.append("%d of %d first attempts failed (%.0f%%, limit more than %.0f%%)" % (len(failed), len(firsts), sh * 100, share * 100))
    if len(with52) >= min_52x:
        why.append("%d fetch(es) met a Cloudflare 52x (limit %d)" % (len(with52), min_52x))
    return {"fetches": len(firsts), "first_failed": len(failed), "share": round(sh, 4), "fetches_52x": len(with52),
            "tripped": bool(why), "reason": "; ".join(why), "failed": failed, "recovered": recovered,
            "dropped": _STATE["dropped"] if records is ATTEMPTS else 0}


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
    fid = next(_FETCH_IDS)
    for attempt in range(len(waits) + 1):
        if attempt:
            log("live_fetch: RETRY %d/%d in %ss after %s on %s" % (attempt, len(waits), waits[attempt - 1], last, url))
            sleep(waits[attempt - 1])
        started, t0 = datetime.datetime.now(datetime.timezone.utc), time.monotonic()
        try:
            status, hdrs, body = _once(url, h, timeout, opener)
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, ssl.SSLError,
                http.client.HTTPException, OSError) as e:
            last = "%s: %s" % (type(e).__name__, getattr(e, "reason", e))
            _note(fid, attempt + 1, url, started, t0, error=last[:300])
            continue
        _note(fid, attempt + 1, url, started, t0, status=status, hdrs=hdrs)
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
