#!/usr/bin/env python3
"""live_check.py - the hourly live check: indexing outcome, every sitemap URL, one run per calculator (S117 WP-1, I297).

Three parts, against the live site (or any base URL):
  1. INDEXABLE-001, with indexing_outcome.py (a byte-identical copy of the deploy gate's module), on the
     committed indexable list (expected-urls.json: the site repo's one-way ratchet, made by
     sync_live_check.py) UNION every URL the live sitemap lists, minus the declared-noindex paths. The list
     keeps a page that drops out of the sitemap in the check; the sitemap brings in a new page before
     anyone syncs the list. Declared-noindex pages are fetched too, and must really be noindex.
  2. HTTP status of every sitemap URL that part 1 did not fetch: each must answer 200.
  3. one headless Chrome run per calculator with a fixed input and an outside-sourced expected result
     (calculators.json, run_calculators.js). Needs node, puppeteer-core and Chrome; when they are missing the
     run ends in exit 2, never a pass. Fewer results than configured calculators is a finding.
FLOORS (audit round 1, finding 9): an empty committed list is an error; a list shorter than the count
sync-manifest.json records, or fewer URLs checked than the list holds, is a finding.
Writes results/latest.json (the dead-man check in the workspace daily digest reads its checked_at) and appends
one line to results/history.jsonl. Every fetch goes through live_fetch.py (a Cloudflare 52x is retried).

THE MONTHLY PLANTED TEST on staging: staging serves the overlay (noindex header, Disallow-all robots.txt)
on purpose, so a plain run there always fails. Run once unplanted with --baseline FILE, plant the fault,
then run with --diff-against FILE: only findings absent from the baseline are reported, and the exit is 1
only when a new finding appears.

Usage: live_check.py [--base https://www.ukfinancetools.co.uk] [--urls expected-urls.json]
                     [--calculators calculators.json] [--skip-calculators] [--only name,name] [--out results/]
                     [--baseline FILE | --diff-against FILE] [--php-builtin]
--php-builtin: a local php -S fixture only (it has no .htaccess rewrite, so /sitemap.php is read for /sitemap.xml).
The summary line names the mode: live, base or php-builtin.
Exit 0 clean, 1 a finding, 2 the check could not run (a missing tool, every fetch failed, an empty list).
"""
import argparse, datetime, json, os, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import indexing_outcome as I  # noqa: E402
import live_fetch  # noqa: E402

LIVE = "https://www.ukfinancetools.co.uk"
UA = "Mozilla/5.0 (X11; Linux x86_64) Chrome/128 ukft-live-check"


def sitemap_urls(base, php_builtin):
    """(status, [canonical-host paths], [problems]) from the live sitemap."""
    st, locs, problems = I.read_sitemap(base, I.make_opener(), php_builtin=php_builtin)
    paths = []
    for u in locs:
        p = I.sitemap_path(u, I.CANON, php_builtin)
        if p is not None and p not in paths:
            paths.append(p)
    return st, paths, problems


def sitemap_statuses(base, paths, skip):
    """Findings for sitemap URLs that do not answer 200 (those in skip were fetched by part 1)."""
    out, checked = [], 0
    for path in paths:
        if path in skip:
            continue
        checked += 1
        try:
            st, h, _ = live_fetch.fetch(base + path, {"User-Agent": UA}, opener=I.make_opener())
        except live_fetch.LiveFetchError as e:
            out.append("%s (sitemap) unreachable: %s" % (path, e))
            continue
        if st != 200:
            out.append("%s (sitemap) status %s%s" % (path, st, (" -> " + h.get("Location", "")) if h and h.get("Location") else ""))
    return out, checked


