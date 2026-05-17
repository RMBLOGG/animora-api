import re
import time
from collections import defaultdict
from functools import wraps
from flask import Flask, jsonify, request, render_template
import cloudscraper
from bs4 import BeautifulSoup

app = Flask(__name__)
BASE = "https://kusonime.com"
scraper = cloudscraper.create_scraper()

# ── RATE LIMITER ──────────────────────────────────────────────────────────────
_rate_store = defaultdict(list)
RATE_LIMIT  = 100
RATE_WINDOW = 60

def rate_limit(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        ip  = request.headers.get("X-Forwarded-For", request.remote_addr).split(",")[0].strip()
        now = time.time()
        hits = [t for t in _rate_store[ip] if now - t < RATE_WINDOW]
        hits.append(now)
        _rate_store[ip] = hits
        if len(hits) > RATE_LIMIT:
            resp = jsonify({
                "status": "error",
                "message": f"Rate limit exceeded. Maksimal {RATE_LIMIT} request per menit. Coba lagi dalam {RATE_WINDOW} detik."
            })
            resp.status_code = 429
            resp.headers["X-RateLimit-Limit"]     = str(RATE_LIMIT)
            resp.headers["X-RateLimit-Remaining"] = "0"
            resp.headers["Retry-After"]           = str(RATE_WINDOW)
            return resp
        result = f(*args, **kwargs)
        if hasattr(result, "headers"):
            result.headers["X-RateLimit-Limit"]     = str(RATE_LIMIT)
            result.headers["X-RateLimit-Remaining"] = str(max(0, RATE_LIMIT - len(hits)))
        return result
    return decorated

def get_soup(url):
    try:
        r = scraper.get(url, timeout=15)
        if r.status_code != 200: return None
        return BeautifulSoup(r.text, "html.parser")
    except Exception:
        return None

def clean_title(raw):
    for sep in [" , download", " , donwload", ", download", ", donwload"]:
        if sep.lower() in raw.lower():
            return raw[:raw.lower().index(sep.lower())].strip()
    return raw.strip()

def get_next_page(soup):
    nav = soup.find("div", class_="wp-pagenavi")
    if nav:
        nxt = nav.find("a", class_="nextpostslink")
        if nxt: return nxt["href"]
    nav2 = soup.find("div", class_="navigation")
    if nav2:
        a = nav2.find("a", href=True)
        if a and "next" in a.get_text(strip=True).lower(): return a["href"]
    return None

def parse_post_list(soup):
    results = []
    for item in soup.select(".venz .kover"):
        thumb  = item.find("div", class_="thumb")
        img    = thumb.find("img") if thumb else None
        poster = (img.get("src") or img.get("data-src")) if img else None
        a      = item.find("a", href=True)
        title  = a.get("title") or a.get_text(strip=True) if a else None
        url    = a["href"] if a else None
        content = item.find("div", class_="content")
        genres  = []
        if content:
            for g in content.find_all("a", href=True):
                if "/genres/" in g["href"]:
                    genres.append(g.get_text(strip=True))
        slug = url.rstrip("/").split("/")[-1] if url else None
        results.append({"title": title, "slug": slug, "poster": poster, "genres": genres, "url": url})
    return results

def parse_detail(soup):
    result = {}
    venu = soup.find("div", class_="venutama")
    h1   = venu.find("h1") if venu else None
    result["title"] = clean_title(h1.get_text(strip=True)) if h1 else None
    pt   = soup.find("div", class_="post-thumb")
    img  = pt.find("img") if pt else None
    result["poster"] = (img.get("src") or img.get("data-src")) if img else None
    result["info"] = {}
    lexot = soup.find("div", class_="lexot")
    info  = lexot.find("div", class_="info") if lexot else soup.find("div", class_="info")
    if info:
        for p in info.find_all("p"):
            b = p.find("b")
            if not b: continue
            key = b.get_text(strip=True).lower().replace(" ", "_")
            raw = p.get_text(strip=True)
            val = raw[raw.find(":")+1:].strip()
            result["info"][key] = val
    dlbodz = soup.find("div", class_="dlbodz")
    syn_parts = []
    if dlbodz:
        for el in dlbodz.previous_siblings:
            if el.name == "p":
                t = el.get_text(strip=True)
                if len(t) > 40 and "kusonime" not in t.lower() and "download" not in t.lower()[:10]:
                    syn_parts.append(t)
    result["synopsis"] = " ".join(reversed(syn_parts)).strip() or None
    result["downloads"] = []
    if dlbodz:
        smokeddlrh = dlbodz.find("div", class_="smokeddlrh")
        if smokeddlrh:
            ttl = smokeddlrh.find("div", class_="smokettlrh")
            result["batch_title"] = ttl.get_text(strip=True) if ttl else None
            for url_div in smokeddlrh.find_all("div", class_="smokeurlrh"):
                strong     = url_div.find("strong")
                resolution = strong.get_text(strip=True) if strong else "Unknown"
                links = [{"host": a.get_text(strip=True), "url": a["href"]}
                         for a in url_div.find_all("a", href=True)]
                result["downloads"].append({"resolution": resolution, "links": links})
    return result

def ok(data, page=None, next_page=None):
    out = {"status": "ok", "data": data}
    if page is not None:
        out["page"] = page
        out["next_page"] = next_page
    return jsonify(out)

def err(msg, code=500):
    return jsonify({"status": "error", "message": msg}), code

# ── DOCS PAGE ─────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    base_url = request.host_url.rstrip("/")
    return render_template("index.html", base_url=base_url)

# ── API ENDPOINTS ─────────────────────────────────────────────────────────────
@rate_limit
@app.route("/anime/home")
def home():
    soup = get_soup(BASE)
    if not soup: return err("Gagal scrape home")
    return ok(parse_post_list(soup))

@rate_limit
@app.route("/anime/search/<query>")
def search(query):
    page = max(1, request.args.get("page", 1, type=int))
    url  = f"{BASE}/?s={query}" if page == 1 else f"{BASE}/page/{page}/?s={query}"
    soup = get_soup(url)
    if not soup: return err("Gagal scrape search")
    return ok(parse_post_list(soup), page=page, next_page=get_next_page(soup))

@rate_limit
@app.route("/anime/genre-list")
def genre_list():
    soup = get_soup(f"{BASE}/genre-anime/")
    if not soup: return err("Gagal scrape genre list")
    genres = []
    for a in soup.select(".tagcloud a"):
        href = a["href"]
        if "/genres/" in href:
            genres.append({"name": a.get_text(strip=True), "slug": href.rstrip("/").split("/")[-1], "url": href})
    return ok(genres)

@rate_limit
@app.route("/anime/genre/<slug>")
def genre(slug):
    page = max(1, request.args.get("page", 1, type=int))
    url  = f"{BASE}/genres/{slug}/" if page == 1 else f"{BASE}/genres/{slug}/page/{page}/"
    soup = get_soup(url)
    if not soup: return err("Gagal scrape genre")
    return ok(parse_post_list(soup), page=page, next_page=get_next_page(soup))

@rate_limit
@app.route("/anime/season-list")
def season_list():
    soup = get_soup(f"{BASE}/seasons-list/")
    if not soup: return err("Gagal scrape season list")
    seasons = []
    for a in soup.select(".tagcloud a"):
        href = a["href"]
        if "/seasons/" in href:
            seasons.append({"name": a.get_text(strip=True), "slug": href.rstrip("/").split("/")[-1], "url": href})
    return ok(seasons)

@rate_limit
@app.route("/anime/season/<slug>")
def season(slug):
    page = max(1, request.args.get("page", 1, type=int))
    url  = f"{BASE}/seasons/{slug}/" if page == 1 else f"{BASE}/seasons/{slug}/page/{page}/"
    soup = get_soup(url)
    if not soup: return err("Gagal scrape season")
    return ok(parse_post_list(soup), page=page, next_page=get_next_page(soup))

@rate_limit
@app.route("/anime/all")
def all_anime():
    page = max(1, request.args.get("page", 1, type=int))
    url  = f"{BASE}/list-anime-batch-sub-indo/" if page == 1 else f"{BASE}/list-anime-batch-sub-indo/page/{page}/"
    soup = get_soup(url)
    if not soup: return err("Gagal scrape all anime")
    results = []
    for item in soup.select(".jdlbar ul li a.kmz"):
        href = item["href"]
        results.append({"title": item.get("title") or item.get_text(strip=True), "slug": href.rstrip("/").split("/")[-1], "url": href})
    return ok(results, page=page, next_page=get_next_page(soup))

@rate_limit
@app.route("/anime/detail/<slug>")
def detail(slug):
    soup = get_soup(f"{BASE}/{slug}/")
    if not soup: return err("Gagal scrape detail")
    return ok(parse_detail(soup))

if __name__ == "__main__":
    app.run(debug=True)
