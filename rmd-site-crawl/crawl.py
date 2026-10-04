#!/usr/bin/env python3
"""
RMD Engineering College - PHASE 1 structural crawler.

Crawls https://www.rmd.ac.in/ (HTML pages only), saves the ORIGINAL page source,
and builds inventories of pages, links, external links, assets, errors and
redirects.  Assets (PDF, images, ...) are only RECORDED, never downloaded.

Pure standard library (Python 3.8+).  State lives in data/crawl.db (SQLite) so
the crawl is resumable: just run the same command again.

    python crawl.py run                  # crawl (resumes automatically)
    python crawl.py run --check-assets   # ... then HEAD-check internal assets
    python crawl.py check-assets         # HEAD-check internal assets only
    python crawl.py export               # regenerate CSVs + reports from the DB
    python crawl.py status               # quick queue/status counts
"""
import argparse
import csv
import gzip
import hashlib
import json
import os
import re
import socket
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import Counter, OrderedDict, defaultdict
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.robotparser import RobotFileParser

# --------------------------------------------------------------------------- #
# Configuration constants
# --------------------------------------------------------------------------- #
CANON_HOST = "www.rmd.ac.in"
INTERNAL_HOSTS = {"rmd.ac.in", "www.rmd.ac.in"}
SEED = "https://www.rmd.ac.in/"
ROBOTS_URL = "https://www.rmd.ac.in/robots.txt"
ROBOT_TOKEN = "RMD-Phase1-Crawler"
USER_AGENT = ("RMD-Phase1-Crawler/1.0 (website migration research crawl; "
              "structural HTML only; respects robots.txt)")
HOST_VARIANTS = ["http://www.rmd.ac.in/", "http://rmd.ac.in/", "https://rmd.ac.in/"]

MAX_REDIRECTS = 10
MAX_HTML_BYTES = 25 * 1024 * 1024
MAX_QUERY_VARIANTS_PER_PATH = 100
MAX_URL_LENGTH = 400
MAX_PATH_SEGMENTS = 14

RETRY_STATUS = {429, 500, 502, 503, 504}

TRACKING_EXACT = {
    "fbclid", "gclid", "dclid", "msclkid", "yclid", "igshid", "gbraid", "wbraid",
    "srsltid", "mc_cid", "mc_eid", "_ga", "_gl", "_hsenc", "_hsmi", "ref_src",
    "phpsessid", "jsessionid", "sessionid", "session_id", "session", "aspsessionid",
    "cfid", "cftoken", "sid_session", "s_cid", "trk", "trkcampaign",
}
TRACKING_PREFIX = ("utm_", "pk_", "mtm_", "hsa_", "aspsessionid")

INDEX_NAMES = {"index.html", "index.htm", "index.php", "index.asp", "index.aspx",
               "default.html", "default.htm", "default.asp", "default.aspx"}

ASSET_EXT = {}
for _t, _exts in {
    "PDF": "pdf",
    "IMAGE": "jpg jpeg jpe jfif png gif bmp webp svg ico tif tiff avif heic psd",
    "VIDEO": "mp4 m4v avi mov wmv flv mkv webm mpeg mpg 3gp ogv",
    "AUDIO": "mp3 wav ogg oga aac m4a wma flac mid midi",
    "DOCUMENT": "doc docx odt rtf txt",
    "SPREADSHEET": "xls xlsx xlsm ods csv",
    "PRESENTATION": "ppt pptx pps ppsx odp",
    "ARCHIVE": "zip rar 7z tar gz tgz bz2",
    "FONT": "woff woff2 ttf otf eot",
    "OTHER": "css js json xml swf exe msi apk dmg bin iso jar bat com dll map",
}.items():
    for _e in _exts.split():
        ASSET_EXT[_e] = _t

# Reference types.  PAGE refs are "links"; the others are page resources.
CRAWL_REFS = {"a", "area", "iframe", "frame", "meta-refresh", "onclick",
              "script-ref", "link-canonical", "link-alternate", "data-href"}
PAGE_REFS = CRAWL_REFS | {"form"}

ONCLICK_RE = re.compile(
    r"""(?:window\.open|location\.assign|location\.replace|document\.location(?:\.href)?|location(?:\.href)?)"""
    r"""\s*(?:\(|=)\s*['"]([^'"]+)['"]""", re.I)
SCRIPT_STR_RE = re.compile(
    r"""["']([^"'\s<>{}$+()]{1,300}\.(?:html?|php|aspx?|pdf|docx?|xlsx?|pptx?|zip|rar|jpe?g|png|gif))(?:\?[^"'\s]*)?["']""",
    re.I)
CSS_URL_RE = re.compile(r"""url\(\s*['"]?([^'")]+?)['"]?\s*\)""", re.I)

WIN_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(*a):
    print(*a, flush=True)


# --------------------------------------------------------------------------- #
# URL normalisation / classification
# --------------------------------------------------------------------------- #
def is_tracking_param(name):
    n = name.lower()
    return n in TRACKING_EXACT or n.startswith(TRACKING_PREFIX)


def clean_query(q):
    if not q:
        return ""
    try:
        pairs = urllib.parse.parse_qsl(q, keep_blank_values=True)
    except Exception:
        return q
    keep = sorted((k, v) for k, v in pairs if not is_tracking_param(k))
    return urllib.parse.urlencode(keep, quote_via=urllib.parse.quote)


def normalize(raw, base=None):
    """Return (kind, url).  kind: internal | external | nonhttp | fragment | empty | invalid."""
    if raw is None:
        return "empty", ""
    raw = re.sub(r"[\t\r\n]+", "", raw.strip())
    if not raw:
        return "empty", ""
    if raw.startswith("#"):
        return "fragment", raw
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):", raw)
    if m and m.group(1).lower() not in ("http", "https"):
        return "nonhttp", raw
    raw2 = raw.replace("\\", "/")
    try:
        absu = urllib.parse.urljoin(base, raw2) if base else raw2
        p = urllib.parse.urlsplit(absu)
        host = (p.hostname or "").lower().rstrip(".")
        port = p.port
    except ValueError:
        return "invalid", raw
    if p.scheme not in ("http", "https") or not host:
        return "invalid", raw
    default_port = 443 if p.scheme == "https" else 80
    if host in INTERNAL_HOSTS and port in (None, default_port):
        path = re.sub(r"/{2,}", "/", p.path or "/")
        path = urllib.parse.quote(path, safe="/%:@!$&'()*+,;=-._~")
        path = re.sub(r"%[0-9a-fA-F]{2}", lambda mm: mm.group(0).upper(), path)
        last = path.rsplit("/", 1)[-1]
        if last.lower() in INDEX_NAMES:
            path = path[: len(path) - len(last)]
        q = clean_query(p.query)
        return "internal", f"https://{CANON_HOST}{path}" + (f"?{q}" if q else "")
    netloc = host + (f":{port}" if port and port != default_port else "")
    ext_url = urllib.parse.urlunsplit((p.scheme, netloc, p.path or "/", p.query, ""))
    return "external", ext_url


def url_ext(url):
    path = urllib.parse.urlsplit(url).path
    seg = path.rsplit("/", 1)[-1]
    if "." not in seg:
        return ""
    ext = seg.rsplit(".", 1)[-1].lower()
    return ext if re.fullmatch(r"[a-z0-9]{1,8}", ext) else ""


def asset_type_for(url):
    return ASSET_EXT.get(url_ext(url))


def url_filename(url):
    seg = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
    return urllib.parse.unquote(seg)


def asset_type_from_ctype(ct):
    ct = (ct or "").split(";")[0].strip().lower()
    if ct == "application/pdf":
        return "PDF"
    if ct.startswith("image/"):
        return "IMAGE"
    if ct.startswith("video/"):
        return "VIDEO"
    if ct.startswith("audio/"):
        return "AUDIO"
    if ct.startswith("font/") or "font" in ct:
        return "FONT"
    if "msword" in ct or "wordprocessingml" in ct or "opendocument.text" in ct or ct == "text/plain":
        return "DOCUMENT"
    if "ms-excel" in ct or "spreadsheetml" in ct or "opendocument.spreadsheet" in ct or ct == "text/csv":
        return "SPREADSHEET"
    if "ms-powerpoint" in ct or "presentationml" in ct or "opendocument.presentation" in ct:
        return "PRESENTATION"
    if ct in ("application/zip", "application/x-zip-compressed", "application/x-rar-compressed",
              "application/vnd.rar", "application/x-7z-compressed", "application/gzip", "application/x-tar"):
        return "ARCHIVE"
    return "OTHER"


def suspicious_reason(url):
    """Heuristic traps that could cause infinite crawling."""
    if len(url) > MAX_URL_LENGTH:
        return f"URL longer than {MAX_URL_LENGTH} chars"
    p = urllib.parse.urlsplit(url)
    segs = [s for s in p.path.split("/") if s]
    if len(segs) > MAX_PATH_SEGMENTS:
        return f"more than {MAX_PATH_SEGMENTS} path segments"
    counts = Counter(s.lower() for s in segs)
    if counts and counts.most_common(1)[0][1] >= 3:
        return "same path segment repeated 3+ times (relative-link loop?)"
    return None


