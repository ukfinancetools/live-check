#!/usr/bin/env python3
"""compare_values.py - the daily value comparison: live pages and the live engine against the live registry (S117 WP-1).

The live site serves the registry, /data/tax-params.json (checked 2026-10-13: HTTP 200), so this runs in the
public live-check repo with no access to the site repo. Two comparisons, every day:
  1. ENGINE. /assets/ukft-tax-engine.js carries a generated PARAMS block (gen_engine_params.py) with the
     registry's sha256 and every period of each parameter the code names. The block's sha must equal the
     sha256 of the served registry, and every row must equal the registry's periods. A mismatch means a
     partial deploy or a stale cache: the engine computes from values the registry no longer holds.
  2. PAGES. stated-values.json (made by sync_live_check.py from the site repo: every ukft_param() call a
     PHP page renders, with its year, nation and render form) names each figure a live page states from the
     registry. The registry's value for that year is rendered as the page renders it (number, 2 dp, percent,
     date, plain echo) and must appear in the live page's text as a whole token: no digit, decimal point or
     comma-digit on either side, '%' after a percent, '£' before money, 'month' after a months value, and
     no bare-number fallback. A missing figure means the page states a value the registry no longer gives:
     a stale page file, or a value typed into the page.
     ANCHORS (I319). The sync renders each page with the value set to a stand-in and records the page text on
     each side of every place the figure prints. Where a row has anchors, the registry value must appear
     BETWEEN them, once per anchor, so a value typed at a render point fails even when the page's prose also
     types the true value (VAT '20%', PRR '9 months'). Digits inside an anchor match any number, so another
     figure's change beside it does not break it. A row with no anchor (an attribute, a script value) is
     searched on the whole page, as before.
FLOORS. An empty stated list is an error; a figure count below the list, or a list that is not the length
sync-manifest.json records, is a finding.
A changed registry value that no live page reflects, or a live page that states a figure the registry does
not hold, is a finding. Exit 0 clean, 1 a finding, 2 the check could not run. Fetches use live_fetch.py.

Usage: compare_values.py [--base https://www.ukfinancetools.co.uk] [--stated stated-values.json] [--out results/]
"""
import argparse, datetime, decimal, hashlib, html, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import live_fetch  # noqa: E402

LIVE = "https://www.ukfinancetools.co.uk"
UA = "Mozilla/5.0 (X11; Linux x86_64) Chrome/128 ukft-live-check values"
BEGIN, END = "/* @generated tax-params BEGIN", "/* @generated tax-params END */"
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


def get(base, path):
    st, _, body = live_fetch.fetch(base + path, {"User-Agent": UA})
    if st != 200:
        raise RuntimeError("%s status %s" % (path, st))
    return body


def year_span(year, basis):
    """'2026/27' -> the first and last ISO dates of that tax or financial year."""
    y = year.strip()
    if "-" in y and "/" not in y:
        p = y.split("-")
        y = p[0] + "/" + p[1][-2:]
    s = int(y[:4])
    return ("%d-04-01" % s, "%d-03-31" % (s + 1)) if basis == "financial_year" else ("%d-04-06" % s, "%d-04-05" % (s + 1))


def resolve(params, pid, at, nation="uk"):
    """The registry value as ukft_param() in src/includes/tax_params.php resolves it (same rules, no rule arg)."""
    p = params[pid]
    rows = [r for r in p.get("periods", []) if r.get("nation") == (nation or "uk")]
    if not rows:
        raise KeyError("%s has no nation %s" % (pid, nation))
    basis = p.get("year_basis", "none")
    if basis not in ("tax_year", "financial_year"):
        return rows[0]["value"]
    if at and re.match(r"^\d{4}-\d{2}-\d{2}$", at):
        for r in rows:
            if r["from"] <= at <= r["to"]:
                return r["value"]
        raise KeyError("%s has no period covering %s" % (pid, at))
    a, b = year_span(at, basis)
    inside = [r for r in rows if r["from"] <= b and r["to"] >= a]
    if len(inside) == 1 and inside[0]["from"] <= a and inside[0]["to"] >= b:
        return inside[0]["value"]
    raise KeyError("%s changes inside %s or has no value for it" % (pid, at))


def php_number_format(v, dp=0):
    """PHP number_format(): thousands commas, half away from zero (Python's format rounds half to even)."""
    q = decimal.Decimal(repr(float(v))).quantize(decimal.Decimal(1).scaleb(-dp), rounding=decimal.ROUND_HALF_UP)
    return "{:,.{}f}".format(q, dp)


def php_echo(v):
    """How PHP 8 echoes a number: 9 -> '9', 9.0 -> '9', 3.5 -> '3.5'."""
    v = float(v)
    return str(int(v)) if v == int(v) else repr(v)


