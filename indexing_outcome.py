#!/usr/bin/env python3
"""indexing_outcome.py - INDEXABLE-001: every URL that must be indexed is served indexable (S117 WP-1, I298).

ORIGIN. On 2026-10-03 commit 1477cc9 committed the staging-only noindex overlay, and the live site sent
noindex with a block-all robots.txt for about 18 hours. OVERLAY-001 reads the two repo files and, after a
deploy, the live home page and one guide. It cannot see a page-level noindex meta tag, a googlebot-only
noindex, a canonical that points at another host, an .htaccess redirect of an indexable URL, or a sitemap
that drops pages. This check reads the OUTCOME: the real HTTP response of every indexable URL.

ONE HOME. This file is the only implementation. The deploy step (I1) runs it against the built src/ tree
served by Apache 2.4 with mod_php on the runner (Apache honours .htaccess, as the live host does; the PHP
built-in server does not, and caught 10 of the 12 prototype plants). The hourly live check
(tools/live-check/) runs a byte-identical copy, kept current by test-scripts/deploy-guard/sync_live_check.py,
whose --check fails the deploy when the copies drift.

WHAT IS INDEXABLE. The list comes from the repo, never from the sitemap, so a page that drops out of the
sitemap is still checked. A reader page is every .php under src/ outside the non-page dirs and files below.
A page is noindex BY DECLARATION when its path starts with /news/ (header.php, S24, 2026-09-25), it is a
src/sandbox/ page whose name carries '-preview' (sandbox previews, the sitemap rule), or its PHP code (not a
comment, not inline HTML) sets $pageNoindex = true or 'noindex' => true (tool_master.php). Every other page
must be indexable, and must never become noindex without such a declaration. A declared noindex page must
not be listed in the sitemap, and must really be served noindex.

ASSERTIONS, for each indexable URL, on the real response:
  - status 200 with no redirect, and an HTML body that ends with </html> (a fatal error after the head is
    sent gives a 200 with a truncated page); complete <script> elements after </html> are allowed, because
    the guide and calculator templates print their widget scripts there;
  - no noindex, none or unavailable_after in any X-Robots-Tag header (any user-agent prefix), robots,
    googlebot, googlebot-news or bingbot meta tag, or http-equiv X-Robots-Tag meta tag. Directives are split
    on commas and matched as whole tokens, so max-image-preview:none is not a finding;
  - exactly one canonical, counting the HTML <link rel=canonical> and every rel=canonical in a Link
    response header, equal to the page itself on https://www.ukfinancetools.co.uk (exact; the root URL with
    an empty path is the one equivalent form);
  - a non-empty <title>;
  - robots.txt allows Googlebot and '*' under RFC 9309 as Google applies it: the group of the most specific
    user-agent token (case-insensitive; several User-agent lines share a group; groups for one token merge),
    else the '*' group; the longest matching rule wins; '*' and a final '$' are wildcards; Allow wins a tie;
    an empty Disallow allows all;
  - the page is listed in sitemap.xml. /sitemap.xml itself must answer 200 with a body that starts with
    <?xml and parses as a <urlset>; every <loc> is exactly on the canonical host. Only the PHP built-in
    server (--serve-php, which has no .htaccess rewrite) reads /sitemap.php in its place.
Repo mode (--serve-php, or --lint-only) also lints every src/**/.htaccess: any X-Robots-Tag Header
directive; a Redirect, RedirectMatch, RedirectPermanent or RedirectTemp of an indexable path; an
unconditional RewriteRule with an R flag (R, R=3xx, R=permanent, redirect=...) or an absolute http(s)
target whose pattern matches an indexable path; and a src/.htaccess with no unconditional RewriteRule that
serves /sitemap.xml from sitemap.php. The lint is a fast backstop for the pre-commit hook and the fault
replay, which have no Apache; the served check is the gate.

NOT COVERED. A noindex meta tag that the page's JavaScript injects after load (no crawler-free check can see
it without a browser; Google renders it). A robots.txt path with percent-encoding (matched as written). A
heredoc in PHP source (the comment strip does not read heredocs).

USAGE
  indexing_outcome.py --serve-apache src              deploy: Apache 2.4 + mod_php + a self-signed cert on a free port
  indexing_outcome.py --serve-php src                 replay and hook: php -S (no .htaccess), plus the repo lint
  indexing_outcome.py --base https://www.ukfinancetools.co.uk [--urls expected-urls.json]   the live site
  indexing_outcome.py --repo . --lint-only            the .htaccess lint alone
  indexing_outcome.py --repo . --list                 print the expected URL list as JSON (the sync script)
Options: --repo DIR (default: the repo this file sits in), --urls FILE (the list, when there is no repo),
--limit N (first N indexable paths, for a fixture), --host-header HOST and --insecure (a local HTTPS server).
The summary line names the server mode: apache, php-builtin, live or base.
Exit 0 clean, 1 a finding, 2 a usage, server or fetch error. Every live fetch goes through live_fetch.py
(S99, I170: a Cloudflare 52x is retried, never read as a finding or a pass).
"""
import argparse, json, os, re, shutil, socket, ssl, subprocess, sys, tempfile, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
for _d in (HERE, os.path.join(HERE, "..", "shared")):
    if os.path.isfile(os.path.join(_d, "live_fetch.py")) and _d not in sys.path:
        sys.path.insert(0, _d)
