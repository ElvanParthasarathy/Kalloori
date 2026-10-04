#!/usr/bin/env python3
"""
Tests all 260 unvisited internal targets extracted from all HTML files against the live server.
If any target returns 200 OK and HTML, it is saved into html/ and added to crawl.db.
If it returns 404/403/error, it is confirmed as a broken link on the live server.
"""
import os
import re
import ssl
import time
import sqlite3
import glob
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


def main():
    print("=" * 60)
    print("Full Parity Check: Extracting all internal targets from HTML files...")
    print("=" * 60)

    html_files = glob.glob(str(HTML_DIR / "**/*.html"), recursive=True)
    all_internal_targets = set()

    for hf in html_files:
        rel = hf.replace("\\", "/").split("/html/")[-1]
        base_url = f"https://{CANON_HOST}/{rel}"
        try:
            content = open(hf, "r", encoding="utf-8", errors="ignore").read()
            for href in re.findall(r'href=[\'\"]([^\'\">]+)[\'\"]', content, re.I):
                href = href.strip()
                if not href or href.startswith("#") or href.startswith("javascript:") or href.startswith("mailto:"):
                    continue
                joined = urllib.parse.urljoin(base_url, href.replace("\\", "/"))
                p = urllib.parse.urlsplit(joined)
                if (p.hostname or "").lower() in INTERNAL_HOSTS:
                    ext = p.path.rsplit(".", 1)[-1].lower() if "." in p.path.rsplit("/", 1)[-1] else ""
                    if ext in ("html", "htm", "php", ""):
                        all_internal_targets.add(f"https://{CANON_HOST}{p.path}")
        except Exception:
            pass

    print(f"Total internal targets found: {len(all_internal_targets)}")

    missing = []
    for target in all_internal_targets:
        p = urllib.parse.urlsplit(target).path.lstrip("/")
        if not p or p.endswith("/"):
            p += "index.html"
        elif not p.endswith((".html", ".htm", ".php")):
            p += ".html"
        local_path = HTML_DIR / Path(*p.split("/"))
        if not local_path.exists():
            missing.append((target, local_path))

    print(f"Targets to test against live server: {len(missing)}")

    db = sqlite3.connect(str(DB_PATH))
    cur = db.cursor()

    saved = 0
    confirmed_404 = 0
    confirmed_403 = 0
    other_err = 0

    for idx, (url, local_path) in enumerate(sorted(missing), 1):
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Referer": "https://www.rmd.ac.in/"
        })
        try:
            with opener.open(req, timeout=8) as resp:
                st = resp.getcode()
                body = resp.read()
                ct = resp.headers.get("content-type", "").lower()
                if 200 <= st < 300 and ("html" in ct or b"<html" in body[:2000].lower()):
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    local_path.write_bytes(body)
                    saved += 1

                    # Title
                    m = re.search(rb"<title>(.*?)</title>", body, re.I | re.S)
                    title = m.group(1).decode("utf-8", "ignore").strip() if m else "RMD"

                    print(f"[{idx}/{len(missing)}] [SAVED 200] ({len(body):>6} B) {url} - {title[:35]}")
                    cur.execute("""
                        INSERT OR REPLACE INTO pages(url, canonical_url, final_url, http_status, title, content_type, content_length, crawled_at, crawl_status, html_path)
                        VALUES(?, ?, ?, 200, ?, 'text/html', ?, datetime('now'), 'OK', ?)
                    """, (url, url, url, title, len(body), str(local_path).replace("\\", "/")))
                    cur.execute("INSERT OR REPLACE INTO urls(url, base_path, raw_first, status, depth, discovered_at) VALUES(?, ?, ?, 'done', 2, datetime('now'))",
                                (url, url, url))
                    db.commit()
                else:
                    print(f"[{idx}/{len(missing)}] [NON-HTML {st}] {url}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                confirmed_404 += 1
            elif e.code == 403:
                confirmed_403 += 1
            else:
                other_err += 1
            cur.execute("INSERT OR REPLACE INTO urls(url, base_path, raw_first, status, depth, discovered_at) VALUES(?, ?, ?, 'error', 2, datetime('now'))",
                        (url, url, url))
            cur.execute("INSERT OR REPLACE INTO errors(url, source_url, error_type, status, message, attempts, ts) VALUES(?, 'crawl_audit', ?, ?, ?, 1, datetime('now'))",
                        (url, f"HTTP_{e.code}", e.code, f"HTTP {e.code} on live server"))
            db.commit()
        except Exception as e:
            other_err += 1
            cur.execute("INSERT OR REPLACE INTO urls(url, base_path, raw_first, status, depth, discovered_at) VALUES(?, ?, ?, 'error', 2, datetime('now'))",
                        (url, url, url))
            db.commit()

        time.sleep(0.04)

    print("=" * 60)
    print("Full Parity Audit Complete!")
    print(f"Total missing URLs tested:  {len(missing)}")
    print(f"New valid pages saved:     {saved}")
    print(f"Confirmed live 404s:       {confirmed_404}")
    print(f"Confirmed live 403s:       {confirmed_403}")
    print(f"Other errors / timeouts:   {other_err}")
    print("=" * 60)
    db.close()


if __name__ == "__main__":
    main()
