#!/usr/bin/env python3
"""
Downloads ONLY styling and essential UI template assets:
- CSS stylesheets
- JavaScript files
- Fonts (.woff, .woff2, .ttf, .eot, .svg, .ico)
- CSS-referenced background images, UI icons, and menu arrows
- Key UI branding images (headerlogo.png, navigation icons, logos)

DOES NOT download heavy assets:
- NO PDFs
- NO documents/spreadsheets/presentations
- NO student/event/gallery photo archives
- NO videos

Saves all downloaded files into html/ preserving the website's exact relative URL paths.
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

# Create SSL context
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))


def normalize_internal_url(raw, base="https://www.rmd.ac.in/"):
    if not raw or raw.startswith("#") or raw.startswith("data:"):
        return None
    try:
        joined = urllib.parse.urljoin(base, raw.strip().replace("\\", "/"))
        p = urllib.parse.urlsplit(joined)
        host = (p.hostname or "").lower()
        if host in INTERNAL_HOSTS:
            path = re.sub(r"/{2,}", "/", p.path or "/")
            return f"https://{CANON_HOST}{path}"
    except Exception:
        pass
    return None


def url_to_local_path(url):
    p = urllib.parse.urlsplit(url)
    clean_path = urllib.parse.unquote(p.path).lstrip("/")
    # Clean segment names for Windows safety
    parts = clean_path.split("/")
    safe_parts = []
    for part in parts:
        safe_part = re.sub(r'[<>:"|?*\x00-\x1f]', "_", part)
        safe_parts.append(safe_part)
    return HTML_DIR / Path(*safe_parts)


def fetch_file(url):
    local_path = url_to_local_path(url)
    if local_path.is_file() and local_path.stat().st_size > 0:
        return "EXISTS", local_path, None

    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Referer": "https://www.rmd.ac.in/"
    })
    
    try:
        with opener.open(req, timeout=15) as resp:
            status = resp.getcode()
            if 200 <= status < 300:
                body = resp.read()
                local_path.parent.mkdir(parents=True, exist_ok=True)
                local_path.write_bytes(body)
                return "OK", local_path, body
            else:
                return f"HTTP_{status}", local_path, None
    except urllib.error.HTTPError as e:
        return f"HTTP_{e.code}", local_path, None
    except Exception as e:
        return f"ERR_{type(e).__name__}", local_path, None


def extract_css_urls(css_text, base_url):
    found = []
    # url(...)
    for m in re.finditer(r"""url\(\s*['"]?([^'")]+?)['"]?\s*\)""", css_text, re.I):
        u = m.group(1).strip()
        norm = normalize_internal_url(u, base_url)
        if norm:
            found.append(norm)
    # @import ...
    for m in re.finditer(r"""@import\s+['"]([^'"]+)['"]""", css_text, re.I):
        u = m.group(1).strip()
        norm = normalize_internal_url(u, base_url)
        if norm:
            found.append(norm)
    return found


def main():
    print("=" * 60)
    print("RMD Website - Option 1: Downloading Styling & UI Assets Only")
    print("=" * 60)

    db = sqlite3.connect(str(DB_PATH))
    cur = db.cursor()

    targets = set()

    # 1. All CSS, JS, and Font files recorded in the database
    cur.execute("""
        SELECT DISTINCT url FROM assets 
        WHERE scope='internal' 
          AND (extension IN ('css', 'js', 'ico', 'woff', 'woff2', 'ttf', 'eot', 'svg')
               OR ref_type IN ('stylesheet', 'script', 'icon'))
    """)
    for (u,) in cur.fetchall():
        targets.add(u)

    # 2. Key UI branding & navigation icons from database
    cur.execute("""
        SELECT DISTINCT url FROM assets 
        WHERE scope='internal' 
          AND (
            url LIKE '%headerlogo%'
            OR url LIKE '%/newimages/%'
            OR url LIKE '%/logo%'
            OR url LIKE '%logo.%'
            OR url LIKE '%/icons/%'
            OR url LIKE '%new.gif%'
            OR url LIKE '%new.png%'
            OR url LIKE '%arrow%'
            OR url LIKE '%button%'
          )
          AND extension IN ('png', 'jpg', 'jpeg', 'gif', 'svg', 'ico')
    """)
    for (u,) in cur.fetchall():
        targets.add(u)

    # 3. Explicit essential known styling assets
    essentials = [
        "https://www.rmd.ac.in/css/bootstrap.min.css",
        "https://www.rmd.ac.in/css/style.css",
        "https://www.rmd.ac.in/css/responsive.css",
        "https://www.rmd.ac.in/css/animate.css",
        "https://www.rmd.ac.in/css/owl.carousel.css",
        "https://www.rmd.ac.in/css/owl.theme.css",
        "https://www.rmd.ac.in/css/colorbox.css",
        "https://www.rmd.ac.in/css/generic.css",
        "https://www.rmd.ac.in/css/js-image-slider.css",
        "https://www.rmd.ac.in/template/tabcontent.css",
        "https://www.rmd.ac.in/popup/bootstrap-theme.min.css",
        "https://www.rmd.ac.in/popup/jquery.min.js",
        "https://www.rmd.ac.in/js/jquery.js",
        "https://www.rmd.ac.in/js/bootstrap.min.js",
        "https://www.rmd.ac.in/js/custom.js",
        "https://www.rmd.ac.in/js/js-image-slider.js",
        "https://www.rmd.ac.in/template/tabcontent.js",
        "https://www.rmd.ac.in/images/headerlogo.png",
        "https://www.rmd.ac.in/images/blank_hover.png",
        "https://www.rmd.ac.in/images/sidebar_hover.gif",
        "https://www.rmd.ac.in/images/down-arrow-white.png",
        "https://www.rmd.ac.in/images/down-arrow-color.png",
        "https://www.rmd.ac.in/images/down-arrow-transparent.png",
        "https://www.rmd.ac.in/images/arrow_submenu_left.gif",
        "https://www.rmd.ac.in/images/arrow_submenu_right.gif",
        "https://www.rmd.ac.in/images/tf_menu_right.gif",
        "https://www.rmd.ac.in/images/tf_menu_left.gif",
        "https://www.rmd.ac.in/images/crossword.png",
        "https://www.rmd.ac.in/images/ribbon_new.png",
        "https://www.rmd.ac.in/images/ico_arrow_blue2.gif",
        "https://www.rmd.ac.in/new.gif",
        "https://www.rmd.ac.in/newimages/alumni.png",
        "https://www.rmd.ac.in/newimages/creditcard.png",
        "https://www.rmd.ac.in/newimages/keyman.png",
        "https://www.rmd.ac.in/newimages/pay.png",
        "https://www.rmd.ac.in/newimages/professor.png",
        "https://www.rmd.ac.in/newimages/scholar.png",
        "https://www.rmd.ac.in/newimages/science.png"
    ]
    for e in essentials:
        targets.add(e)

    queue = sorted(list(targets))
    processed = set()
    success_count = 0
    fail_count = 0
    bytes_total = 0

    print(f"Initial styling targets queued: {len(queue)}")

    while queue:
        url = queue.pop(0)
        if url in processed:
            continue
        processed.add(url)

        status, local_path, body = fetch_file(url)

        if status in ("OK", "EXISTS"):
            success_count += 1
            size = local_path.stat().st_size
            bytes_total += size
            print(f"[{status}] ({size:>8} B) {url}")

            # If it's a CSS file, parse for font/image sub-resources
            if url.lower().endswith(".css") or ".css?" in url.lower():
                try:
                    css_content = body.decode("utf-8", "ignore") if body else local_path.read_text("utf-8", "ignore")
                    sub_urls = extract_css_urls(css_content, url)
                    for s_url in sub_urls:
                        # Only follow CSS sub-resources like fonts, icons, images
                        ext = s_url.rsplit(".", 1)[-1].split("?")[0].lower()
                        if ext in ("woff", "woff2", "ttf", "eot", "svg", "png", "gif", "jpg", "jpeg", "css"):
                            if s_url not in processed and s_url not in queue:
                                queue.append(s_url)
                except Exception as e:
                    pass
        else:
            fail_count += 1
            print(f"[{status}] {url}")

        time.sleep(0.05)  # Gentle 50ms pause

    print("=" * 60)
    print("Styling Download Complete!")
    print(f"Total processed: {len(processed)}")
    print(f"Successful:      {success_count}")
    print(f"Failed / 404:    {fail_count}")
    print(f"Total size:      {bytes_total / (1024*1024):.2f} MB")
    print("All styling assets are saved directly into html/ matching their web paths.")
    print("=" * 60)


if __name__ == "__main__":
    main()