import live_fetch  # noqa: E402

GATE = "INDEXABLE-001"
CANON = "https://www.ukfinancetools.co.uk"
UA = "Mozilla/5.0 (X11; Linux x86_64) Chrome/128 ukft-indexable-check"
# Not reader pages: includes, generators and endpoints (sitemap.php's exclude lists, blast-radius NOT_A_PAGE).
NOT_PAGE_DIRS = frozenset({"templates", "includes", "assets", "data", "go", ".git", "node_modules"})
NOT_PAGE_FILES = frozenset({"header.php", "footer.php", "sitemap.php", "llms.php", "search-index.php", "404.php"})
NOINDEX_DIRS = frozenset({"news"})  # header.php: every /news/ path is noindex,follow by decision (S24)
PREVIEW_DIR = "sandbox"  # a -preview page is noindex by declaration only here (the sitemap drops -preview anywhere)
DECLARED_NOINDEX = re.compile(r"\$pageNoindex\s*=\s*true\b|['\"]noindex['\"]\s*=>\s*true\b")
ROBOTS_META_NAMES = ("robots", "googlebot", "googlebot-news", "bingbot")
# Robots directives that carry a value after a colon. Any other "word:" at the start of a token is a
# user-agent prefix ("googlebot: noindex").
VALUE_DIRECTIVES = frozenset({"max-snippet", "max-image-preview", "max-video-preview", "unavailable_after"})
HTACCESS_XROBOTS = re.compile(r"^\s*Header\s+(?:always\s+|onsuccess\s+)?(?:set|add|append|merge|setifempty|edit\*?)\s+\"?X-Robots-Tag\b", re.I)
REWRITE_RULE = re.compile(r"^\s*RewriteRule\s+(\S+)\s+(\S+)(?:\s+\[([^\]]*)\])?", re.I)
ALIAS_REDIRECT = re.compile(r"^\s*(RedirectMatch|RedirectPermanent|RedirectTemp|Redirect)\s+(.*)$", re.I)
HUBS = ("/calculators/", "/guides/", "/news/")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def make_opener(insecure=False):
    handlers = [NoRedirect()]
    if insecure:  # a local Apache with a self-signed certificate (the deploy runner), never the live site
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


class Head(HTMLParser):
    """The robots meta tags, canonical links and title of a page."""

    def __init__(self):
        super().__init__()
        self.metas, self.canon, self.title, self._in_title = [], [], "", False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            name = a.get("name", "").strip().lower()
            equiv = a.get("http-equiv", "").strip().lower()
            if name in ROBOTS_META_NAMES:
                self.metas.append((name, a.get("content", "")))
            elif equiv == "x-robots-tag":
                self.metas.append(("http-equiv X-Robots-Tag", a.get("content", "")))
        if tag == "link" and "canonical" in a.get("rel", "").lower().split():
            self.canon.append(a.get("href", "").strip())
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, d):
        if self._in_title:
            self.title += d


def blocking(value):
    """The directives in one robots meta content or X-Robots-Tag value that take a page out of the index:
    noindex, none and unavailable_after (any date), whole comma-separated tokens only, after any user-agent
    prefix. 'index, follow, max-image-preview:none' gives []."""
    out = []
    for tok in (value or "").split(","):
        t = tok.strip().lower()
        m = re.match(r"([a-z][\w.-]*)\s*:\s*(.*)$", t, re.S)
        if m and m.group(1) not in VALUE_DIRECTIVES:
            t = m.group(2).strip()  # a user-agent prefix: "googlebot: noindex"
        name = re.split(r"\s*:\s*", t, maxsplit=1)[0].strip()
        if name in ("noindex", "none"):
            out.append(name)
        elif name == "unavailable_after":
            out.append(tok.strip())
    return out


