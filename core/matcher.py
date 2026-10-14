"""
Matching de productos propios vs competencia.

Orden de prioridad por cada (producto propio, competidor):
  1. Manual   -> confirmado por el usuario en data/overrides.csv           (100)
  2. EAN      -> algún EAN en común                                          (100)
  3. Código   -> código de estilo/artículo en común (SKU sin talle, ref)     (85-97)
  4. Nombre   -> similitud de título (rapidfuzz) dentro de la misma marca,
                 con penalización si los colores no coinciden                (0-100)

Los pares rechazados manualmente nunca se vuelven a proponer.
"""
from __future__ import annotations

import re
import unicodedata

import numpy as np
import pandas as pd
from rapidfuzz import fuzz, process

STOPWORDS = {"de", "del", "la", "el", "los", "las", "y", "con", "para", "en", "a", "the", "and", "x"}
COLORS = {
    "negro": "black", "black": "black", "blanco": "white", "white": "white", "azul": "blue",
    "blue": "blue", "navy": "navy", "marino": "navy", "rojo": "red", "red": "red", "verde": "green",
    "green": "green", "gris": "grey", "gray": "grey", "grey": "grey", "rosa": "pink", "pink": "pink",
    "fucsia": "pink", "beige": "beige", "crema": "beige", "bone": "beige", "marron": "brown",
    "brown": "brown", "chocolate": "brown", "amarillo": "yellow", "yellow": "yellow",
    "naranja": "orange", "orange": "orange", "violeta": "purple", "lila": "purple",
    "purple": "purple", "celeste": "lightblue", "turquesa": "lightblue", "bordo": "burgundy",
    "bordeaux": "burgundy", "dorado": "gold", "gold": "gold", "plateado": "silver", "silver": "silver",
    "khaki": "khaki", "caqui": "khaki", "multicolor": "multi",
}
BRAND_NOISE = re.compile(r"[-_ ](ar|arg|argentina|oficial|official)$")


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))


def norm_brand(b) -> str:
    b = strip_accents(str(b or "")).lower().strip()
    b = BRAND_NOISE.sub("", b)
    return {"mhw": "mountain hardwear"}.get(b, b)


def norm_title(t: str, brand: str = "") -> str:
    t = strip_accents(str(t or "")).lower()
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    brand_words = set(norm_brand(brand).split())
    toks = [w for w in t.split() if w not in STOPWORDS and w not in brand_words]
    return " ".join(toks)


def colors_of(t: str) -> set[str]:
    t = strip_accents(str(t or "")).lower()
    out = set()
    for w in re.findall(r"[a-z]+", t):
        for v in (w, w[:-1] if w.endswith("s") else w, (w[:-1] + "o") if w.endswith("a") else w,
                  (w[:-2] + "o") if w.endswith("as") else w, w[:-2] if w.endswith("es") else w):
            if v in COLORS:
                out.add(COLORS[v])
                break
    return out


def _code_candidates(s: str) -> set[str]:
    s = strip_accents(str(s or "")).upper().strip()
    out = set()
    if not s:
        return out
    full = re.sub(r"[^A-Z0-9]", "", s)
    if len(full) >= 5 and sum(c.isdigit() for c in full) >= 3:
        out.add(full)
    parts = [p for p in re.split(r"[-_./\s]+", s) if p]
    for k in range(1, len(parts)):
        pref = "".join(parts[:k])
        if len(pref) >= 5 and sum(c.isdigit() for c in pref) >= 3:
            out.add(pref)
    return out


def codes_of(row) -> set[str]:
    codes = set()
    for sku in str(row.get("skus") or "").split("|"):
        codes |= _code_candidates(sku)
    codes |= _code_candidates(row.get("ref"))
    # códigos dentro del título (ej. "Classic Clog 10001-001")
    for tok in re.findall(r"\b[A-Za-z0-9]{3,}(?:[-_/][A-Za-z0-9]{2,})+\b|\b[A-Za-z0-9]{6,}\b",
                          str(row.get("title") or "")):
        codes |= _code_candidates(tok)
    return codes


def eans_of(row) -> set[str]:
    return {e.strip().lstrip("0") for e in str(row.get("eans") or "").split("|")
            if e.strip() and e.strip().isdigit() and len(e.strip()) >= 8}