# A rendered figure is found only as a whole token: no digit, decimal point or comma-digit on either side, so
# '9' is not found in '2026', '19' or '£1,950', and '20%' is not found in '120%'.
LEFT, RIGHT = r"(?<![0-9.])(?<![0-9],)", r"(?![0-9])(?![.,][0-9])"


def render(value, form, unit=None):
    """[(label, regex)] for the page's rendering of a registry value, as tax_params.php, number_format() and a
    plain echo give it. Audit round 1 (finding 5): no bare-number fallback. A percent parameter (registry unit
    'percent', stored as a fraction) must appear with '%' (ukft_pct or a page's own closure); a money
    parameter (unit 'GBP...') with '£' before it; a 'months' parameter followed by 'month' ('9 months',
    '9-month'). Each is matched as a whole token."""
    if unit == "percent" and form in ("number", "raw"):
        form = "pct"
    if form == "pct":
        s = ("%.4f" % (float(value) * 100)).rstrip("0").rstrip(".")
        return [(s + "%", LEFT + re.escape(s) + r"\s?%")]
    if form in ("date", "date_html"):
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", str(value))
        if not m:
            raise ValueError("%r is not an ISO date" % value)
        s = "%d %s %s" % (int(m.group(3)), MONTHS[int(m.group(2)) - 1], m.group(1))
        return [(s, LEFT + re.escape(s) + RIGHT)]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return [(str(value), re.escape(str(value)))]
    if form == "number2":
        texts = [php_number_format(value, 2)]
    elif form == "raw":  # a plain echo, or a variable the page formats later
        texts = sorted({php_echo(value), php_number_format(value, 0 if float(value) == int(value) else 2)})
    else:
        texts = [php_number_format(value)]
    if unit and unit.startswith("GBP"):
        return [("£" + t, "£" + re.escape(t) + RIGHT) for t in texts]
    if unit == "months":
        return [(t + " months", LEFT + re.escape(t) + r"[\s\u00a0-]?months?\b") for t in texts]
    return [(t, LEFT + re.escape(t) + RIGHT) for t in texts]


def text_of(page_html):
    # The scripts' text stays: footer.php states two registry values inside the tooltip script's strings.
    t = re.sub(r"<[^>]+>", "", page_html)
    return re.sub(r"\s+", " ", html.unescape(t)).replace("\u00a0", " ")


MONTH_RX = "(?:" + "|".join(MONTHS) + ")"
VARIABLE = re.compile(r"([0-9][0-9.,]*|" + "|".join(MONTHS) + ")")


def anchor_rx(text):
    """An anchor's words as a pattern. Digits match any number and a month name any month, because other registry
    figures and dates can sit beside a render point and change on their own (audit F3); the rest is exact."""
    out = []
    for part in VARIABLE.split(text):
        if not part:
            continue
        out.append(r"[0-9][0-9.,]*" if part[:1].isdigit() else MONTH_RX if part in MONTHS else re.escape(part))
    return "".join(out)


def anchor_status(t, anchors, forms):
    """(typed, stale) for one stated row. Each is a list of (left, right, want, got).
    typed: the anchor's words are on the page as many times as recorded, but with something other than the registry
    value between them - a typed or stale value at the render point (a finding).
    stale: the anchor's words are not on the page as often as recorded - the copy beside the figure changed since the
    sync, so this place cannot be judged (a notice: re-run sync_live_check.py --write and copy the list)."""
    need = {}
    for an in anchors:
        k = (an["left"], an["right"])
        need[k] = need.get(k, 0) + 1
    core = "(?:" + "|".join(rx for _, rx in forms) + ")"
    typed, stale = [], []
    for (left, right), want in need.items():
        got = len(re.findall(anchor_rx(left) + core + anchor_rx(right), t))
        if got >= want:
            continue
        loose = len(re.findall(anchor_rx(left) + r".{1,30}?" + anchor_rx(right), t))
        (typed if loose >= want else stale).append((left, right, want, got))
    return typed, stale


def at_anchors(t, anchors, forms):
    """The typed list of anchor_status (each a finding)."""
    return anchor_status(t, anchors, forms)[0]


def compare_engine(reg_bytes, params, engine_js):
    find = []
    m = re.search(re.escape(BEGIN) + r".*?registry sha256:([0-9a-f]{64}) \*/\s*const PARAMS = (\{.*?\});\s*" + re.escape(END), engine_js, re.S)
    if not m:
        return ["engine: no generated tax-params block in /assets/ukft-tax-engine.js"]
    sha = hashlib.sha256(reg_bytes).hexdigest()
    if m.group(1) != sha:
        find.append("engine: block built from registry sha %s..., the served registry is %s..." % (m.group(1)[:12], sha[:12]))
    block = json.loads(m.group(2))
    for pid, spec in block.items():
        if pid not in params:
            find.append("engine: %s is in the engine block but not in the registry" % pid)
            continue
        want = [[q["from"], q["to"], q["nation"], q["value"]] for q in params[pid]["periods"]]
        if spec["p"] != want:
            find.append("engine: %s periods differ from the registry (%d engine rows, %d registry rows)" % (pid, len(spec["p"]), len(want)))
    return find, len(block)