def header_values(h, name):
    if h is None:
        return []
    if hasattr(h, "get_all"):
        return list(h.get_all(name) or [])
    return [v for k, v in h.items() if k.lower() == name.lower()]


def link_canonicals(h):
    """Every rel=canonical target in the Link response headers (any number, quoted or bare, any case)."""
    out = []
    for v in header_values(h, "Link"):
        for m in re.finditer(r"<([^>]*)>((?:\s*;\s*[^;,<]*)*)", v):
            for p in re.finditer(r";\s*rel\s*=\s*(\"[^\"]*\"|[^;,\s]+)", m.group(2), re.I):
                if "canonical" in p.group(1).strip('"').lower().split():
                    out.append(m.group(1).strip())
    return out


# ---------------------------------------------------------------- robots.txt (RFC 9309, Google's reading)

class Robots:
    """robots.txt as RFC 9309 and Google's open-source parser read it."""

    def __init__(self, text):
        self.groups = []  # [(agent tokens, [(allow, pattern)])]
        cur, last_rule = None, False
        # Google skips a UTF-8 byte order mark; .strip() does not remove U+FEFF (verification round 2, N4).
        for raw in (text or "").lstrip("\ufeff").splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, val = line.split(":", 1)
            key, val = key.strip().lower(), val.strip()
            if key == "user-agent":
                if cur is None or last_rule:
                    cur, last_rule = ([], []), False
                    self.groups.append(cur)
                m = re.match(r"\*|[A-Za-z_-]+", val)  # the product token: "Googlebot/2.1" -> googlebot
                if m:
                    cur[0].append(m.group(0).lower())
            elif key in ("allow", "disallow"):
                if cur is None:
                    continue  # a rule before any User-agent line belongs to no group
                last_rule = True
                if val:  # an empty Disallow (or Allow) matches nothing: it allows all
                    cur[1].append((key == "allow", val))
            # Other records (Sitemap, Crawl-delay, Content-Signal) neither start nor end a group.

    def rules_for(self, agent):
        token = agent.lower()
        rules = [r for ags, rs in self.groups if token in ags for r in rs]
        if token != "*" and not any(token in ags for ags, _ in self.groups):
            rules = [r for ags, rs in self.groups if "*" in ags for r in rs]
        return rules

    @staticmethod
    def matches(pattern, path):
        anchored = pattern.endswith("$")
        body = pattern[:-1] if anchored else pattern
        rx = "".join(".*" if c == "*" else re.escape(c) for c in body) + (r"\Z" if anchored else "")
        return re.match(rx, path, re.S) is not None

    def allowed(self, agent, path):
        best = None  # (pattern length, allow)
        for allow, pat in self.rules_for(agent):
            if self.matches(pat, path):
                n = len(pat)
                if best is None or n > best[0] or (n == best[0] and allow):
                    best = (n, allow)
        return best is None or best[1]


# ---------------------------------------------------------------- the expected list

def php_code(text):
    """The PHP code of a file: inline HTML, comments (//, # and /* */) dropped, string literals kept,
    so a comment or prose that names $pageNoindex = true does not declare anything."""
    out, i, n = [], 0, len(text)
    tok = re.compile(r"\?>|'|\"|//|#(?!\[)|/\*")
    while i < n:
        j = text.find("<?", i)
        if j < 0:
            break
        i = j + (5 if text.startswith("<?php", j) else 3 if text.startswith("<?=", j) else 2)
        while i < n:
            m = tok.search(text, i)
            if not m:
                out.append(text[i:])
                i = n
                break
            out.append(text[i:m.start()])
            t, i = m.group(0), m.end()
            if t == "?>":
                out.append(" ")
                break
            if t in ("'", '"'):
                k = i
                while k < n and text[k] != t:
                    k += 2 if text[k] == "\\" else 1
                out.append(t + text[i:k + 1])
                i = k + 1
            elif t == "/*":
                k = text.find("*/", i)
                i = n if k < 0 else k + 2
                out.append(" ")
            else:  # a line comment ends at the newline or at ?>
                e = re.compile(r"\n|\?>").search(text, i)
                i = n if not e else e.start()
        out.append(" ")
    return "".join(out)


def declared_noindex_path(path):
    """The path rules of a noindex declaration (no source needed: the hourly live check uses them)."""
    if path.startswith("/news/"):
        return True
    return path.startswith("/%s/" % PREVIEW_DIR) and "-preview" in path.rsplit("/", 1)[-1]