def _prep(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy().reset_index(drop=True)
    df["_brand"] = df["brand"].map(norm_brand)
    df["_title"] = [norm_title(t, b) for t, b in zip(df["title"], df["brand"])]
    df["_colors"] = df["title"].map(colors_of)
    df["_codes"] = [codes_of(r) for r in df.to_dict("records")]
    df["_eans"] = [eans_of(r) for r in df.to_dict("records")]
    df["_key"] = df["site"].astype(str) + "::" + df["product_id"].astype(str)
    return df


def _status(diff_pct):
    if pd.isna(diff_pct):
        return "Sin precio"
    if diff_pct > 1:
        return "Más caro"
    if diff_pct < -1:
        return "Más barato"
    return "Igual"


def match_products(ours: pd.DataFrame, comp: pd.DataFrame, threshold: int = 80,
                   doubt_threshold: int = 65, overrides: pd.DataFrame | None = None) -> pd.DataFrame:
    """Devuelve 1 fila por (producto propio, competidor) con el mejor candidato."""
    if ours.empty or comp.empty:
        return pd.DataFrame()
    O, C = _prep(ours), _prep(comp)

    forced, rejected = {}, set()
    if overrides is not None and not overrides.empty:
        for r in overrides.to_dict("records"):
            pair = (str(r["our_key"]), str(r["comp_key"]))
            if str(r.get("status")).lower() in ("ok", "si", "sí", "confirmado"):
                comp_site = str(r["comp_key"]).split("::")[0]
                forced[(pair[0], comp_site)] = pair[1]
            else:
                rejected.add(pair)

    results = []
    for site, Cs in C.groupby("site", sort=False):
        Cs = Cs.reset_index(drop=True)
        key_to_idx = {k: i for i, k in enumerate(Cs["_key"])}
        ean_idx, code_idx = {}, {}
        for i, (es, cs) in enumerate(zip(Cs["_eans"], Cs["_codes"])):
            for e in es:
                ean_idx.setdefault(e, []).append(i)
            for c in cs:
                code_idx.setdefault(c, []).append(i)

        # Matriz de similitud de títulos (vectorizada) — se calcula por marca
        comp_brands = Cs["_brand"].values
        sim_cache = {}

        def fuzzy_scores(brand):
            if brand in sim_cache:
                return sim_cache[brand]
            if brand and brand in set(comp_brands):
                mask = (comp_brands == brand) | (comp_brands == "")
            else:
                mask = np.ones(len(Cs), dtype=bool)
            idxs = np.where(mask)[0]
            q = O.loc[O["_brand"] == brand, "_title"].tolist()
            if not len(idxs) or not q:
                sim_cache[brand] = (idxs, None, {})
                return sim_cache[brand]
            choices = Cs.loc[idxs, "_title"].tolist()
            a = process.cdist(q, choices, scorer=fuzz.token_set_ratio, workers=-1)
            b = process.cdist(q, choices, scorer=fuzz.token_sort_ratio, workers=-1)
            sim = (0.55 * a + 0.45 * b).astype(np.float32)
            qidx = O.index[O["_brand"] == brand].tolist()
            sim_cache[brand] = (idxs, sim, {r: k for k, r in enumerate(qidx)})
            return sim_cache[brand]

        for oi, orow in O.iterrows():
            okey = orow["_key"]
            best, method, score = None, None, 0.0

            # 1. Manual
            fk = forced.get((okey, site))
            if fk and fk in key_to_idx:
                best, method, score = key_to_idx[fk], "Manual", 100.0

            # 2. EAN
            if best is None:
                for e in orow["_eans"]:
                    for ci in ean_idx.get(e, []):
                        if (okey, Cs.at[ci, "_key"]) not in rejected:
                            best, method, score = ci, "EAN", 100.0
                            break
                    if best is not None:
                        break

            # 3. Código de estilo (el código compartido más largo gana)
            if best is None and orow["_codes"]:
                cands = {}
                for c in orow["_codes"]:
                    for ci in code_idx.get(c, []):
                        if (okey, Cs.at[ci, "_key"]) in rejected:
                            continue
                        cands[ci] = max(cands.get(ci, 0), len(c))
                if cands:
                    ci, ln = max(cands.items(), key=lambda kv: kv[1])
                    tsim = fuzz.token_set_ratio(orow["_title"], Cs.at[ci, "_title"])
                    if ln >= 7:
                        best, method, score = ci, "Código", min(97.0, 90 + ln * 0.5)
                    elif tsim >= 60:
                        best, method, score = ci, "Código+Nombre", 85.0

            # 4. Nombre
            if best is None:
                idxs, sim, qrows = fuzzy_scores(orow["_brand"])
                if sim is not None:
                    row_sims = sim[qrows[oi]]
                    order = np.argsort(-row_sims)[:8]
                    for j in order:
                        ci = int(idxs[j])
                        if (okey, Cs.at[ci, "_key"]) in rejected:
                            continue
                        s = float(row_sims[j])
                        oc, cc = orow["_colors"], Cs.at[ci, "_colors"]
                        if oc and cc and not (oc & cc):
                            s -= 15
                        if s > score:
                            best, method, score = ci, "Nombre", s
                if score < doubt_threshold:
                    best = None

            if best is None:
                continue
            crow = Cs.loc[best]
            results.append({
                "our_key": okey, "our_site": orow["site"], "our_title": orow["title"],
                "our_brand": orow["brand"], "our_skus": orow["skus"], "our_price": orow["price"],
                "our_list_price": orow["list_price"], "our_available": orow["available"],
                "our_url": orow["url"], "our_image": orow["image"],
                "comp_key": crow["_key"], "comp_site": site, "comp_title": crow["title"],
                "comp_brand": crow["brand"], "comp_price": crow["price"],
                "comp_list_price": crow["list_price"], "comp_available": crow["available"],
                "comp_url": crow["url"], "comp_image": crow["image"],
                "score": round(score, 1), "method": method,
            })

    M = pd.DataFrame(results)
    if M.empty:
        return M
    M["diff_abs"] = M["our_price"] - M["comp_price"]
    M["diff_pct"] = (M["our_price"] / M["comp_price"] - 1) * 100
    M["comp_discount_pct"] = (1 - M["comp_price"] / M["comp_list_price"]) * 100
    M["our_discount_pct"] = (1 - M["our_price"] / M["our_list_price"]) * 100
    M["status"] = M["diff_pct"].map(_status)
    M["confidence"] = np.select(
        [M["score"] >= 90, M["score"] >= threshold], ["Alta", "Media"], default="Dudoso")
    for c in ("diff_pct", "comp_discount_pct", "our_discount_pct"):
        M[c] = M[c].round(1)
    return M
