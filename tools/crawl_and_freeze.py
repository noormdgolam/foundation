#!/usr/bin/env python3
"""
Fallback / verification static-site freezer for bongshaifoundation.org.

Primary conversion path is the Simply Static WP plugin (run from inside
WordPress by Antigravity, which has FTP/wp-admin access this script does not).
This script is Plan B and a cross-check: it crawls the LIVE rendered site over
HTTPS (which this environment can reach) and freezes it to static HTML/assets.

Only run this once https://bongshaifoundation.org/ returns HTTP 200 again.

Usage:
    python crawl_and_freeze.py
Output:
    ../static-site-crawled/  (mirrors the live site, relative links, forms flagged)
"""
import json
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

BASE = "https://bongshaifoundation.org"
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "static-site-crawled")
ASSET_EXT = (".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
             ".woff", ".woff2", ".ttf", ".eot", ".ico", ".pdf", ".mp4", ".json")
SKIP_QUERY_PATTERNS = ("replytocom=", "add-to-cart=", "wc-ajax=")
HEADERS = {"User-Agent": "Mozilla/5.0 (static-site-freezer; +https://bongshaifoundation.org/)"}

session = requests.Session()
session.headers.update(HEADERS)

visited_pages = set()
queue = [BASE + "/"]
downloaded_assets = set()
forms_found = []
broken_links = []


def is_same_site(url):
    return urlparse(url).netloc in ("", urlparse(BASE).netloc)


def clean_url(url):
    url, _ = urldefrag(url)
    return url


def local_path_for_page(url):
    parsed = urlparse(url)
    path = parsed.path
    if not path or path == "/":
        return os.path.join(OUT_DIR, "index.html")
    path = path.strip("/")
    if "." in os.path.basename(path):
        return os.path.join(OUT_DIR, path)
    return os.path.join(OUT_DIR, path, "index.html")


def local_path_for_asset(url):
    parsed = urlparse(url)
    path = parsed.path.lstrip("/")
    return os.path.join(OUT_DIR, path)


def rel_link(from_path, to_path):
    rel = os.path.relpath(to_path, os.path.dirname(from_path))
    return rel.replace(os.sep, "/")


def fetch(url):
    try:
        r = session.get(url, timeout=20)
        return r
    except requests.RequestException as e:
        broken_links.append((url, str(e)))
        return None


def try_sitemap():
    """Prefer WordPress's auto sitemap over blind crawling for a clean URL list."""
    urls = set()
    for sm in ("/wp-sitemap.xml", "/sitemap.xml", "/sitemap_index.xml"):
        r = fetch(BASE + sm)
        if not r or r.status_code != 200:
            continue
        try:
            soup = BeautifulSoup(r.content, "xml")
        except Exception:
            soup = BeautifulSoup(r.content, "html.parser")
        locs = [loc.text.strip() for loc in soup.find_all("loc")]
        sub_sitemaps = [l for l in locs if l.endswith(".xml")]
        page_urls = [l for l in locs if not l.endswith(".xml")]
        urls.update(page_urls)
        for sub in sub_sitemaps:
            r2 = fetch(sub)
            if r2 and r2.status_code == 200:
                try:
                    s2 = BeautifulSoup(r2.content, "xml")
                except Exception:
                    s2 = BeautifulSoup(r2.content, "html.parser")
                urls.update(loc.text.strip() for loc in s2.find_all("loc") if not loc.text.strip().endswith(".xml"))
        if urls:
            break
    return urls


def download_asset(url):
    url = clean_url(url)
    if url in downloaded_assets or not is_same_site(url):
        return
    downloaded_assets.add(url)
    r = fetch(url)
    if not r or r.status_code != 200:
        broken_links.append((url, f"status {r.status_code if r else 'error'}"))
        return
    dest = local_path_for_asset(url)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(r.content)


def process_page(url):
    url = clean_url(url)
    if url in visited_pages or not is_same_site(url):
        return
    if any(p in url for p in SKIP_QUERY_PATTERNS):
        return
    visited_pages.add(url)

    r = fetch(url)
    if not r:
        return
    if r.status_code != 200:
        broken_links.append((url, f"status {r.status_code}"))
        return
    ctype = r.headers.get("Content-Type", "")
    if "text/html" not in ctype:
        # non-HTML asset reached via a page-like link
        download_asset(url)
        return

    soup = BeautifulSoup(r.text, "html.parser")
    dest = local_path_for_page(url)

    # Flag forms for manual/AI follow-up (WP form handlers won't run statically)
    for form in soup.find_all("form"):
        forms_found.append({
            "page": url,
            "action": form.get("action", ""),
            "method": form.get("method", "get"),
            "id": form.get("id", ""),
            "class": form.get("class", ""),
        })
        form.insert(0, soup.new_string(
            f" STATIC-SITE TODO: replace form action ({form.get('action','')}) "
            "with a static-friendly endpoint (e.g. Formspree) — WP form handlers do not run on a static host. "
        ))

    # Strip WordPress-only runtime bits that are meaningless/broken once static
    for tag in soup.find_all("link", rel="https://api.w.org/"):
        tag.decompose()
    for tag in soup.find_all("link", href=re.compile(r"xmlrpc\.php")):
        tag.decompose()

    # Rewrite + queue same-site links
    for tag, attr in (("a", "href"), ("img", "src"), ("script", "src"), ("link", "href")):
        for el in soup.find_all(tag, **{attr: True}):
            raw = el[attr]
            if raw.startswith(("mailto:", "tel:", "#", "javascript:")):
                continue
            absolute = clean_url(urljoin(url, raw))
            if not is_same_site(absolute):
                continue
            if tag == "a":
                if any(p in absolute for p in SKIP_QUERY_PATTERNS):
                    continue
                queue.append(absolute)
                target_path = local_path_for_page(absolute)
            else:
                download_asset(absolute)
                target_path = local_path_for_asset(absolute)
            el[attr] = rel_link(dest, target_path)

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        f.write(str(soup))


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    sitemap_urls = try_sitemap()
    if sitemap_urls:
        print(f"Using {len(sitemap_urls)} URLs from sitemap")
        queue.clear()
        queue.extend(sitemap_urls)
        queue.append(BASE + "/")
    else:
        print("No sitemap found, falling back to link-crawl from homepage")

    seen_in_queue = set()
    i = 0
    while i < len(queue):
        url = queue[i]
        i += 1
        if url in seen_in_queue:
            continue
        seen_in_queue.add(url)
        process_page(url)
        time.sleep(0.2)  # be polite to the origin server

    manifest = {
        "pages_crawled": sorted(visited_pages),
        "assets_downloaded": sorted(downloaded_assets),
        "forms_found": forms_found,
        "broken_links": broken_links,
    }
    with open(os.path.join(OUT_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"Pages: {len(visited_pages)}  Assets: {len(downloaded_assets)}  "
          f"Forms flagged: {len(forms_found)}  Broken: {len(broken_links)}")
    print(f"Output: {os.path.abspath(OUT_DIR)}")


if __name__ == "__main__":
    if requests.get(BASE + "/", timeout=15, headers=HEADERS).status_code != 200:
        print(f"{BASE}/ is not returning HTTP 200 yet — fix the site before freezing it.")
        sys.exit(1)
    main()