def page_path(rel):
    """src-relative file -> URL path: index.php -> '/', calculators/index.php -> '/calculators/'."""
    rel = rel.replace(os.sep, "/")
    if rel == "index.php":
        return "/"
    if rel.endswith("/index.php"):
        return "/" + rel[: -len("index.php")]
    return "/" + rel


def expected(repo):
    """{'indexable': [paths], 'noindex': [paths]} derived from src/ (the repo, never the sitemap)."""
    src = os.path.join(repo, "src")
    if not os.path.isdir(src):
        raise FileNotFoundError("no src/ under %s" % repo)
    indexable, noindex = [], []
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if d not in NOT_PAGE_DIRS)
        for fn in sorted(files):
            if not fn.endswith(".php") or fn in NOT_PAGE_FILES:
                continue
            rel = os.path.relpath(os.path.join(root, fn), src)
            path = page_path(rel)
            top = rel.replace(os.sep, "/").split("/")[0]
            try:
                body = open(os.path.join(root, fn), encoding="utf-8", errors="replace").read()
            except OSError:
                body = ""
            if top in NOINDEX_DIRS or declared_noindex_path(path) or DECLARED_NOINDEX.search(php_code(body)):
                noindex.append(path)
            else:
                indexable.append(path)
    return {"indexable": indexable, "noindex": noindex}


# ---------------------------------------------------------------- the .htaccess lint

def htaccess_files(repo):
    src = os.path.join(repo, "src")
    out = []
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if d not in (".git", "node_modules"))
        if ".htaccess" in files:
            out.append(os.path.join(root, ".htaccess"))
    return out


def _alias_hits(name, args, paths):
    """Indexable paths a mod_alias Redirect* line sends away (or marks gone)."""
    parts = re.findall(r"\"[^\"]*\"|\S+", args)
    parts = [p.strip('"') for p in parts]
    if name.lower() in ("redirect", "redirectmatch") and parts and re.match(r"^(\d{3}|permanent|temp|seeother|gone)$", parts[0], re.I):
        parts = parts[1:]
    if not parts:
        return []
    if name.lower() == "redirectmatch":
        try:
            rx = re.compile(parts[0])
        except re.error:
            return list(paths)  # an unreadable pattern is judged to match: a finding, never a silent pass
        return [p for p in paths if rx.search(p) or (p.endswith("/") and rx.search(p + "index.php"))]
    up = parts[0]
    pre = up.rstrip("/")
    return [p for p in paths if p == up or p.startswith(pre + "/") or p.rstrip("/") == pre]


def lint_htaccess(repo, indexable):
    """Repo-file backstop for the .htaccess classes Apache alone can serve."""
    src = os.path.join(repo, "src")
    files = htaccess_files(repo)
    out = [] if os.path.exists(os.path.join(src, ".htaccess")) else ["src/.htaccess is missing"]
    serves_sitemap = False
    for p in files:
        reldir = os.path.relpath(os.path.dirname(p), src).replace(os.sep, "/")
        scope = "/" if reldir == "." else "/" + reldir + "/"
        label = "src/.htaccess" if reldir == "." else "src/%s/.htaccess" % reldir
        here = [x for x in indexable if x.startswith(scope)]
        cond_pending = False
        for i, raw in enumerate(open(p, encoding="utf-8", errors="replace"), 1):
            line = raw.split("#", 1)[0].rstrip()
            if not line.strip():
                continue
            if HTACCESS_XROBOTS.search(line):
                out.append("%s line %d sets an X-Robots-Tag header: %s" % (label, i, line.strip()))
            a = ALIAS_REDIRECT.match(line)
            if a:
                for x in _alias_hits(a.group(1), a.group(2), here):
                    out.append("%s line %d redirects the indexable URL %s: %s" % (label, i, x, line.strip()))
                continue
            if re.match(r"^\s*RewriteCond\b", line, re.I):
                cond_pending = True
                continue
            m = REWRITE_RULE.match(line)
            if not m:
                continue
            pattern, target, flags = m.group(1), m.group(2), (m.group(3) or "")
            conditional, cond_pending = cond_pending, False
            redirect = any(re.match(r"^(r|redirect)(=.*)?$", f.strip(), re.I) for f in flags.split(","))
            negate = pattern.startswith("!")
            try:
                rx = re.compile(pattern[1:] if negate else pattern)
            except re.error:
                continue
            if scope == "/" and not (conditional or negate or redirect) and rx.search("sitemap.xml") and re.match(r"^/?sitemap\.php$", target):
                serves_sitemap = True
            if conditional or not (redirect or re.match(r"^https?://", target, re.I)):
                continue  # a conditional rule is Apache's to judge; a rule to a local path with no R flag is an internal map
            for x in here:
                rel = x[len(scope):]
                hit = bool(rx.search(rel) or (x.endswith("/") and rx.search(rel + "index.php")))
                if hit != negate:
                    out.append("%s line %d redirects the indexable URL %s: %s" % (label, i, x, line.strip()))
    if files and not serves_sitemap and os.path.exists(os.path.join(src, ".htaccess")):
        # php -S cannot show it: robots.txt and Search Console read /sitemap.xml, which only this rule serves.
        out.append("src/.htaccess has no unconditional RewriteRule that serves /sitemap.xml from sitemap.php")
    return out