# --------------------------------------------------------------------------- #
# URL -> local file path mapping (deterministic, Windows-safe)
# --------------------------------------------------------------------------- #
def _safe_segment(s, maxlen=80):
    s = re.sub(r'[<>:"|?*\x00-\x1f\\]', "_", s).rstrip(" .")
    if not s:
        s = "_"
    if s.lower().split(".")[0] in WIN_RESERVED:
        s = "_" + s
    if len(s) > maxlen:
        stem, dot, ext = s.rpartition(".")
        h = hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()[:8]
        if dot and len(ext) <= 8:
            s = stem[: maxlen - 10 - len(ext)] + "__" + h + "." + ext
        else:
            s = s[: maxlen - 10] + "__" + h
    return s


def url_to_relpath(url):
    p = urllib.parse.urlsplit(url)
    parts = [s for s in urllib.parse.unquote(p.path).split("/") if s not in ("", ".")]
    if p.path.endswith("/") or not parts:
        dirs, fname = parts, "index.html"
    else:
        dirs, last = parts[:-1], parts[-1]
        fname = last if last.lower().endswith((".html", ".htm")) else last + ".html"
    if p.query:
        stem, dot, ext = fname.rpartition(".")
        h = hashlib.sha1(p.query.encode()).hexdigest()[:10]
        fname = f"{stem}__q-{h}.{ext}"
    segs = [_safe_segment(d) for d in dirs] + [_safe_segment(fname)]
    rel = "/".join(segs)
    if len(rel) > 150:
        rel = "_long/" + hashlib.sha1(url.encode()).hexdigest()[:20] + ".html"
    return rel


# --------------------------------------------------------------------------- #
# HTML parsing
# --------------------------------------------------------------------------- #
class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.h1 = ""
        self.lang = ""
        self.base = None
        self.description = ""
        self.rel_canonical = ""
        self.refs = []           # dicts: ref_type, raw, text, menu
        self._in_title = False
        self._in_h1 = False
        self._in_script = False
        self._in_style = False
        self._script_buf = []
        self._style_buf = []
        self._stack = []         # list nesting: {'t':'ul'} / {'t':'li','label':..}
        self._a = None

    # -- helpers
    def _menu(self):
        lis = [e for e in self._stack if e["t"] == "li"]
        return [e["label"] for e in lis[:-1] if e["label"]]

    def _add(self, ref_type, raw, text="", menu=None):
        if raw is None:
            return
        raw = raw.strip()
        if raw:
            self.refs.append({"ref_type": ref_type, "raw": raw, "text": text,
                              "menu": menu if menu is not None else []})

    def _css_urls(self, css, ref_type="css-url"):
        for m in CSS_URL_RE.finditer(css or ""):
            self._add(ref_type, m.group(1))

    def _close_a(self):
        a, self._a = self._a, None
        if not a:
            return
        text = " ".join("".join(a["buf"]).split())[:300]
        if not text and a["alts"]:
            text = "[img] " + " ".join(" ".join(a["alts"]).split())[:280]
        if a["href"] is not None:
            self._add("a", a["href"], text, a["menu"])
        for e in reversed(self._stack):
            if e["t"] == "li":
                if e["label"] is None and text:
                    e["label"] = text[:100]
                break

    # -- parser callbacks
    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v if v is not None else "") for k, v in attrs}
        tag = tag.lower()
        if len(self._stack) > 400:
            self._stack = self._stack[-100:]

        if a.get("style"):
            self._css_urls(a["style"])
        if a.get("onclick"):
            for m in ONCLICK_RE.finditer(a["onclick"]):
                self._add("onclick", m.group(1))
        for k in ("data-href", "data-url", "data-link"):
            if a.get(k):
                self._add("data-href", a[k])
        for k in ("data-bg", "data-background", "data-image", "data-src", "data-original",
                  "data-lazy-src", "data-full", "data-poster"):
            if a.get(k) and tag != "img":
                self._add("data-attr", a[k])

        if tag == "html":
            self.lang = a.get("lang", "")
        elif tag == "base":
            if a.get("href") and self.base is None:
                self.base = a["href"].strip()
        elif tag == "title":
            self._in_title = True
        elif tag == "h1":
            if not self.h1:
                self._in_h1 = True
        elif tag in ("ul", "ol"):
            self._stack.append({"t": "ul"})
        elif tag == "li":
            while self._stack and self._stack[-1]["t"] == "li":
                self._stack.pop()
            self._stack.append({"t": "li", "label": None})
        elif tag == "a":
            if self._a:
                self._close_a()
            self._a = {"href": a.get("href"), "buf": [], "alts": [], "menu": self._menu()}
        elif tag == "area":
            self._add("area", a.get("href"), a.get("alt", ""), self._menu())
        elif tag in ("iframe", "frame"):
            self._add(tag if tag == "frame" else "iframe", a.get("src"))
        elif tag == "img":
            if self._a is not None and a.get("alt"):
                self._a["alts"].append(a["alt"])
            for k in ("src", "data-src", "data-original", "data-lazy-src"):
                self._add("img", a.get(k))
            for k in ("srcset", "data-srcset"):
                if a.get(k):
                    for part in a[k].split(","):
                        tok = part.strip().split(" ")[0]
                        self._add("img-srcset", tok)
        elif tag == "script":
            self._in_script = True
            self._script_buf = []
            self._add("script", a.get("src"))
        elif tag == "style":
            self._in_style = True
            self._style_buf = []
        elif tag == "link":
            rel = a.get("rel", "").lower()
            href = a.get("href")
            if "canonical" in rel:
                self.rel_canonical = (href or "").strip()
                self._add("link-canonical", href)
            elif any(x in rel for x in ("alternate", "next", "prev")) and "stylesheet" not in rel:
                self._add("link-alternate", href)
            elif "stylesheet" in rel:
                self._add("stylesheet", href)
            elif "icon" in rel:
                self._add("icon", href)
            else:
                self._add("link-other", href)
        elif tag in ("source", "track", "embed", "audio"):
            self._add(tag, a.get("src"))
            if a.get("srcset"):
                for part in a["srcset"].split(","):
                    self._add("img-srcset", part.strip().split(" ")[0])
        elif tag == "video":
            self._add("video", a.get("src"))
            self._add("poster", a.get("poster"))
        elif tag == "object":
            self._add("object", a.get("data"))
        elif tag == "input":
            if a.get("type", "").lower() == "image":
                self._add("img", a.get("src"))
        elif tag == "form":
            self._add("form", a.get("action"))
        elif tag == "meta":
            name = (a.get("name") or a.get("property") or "").lower()
            content = a.get("content", "")
            if a.get("http-equiv", "").lower() == "refresh":
                m = re.search(r"url\s*=\s*['\"]?([^'\";]+)", content, re.I)
                if m:
                    self._add("meta-refresh", m.group(1))
            elif name == "description" and not self.description:
                self.description = " ".join(content.split())[:500]
            elif name in ("og:image", "twitter:image", "og:video", "og:audio", "msapplication-tileimage"):
                self._add("meta-image", content)
        if a.get("background") and tag in ("body", "table", "td", "th", "tr"):
            self._add("bg-attr", a["background"])

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() in ("a", "title", "script", "style", "li", "ul", "ol", "h1"):
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "a":
            self._close_a()
        elif tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False
        elif tag in ("ul", "ol"):
            while self._stack:
                if self._stack.pop()["t"] == "ul":
                    break
        elif tag == "li":
            if self._stack and self._stack[-1]["t"] == "li":
                self._stack.pop()
        elif tag == "script":
            self._in_script = False
            code = "".join(self._script_buf)
            for m in SCRIPT_STR_RE.finditer(code):
                self._add("script-ref", m.group(1))
            self._css_urls(code, "script-ref")
        elif tag == "style":
            self._in_style = False
            self._css_urls("".join(self._style_buf))

    def handle_data(self, data):
        if self._in_script:
            self._script_buf.append(data)
            return
        if self._in_style:
            self._style_buf.append(data)
            return
        if self._in_title:
            self.title += data
        if self._in_h1:
            self.h1 += data
        if self._a is not None:
            self._a["buf"].append(data)
        elif data.strip():
            for e in reversed(self._stack):
                if e["t"] == "li":
                    if e["label"] is None:
                        e["label"] = " ".join(data.split())[:100]
                    break
                break


