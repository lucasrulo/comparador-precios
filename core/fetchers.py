"""
Extractores de catálogo por plataforma.

Todos devuelven un DataFrame con el mismo esquema (ver COLUMNS) para que el
matcher no tenga que saber de dónde vino cada producto.

Plataformas soportadas:
  - shopify      -> /products.json (público, sin token)
  - vtex         -> /api/catalog_system/pub/products/search (público)
  - woocommerce  -> /wp-json/wc/store/v1/products (Store API pública)
  - generic      -> sitemap.xml + JSON-LD schema.org/Product (Tiendanube, Magento, custom...)
"""
from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

COLUMNS = [
    "site", "role", "platform", "product_id", "title", "brand", "skus", "ref",
    "eans", "price", "list_price", "available", "url", "image", "scanned_at",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
}

ProgressCB = Optional[Callable[[str], None]]


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=1.0, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET"]))
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=16))
    s.mount("http://", HTTPAdapter(max_retries=retry, pool_maxsize=16))
    s.headers.update(HEADERS)
    return s


def base_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith("http"):
        url = "https://" + url
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def to_float(v) -> Optional[float]:
    """Convierte '12.999,00', '12999.00', '$ 12.999', 12999 -> float."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d,.\-]", "", str(v))
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):          # 12.999,50 (AR)
            s = s.replace(".", "").replace(",", ".")
        else:                                    # 12,999.50 (US)
            s = s.replace(",", "")
    elif "," in s:
        # 12999,50 -> decimal ; 12,999 -> miles
        s = s.replace(",", ".") if len(s.split(",")[-1]) != 3 else s.replace(",", "")
    elif s.count(".") > 1 or (s.count(".") == 1 and len(s.split(".")[-1]) == 3):
        s = s.replace(".", "")                   # 12.999 -> miles
    try:
        return float(s)
    except ValueError:
        return None


def parse_terms(terms) -> list[str]:
    if not terms:
        return []
    if isinstance(terms, list):
        return [t.strip() for t in terms if str(t).strip()]
    return [t.strip() for t in str(terms).split(",") if t.strip()]


def _matches_terms(text: str, terms: list[str]) -> bool:
    if not terms:
        return True
    t = (text or "").lower()
    return any(term.lower() in t for term in terms)


def _row(**kw) -> dict:
    r = {c: kw.get(c) for c in COLUMNS}
    r["skus"] = "|".join(dict.fromkeys(s for s in (kw.get("skus") or []) if s))
    r["eans"] = "|".join(dict.fromkeys(e for e in (kw.get("eans") or []) if e))
    return r


def _get_json(session, url, timeout, params=None):
    r = session.get(url, params=params, timeout=timeout)
    if r.status_code not in (200, 206):
        return None
    try:
        return r.json()
    except ValueError:
        return None


# ----------------------------------------------------------------------------
# Detección de plataforma
# ----------------------------------------------------------------------------
def detect_platform(url: str, timeout: int = 15, session: requests.Session | None = None) -> str:
    s = session or make_session()
    b = base_url(url)
    try:
        s.get(b, timeout=timeout)
    except requests.RequestException as e:
        raise RuntimeError(f"No se pudo conectar a {b}: {type(e).__name__}") from e
    try:
        j = _get_json(s, f"{b}/products.json", timeout, {"limit": 1})
        if isinstance(j, dict) and "products" in j:
            return "shopify"
    except requests.RequestException:
        pass
    try:
        j = _get_json(s, f"{b}/api/catalog_system/pub/products/search", timeout, {"_from": 0, "_to": 0})
        if isinstance(j, list):
            return "vtex"
    except requests.RequestException:
        pass
    try:
        j = _get_json(s, f"{b}/wp-json/wc/store/v1/products", timeout, {"per_page": 1})
        if isinstance(j, list):
            return "woocommerce"
    except requests.RequestException:
        pass
    return "generic"


# ----------------------------------------------------------------------------
# Shopify
# ----------------------------------------------------------------------------
def fetch_shopify(site, b, terms, max_products, timeout, session, progress: ProgressCB = None):
    rows, page = [], 1
    now = datetime.now(timezone.utc).isoformat()
    while len(rows) < max_products:
        j = _get_json(session, f"{b}/products.json", timeout, {"limit": 250, "page": page})
        prods = (j or {}).get("products") or []
        if not prods:
            break
        for p in prods:
            haystack = " ".join([p.get("title", ""), p.get("vendor", ""), p.get("product_type", ""),
                                 " ".join(p.get("tags") or []) if isinstance(p.get("tags"), list) else str(p.get("tags", ""))])
            if not _matches_terms(haystack, terms):
                continue
            variants = p.get("variants") or []
            avail = [v for v in variants if v.get("available")]
            pool = avail or variants
            prices = [to_float(v.get("price")) for v in pool if to_float(v.get("price"))]
            lists = [to_float(v.get("compare_at_price")) for v in pool if to_float(v.get("compare_at_price"))]
            price = min(prices) if prices else None
            list_price = max(lists) if lists else price
            if list_price and price and list_price < price:
                list_price = price
            imgs = p.get("images") or []
            rows.append(_row(
                site=site, platform="shopify", product_id=str(p.get("id")), title=p.get("title"),
                brand=p.get("vendor"), skus=[v.get("sku") for v in variants],
                ref="", eans=[v.get("barcode") for v in variants if v.get("barcode")],
                price=price, list_price=list_price, available=bool(avail),
                url=f"{b}/products/{p.get('handle')}", image=imgs[0].get("src") if imgs else None,
                scanned_at=now,
            ))
        if progress:
            progress(f"{site}: página {page} · {len(rows)} productos")
        page += 1
        time.sleep(0.4)
    return rows[:max_products]


# ----------------------------------------------------------------------------
# VTEX
# ----------------------------------------------------------------------------
def _parse_vtex_product(site, b, p, now):
    items = p.get("items") or []
    eans, skus, offers = [], [], []
    for it in items:
        if it.get("ean"):
            eans.append(str(it["ean"]))
        for ref in it.get("referenceId") or []:
            if ref.get("Value"):
                skus.append(str(ref["Value"]))
        skus.append(str(it.get("itemId", "")))
        for s in it.get("sellers") or []:
            co = s.get("commertialOffer") or {}
            pr = to_float(co.get("Price"))
            if pr:
                offers.append((pr, to_float(co.get("ListPrice")) or pr, (co.get("AvailableQuantity") or 0) > 0))
    avail = [o for o in offers if o[2]]
    pool = avail or offers
    price = min(o[0] for o in pool) if pool else None
    list_price = max(o[1] for o in pool) if pool else None
    img = None
    if items and items[0].get("images"):
        img = items[0]["images"][0].get("imageUrl")
    link = p.get("link") or f"{b}/{p.get('linkText', '')}/p"
    return _row(
        site=site, platform="vtex", product_id=str(p.get("productId")), title=p.get("productName"),
        brand=p.get("brand"), skus=skus, ref=p.get("productReference") or "", eans=eans,
        price=price, list_price=list_price, available=bool(avail), url=link, image=img, scanned_at=now,
    )


def fetch_vtex(site, b, terms, max_products, timeout, session, progress: ProgressCB = None):
    now = datetime.now(timezone.utc).isoformat()
    seen, rows = set(), []
    for term in (terms or [None]):
        start = 0
        while start < 2500 and len(rows) < max_products:   # VTEX corta en _from=2500
            params = {"_from": start, "_to": start + 49}
            if term:
                params["ft"] = term
            j = _get_json(session, f"{b}/api/catalog_system/pub/products/search", timeout, params)
            if not j:
                break
            for p in j:
                pid = str(p.get("productId"))
                if pid in seen:
                    continue
                seen.add(pid)
                rows.append(_parse_vtex_product(site, b, p, now))
            if progress:
                progress(f"{site}: '{term or '*'}' {start + len(j)} · {len(rows)} productos")
            if len(j) < 50:
                break
            start += 50
            time.sleep(0.3)
    return rows[:max_products]


# ----------------------------------------------------------------------------
# WooCommerce (Store API)
# ----------------------------------------------------------------------------
def fetch_woocommerce(site, b, terms, max_products, timeout, session, progress: ProgressCB = None):
    now = datetime.now(timezone.utc).isoformat()
    seen, rows = set(), []
    for term in (terms or [None]):
        page = 1
        while len(rows) < max_products:
            params = {"per_page": 100, "page": page}
            if term:
                params["search"] = term
            j = _get_json(session, f"{b}/wp-json/wc/store/v1/products", timeout, params)
            if not j:
                break
            for p in j:
                pid = str(p.get("id"))
                if pid in seen:
                    continue
                seen.add(pid)
                pr = p.get("prices") or {}
                div = 10 ** int(pr.get("currency_minor_unit", 0) or 0)
                price = (to_float(pr.get("price")) or 0) / div or None
                lp = (to_float(pr.get("regular_price")) or 0) / div or price
                brands = p.get("brands") or []
                imgs = p.get("images") or []
                rows.append(_row(
                    site=site, platform="woocommerce", product_id=pid,
                    title=BeautifulSoup(p.get("name", ""), "lxml").get_text(),
                    brand=brands[0].get("name") if brands else None, skus=[p.get("sku")], ref="",
                    eans=[], price=price, list_price=lp, available=bool(p.get("is_in_stock")),
                    url=p.get("permalink"), image=imgs[0].get("src") if imgs else None, scanned_at=now,
                ))
            if progress:
                progress(f"{site}: '{term or '*'}' pág {page} · {len(rows)} productos")
            if len(j) < 100:
                break
            page += 1
            time.sleep(0.3)
    return rows[:max_products]


# ----------------------------------------------------------------------------
# Genérico: sitemap + JSON-LD
# ----------------------------------------------------------------------------
PRODUCT_URL_HINTS = ("/product", "/producto", "/productos/", "/p/", "/item", ".html")


def _sitemap_urls(b, session, timeout, limit_sitemaps=40):
    candidates = []
    try:
        r = session.get(f"{b}/robots.txt", timeout=timeout)
        if r.ok:
            candidates += re.findall(r"(?im)^sitemap:\s*(\S+)", r.text)
    except requests.RequestException:
        pass
    candidates += [f"{b}/sitemap.xml", f"{b}/sitemap_index.xml"]
    queue, seen, urls = list(dict.fromkeys(candidates)), set(), []
    while queue and len(seen) < limit_sitemaps:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        try:
            r = session.get(sm, timeout=timeout)
            if not r.ok:
                continue
            root = ET.fromstring(r.content)
        except Exception:
            continue
        tag = root.tag.split("}")[-1]
        locs = [el.text.strip() for el in root.iter() if el.tag.split("}")[-1] == "loc" and el.text]
        if tag == "sitemapindex":
            # priorizar sitemaps de productos
            locs.sort(key=lambda u: 0 if "product" in u.lower() else 1)
            queue.extend(locs)
        else:
            urls.extend(locs)
    return list(dict.fromkeys(urls))


def _iter_jsonld(soup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (ValueError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack.extend(d)
            elif isinstance(d, dict):
                if "@graph" in d:
                    stack.extend(d["@graph"] if isinstance(d["@graph"], list) else [d["@graph"]])
                yield d


def parse_product_html(site, url, html, now):
    soup = BeautifulSoup(html, "lxml")
    for d in _iter_jsonld(soup):
        t = d.get("@type")
        types = t if isinstance(t, list) else [t]
        if "Product" not in types and "ProductGroup" not in types:
            continue
        offers = d.get("offers")
        if not offers and isinstance(d.get("hasVariant"), list):
            offers = [v.get("offers") for v in d["hasVariant"] if isinstance(v, dict) and v.get("offers")]
        if isinstance(offers, dict):
            offers = [offers]
        flat = []
        for o in offers or []:
            if isinstance(o, list):
                flat.extend(o)
            elif isinstance(o, dict) and isinstance(o.get("offers"), list):   # AggregateOffer
                flat.append(o)
                flat.extend(o["offers"])
            else:
                flat.append(o)
        offers = flat
        prices, lists, avail = [], [], False
        for o in offers:
            if not isinstance(o, dict):
                continue
            pr = to_float(o.get("price") or o.get("lowPrice"))
            if pr:
                prices.append(pr)
            hp = to_float(o.get("highPrice"))
            if hp:
                lists.append(hp)
            ps = o.get("priceSpecification")
            if isinstance(ps, list):
                for spec in ps:
                    lp = to_float((spec or {}).get("price"))
                    if lp:
                        lists.append(lp)
            if "InStock" in str(o.get("availability", "")):
                avail = True
        brand = d.get("brand")
        if isinstance(brand, dict):
            brand = brand.get("name")
        elif isinstance(brand, list) and brand:
            brand = brand[0].get("name") if isinstance(brand[0], dict) else brand[0]
        img = d.get("image")
        if isinstance(img, list):
            img = img[0] if img else None
        if isinstance(img, dict):
            img = img.get("url")
        eans = [str(d.get(k)) for k in ("gtin13", "gtin", "gtin14", "gtin12", "gtin8") if d.get(k)]
        price = min(prices) if prices else None
        lp = max(lists) if lists else price
        if lp and price and lp < price:
            lp = price
        return _row(
            site=site, platform="generic", product_id=str(d.get("productID") or d.get("sku") or url),
            title=d.get("name"), brand=brand, skus=[d.get("sku")], ref=d.get("mpn") or "",
            eans=eans, price=price, list_price=lp, available=avail or not offers, url=url,
            image=img, scanned_at=now,
        )
    # Fallback: meta tags OpenGraph
    def meta(prop):
        m = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        return m.get("content") if m else None
    price = to_float(meta("product:price:amount") or meta("og:price:amount"))
    if price:
        return _row(site=site, platform="generic", product_id=url, title=meta("og:title"),
                    brand=meta("product:brand"), skus=[], ref="", eans=[], price=price,
                    list_price=price, available=True, url=url, image=meta("og:image"), scanned_at=now)
    return None


def fetch_generic(site, b, terms, max_products, timeout, session, progress: ProgressCB = None, workers=8):
    now = datetime.now(timezone.utc).isoformat()
    urls = _sitemap_urls(b, session, timeout)
    prod_urls = [u for u in urls if any(h in u.lower() for h in PRODUCT_URL_HINTS)] or urls
    if terms:
        slug_terms = [re.sub(r"\s+", "-", t.lower()) for t in terms]
        prod_urls = [u for u in prod_urls if any(t in u.lower() for t in slug_terms)]
    prod_urls = prod_urls[:max_products]
    if progress:
        progress(f"{site}: {len(prod_urls)} URLs de producto en sitemap")
    rows = []

    def work(u):
        try:
            r = session.get(u, timeout=timeout, headers={"Accept": "text/html"})
            return parse_product_html(site, u, r.text, now) if r.ok else None
        except requests.RequestException:
            return None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, u) for u in prod_urls]
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            if r:
                rows.append(r)
            if progress and i % 25 == 0:
                progress(f"{site}: {i}/{len(prod_urls)} páginas · {len(rows)} productos")
    return rows


# ----------------------------------------------------------------------------
# Dispatcher
# ----------------------------------------------------------------------------
FETCHERS = {
    "shopify": fetch_shopify,
    "vtex": fetch_vtex,
    "woocommerce": fetch_woocommerce,
    "generic": fetch_generic,
}


def fetch_site(cfg: dict, role: str, timeout: int = 20, progress: ProgressCB = None) -> pd.DataFrame:
    """cfg: {name, url, platform, terms, max_products}"""
    session = make_session()
    b = base_url(cfg["url"])
    platform = (cfg.get("platform") or "auto").lower()
    if platform == "auto":
        platform = detect_platform(b, timeout, session)
        if progress:
            progress(f"{cfg['name']}: plataforma detectada → {platform}")
    fn = FETCHERS.get(platform, fetch_generic)
    rows = fn(cfg["name"], b, parse_terms(cfg.get("terms")), int(cfg.get("max_products") or 2000),
              timeout, session, progress)
    df = pd.DataFrame(rows, columns=COLUMNS)
    df["role"] = role
    df["platform"] = platform
    return df


def fetch_all(sites: list[dict], role: str, timeout: int = 20, progress: ProgressCB = None,
              parallel: int = 4) -> pd.DataFrame:
    active = [s for s in sites if s.get("active", True) and s.get("url")]
    frames, errors = [], []
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as ex:
        futs = {ex.submit(fetch_site, s, role, timeout, progress): s for s in active}
        for f in as_completed(futs):
            s = futs[f]
            try:
                df = f.result()
                frames.append(df)
                if progress:
                    progress(f"✅ {s['name']}: {len(df)} productos")
            except Exception as e:  # noqa: BLE001
                errors.append(f"{s['name']}: {e}")
                if progress:
                    progress(f"❌ {s['name']}: {e}")
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)
    out.attrs["errors"] = errors
    return out