# ---------------------------------------------------------------- the served check

def fetch(base, path, opener, host_header=None, waits=None):
    """(status, headers, text). A URL that gives only transient errors (a Cloudflare 52x, a 503, a dropped
    connection, after live_fetch's retries) returns status None: the caller records a finding, because a
    page that cannot be fetched is not indexable, and the run is never an error that hides it."""
    h = {"User-Agent": UA}
    if host_header:
        h["Host"] = host_header
    try:
        status, hdrs, body = live_fetch.fetch(base + path, h, waits=waits, opener=opener)
    except live_fetch.LiveFetchError as e:
        return None, {"X-Fetch-Error": str(e)}, ""
    return status, hdrs, body.decode("utf-8", "replace")


def parse_sitemap(text):
    """(locs, problem): the <loc> values of a sitemap body, read with an XML parser."""
    if not (text or "").lstrip("\ufeff").startswith("<?xml"):
        return [], "does not start with <?xml (%r)" % (text or "")[:40]
    try:
        root = ET.fromstring(text.lstrip("\ufeff").encode("utf-8"))
    except ET.ParseError as e:
        return [], "is not well-formed XML: %s" % e
    if root.tag.rsplit("}", 1)[-1] != "urlset":
        return [], "root element is <%s>, not <urlset>" % root.tag.rsplit("}", 1)[-1]
    locs = [(el.text or "").strip() for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "loc"]
    return locs, None


def read_sitemap(base, opener, host_header=None, waits=None, php_builtin=False):
    """(status, locs, [problems]) for /sitemap.xml. Only the PHP built-in server, which has no .htaccess
    rewrite (it answers /sitemap.xml with the home page), reads /sitemap.php instead. Apache and the live
    site must serve /sitemap.xml itself: a 404, 500 or redirect there is a finding."""
    st, _, sm = fetch(base.rstrip("/"), "/sitemap.php" if php_builtin else "/sitemap.xml", opener, host_header, waits)
    if st != 200:
        return st, [], ["sitemap.xml status %s" % st]
    locs, problem = parse_sitemap(sm)
    if problem:
        return st, [], ["sitemap.xml %s" % problem]
    if not locs:
        return st, [], ["sitemap.xml status 200, 0 urls"]
    return st, locs, []


def sitemap_path(loc, canon_host, php_builtin=False):
    """The URL path of a <loc> exactly as listed, or None when it is not on canon_host."""
    if not loc.startswith(canon_host + "/"):
        return None
    path = loc[len(canon_host):]
    if php_builtin and os.name == "nt" and path.startswith("//"):
        # php -S on Windows only: sitemap.php joins '.' and DIRECTORY_SEPARATOR, so every loc starts '//'
        # (the home page is '///'). The live host, Apache and Linux php -S never do this: compared exactly.
        path = re.sub(r"/{2,}", "/", path)
    return path


def complete_html(body):
    """True when the page was rendered to its end: it has </html>, and after the last one only whitespace,
    comments and complete <script> elements. The guide and calculator templates print the page's widget
    scripts after footer.php closes </html> (live and local alike, 2026-10-13), so a strict 'ends with
    </html>' would fail every such page. A fatal error after the head is sent leaves no </html> at all."""
    low = body.lower()
    k = low.rfind("</html>")
    if k < 0:
        return False
    tail = re.sub(r"<script\b[^>]*>.*?</script\s*>|<!--.*?-->", "", body[k + 7:], flags=re.S | re.I)
    return tail.strip() == ""