def decode_html(body, ctype):
    if body.startswith(b"\xef\xbb\xbf"):
        return body.decode("utf-8-sig", "replace")
    m = re.search(r"charset=([\w\-]+)", ctype or "", re.I)
    enc = m.group(1) if m else None
    if not enc:
        mm = re.search(rb"<meta[^>]+charset=[\"']?([\w\-]+)", body[:4096], re.I)
        enc = mm.group(1).decode("ascii", "ignore") if mm else None
    for e in (enc, "utf-8", "cp1252"):
        if not e:
            continue
        try:
            return body.decode(e)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Fetcher:
    def __init__(self, delay=1.0, timeout=30, retries=2, insecure=False):
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.last = 0.0
        self.max_bytes = MAX_HTML_BYTES
        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ctx))

    def _wait(self):
        w = self.last + self.delay - time.time()
        if w > 0:
            time.sleep(w)
        self.last = time.time()

    @staticmethod
    def _classify(e):
        r = getattr(e, "reason", e)
        if isinstance(r, (socket.timeout, TimeoutError)) or "timed out" in str(r).lower():
            return "TIMEOUT"
        if isinstance(r, socket.gaierror):
            return "DNS"
        if isinstance(r, ssl.SSLError) or "certificate" in str(r).lower():
            return "SSL"
        if isinstance(r, ConnectionError):
            return "CONNECTION"
        return "OTHER"

    def request(self, url, method="GET", want_body=False, extra_headers=None):
        hdrs = {"User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "en",
                "Accept-Encoding": "gzip, deflate"}
        if extra_headers:
            hdrs.update(extra_headers)
        safe = urllib.parse.quote(url, safe="%/:=&?~#+!$,;'@()*[]")
        last_err, last_type, attempt = None, None, 0
        for attempt in range(1, self.retries + 2):
            self._wait()
            t0 = time.time()
            try:
                req = urllib.request.Request(safe, headers=hdrs, method=method)
                try:
                    resp = self.opener.open(req, timeout=self.timeout)
                except urllib.error.HTTPError as e:
                    resp = e
                status = resp.getcode()
                headers = {k.lower(): v for k, v in resp.headers.items()}
                body, truncated = None, False
                if method != "HEAD" and want_body and (not callable(want_body) or want_body(status, headers)):
                    raw = resp.read(self.max_bytes + 1)
                    if len(raw) > self.max_bytes:
                        raw, truncated = raw[: self.max_bytes], True
                    enc = headers.get("content-encoding", "").lower()
                    try:
                        if enc == "gzip":
                            raw = gzip.decompress(raw)
                        elif enc == "deflate":
                            try:
                                raw = zlib.decompress(raw)
                            except zlib.error:
                                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                    except Exception:
                        pass
                    body = raw
                resp.close()
                elapsed = int((time.time() - t0) * 1000)
                if status in RETRY_STATUS and attempt <= self.retries:
                    wait = 2 ** attempt
                    ra = headers.get("retry-after", "")
                    if ra.isdigit():
                        wait = min(int(ra), 120)
                    if status in (429, 503):
                        self.delay = min(self.delay * 2, 8.0)
                    time.sleep(wait)
                    continue
                return dict(status=status, headers=headers, body=body, truncated=truncated,
                            error=None, error_type=None, attempts=attempt, elapsed_ms=elapsed)
            except Exception as e:  # network-level failure
                last_err, last_type = f"{type(e).__name__}: {e}", self._classify(e)
                if attempt <= self.retries:
                    time.sleep(2 ** attempt)
                    continue
        return dict(status=None, headers={}, body=None, truncated=False, error=last_err,
                    error_type=last_type, attempts=attempt, elapsed_ms=0)


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS urls(
  id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT UNIQUE NOT NULL, base_path TEXT, raw_first TEXT,
  status TEXT NOT NULL, depth INTEGER, parent TEXT, via TEXT, discovered_at TEXT);
CREATE INDEX IF NOT EXISTS idx_urls_status ON urls(status, depth, id);
CREATE INDEX IF NOT EXISTS idx_urls_base ON urls(base_path);
CREATE TABLE IF NOT EXISTS pages(
  url TEXT PRIMARY KEY, canonical_url TEXT, final_url TEXT, first_status INTEGER, http_status INTEGER,
  title TEXT, content_type TEXT, content_length INTEGER, crawled_at TEXT, internal_links INTEGER,
  external_links INTEGER, assets INTEGER, crawl_status TEXT, html_path TEXT, sha256 TEXT,
  rel_canonical TEXT, meta_description TEXT, h1 TEXT, lang TEXT, attempts INTEGER, error TEXT, fetch_ms INTEGER);
CREATE TABLE IF NOT EXISTS files(path_lower TEXT PRIMARY KEY, url TEXT);
CREATE TABLE IF NOT EXISTS links(
  id INTEGER PRIMARY KEY AUTOINCREMENT, source_url TEXT, dest_url TEXT, dest_raw TEXT, scope TEXT,
  link_type TEXT, dest_kind TEXT, anchor_text TEXT, menu_path TEXT,
  UNIQUE(source_url, dest_url, link_type, anchor_text));
CREATE INDEX IF NOT EXISTS idx_links_dest ON links(dest_url);
CREATE INDEX IF NOT EXISTS idx_links_src ON links(source_url);
CREATE TABLE IF NOT EXISTS external(
  id INTEGER PRIMARY KEY AUTOINCREMENT, source_url TEXT, dest_url TEXT, domain TEXT, link_type TEXT,
  anchor_text TEXT, UNIQUE(source_url, dest_url, link_type));
CREATE TABLE IF NOT EXISTS assets(
  id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT, source_url TEXT, ref_type TEXT, asset_type TEXT,
  extension TEXT, filename TEXT, scope TEXT, content_type TEXT, downloadable INTEGER,
  UNIQUE(url, source_url, ref_type));
CREATE INDEX IF NOT EXISTS idx_assets_url ON assets(url);
CREATE TABLE IF NOT EXISTS asset_checks(
  url TEXT PRIMARY KEY, status INTEGER, content_type TEXT, content_length TEXT, final_url TEXT,
  error TEXT, note TEXT, checked_at TEXT);
CREATE TABLE IF NOT EXISTS errors(
  id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT, source_url TEXT, error_type TEXT, status INTEGER,
  message TEXT, attempts INTEGER, ts TEXT);
CREATE TABLE IF NOT EXISTS redirects(
  original_url TEXT PRIMARY KEY, final_url TEXT, first_status INTEGER, statuses TEXT, hops INTEGER,
  chain TEXT, final_status INTEGER, final_scope TEXT, kind TEXT, discovered_from TEXT, ts TEXT);