def compare_pages(base, params, stated, notices=None):
    find, n = [], 0
    notices = [] if notices is None else notices
    by_page = {}
    for s in stated:
        by_page.setdefault(s["page"], []).append(s)
    for page, items in sorted(by_page.items()):
        try:
            t = text_of(get(base, page).decode("utf-8", "replace"))
        except (RuntimeError, live_fetch.LiveFetchError) as e:
            find.append("page %s could not be read: %s" % (page, e))
            continue
        for s in items:
            n += 1
            try:
                val = resolve(params, s["param"], s.get("at"), s.get("nation", "uk"))
                forms = render(val, s.get("render", "number"), params[s["param"]].get("unit"))
            except (KeyError, ValueError) as e:
                find.append("page %s: %s cannot be resolved from the registry: %s" % (page, s["param"], e))
                continue
            if not any(re.search(rx, t) for _, rx in forms):
                find.append("page %s states no %s for %s %s (registry value %s, rendered %s)" % (page, s["param"], s.get("at") or "(any)", s.get("nation", "uk"), val, " or ".join(lb for lb, _ in forms)))
                continue
            typed, stale = anchor_status(t, s.get("anchors") or [], forms)
            for left, right, want, got in typed:
                find.append("page %s: %s %s %s is not %s at its render point '...%s[ ]%s...' (%d of %d place(s): another value is printed there)" % (
                    page, s["param"], s.get("at") or "(any)", s.get("nation", "uk"), " or ".join(lb for lb, _ in forms), left[-16:], right[:16], got, want))
            for left, right, want, got in stale:
                notices.append("page %s: %s render point '...%s[ ]%s...' cannot be judged: the words beside it changed since the sync "
                               "(run sync_live_check.py --write in the site repo and copy stated-values.json here)" % (page, s["param"], left[-16:], right[:16]))
    return find, n


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=LIVE)
    ap.add_argument("--stated", default=os.path.join(HERE, "stated-values.json"))
    ap.add_argument("--out", default=os.path.join(HERE, "results"))
    a = ap.parse_args(argv)
    base = a.base.rstrip("/")
    started = datetime.datetime.now(datetime.timezone.utc)
    try:
        reg_bytes = get(base, "/data/tax-params.json")
        params = json.loads(reg_bytes.decode("utf-8"))["params"]
        engine = get(base, "/assets/ukft-tax-engine.js").decode("utf-8", "replace")
    except (RuntimeError, ValueError, KeyError, live_fetch.LiveFetchError) as e:
        print("VALUES-LIVE ERROR cannot read the live registry or engine: %s" % e)
        return 2
    r = compare_engine(reg_bytes, params, engine)
    f_eng, n_eng = (r, 0) if isinstance(r, list) and r and r[0].startswith("engine: no generated") else r
    stated = json.load(open(a.stated, encoding="utf-8"))["stated"]
    if not stated:
        print("VALUES-LIVE ERROR %s lists no stated figure; the comparison would prove nothing" % a.stated)
        return 2
    notices = []
    f_pg, n_pg = compare_pages(base, params, stated, notices)
    findings = f_eng + f_pg
    # Floors (audit round 1, finding 9): every listed figure is compared, and the list is the one the sync made.
    if n_pg != len(stated):
        findings.append("floor: compared %d page figure(s), %s lists %d" % (n_pg, os.path.basename(a.stated), len(stated)))
    try:
        want = json.load(open(os.path.join(os.path.dirname(os.path.abspath(a.stated)), "sync-manifest.json"), encoding="utf-8")).get("counts", {}).get("stated")
    except (OSError, ValueError):
        want = None
    if want is not None and want != len(stated):
        findings.append("floor: %s lists %d stated figure(s), sync-manifest.json records %d" % (os.path.basename(a.stated), len(stated), want))
    os.makedirs(a.out, exist_ok=True)
    json.dump({"checked_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"), "base": base, "engine_params": n_eng, "page_figures": n_pg,
               "findings": findings, "notices": notices, "outcome": "fail" if findings else "pass"},
              open(os.path.join(a.out, "values-latest.json"), "w", encoding="utf-8"), indent=1)
    for f in findings:
        print("FAIL VALUES-LIVE " + f)
    for x in notices:
        print("NOTICE VALUES-LIVE " + x)
    print("VALUES-LIVE %s: %d finding(s); %d engine parameter(s), %d page figure(s)" % ("FAIL" if findings else "PASS", len(findings), n_eng, n_pg))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