def is_noindex(h, body):
    hp = Head()
    try:
        hp.feed(body)
    except Exception:  # a broken page still reports what it parsed
        pass
    return any(blocking(v) for v in header_values(h, "X-Robots-Tag")) or any(blocking(c) for _, c in hp.metas)


def check(base, paths, noindex_paths=(), canon_host=CANON, opener=None, host_header=None, log=None, waits=None, php_builtin=False):
    """Return the findings for the site at base. paths: the indexable URL paths. waits: live_fetch's retry
    waits (None = its defaults for the live site; () for a local server, which never needs a retry).
    php_builtin: the server is php -S (no .htaccess), which alone may read /sitemap.php for /sitemap.xml."""
    base = base.rstrip("/")
    opener = opener or make_opener()
    log = log or (lambda m: None)
    find = []
    st, _, robots_txt = fetch(base, "/robots.txt", opener, host_header, waits)
    if st == 200:
        robots = Robots(robots_txt)
    else:
        find.append("/robots.txt status %s" % st)
        robots = Robots("")
    _, locs, problems = read_sitemap(base, opener, host_header, waits, php_builtin)
    find += problems
    sm_paths = set()
    for u in locs:
        p = sitemap_path(u, canon_host, php_builtin)
        if p is None:
            find.append("sitemap.xml lists %s, which is not on %s" % (u, canon_host))
            continue
        if "?" in p or "#" in p:
            find.append("sitemap.xml lists %s, which carries a query or fragment" % u)
        sm_paths.add(p)
    for path in noindex_paths:
        if path in sm_paths:
            find.append("%s is noindex by declaration but listed in sitemap.xml" % path)
    for path in paths:
        st, h, body = fetch(base, path, opener, host_header, waits)
        log("%s %s" % (st, path))
        if st is None:
            find.append("%s unreachable: %s" % (path, h.get("X-Fetch-Error")))
            continue
        if st != 200:
            loc = (h.get("Location") if h else None) or ""
            find.append("%s status %s%s" % (path, st, (" -> " + loc) if loc else ""))
            continue
        for v in header_values(h, "X-Robots-Tag"):
            if blocking(v):
                find.append("%s X-Robots-Tag: %s" % (path, v))
        hp = Head()
        try:
            hp.feed(body)
        except Exception:  # a broken page still reports what it parsed
            pass
        for name, content in hp.metas:
            if blocking(content):
                find.append("%s meta %s=%s" % (path, name, content))
        if not complete_html(body):
            find.append("%s body does not end with </html> (a truncated render, %d bytes)" % (path, len(body)))
        canons = hp.canon + link_canonicals(h)
        want = canon_host + path
        if len(canons) != 1:
            find.append("%s has %d canonical links (%d in the HTML, %d in a Link header)" % (path, len(canons), len(hp.canon), len(canons) - len(hp.canon)))
        elif not (canons[0] == want or (path == "/" and canons[0] == canon_host)):
            find.append("%s canonical %s, want %s" % (path, canons[0], want))
        if not hp.title.strip():
            find.append("%s empty title" % path)
        for agent in ("Googlebot", "*"):
            if not robots.allowed(agent, path):
                find.append("%s blocked by robots.txt for %s" % (path, agent))
        if locs and path not in sm_paths and path not in HUBS:
            find.append("%s not in sitemap.xml" % path)
    for path in noindex_paths:
        # A declared noindex page must really be noindex. A 404 or a redirect is not indexed either.
        st, h, body = fetch(base, path, opener, host_header, waits)
        log("%s %s (declared noindex)" % (st, path))
        if st == 200 and not is_noindex(h, body):
            find.append("%s is noindex by declaration but served indexable (no noindex header or meta tag)" % path)
    return find


# ---------------------------------------------------------------- local servers

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_port(port, proc, secs=15):
    t0 = time.time()
    while time.time() - t0 < secs:
        if proc.poll() is not None:
            return False
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return True
        except OSError:
            time.sleep(0.2)
    return False


APACHE_MODULES = ("mpm_prefork", "authz_core", "authz_host", "access_compat", "mime", "dir", "alias", "env",
                  "setenvif", "rewrite", "headers", "expires", "ssl")


def apache_paths():
    """(binary, modules dir, php module) or None when Apache 2.4 with mod_php is not installed."""
    exe = shutil.which("apache2") or shutil.which("httpd") or ("/usr/sbin/apache2" if os.path.exists("/usr/sbin/apache2") else None)
    mods = "/usr/lib/apache2/modules"
    if not exe or not os.path.isdir(mods):
        return None
    php = sorted(f for f in os.listdir(mods) if re.match(r"libphp\d.*\.so$", f))
    if not php:
        return None
    return exe, mods, os.path.join(mods, php[-1])