def configured(cfg_path, only):
    """(names to run, error). An empty config or an unknown --only name is an error, never 0 of 0."""
    try:
        cfg = json.load(open(cfg_path, encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [], "calculators config unreadable: %s" % e
    names = [c.get("calculator") for c in cfg.get("calculators", [])]
    if not names:
        return [], "calculators config %s lists no calculator" % cfg_path
    unknown = [x for x in only if x not in names]
    if unknown:
        return [], "--only names no configured calculator: %s" % ", ".join(unknown)
    return [n for n in names if not only or n in only], None


def calculators(base, cfg_path, only):
    """(findings, per-calculator results, error) from run_calculators.js."""
    want, err = configured(cfg_path, only)
    if err:
        return [], [], err
    node = shutil.which("node")
    if not node:
        return [], [], "node is not on PATH"
    out_json = os.path.join(HERE, "results", "calculators-run.json")
    os.makedirs(os.path.dirname(out_json), exist_ok=True)
    cmd = [node, os.path.join(HERE, "run_calculators.js"), base, cfg_path, "--json", out_json]
    if only:
        cmd += ["--only", ",".join(only)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
    print(r.stdout.strip())
    if r.returncode == 2 or not os.path.isfile(out_json):
        return [], [], "run_calculators.js could not run: %s" % (r.stderr.strip().splitlines() or [r.stdout.strip()[-300:]])[-1][:300]
    res = json.load(open(out_json, encoding="utf-8"))["results"]
    os.remove(out_json)
    find = ["calculator %s: %s" % (x["calculator"], "; ".join(x["problems"])[:400]) for x in res if not x["ok"]]
    ran = [x["calculator"] for x in res]
    if sorted(ran) != sorted(want):
        find.append("calculators: %d result(s) for %d configured calculator(s) (missing: %s)" % (len(ran), len(want), ", ".join(n for n in want if n not in ran) or "none"))
    return find, res, None


def manifest_count(urls_path):
    p = os.path.join(os.path.dirname(os.path.abspath(urls_path)), "sync-manifest.json")
    try:
        return json.load(open(p, encoding="utf-8")).get("counts", {}).get("indexable")
    except (OSError, ValueError):
        return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=LIVE)
    ap.add_argument("--urls", default=os.path.join(HERE, "expected-urls.json"))
    ap.add_argument("--calculators", default=os.path.join(HERE, "calculators.json"))
    ap.add_argument("--skip-calculators", action="store_true")
    ap.add_argument("--only", default="", help="calculator names, for a local run")
    ap.add_argument("--out", default=os.path.join(HERE, "results"))
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--baseline", metavar="FILE", help="write this run's findings as JSON (the unplanted staging run)")
    g.add_argument("--diff-against", metavar="FILE", help="report only findings absent from this baseline")
    ap.add_argument("--php-builtin", action="store_true", help="a local php -S fixture server only")
    a = ap.parse_args(argv)
    base = a.base.rstrip("/")
    mode = "php-builtin" if a.php_builtin else ("live" if base.startswith(LIVE) else "base")
    started = datetime.datetime.now(datetime.timezone.utc)
    exp = json.load(open(a.urls, encoding="utf-8"))
    listed, noindex = list(exp.get("indexable", [])), list(exp.get("noindex", []))
    if not listed:
        print("LIVE-CHECK ERROR (%s): %s lists no indexable URL; the check would prove nothing" % (mode, a.urls))
        return 2
    result = {"checked_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"), "base": base, "mode": mode, "expected_urls_from": exp.get("_from"),
              "listed_urls": len(listed), "parts": {}, "findings": [], "outcome": None}
    error = None
    floor = []
    want = manifest_count(a.urls)
    if want is not None and want != len(listed):
        floor.append("floor: %s lists %d indexable URL(s), sync-manifest.json records %d" % (os.path.basename(a.urls), len(listed), want))
    _, sm_paths, sm_problems = sitemap_urls(base, a.php_builtin)
    extra = [p for p in sm_paths if p not in listed and p not in noindex and not I.declared_noindex_path(p)]
    paths = listed + extra
    try:
        f1 = I.check(base, paths, noindex, opener=I.make_opener(), php_builtin=a.php_builtin)
    except Exception as e:  # noqa: BLE001 - a run that cannot fetch at all is an error, never a pass
        f1, error = [], "indexing outcome could not run: %s: %s" % (type(e).__name__, e)
    # No floor on len(paths): paths = listed + extra, and check() records every URL it cannot fetch (round 2, N6).
    result["parts"]["indexing"] = {"findings": f1, "urls": len(paths), "from_list": len(listed), "from_sitemap_only": extra}
    f2, n2 = sitemap_statuses(base, sm_paths, set(paths) | set(noindex))
    if sm_problems and not any(p in f1 for p in sm_problems):
        f2 = sm_problems + f2
    result["parts"]["sitemap_status"] = {"findings": f2, "extra_urls_checked": n2}
    f3, res3 = [], []
    if a.skip_calculators:
        result["parts"]["calculators"] = {"skipped": True}
    else:
        f3, res3, err3 = calculators(base, a.calculators, [x for x in a.only.split(",") if x])
        result["parts"]["calculators"] = {"findings": f3, "results": res3}
        error = error or err3
    findings = floor + f1 + f2 + f3
    ignored = 0
    if a.diff_against:
        try:
            known = set(json.load(open(a.diff_against, encoding="utf-8"))["findings"])
        except (OSError, ValueError, KeyError) as e:
            print("LIVE-CHECK ERROR (%s): baseline %s unreadable: %s" % (mode, a.diff_against, e))
            return 2
        ignored = sum(1 for f in findings if f in known)
        findings = [f for f in findings if f not in known]
        result["baseline"] = {"file": a.diff_against, "ignored": ignored}
    result["findings"] = findings
    result["urls_checked"] = len(paths)
    result["duration_s"] = round((datetime.datetime.now(datetime.timezone.utc) - started).total_seconds(), 1)
    result["outcome"] = "error" if error else ("fail" if findings else "pass")
    if error:
        result["error"] = error
    os.makedirs(a.out, exist_ok=True)
    json.dump(result, open(os.path.join(a.out, "latest.json"), "w", encoding="utf-8"), indent=1)
    with open(os.path.join(a.out, "history.jsonl"), "a", encoding="utf-8") as h:
        h.write(json.dumps({"checked_at": result["checked_at"], "outcome": result["outcome"], "findings": len(findings),
                            "duration_s": result["duration_s"]}) + "\n")
    if a.baseline:
        json.dump({"base": base, "checked_at": result["checked_at"], "findings": findings}, open(a.baseline, "w", encoding="utf-8"), indent=1)
    for f in findings:
        print("FAIL LIVE-CHECK " + f)
    if error:
        print("LIVE-CHECK ERROR " + error)
    print("LIVE-CHECK %s (%s): %d finding(s)%s; %d indexable URL(s) checked (%d listed, %d from the sitemap only), %d further sitemap URL(s), %d calculator(s); %.0f s"
          % (result["outcome"].upper(), mode, len(findings), (", %d baseline finding(s) ignored" % ignored) if a.diff_against else "",
             len(paths), len(listed), len(extra), n2, len(res3), result["duration_s"]))
    if a.baseline:
        return 2 if error else 0  # a baseline run records the findings; it is not the test
    return 2 if error else (1 if findings else 0)


if __name__ == "__main__":
    sys.exit(main())