"""


def write_csv(path, header, rows):
    n = 0
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(r)
            n += 1
    return n


# --------------------------------------------------------------------------- #
# Crawler
# --------------------------------------------------------------------------- #
class Crawler:
    def __init__(self, args):
        self.args = args
        self.root = Path(__file__).resolve().parent
        self.data = self.root / "data"
        self.htmldir = self.root / "html"
        self.reports = self.root / "reports"
        for d in (self.data, self.htmldir, self.reports):
            d.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.data / "crawl.db"))
        self.db.executescript(SCHEMA)
        self.fetcher = Fetcher(delay=args.delay, timeout=args.timeout, retries=args.retries,
                               insecure=args.insecure)
        self.robots = None

    # ---- small db helpers
    def q1(self, sql, *a):
        return self.db.execute(sql, a).fetchone()[0]

    def meta_get(self, k, default=None):
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
        return r[0] if r else default

    def meta_set(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (k, str(v)))

    def add_error(self, url, source, etype, status, msg, attempts):
        self.db.execute("INSERT INTO errors(url,source_url,error_type,status,message,attempts,ts) VALUES(?,?,?,?,?,?,?)",
                        (url, source, etype, status, (msg or "")[:500], attempts, utcnow()))

    def set_url_status(self, url, status, depth=0, parent=None, via="redirect"):
        self.db.execute(
            "INSERT OR IGNORE INTO urls(url,base_path,raw_first,status,depth,parent,via,discovered_at) VALUES(?,?,?,?,?,?,?,?)",
            (url, url.split("?")[0], url, status, depth, parent, via, utcnow()))
        self.db.execute("UPDATE urls SET status=? WHERE url=?", (status, url))

    def url_finished(self, url):
        r = self.db.execute("SELECT status FROM urls WHERE url=?", (url,)).fetchone()
        return bool(r) and r[0] not in ("pending", "skipped_depth")

    # ---- queueing
    def enqueue(self, url, depth, parent, via, raw):
        if self.db.execute("SELECT 1 FROM urls WHERE url=?", (url,)).fetchone():
            return
        status, reason = "pending", None
        reason = suspicious_reason(url)
        if not reason and "?" in url:
            base = url.split("?")[0]
            n = self.q1("SELECT COUNT(*) FROM urls WHERE base_path=? AND url LIKE '%?%'", base)
            if n >= MAX_QUERY_VARIANTS_PER_PATH:
                reason = f"more than {MAX_QUERY_VARIANTS_PER_PATH} query-string variants of one path"
        if reason:
            status = "skipped_suspicious"
            self.add_error(url, parent, "SKIPPED_SUSPICIOUS", None, reason, 0)
        elif depth > self.args.max_depth:
            status = "skipped_depth"
        self.db.execute(
            "INSERT INTO urls(url,base_path,raw_first,status,depth,parent,via,discovered_at) VALUES(?,?,?,?,?,?,?,?)",
            (url, url.split("?")[0], raw, status, depth, parent, via, utcnow()))

    # ---- setup: robots, host variants, sitemaps, seeds
    def setup(self):
        self.load_robots()
        if self.meta_get("variants_checked") != "1":
            log("Checking http/https and www/non-www host variants (redirect inventory)...")
            for v in HOST_VARIANTS:
                out = self.fetch_chain(v, "GET", False, "asset")
                if out["chain"]:
                    self.add_redirect(v, out, "HOST_VARIANT", None)
                else:
                    res = out["res"]
                    self.add_error(v, None, "HOST_VARIANT_NO_REDIRECT",
                                   res["status"] if res else None,
                                   "Host variant did not redirect to canonical https://www.rmd.ac.in/", 1)
            self.meta_set("variants_checked", "1")
            self.db.commit()
        self.enqueue(SEED, 0, None, "seed", SEED)
        if self.meta_get("sitemap_checked") != "1":
            self.load_sitemaps()
            self.meta_set("sitemap_checked", "1")
        # raising --max-depth re-opens previously depth-skipped URLs
        self.db.execute("UPDATE urls SET status='pending' WHERE status='skipped_depth' AND depth<=?",
                        (self.args.max_depth,))
        self.db.commit()

    def load_robots(self):
        res = self.fetcher.request(ROBOTS_URL, "GET", lambda s, h: 200 <= s < 300)
        if res["error"]:
            self.meta_set("robots", f"ERROR fetching robots.txt: {res['error']}")
            self.db.commit()
            raise SystemExit(f"Cannot fetch robots.txt ({res['error']}); refusing to crawl blind. Retry later.")
        st = res["status"]
        if 200 <= st < 300:
            text = decode_html(res["body"] or b"", res["headers"].get("content-type"))
            (self.reports / "robots.txt").write_text(text, encoding="utf-8")
            rp = RobotFileParser()
            rp.parse(text.splitlines())
            self.robots = rp
            cd = rp.crawl_delay(ROBOT_TOKEN)
            if cd:
                self.fetcher.delay = max(self.fetcher.delay, float(cd))
            self.meta_set("robots", f"FOUND (HTTP {st}); saved to reports/robots.txt")
            self.meta_set("robots_sitemaps", json.dumps(rp.site_maps() or []))
            log(f"robots.txt found (HTTP {st}); rules will be honoured.")
        elif 400 <= st < 500:
            self.robots = None
            self.meta_set("robots", f"NOT FOUND (HTTP {st}) - no robots.txt restrictions apply")
            log(f"robots.txt not present (HTTP {st}) -> no robots restrictions.")
        else:
            self.meta_set("robots", f"SERVER ERROR (HTTP {st})")
            self.db.commit()
            raise SystemExit(f"robots.txt returned HTTP {st}; refusing to crawl. Retry later.")
        self.db.commit()

    def load_sitemaps(self):
        todo = [f"https://{CANON_HOST}/sitemap.xml"]
        todo += json.loads(self.meta_get("robots_sitemaps", "[]"))
        seen, found = set(), 0
        while todo and len(seen) < 20:
            sm = todo.pop(0)
            if sm in seen:
                continue
            seen.add(sm)
            kind, nu = normalize(sm)
            if kind != "internal":
                continue
            res = self.fetcher.request(nu, "GET", lambda s, h: 200 <= s < 300)
            if res["error"] or not (res["status"] and 200 <= res["status"] < 300) or not res["body"]:
                continue
            text = decode_html(res["body"], res["headers"].get("content-type"))
            for loc in re.findall(r"<loc>\s*(.*?)\s*</loc>", text, re.S | re.I):
                loc = loc.replace("&amp;", "&")
                k, u = normalize(loc)
                if k != "internal":
                    continue
                if u.lower().endswith(".xml") and "<sitemapindex" in text.lower():
                    todo.append(u)
                elif not asset_type_for(u):
                    self.enqueue(u, 1, "sitemap.xml", "sitemap", loc)
                    found += 1
            log(f"sitemap {nu}: parsed ({found} page URLs so far)")
        self.meta_set("sitemap_urls_found", found)

    # ---- fetching
    @staticmethod
    def _want_html(status, headers):
        if not (200 <= status < 300):
            return False
        ct = headers.get("content-type", "").lower()
        return (not ct) or "html" in ct

    def fetch_chain(self, start, method, want_body, mode):
        chain, seen, cur = [], {start}, start
        for _ in range(MAX_REDIRECTS + 1):
            res = self.fetcher.request(cur, method, want_body)
            if res["error"]:
                return dict(chain=chain, terminal="net_error", url=cur, res=res)
            st, loc = res["status"], res["headers"].get("location")
            if 300 <= st < 400 and st != 304 and loc:
                nxt = urllib.parse.urljoin(cur, loc.strip().replace("\\", "/"))
                chain.append(dict(frm=cur, to=nxt, status=st))
                kind, nn = normalize(nxt)
                if kind != "internal":
                    return dict(chain=chain, terminal="external" if kind == "external" else "invalid",
                                url=nxt, res=res)
                if nxt in seen:
                    return dict(chain=chain, terminal="loop", url=nxt, res=res)
                seen.add(nxt)
                if mode == "page":
                    if self.url_finished(nn):
                        return dict(chain=chain, terminal="known", url=nxt, final_norm=nn, res=res)
                    if asset_type_for(nn):
                        return dict(chain=chain, terminal="asset", url=nxt, final_norm=nn, res=res)
                    if self.robots and not self.robots.can_fetch(ROBOT_TOKEN, nxt):
                        return dict(chain=chain, terminal="robots", url=nxt, final_norm=nn, res=res)
                cur = nxt
                continue
            return dict(chain=chain, terminal="response", url=cur, res=res)
        return dict(chain=chain, terminal="too_many_redirects", url=cur, res=out_res(res))

    def add_redirect(self, original, out, kind, discovered_from):
        chain = out["chain"]
        if not chain:
            return
        res = out.get("res")
        final_status = res["status"] if (res and out["terminal"] in ("response",)) else None
        if out["terminal"] == "known":
            r = self.db.execute("SELECT http_status FROM pages WHERE url=?", (out.get("final_norm"),)).fetchone()
            final_status = r[0] if r else None
        scope = {"external": "external"}.get(out["terminal"], "internal")
        text = " -> ".join([f"{chain[0]['frm']}"] + [f"[{c['status']}] {c['to']}" for c in chain])
        self.db.execute(
            "INSERT OR REPLACE INTO redirects VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (original, chain[-1]["to"], chain[0]["status"], ",".join(str(c["status"]) for c in chain),
             len(chain), text, final_status, scope, kind, discovered_from, utcnow()))

    # ---- crawl one URL
    def crawl_one(self, url, depth, parent):
        if self.robots and not self.robots.can_fetch(ROBOT_TOKEN, url):
            self.set_url_status(url, "robots_blocked")
            self.add_error(url, parent, "ROBOTS_BLOCKED", None, "Disallowed by robots.txt", 0)
            self.db.commit()
            return "robots"
        out = self.fetch_chain(url, "GET", self._want_html, "page")
        result = self.record_outcome(url, depth, parent, out)
        self.db.commit()
        return result

    def _page_row(self, url, **kw):
        cols = ["canonical_url", "final_url", "first_status", "http_status", "title", "content_type",
                "content_length", "crawled_at", "internal_links", "external_links", "assets",
                "crawl_status", "html_path", "sha256", "rel_canonical", "meta_description", "h1",
                "lang", "attempts", "error", "fetch_ms"]
        vals = [kw.get(c) for c in cols]
        self.db.execute(f"INSERT OR REPLACE INTO pages(url,{','.join(cols)}) VALUES(?,{','.join('?' * len(cols))})",
                        [url] + vals)

    def record_outcome(self, url, depth, parent, out):
        chain, term, res = out["chain"], out["terminal"], out["res"]
        now = utcnow()
        first_status = chain[0]["status"] if chain else (res["status"] if res else None)
        attempts = res["attempts"] if res else 0
        if chain:
            self.add_redirect(url, out, "PAGE", parent)
        final_raw = out["url"]
        kind, fnorm = normalize(final_raw)
        if term == "known" or term in ("asset", "robots"):
            fnorm = out.get("final_norm", fnorm)

        def origin_row(crawl_status, fstatus, err=None):
            self.set_url_status(url, "redirect")
            self._page_row(url, canonical_url=fnorm if kind == "internal" else final_raw,
                           final_url=chain[-1]["to"] if chain else final_raw, first_status=first_status,
                           http_status=fstatus, crawled_at=now, crawl_status=crawl_status,
                           attempts=attempts, error=err)

        if term in ("external", "invalid"):
            origin_row("REDIRECT_EXTERNAL", None)
            self.db.execute("INSERT OR IGNORE INTO external(source_url,dest_url,domain,link_type,anchor_text) VALUES(?,?,?,?,?)",
                            (parent or url, chain[-1]["to"], urllib.parse.urlsplit(chain[-1]["to"]).hostname or "",
                             "redirect", f"redirect from {url}"))
            return "ok"
        if term == "asset":
            origin_row("REDIRECT_TO_ASSET", None)
            self._record_asset(fnorm, parent or url, "redirect", "internal")
            return "ok"
        if term == "known":
            r = self.db.execute("SELECT http_status FROM pages WHERE url=?", (fnorm,)).fetchone()
            origin_row("REDIRECT", r[0] if r else None)
            return "ok"
        if term == "robots":
            origin_row("REDIRECT", None)
            self.set_url_status(fnorm, "robots_blocked", depth, parent)
            self.add_error(fnorm, url, "ROBOTS_BLOCKED", None, "Redirect target disallowed by robots.txt", 0)
            return "robots"
        if term in ("loop", "too_many_redirects"):
            self.set_url_status(url, "error")
            self._page_row(url, canonical_url=url, final_url=final_raw, first_status=first_status,
                           crawled_at=now, crawl_status="REDIRECT_ERROR", attempts=attempts, error=term)
            self.add_error(url, parent, term.upper(), first_status, f"{term} after {len(chain)} hops", attempts)
            return "error"

        # --- a real response (or network error) at the end of the chain
        target = fnorm if kind == "internal" else url
        if chain:
            origin_row("REDIRECT", res["status"] if res and not res["error"] else None)
            self.set_url_status(target, "pending", depth, parent)  # make sure the row exists

        if term == "net_error":
            self.set_url_status(target, "error", depth, parent)
            self._page_row(target, canonical_url=target, final_url=final_raw, first_status=first_status,
                           crawled_at=now, crawl_status="NETWORK_ERROR", attempts=attempts, error=res["error"])
            self.add_error(target, parent, res["error_type"] or "NETWORK", None, res["error"], attempts)
            return "net_error"

        st = res["status"]
        ct = res["headers"].get("content-type", "")
        cl = res["headers"].get("content-length")
        if 200 <= st < 300:
            body = res["body"]
            is_html = body is not None and ("html" in ct.lower() or (
                not ct and re.search(rb"<!doctype html|<html", body[:3000], re.I)))
            if is_html:
                self.process_page(target, depth, parent, final_raw, res, first_status)
                return "ok"
            self.set_url_status(target, "nonhtml", depth, parent)
            self._page_row(target, canonical_url=target, final_url=final_raw, first_status=first_status,
                           http_status=st, content_type=ct, content_length=int(cl) if cl and cl.isdigit() else None,
                           crawled_at=now, crawl_status="NOT_HTML", attempts=attempts)
            self._record_asset(target, parent or url, "link-content-type", "internal", ct)
            return "ok"
        # HTTP error
        self.set_url_status(target, "error", depth, parent)
        self._page_row(target, canonical_url=target, final_url=final_raw, first_status=first_status,
                       http_status=st, content_type=ct, crawled_at=now, crawl_status="HTTP_ERROR",
                       attempts=attempts, error=f"HTTP {st}")
        self.add_error(target, parent, f"HTTP_{st}", st, f"HTTP {st} for {final_raw}", attempts)
        return "error"

    def _record_asset(self, url, source, ref_type, scope, ctype=None):
        ext = url_ext(url)
        atype = ASSET_EXT.get(ext) or (asset_type_from_ctype(ctype) if ctype else "OTHER")
        self.db.execute(
            "INSERT OR IGNORE INTO assets(url,source_url,ref_type,asset_type,extension,filename,scope,content_type,downloadable) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (url, source, ref_type, atype, ext, url_filename(url), scope, ctype, 1))

    # ---- process a successfully fetched HTML page
    def map_file(self, url):
        rel = url_to_relpath(url)
        low = rel.lower()
        r = self.db.execute("SELECT url FROM files WHERE path_lower=?", (low,)).fetchone()
        if r and r[0] != url:
            stem, dot, ext = rel.rpartition(".")
            rel = f"{stem}__{hashlib.sha1(url.encode()).hexdigest()[:8]}.{ext}"
            low = rel.lower()
        self.db.execute("INSERT OR REPLACE INTO files VALUES(?,?)", (low, url))
        return rel

    def process_page(self, norm, depth, parent, final_raw, res, first_status):
        body = res["body"]
        ct = res["headers"].get("content-type", "")
        sha = hashlib.sha256(body).hexdigest()
        rel = self.map_file(norm)
        dest = self.htmldir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(body)  # ORIGINAL bytes, untouched
        os.replace(tmp, dest)

        parser = PageParser()
        try:
            parser.feed(decode_html(body, ct))
            parser.close()
        except Exception as e:
            self.add_error(norm, parent, "PARSE_WARNING", None, f"{type(e).__name__}: {e}", 1)
        base = final_raw
        if parser.base:
            base = urllib.parse.urljoin(final_raw, parser.base)

        for t in ("links", "external", "assets"):
            self.db.execute(f"DELETE FROM {t} WHERE source_url=?", (norm,))
        n_int = n_ext = n_assets = 0
        for r in parser.refs:
            rt, raw = r["ref_type"], r["raw"]
            kind, u = normalize(raw, base)
            if kind in ("empty",):
                continue
            is_page_ref = rt in PAGE_REFS
            menu = json.dumps(r["menu"], ensure_ascii=False) if r["menu"] else ""
            if kind in ("fragment", "nonhttp", "invalid"):
                if is_page_ref:
                    c = self.db.execute(
                        "INSERT OR IGNORE INTO links(source_url,dest_url,dest_raw,scope,link_type,dest_kind,anchor_text,menu_path) VALUES(?,?,?,?,?,?,?,?)",
                        (norm, u, raw, kind, rt, "", r["text"], menu))
                continue
            atype = asset_type_for(u)
            scope = kind
            if (not is_page_ref) or atype:
                before = self.db.total_changes
                ext = url_ext(u)
                self.db.execute(
                    "INSERT OR IGNORE INTO assets(url,source_url,ref_type,asset_type,extension,filename,scope,content_type,downloadable) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (u, norm, rt, atype or "OTHER", ext, url_filename(u), scope, None,
                     1 if (atype or scope == "internal") else 0))
                n_assets += self.db.total_changes - before
            if is_page_ref:
                before = self.db.total_changes
                self.db.execute(
                    "INSERT OR IGNORE INTO links(source_url,dest_url,dest_raw,scope,link_type,dest_kind,anchor_text,menu_path) VALUES(?,?,?,?,?,?,?,?)",
                    (norm, u, raw, scope, rt, "ASSET" if atype else "PAGE", r["text"], menu))
                added = self.db.total_changes - before
                if scope == "internal":
                    n_int += added
                else:
                    n_ext += added
            if scope == "external":
                self.db.execute(
                    "INSERT OR IGNORE INTO external(source_url,dest_url,domain,link_type,anchor_text) VALUES(?,?,?,?,?)",
                    (norm, u, urllib.parse.urlsplit(u).hostname or "", rt, r["text"]))
            if scope == "internal" and rt in CRAWL_REFS and not atype:
                self.enqueue(u, depth + 1, norm, rt, raw)

        self.set_url_status(norm, "done", depth, parent)
        cl = res["headers"].get("content-length")
        self._page_row(
            norm, canonical_url=norm, final_url=final_raw, first_status=first_status,
            http_status=res["status"], title=" ".join(parser.title.split()), content_type=ct,
            content_length=len(body) if body else (int(cl) if cl and cl.isdigit() else None),
            crawled_at=utcnow(), internal_links=n_int, external_links=n_ext, assets=n_assets,
            crawl_status="OK_TRUNCATED" if res["truncated"] else "OK",
            html_path="html/" + rel, sha256=sha, rel_canonical=parser.rel_canonical,
            meta_description=parser.description, h1=" ".join(parser.h1.split())[:300], lang=parser.lang,
            attempts=res["attempts"], fetch_ms=res["elapsed_ms"])

    # ---- main loop
    def run(self):
        a = self.args
        if a.retry_failed:
            rows = self.db.execute(
                "SELECT url FROM pages WHERE crawl_status='NETWORK_ERROR' OR http_status>=500 OR http_status=429").fetchall()
            for (u,) in rows:
                self.db.execute("UPDATE urls SET status='pending' WHERE url=? AND status='error'", (u,))
                self.db.execute("DELETE FROM errors WHERE url=?", (u,))
            self.db.commit()
            log(f"--retry-failed: re-queued {len(rows)} transient failures.")
        self.setup()
        stop = "QUEUE_EXHAUSTED"
        fetched = self.q1("SELECT COUNT(*) FROM urls WHERE status IN ('done','error','nonhtml','redirect')")
        consecutive_fail = 0
        t_start = time.time()
        crawled_now = 0
        try:
            while True:
                row = self.db.execute(
                    "SELECT url,depth,parent FROM urls WHERE status='pending' ORDER BY depth,id LIMIT 1").fetchone()
                if not row:
                    break
                if fetched >= a.max_pages:
                    stop = "MAX_PAGES_REACHED"
                    break
                if consecutive_fail >= a.max_consecutive_failures:
                    stop = "TOO_MANY_CONSECUTIVE_NETWORK_FAILURES"
                    break
                url, depth, parent = row
                r = self.crawl_one(url, depth, parent)
                fetched += 1
                crawled_now += 1
                consecutive_fail = consecutive_fail + 1 if r == "net_error" else 0
                pending = self.q1("SELECT COUNT(*) FROM urls WHERE status='pending'")
                st = self.db.execute("SELECT crawl_status,http_status FROM pages WHERE url=?", (url,)).fetchone()
                log(f"[{fetched}] d={depth} {st[0] if st else r}/{st[1] if st else ''} queue={pending} {url}")
        except KeyboardInterrupt:
            stop = "INTERRUPTED"
            self.db.commit()
            log("\nInterrupted - progress saved; re-run to resume.")
        self.meta_set("stop_reason", stop)
        self.meta_set("last_run_finished", utcnow())
        self.meta_set("last_run_pages", crawled_now)
        self.db.commit()
        if a.check_assets and stop == "QUEUE_EXHAUSTED":
            self.check_assets()
        self.export()
        return stop

    # ---- optional: HEAD-check internal assets (no body downloaded)
    def check_assets(self):
        rows = self.db.execute(
            "SELECT DISTINCT url FROM assets WHERE scope='internal' AND url NOT IN (SELECT url FROM asset_checks) ORDER BY url"
        ).fetchall()
        log(f"Checking {len(rows)} internal asset URLs with HEAD requests (nothing is downloaded)...")
        try:
            for i, (u,) in enumerate(rows, 1):
                if self.robots and not self.robots.can_fetch(ROBOT_TOKEN, u):
                    self.db.execute("INSERT OR REPLACE INTO asset_checks VALUES(?,?,?,?,?,?,?,?)",
                                    (u, None, None, None, None, "robots.txt disallows", "ROBOTS_BLOCKED", utcnow()))
                    continue
                out = self.fetch_chain(u, "HEAD", False, "asset")
                res = out["res"]
                if out["terminal"] == "response" and res["status"] in (405, 501):
                    out = self.fetch_chain(u, "GET", False, "asset")  # headers only; body never read
                    res = out["res"]
                note = ""
                if out["chain"]:
                    self.add_redirect(u, out, "ASSET", None)
                if res and not res["error"] and out["terminal"] == "response":
                    ct = res["headers"].get("content-type", "")
                    atype = asset_type_for(u)
                    if atype in ("PDF", "IMAGE", "VIDEO", "AUDIO", "ARCHIVE", "DOCUMENT", "SPREADSHEET",
                                 "PRESENTATION", "FONT") and 200 <= res["status"] < 300 and "html" in ct.lower():
                        note = "CONTENT_TYPE_MISMATCH(html returned for asset URL)"
                    self.db.execute("INSERT OR REPLACE INTO asset_checks VALUES(?,?,?,?,?,?,?,?)",
                                    (u, res["status"], ct, res["headers"].get("content-length"),
                                     out["url"] if out["chain"] else "", "", note, utcnow()))
                    self.db.execute("UPDATE assets SET content_type=? WHERE url=?", (ct, u))
                else:
                    err = res["error"] if res and res["error"] else out["terminal"]
                    self.db.execute("INSERT OR REPLACE INTO asset_checks VALUES(?,?,?,?,?,?,?,?)",
                                    (u, res["status"] if res else None, None, None, "", err,
                                     out["terminal"].upper(), utcnow()))
                self.db.commit()
                if i % 25 == 0:
                    log(f"  asset check {i}/{len(rows)}")
        except KeyboardInterrupt:
            log("\nAsset check interrupted - progress saved.")
        self.db.commit()

    # ------------------------------------------------------------------ #
    # Export & reports
    # ------------------------------------------------------------------ #
    def export(self):
        db, d = self.db, self.data
        # pages.csv
        rows = db.execute("""
            SELECT COALESCE(p.canonical_url,u.url), u.url, u.raw_first, p.http_status, p.final_url, p.title, u.depth,
                   u.parent, p.content_type, p.content_length, p.crawled_at, p.internal_links, p.external_links,
                   p.assets, COALESCE(p.crawl_status, UPPER(u.status)), p.first_status, p.html_path, p.sha256,
                   p.rel_canonical, p.meta_description, p.h1, p.lang, u.via
            FROM urls u LEFT JOIN pages p ON p.url=u.url ORDER BY u.id""")
        write_csv(d / "pages.csv",
                  ["canonical_url", "original_url", "raw_href_first_seen", "http_status", "final_url", "title", "depth",
                   "parent_url", "content_type", "content_length", "crawl_timestamp", "internal_links_count",
                   "external_links_count", "assets_count", "crawl_status", "first_response_status", "html_path",
                   "content_sha256", "rel_canonical", "meta_description", "h1", "lang", "discovered_via"], rows)
        # links.csv
        rows = db.execute("""
            SELECT l.source_url, l.dest_url, l.dest_raw, l.scope, l.link_type, l.dest_kind,
                   COALESCE(CASE WHEN l.dest_kind='ASSET' THEN c.status ELSE p.http_status END,''),
                   l.anchor_text, l.menu_path
            FROM links l LEFT JOIN pages p ON p.url=l.dest_url AND l.scope='internal' AND l.dest_kind='PAGE'
                         LEFT JOIN asset_checks c ON c.url=l.dest_url AND l.dest_kind='ASSET'
            ORDER BY l.id""")
        write_csv(d / "links.csv", ["source_url", "destination_url", "destination_raw", "scope", "link_type",
                                    "destination_kind", "http_status_if_checked", "anchor_text", "menu_path"], rows)
        # external-links.csv
        rows = db.execute("SELECT source_url,dest_url,domain,link_type,anchor_text FROM external ORDER BY id").fetchall()
        write_csv(d / "external-links.csv",
                  ["source_url", "destination_url", "domain", "is_rmd_subdomain", "link_type", "anchor_text",
                   "looks_like_asset", "asset_type"],
                  [(s, u, dom, "yes" if dom.endswith(".rmd.ac.in") or dom == "rmd.ac.in" else "no", t, a,
                    "yes" if asset_type_for(u) else "no", asset_type_for(u) or "") for s, u, dom, t, a in rows])
        # assets.csv
        rows = db.execute("""
            SELECT a.url, a.source_url, a.ref_type, a.asset_type, a.extension, a.filename, a.scope,
                   COALESCE(c.content_type,a.content_type,''), a.downloadable, COALESCE(c.status,''),
                   COALESCE(c.content_length,''), COALESCE(c.note,'')
            FROM assets a LEFT JOIN asset_checks c ON c.url=a.url ORDER BY a.id""")
        write_csv(d / "assets.csv",
                  ["asset_url", "source_page", "reference_type", "asset_type", "extension", "filename", "scope",
                   "content_type", "appears_downloadable", "http_status_if_checked", "content_length_if_known",
                   "check_note"], rows)
        # errors.csv
        write_csv(d / "errors.csv", ["url", "source_url", "error_type", "http_status", "message", "attempts", "timestamp"],
                  db.execute("SELECT url,source_url,error_type,status,message,attempts,ts FROM errors ORDER BY id"))
        # redirects.csv
        write_csv(d / "redirects.csv",
                  ["original_url", "final_url", "first_status", "all_statuses", "hops", "chain", "final_status",
                   "final_scope", "kind", "discovered_from", "timestamp"],
                  db.execute("SELECT original_url,final_url,first_status,statuses,hops,chain,final_status,final_scope,kind,discovered_from,ts FROM redirects ORDER BY original_url"))
        # PHASE-2-ASSETS.csv  (internal, downloadable, not confirmed broken)
        rows = db.execute("""
            SELECT a.url, MIN(a.asset_type), MIN(a.extension), MIN(a.filename), COALESCE(MAX(c.content_type),''),
                   COALESCE(MAX(c.content_length),''), COALESCE(MAX(c.status),''), COALESCE(MAX(c.note),''),
                   COUNT(DISTINCT a.source_url), MIN(a.source_url), GROUP_CONCAT(DISTINCT a.ref_type)
            FROM assets a LEFT JOIN asset_checks c ON c.url=a.url
            WHERE a.scope='internal' AND a.downloadable=1
              AND (c.url IS NULL OR (c.error='' AND c.status<400 AND c.note NOT LIKE 'ROBOTS%'))
            GROUP BY a.url ORDER BY MIN(a.asset_type), a.url""").fetchall()
        write_csv(self.root / "PHASE-2-ASSETS.csv",
                  ["asset_url", "asset_type", "extension", "filename", "url_path", "content_type", "content_length",
                   "http_status_if_checked", "check_note", "source_page_count", "first_source_page", "reference_types"],
                  [(r[0], r[1], r[2], r[3], urllib.parse.urlsplit(r[0]).path) + r[4:] for r in rows])
        self.write_broken_links()
        self.write_site_tree()
        summary = self.build_summary()
        (self.reports / "crawl-summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False),
                                                         encoding="utf-8")
        self.print_final(summary)
        return summary

    def broken_destinations(self):
        """Return list of (dest_url, status_label, kind) for broken internal destinations."""
        out = []
        for url, st, ct, err in self.db.execute(
                "SELECT p.url,p.http_status,p.crawl_status,p.error FROM pages p JOIN urls u ON u.url=p.url "
                "WHERE u.status='error'"):
            out.append((url, st if st else (err or ct), "PAGE"))
        for url, st, err, note in self.db.execute(
                "SELECT url,status,error,note FROM asset_checks WHERE (status>=400 OR (error!='' AND note!='ROBOTS_BLOCKED'))"):
            out.append((url, st if st else err, "ASSET"))
        return out

    def write_broken_links(self):
        lines = ["BROKEN LINKS (generated from crawl database)", "=" * 60, ""]
        broken = self.broken_destinations()
        lines.append(f"{len(broken)} broken internal destination URL(s)\n")
        total_occ = 0
        for url, label, kind in sorted(broken, key=lambda x: (str(x[1]), x[0])):
            srcs = self.db.execute(
                "SELECT source_url,link_type,anchor_text,dest_raw FROM links WHERE dest_url=? "
                "UNION SELECT source_url,ref_type,'',url FROM assets WHERE url=?", (url, url)).fetchall()
            total_occ += len(srcs)
            lines.append(f"[{label}] ({kind}) {url}")
            lines.append(f"    referenced {len(srcs)} time(s)")
            for s, t, a, raw in srcs[:25]:
                lines.append(f"      - from {s}  <{t}> text={a!r} href={raw!r}")
            if len(srcs) > 25:
                lines.append(f"      ... and {len(srcs) - 25} more")
            lines.append("")
        suspects = self.db.execute("SELECT url,note FROM asset_checks WHERE note LIKE 'CONTENT_TYPE_MISMATCH%'").fetchall()
        if suspects:
            lines += ["", "SUSPECT ASSETS (HTTP 200 but HTML returned - likely soft-404)", "-" * 60]
            lines += [f"  {u}" for u, _ in suspects]
        rb = self.db.execute("SELECT url,message FROM errors WHERE error_type='ROBOTS_BLOCKED'").fetchall()
        if rb:
            lines += ["", "BLOCKED BY robots.txt", "-" * 60] + [f"  {u}" for u, _ in rb]
        lines += ["", "NOTE: external links are intentionally not checked in Phase 1.",
                  f"Total broken-link occurrences: {total_occ}"]
        (self.reports / "broken-links.txt").write_text("\n".join(lines), encoding="utf-8")

    # ---- site tree
    @staticmethod
    def _print_tree(node, prefix, out, cap):
        items = list(node["children"].items())
        for i, (label, child) in enumerate(items):
            if len(out) >= cap:
                out.append(prefix + "... (truncated)")
                return
            last = i == len(items) - 1
            out.append(f"{prefix}{'└── ' if last else '├── '}{child['text'] if 'text' in child else label}")
            Crawler._print_tree(child, prefix + ("    " if last else "│   "), out, cap)

    def write_site_tree(self):
        out = []
        pinfo = {u: (t, s) for u, t, s in self.db.execute("SELECT url,title,http_status FROM pages WHERE crawl_status LIKE 'OK%'")}
        # A) navigation tree from homepage menu structure
        out += ["A) NAVIGATION TREE (homepage menu structure, from <ul>/<li> nesting of the crawled homepage)",
                "   [code] = destination did not return HTTP 200", ""]
        root = {"children": OrderedDict()}
        stat = {u: s for u, s in self.db.execute("SELECT url,http_status FROM pages")}
        for dest, raw, scope, text, menu, kind in self.db.execute(
                "SELECT dest_url,dest_raw,scope,anchor_text,menu_path,dest_kind FROM links WHERE source_url=? AND link_type IN ('a','area') ORDER BY id",
                (SEED,)):
            path = (json.loads(menu) if menu else []) + [text or raw]
            node = root
            for lab in path:
                node = node["children"].setdefault(lab, {"children": OrderedDict()})
            if scope == "internal":
                loc = urllib.parse.urlsplit(dest)
                tag = loc.path + (f"?{loc.query}" if loc.query else "")
                code = stat.get(dest)
                node.setdefault("urls", [])
                if tag not in node["urls"]:
                    node["urls"].append(tag + (f" [{code}]" if code and code != 200 else "") +
                                        (" (asset)" if kind == "ASSET" else ""))
        def label_nodes(n, label):
            n["text"] = label + (f"   → {', '.join(n['urls'])}" if n.get("urls") else "")
            for lab, ch in n["children"].items():
                label_nodes(ch, lab)
        home_title = pinfo.get(SEED, ("RMD", 200))[0] or "RMD"
        out.append(f"RMD  ({home_title})")
        for lab, ch in root["children"].items():
            label_nodes(ch, lab)
        self._print_tree(root, "", out, 5000)

        # B) URL-path tree of successfully crawled pages
        out += ["", "", "B) URL-PATH TREE (every successfully crawled HTML page, titles from <title>)", ""]
        proot = {"children": OrderedDict()}
        for url in sorted(pinfo, key=str.lower):
            sp = urllib.parse.urlsplit(url)
            segs = [s for s in urllib.parse.unquote(sp.path).split("/") if s]
            if sp.query:
                segs = (segs or ["(root)"])[:-1] + [(segs[-1] if segs else "") + "?" + sp.query]
            node = proot
            for s in segs:
                node = node["children"].setdefault(s, {"children": OrderedDict()})
            node["title"] = pinfo[url][0]
            node["is_page"] = True
        def label_p(n, label):
            n["text"] = label + ("/" if n["children"] and not n.get("is_page") else "") + \
                        (f"  — {n['title']}" if n.get("title") else "")
            for lab, ch in n["children"].items():
                label_p(ch, lab)
        out.append(f"RMD  {SEED}" + (f"  — {pinfo[SEED][0]}" if SEED in pinfo and pinfo[SEED][0] else ""))
        for lab, ch in proot["children"].items():
            label_p(ch, lab)
        self._print_tree(proot, "", out, 20000)

        # C) discovery tree (who first linked to whom)
        out += ["", "", "C) DISCOVERY TREE (each URL under the page on which it was first found; crawl depth order)", ""]
        kids = defaultdict(list)
        rows = self.db.execute("SELECT url,parent,status FROM urls ORDER BY depth,id").fetchall()
        for u, p, s in rows:
            if u == SEED:
                continue
            kids[p if p and p != "sitemap.xml" else SEED].append((u, s))
        titles = {u: (t or "") for u, t in self.db.execute("SELECT url,title FROM pages")}
        seen = {SEED}
        lines = []
        def walk(u, prefix, depth=0):
            ch = kids.get(u, [])
            for i, (c, s) in enumerate(ch):
                if len(lines) > 20000:
                    return
                if c in seen:
                    continue
                seen.add(c)
                last = i == len(ch) - 1
                sp = urllib.parse.urlsplit(c)
                mark = "" if s == "done" else f" [{s}]"
                t = f"  — {titles[c]}" if titles.get(c) else ""
                lines.append(f"{prefix}{'└── ' if last else '├── '}{sp.path}{'?' + sp.query if sp.query else ''}{t}{mark}")
                walk(c, prefix + ("    " if last else "│   "), depth + 1)
        out.append(f"{SEED}")
        walk(SEED, "")
        out += lines
        (self.reports / "site-tree.txt").write_text("\n".join(out), encoding="utf-8")

    # ---- summary
    def build_summary(self):
        q1, db = self.q1, self.db
        by_status = dict(db.execute("SELECT status,COUNT(*) FROM urls GROUP BY status").fetchall())
        total_urls = sum(by_status.values())
        broken = self.broken_destinations()
        broken_urls = [b[0] for b in broken]
        occ = 0
        for u in broken_urls:
            occ += q1("SELECT COUNT(*) FROM links WHERE dest_url=?", u)
        def asset_counts(scope=None):
            sql = "SELECT asset_type,COUNT(DISTINCT url) FROM assets" + (" WHERE scope=?" if scope else "") + " GROUP BY asset_type"
            return dict(db.execute(sql, (scope,) if scope else ()).fetchall())
        all_a, int_a, ext_a = asset_counts(), asset_counts("internal"), asset_counts("external")
        total_assets = q1("SELECT COUNT(DISTINCT url) FROM assets")
        internal_assets = q1("SELECT COUNT(DISTINCT url) FROM assets WHERE scope='internal'")
        unique_internal = q1("SELECT COUNT(*) FROM (SELECT url FROM urls UNION SELECT url FROM assets WHERE scope='internal')")
        pending = by_status.get("pending", 0)
        skipped_depth = by_status.get("skipped_depth", 0)
        stop = self.meta_get("stop_reason", "NOT_RUN")
        exhausted = stop == "QUEUE_EXHAUSTED" and pending == 0 and skipped_depth == 0
        susp = {
            "skipped_by_safety_rules": [dict(url=u, reason=m) for u, m in db.execute(
                "SELECT url,message FROM errors WHERE error_type='SKIPPED_SUSPICIOUS' LIMIT 100")],
            "paths_with_many_query_variants": [dict(path=b, variants=n) for b, n in db.execute(
                "SELECT base_path,COUNT(*) FROM urls WHERE url LIKE '%?%' GROUP BY base_path HAVING COUNT(*)>10 ORDER BY 2 DESC LIMIT 20")],
            "duplicate_content_groups_5plus_urls": [dict(sha256=h, urls=n, example=e) for h, n, e in db.execute(
                "SELECT sha256,COUNT(*),MIN(url) FROM pages WHERE sha256 IS NOT NULL GROUP BY sha256 HAVING COUNT(*)>=5 ORDER BY 2 DESC LIMIT 20")],
            "hrefs_with_backslash_or_space": q1("SELECT COUNT(*) FROM links WHERE dest_raw LIKE '%\\%' OR dest_raw LIKE '% %'"),
            "urls_longer_than_200_chars": q1("SELECT COUNT(*) FROM urls WHERE LENGTH(url)>200"),
        }
        not_crawled = {
            "robots_blocked": [u for (u,) in db.execute("SELECT url FROM urls WHERE status='robots_blocked' LIMIT 200")],
            "skipped_depth_limit": skipped_depth,
            "skipped_suspicious": by_status.get("skipped_suspicious", 0),
            "still_pending": pending,
            "failed_by_error_type": dict(db.execute(
                "SELECT error_type,COUNT(*) FROM errors WHERE error_type NOT IN ('SKIPPED_SUSPICIOUS','ROBOTS_BLOCKED','HOST_VARIANT_NO_REDIRECT','PARSE_WARNING') GROUP BY error_type").fetchall()),
            "rmd_subdomains_linked_but_not_crawled": [
                r[0] for r in db.execute(
                    "SELECT DISTINCT domain FROM external WHERE domain LIKE '%.rmd.ac.in' AND domain!='www.rmd.ac.in' ORDER BY 1")],
        }
        return {
            "generated_at": utcnow(),
            "seed": SEED,
            "settings": {"max_pages": self.args.max_pages, "max_depth": self.args.max_depth,
                         "delay_seconds": self.fetcher.delay, "timeout_seconds": self.args.timeout,
                         "retries": self.args.retries},
            "crawl_exhausted": exhausted,
            "stop_reason": stop,
            "queue": {"html_urls_discovered": total_urls, "remaining_pending": pending,
                      "skipped_depth": skipped_depth, "by_status": by_status},
            "report": {
                "1_total_html_pages_discovered": total_urls,
                "2_total_html_pages_successfully_crawled": by_status.get("done", 0),
                "3_total_failed_pages": by_status.get("error", 0),
                "4_total_pdfs_discovered_unique": all_a.get("PDF", 0),
                "5_total_images_discovered_unique": all_a.get("IMAGE", 0),
                "6_total_other_assets_discovered_unique": total_assets - all_a.get("PDF", 0) - all_a.get("IMAGE", 0),
                "7_total_internal_links": {
                    "occurrences": q1("SELECT COUNT(*) FROM links WHERE scope='internal'"),
                    "unique_destinations": q1("SELECT COUNT(DISTINCT dest_url) FROM links WHERE scope='internal'")},
                "8_total_external_links": {
                    "occurrences": q1("SELECT COUNT(*) FROM external"),
                    "unique_destinations": q1("SELECT COUNT(DISTINCT dest_url) FROM external"),
                    "unique_domains": q1("SELECT COUNT(DISTINCT domain) FROM external")},
                "9_total_unique_internal_urls": unique_internal,
                "10_total_redirects": q1("SELECT COUNT(*) FROM redirects"),
                "11_total_broken_links": {"unique_broken_urls": len(broken), "occurrences": occ},
                "12_max_crawl_depth_reached": q1("SELECT COALESCE(MAX(depth),0) FROM urls WHERE status IN ('done','error','nonhtml','redirect')"),
                "13_areas_not_crawled": not_crawled,
                "14_robots_txt": self.meta_get("robots", "not checked"),
                "15_suspicious_url_patterns": susp,
            },
            "assets": {
                "unique_asset_urls_total": total_assets,
                "unique_internal_asset_urls": internal_assets,
                "unique_external_asset_urls": total_assets - internal_assets,
                "by_type_all": all_a, "by_type_internal": int_a, "by_type_external": ext_a,
                "internal_assets_head_checked": q1("SELECT COUNT(*) FROM asset_checks"),
                "phase2_assets_csv_rows": sum(1 for _ in open(self.root / "PHASE-2-ASSETS.csv", encoding="utf-8-sig")) - 1,
                "downloaded": 0,
            },
            "other": {
                "nonhtml_urls_reached_via_page_links": by_status.get("nonhtml", 0),
                "redirect_aliases": by_status.get("redirect", 0),
                "sitemap_urls_found": self.meta_get("sitemap_urls_found", "0"),
                "html_files_saved": q1("SELECT COUNT(*) FROM pages WHERE html_path IS NOT NULL"),
            },
        }

    def print_final(self, s):
        r, q, a = s["report"], s["queue"], s["assets"]
        log("\n" + "=" * 64)
        log("PHASE 1 CRAWL REPORT")
        log("=" * 64)
        log(f"HTML URLs discovered     : {q['html_urls_discovered']}")
        log(f"  crawled OK             : {r['2_total_html_pages_successfully_crawled']}")
        log(f"  failed                 : {r['3_total_failed_pages']}")
        log(f"  redirect aliases       : {q['by_status'].get('redirect', 0)}")
        log(f"  non-HTML (by header)   : {q['by_status'].get('nonhtml', 0)}")
        log(f"  robots-blocked         : {q['by_status'].get('robots_blocked', 0)}")
        log(f"  skipped (suspicious)   : {q['by_status'].get('skipped_suspicious', 0)}")
        log(f"  skipped (depth limit)  : {q['skipped_depth']}")
        log(f"  REMAINING in queue     : {q['remaining_pending']}")
        log(f"Assets recorded, NOT downloaded: {a['unique_asset_urls_total']} unique "
            f"({a['unique_internal_asset_urls']} internal)")
        log(f"Redirects: {r['10_total_redirects']} | broken URLs: {r['11_total_broken_links']['unique_broken_urls']} "
            f"| max depth: {r['12_max_crawl_depth_reached']}")
        log(f"Stop reason: {s['stop_reason']}")
        log("CRAWL GENUINELY EXHAUSTED: " + ("YES" if s["crawl_exhausted"] else "NO"))
        log("=" * 64)

    def status(self):
        for k, v in self.db.execute("SELECT status,COUNT(*) FROM urls GROUP BY status ORDER BY 2 DESC"):
            log(f"{k:20s} {v}")
        log(f"stop_reason: {self.meta_get('stop_reason', '-')}")


def out_res(res):
    return res


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--max-pages", type=int, default=10000, help="hard cap on fetched HTML URLs (default 10000)")
    common.add_argument("--max-depth", type=int, default=20, help="maximum crawl depth (default 20)")
    common.add_argument("--delay", type=float, default=1.0, help="seconds between requests (default 1.0)")
    common.add_argument("--timeout", type=float, default=30, help="request timeout seconds (default 30)")
    common.add_argument("--retries", type=int, default=2, help="extra attempts for transient failures (default 2)")
    common.add_argument("--max-consecutive-failures", type=int, default=25,
                        help="stop if this many network failures occur in a row (default 25)")
    common.add_argument("--insecure", action="store_true", help="skip TLS certificate verification")
    ap = argparse.ArgumentParser(description="RMD Phase 1 structural crawler")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", parents=[common], help="crawl / resume")
    r.add_argument("--check-assets", action="store_true", help="after the crawl, HEAD-check internal assets")
    r.add_argument("--retry-failed", action="store_true", help="re-queue transient failures (timeouts, 5xx)")
    sub.add_parser("check-assets", parents=[common], help="HEAD-check internal assets (no downloads)")
    sub.add_parser("export", parents=[common], help="rebuild CSVs and reports from the database")
    sub.add_parser("status", parents=[common], help="show queue status")
    if len(sys.argv) == 1:
        sys.argv.append("run")
    args = ap.parse_args()
    for attr in ("check_assets", "retry_failed"):
        if not hasattr(args, attr):
            setattr(args, attr, False)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    c = Crawler(args)
    if args.cmd == "run":
        c.run()
    elif args.cmd == "check-assets":
        c.load_robots()
        c.check_assets()
        c.export()
    elif args.cmd == "export":
        c.export()
    elif args.cmd == "status":
        c.status()


if __name__ == "__main__":
    main()