def _php_flags():
    """The shared PHP settings (PHP-FLAGS-001, I322): errors shown in the page whatever php.ini says, so a warning
    breaks the page shape this check reads. Read only when a test server starts. The module runs from
    test-scripts/deploy-guard/ and, in the site repo, from its synced copy in tools/live-check/; the public
    live-check repo never starts a test server, and has no test-scripts/ at all."""
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (os.path.join(here, "..", "shared"), os.path.join(here, "..", "..", "test-scripts", "shared")):
        d = os.path.normpath(d)
        if os.path.isfile(os.path.join(d, "php_flags.py")):
            break
    else:
        raise RuntimeError("test-scripts/shared/php_flags.py not found near %s: a test server needs it" % __file__)
    if d not in sys.path:
        sys.path.insert(0, d)
    import php_flags
    return php_flags


def apache_conf(docroot, port, tmp, mods, php_so):
    lines = ['ServerRoot "/etc/apache2"', "PidFile %s/httpd.pid" % tmp, "Listen 127.0.0.1:%d" % port]
    for m in APACHE_MODULES:
        lines.append("LoadModule %s_module %s/mod_%s.so" % (m, mods, m))
    lines.append("LoadModule php_module %s" % php_so)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        lines += ["User www-data", "Group www-data"]
    lines += [
        "ServerName www.ukfinancetools.co.uk",
        "TypesConfig /etc/mime.types",
        "ErrorLog %s/error.log" % tmp, "LogLevel warn",
        'DocumentRoot "%s"' % docroot,
        '<Directory "%s">' % docroot, "    Options FollowSymLinks", "    AllowOverride All", "    Require all granted", "</Directory>",
        '<FilesMatch "\\.php$">', "    SetHandler application/x-httpd-php", "</FilesMatch>",
        # No opcode or realpath cache: a fixture re-plants the docroot between requests and must see each change.
        "php_admin_flag opcache.enable Off", "php_admin_value realpath_cache_size 0",
        # No PHP-FLAGS-001 settings here, by design (I322, CI run 38457363978): Apache mode serves the pages as the
        # live host does, where errors are not shown and a fatal error before output is a 500. With errors shown,
        # Apache answered 200 with the error text, so this check would judge a page the live host never serves. PHP
        # warnings are caught by the census renders (RULES-001, TYPED-001), which use the shared settings.
        "DirectoryIndex index.php",
        "SSLEngine on", "SSLCertificateFile %s/cert.pem" % tmp, "SSLCertificateKeyFile %s/key.pem" % tmp,
    ]
    return "\n".join(lines) + "\n"


class ApacheServer:
    """Apache 2.4 + mod_php + mod_ssl on a free port, serving docroot as www.ukfinancetools.co.uk.
    The .htaccess forces HTTPS and www, so the server speaks TLS (a 2-day self-signed certificate in a
    temp dir) and the client sends the production Host header. Stopped by PID."""
    mode = "apache"

    def __init__(self, docroot):
        self.docroot = os.path.abspath(docroot)
        self.port = free_port()
        self.tmp = tempfile.mkdtemp(prefix="ukft-apache-")
        self.proc = None
        self.base = "https://127.0.0.1:%d" % self.port
        self.host_header = "www.ukfinancetools.co.uk"
        self.opener = make_opener(insecure=True)

    def __enter__(self):
        found = apache_paths()
        if not found:
            raise RuntimeError("Apache 2.4 with mod_php is not installed (apache2 and libapache2-mod-php)")
        exe, mods, php_so = found
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=www.ukfinancetools.co.uk",
                        "-keyout", os.path.join(self.tmp, "key.pem"), "-out", os.path.join(self.tmp, "cert.pem")],
                       check=True, capture_output=True)
        conf = os.path.join(self.tmp, "httpd.conf")
        open(conf, "w", encoding="utf-8").write(apache_conf(self.docroot, self.port, self.tmp, mods, php_so))
        # Its own session (setsid): started in the caller's process group, Apache's start-up signals reached the
        # calling Python and ended it (seen 2026-10-13 in the cloud sandbox: exit 143 two seconds in).
        self.proc = subprocess.Popen([exe, "-d", "/etc/apache2", "-f", conf, "-DFOREGROUND"], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, start_new_session=True)
        if not wait_port(self.port, self.proc):
            err = (self.proc.stderr.read() or b"").decode("utf-8", "replace") if self.proc.stderr else ""
            log = os.path.join(self.tmp, "error.log")
            if os.path.exists(log):
                err += open(log, encoding="utf-8", errors="replace").read()[-2000:]
            self.__exit__(None, None, None)
            raise RuntimeError("Apache did not start on port %d: %s" % (self.port, err.strip()[-2000:]))
        return self

    def __exit__(self, *a):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)


