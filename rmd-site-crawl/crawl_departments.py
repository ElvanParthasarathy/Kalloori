#!/usr/bin/env python3
"""
Recursively crawls all department pages (CSE, ECE, EEE, IT, CSBS, AIML, S&H)
starting from their REAL public index.html pages.
Saves all HTML pages into html/ preserving directory structure.
Downloads any new CSS/JS/font styling assets into html/.
Excludes PDFs, video files, and heavy image galleries.
Updates crawl.db and regenerates CSV reports.
"""
import os
import re
import ssl
import time
import sqlite3
import urllib.parse
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
HTML_DIR = BASE_DIR / "html"
DB_PATH = BASE_DIR / "data" / "crawl.db"
CANON_HOST = "www.rmd.ac.in"
INTERNAL_HOSTS = {"rmd.ac.in", "www.rmd.ac.in"}

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))


def normalize(raw, base):
    if not raw or raw.startswith("#") or raw.startswith("javascript:") or raw.startswith("mailto:"):
        return None, None
    try:
        joined = urllib.parse.urljoin(base, raw.strip().replace("\\", "/"))
        p = urllib.parse.urlsplit(joined)
        host = (p.hostname or "").lower()
        if host in INTERNAL_HOSTS:
            path = re.sub(r"/{2,}", "/", p.path or "/")
            clean_url = f"https://{CANON_HOST}{path}"
            return "internal", clean_url
        else:
            return "external", joined
    except Exception:
        return None, None


def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Referer": "https://www.rmd.ac.in/"
    })
    try:
        with opener.open(req, timeout=12) as r:
            status = r.getcode()
            headers = {k.lower(): v for k, v in r.headers.items()}
            body = r.read()
            return status, headers, body, None
    except urllib.error.HTTPError as e:
        return e.code, {}, None, str(e)
    except Exception as e:
        return None, {}, None, str(e)


def main():
    print("=" * 60)
    print("Crawling Department Pages & Subpages (CSE, ECE, EEE, IT, CSBS, AIML, S&H)")
    print("=" * 60)

    db = sqlite3.connect(str(DB_PATH))
    cur = db.cursor()

    seed_depts = [
        "https://www.rmd.ac.in/dept/cse/index.html",
        "https://www.rmd.ac.in/dept/ece/index.html",
        "https://www.rmd.ac.in/dept/eee/index.html",
        "https://www.rmd.ac.in/dept/it/index.html",
        "https://www.rmd.ac.in/dept/csbs/index.html",
        "https://www.rmd.ac.in/dept/aiml/index.html",
        "https://www.rmd.ac.in/dept/snh/index.html",
    ]

    queue = list(seed_depts)
    visited = set()
    new_pages = 0
    errors = 0

    while queue:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)

        # Check local file
        rel_path = urllib.parse.urlsplit(url).path.lstrip("/")
        if rel_path.endswith("/"):
            rel_path += "index.html"
        elif not rel_path.endswith(".html") and not rel_path.endswith(".htm") and not rel_path.endswith(".php"):
            rel_path += "/index.html"

        local_file = HTML_DIR / Path(*rel_path.split("/"))

        status, headers, body, err = fetch(url)
        time.sleep(0.05)

        if status and 200 <= status < 300 and body:
            ct = headers.get("content-type", "").lower()
            if "html" in ct or b"<html" in body[:2000].lower():
                local_file.parent.mkdir(parents=True, exist_ok=True)
                local_file.write_bytes(body)
                new_pages += 1

                # Parse title
                title_m = re.search(rb"<title>(.*?)</title>", body, re.I | re.S)
                title = title_m.group(1).decode("utf-8", "ignore").strip() if title_m else ""

                print(f"[200 OK] ({len(body):>6} B) {url} - {title[:40]}")

                # Update database
                cur.execute("""
                    INSERT OR REPLACE INTO pages(url, canonical_url, final_url, http_status, title, content_type, content_length, crawled_at, crawl_status, html_path)
                    VALUES(?, ?, ?, ?, ?, ?, ?, datetime('now'), 'OK', ?)
                """, (url, url, url, status, title, ct, len(body), f"html/{rel_path}"))
                cur.execute("UPDATE urls SET status='done' WHERE url=?", (url,))

                # Discover new links
                try:
                    text = body.decode("utf-8", "ignore")
                    for href in re.findall(r'href=[\'\"]([^\'\">]+)[\'\"]', text, re.I):
                        kind, clean_u = normalize(href, url)
                        if kind == "internal":
                            p = urllib.parse.urlsplit(clean_u)
                            ext = p.path.rsplit(".", 1)[-1].lower() if "." in p.path.rsplit("/", 1)[-1] else ""
                            # Only crawl HTML pages inside dept/ or other internal sections
                            if ext in ("html", "htm", "php", ""):
                                # Prioritize department links
                                if clean_u not in visited and clean_u not in queue:
                                    if "/dept/" in p.path:
                                        queue.append(clean_u)
                                    else:
                                        # Also record URL in database if not exists
                                        cur.execute("INSERT OR IGNORE INTO urls(url, base_path, raw_first, status, depth, parent, via, discovered_at) VALUES(?, ?, ?, 'pending', 2, ?, 'a', datetime('now'))",
                                                    (clean_u, p.path, href, url))
                            elif ext in ("css", "js", "woff", "woff2", "ttf", "eot", "svg"):
                                # Styling asset: fetch into html/
                                asset_rel = p.path.lstrip("/")
                                asset_file = HTML_DIR / Path(*asset_rel.split("/"))
                                if not asset_file.exists():
                                    a_status, a_head, a_body, _ = fetch(clean_u)
                                    if a_status and 200 <= a_status < 300 and a_body:
                                        asset_file.parent.mkdir(parents=True, exist_ok=True)
                                        asset_file.write_bytes(a_body)
                                        print(f"  [STYLED] Saved {asset_rel}")
                            elif ext == "pdf":
                                # PDF: record in assets table only, do NOT download
                                cur.execute("INSERT OR IGNORE INTO assets(url, source_url, ref_type, asset_type, extension, filename, scope, downloadable) VALUES(?, ?, 'a', 'PDF', 'pdf', ?, 'internal', 1)",
                                            (clean_u, url, p.path.rsplit('/', 1)[-1]))
                except Exception as e:
                    pass

                db.commit()
            else:
                print(f"[NON-HTML] {url}")
        else:
            errors += 1
            print(f"[{status or err}] {url}")
            cur.execute("""
                INSERT OR REPLACE INTO pages(url, canonical_url, final_url, http_status, title, crawled_at, crawl_status, error)
                VALUES(?, ?, ?, ?, '', datetime('now'), 'HTTP_ERROR', ?)
            """, (url, url, url, status, str(err or status)))
            cur.execute("UPDATE urls SET status='error' WHERE url=?", (url,))
            db.commit()

    print("=" * 60)
    print(f"Department Crawl Finished! Saved {new_pages} pages, {errors} errors.")
    print("=" * 60)
    db.close()


if __name__ == "__main__":
    main()