class PhpServer:
    """php -S on a free port (no .htaccess: for the hook and the fault replay, with the repo lint)."""
    mode = "php-builtin"

    def __init__(self, docroot):
        self.docroot = os.path.abspath(docroot)
        self.port = free_port()
        self.proc = None
        self.base = "http://127.0.0.1:%d" % self.port
        self.host_header = None
        self.opener = make_opener()

    def __enter__(self):
        php = shutil.which("php")
        if not php:
            raise RuntimeError("php is not on PATH")
        if not os.path.isdir(self.docroot):
            raise RuntimeError("php -S docroot %s is not a directory" % self.docroot)
        # No opcode or realpath cache, as the Apache server: a runner php.ini that enables opcache with a
        # revalidate delay serves a file planted seconds after its first request from the stale cache.
        self.proc = subprocess.Popen([php, "-d", "opcache.enable=0", "-d", "opcache.enable_cli=0", "-d", "realpath_cache_size=0"]
                                     + _php_flags().html() + ["-S", "127.0.0.1:%d" % self.port, "-t", self.docroot],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_port(self.port, self.proc):
            self.__exit__(None, None, None)
            raise RuntimeError("php -S did not start on port %d" % self.port)
        return self

    def __exit__(self, *a):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def serve_and_check(server, paths, noindex, log):
    """Run check() on a started server; its mode decides the php -S allowances."""
    return check(server.base, paths, noindex, opener=server.opener, host_header=server.host_header, log=log, waits=(),
                 php_builtin=server.mode == "php-builtin")


def run(a):
    if a.urls:
        exp = json.load(open(a.urls, encoding="utf-8"))
        paths, noindex = list(exp["indexable"]), list(exp.get("noindex", []))
        repo = a.repo if a.repo and os.path.isdir(os.path.join(a.repo, "src")) else None
    else:
        repo = a.repo or os.path.normpath(os.path.join(HERE, "..", ".."))
        exp = expected(repo)
        paths, noindex = exp["indexable"], exp["noindex"]
    if a.list:
        print(json.dumps(exp, indent=1))
        return 0
    if a.limit:
        paths = paths[: a.limit]
        noindex = noindex[: a.limit]
    log = (lambda m: print(m, flush=True)) if a.verbose else None
    find, mode = [], None
    if a.lint_only:
        mode = "lint"
        find = lint_htaccess(repo, paths)
    elif a.serve_apache:
        with ApacheServer(a.serve_apache) as s:
            mode = s.mode
            find = serve_and_check(s, paths, noindex, log)
    elif a.serve_php:
        with PhpServer(a.serve_php) as s:
            mode = s.mode
            find = serve_and_check(s, paths, noindex, log)
        if repo:
            find += lint_htaccess(repo, paths)
    elif a.base:
        mode = "live" if a.base.startswith(CANON) else "base"
        find = check(a.base, paths, noindex, opener=make_opener(insecure=a.insecure), host_header=a.host_header, log=log)
    else:
        print("%s: give --serve-apache DIR, --serve-php DIR, --base URL, --lint-only or --list" % GATE)
        return 2
    for f in find:
        print("FAIL %s %s" % (GATE, f))
    print("%s (%s): %d finding(s) on %d indexable URL(s), %d declared noindex" % (GATE, mode, len(find), len(paths), len(noindex)))
    return 1 if find else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo")
    ap.add_argument("--urls", help="expected-urls.json from --list, when there is no repo (the public live check)")
    ap.add_argument("--base")
    ap.add_argument("--serve-apache", metavar="DIR")
    ap.add_argument("--serve-php", metavar="DIR")
    ap.add_argument("--lint-only", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--host-header")
    ap.add_argument("--insecure", action="store_true", help="a local HTTPS server with a self-signed certificate only")
    ap.add_argument("--verbose", "-v", action="store_true")
    a = ap.parse_args(argv)
    try:
        return run(a)
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.CalledProcessError) as e:
        print("%s: ERROR %s: %s" % (GATE, type(e).__name__, e))
        return 2


if __name__ == "__main__":
    sys.exit(main())
