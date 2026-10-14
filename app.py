"""Comparador de precios vs competencia — Streamlit."""
from __future__ import annotations

import io
import os
import queue
import threading
from datetime import datetime

import pandas as pd
import plotly.express as px
import streamlit as st

st.set_page_config(page_title="Comparador de precios", page_icon="💲", layout="wide")

# Secrets -> env (así los threads de scraping también los ven)
for _k in ("GITHUB_TOKEN", "GITHUB_REPO", "GITHUB_BRANCH", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
    try:
        if _k in st.secrets and not os.environ.get(_k):
            os.environ[_k] = str(st.secrets[_k])
    except Exception:
        pass

from core import storage  # noqa: E402
from core.fetchers import detect_platform, fetch_site  # noqa: E402
from core.pipeline import rematch, run_scan  # noqa: E402

PLATFORMS = ["auto", "shopify", "vtex", "woocommerce", "generic"]
SITE_COLS = ["active", "name", "url", "platform", "terms", "max_products"]

# ----------------------------------------------------------------------------
# Login opcional
# ----------------------------------------------------------------------------
_pw = storage.secret("APP_PASSWORD")
if _pw and not st.session_state.get("auth"):
    st.title("💲 Comparador de precios")
    x = st.text_input("Contraseña", type="password")
    if x and x == _pw:
        st.session_state.auth = True
        st.rerun()
    elif x:
        st.error("Contraseña incorrecta")
    st.stop()


# ----------------------------------------------------------------------------
# Estado
# ----------------------------------------------------------------------------
@st.cache_data(ttl=300, show_spinner=False)
def _latest():
    return storage.load_latest()


@st.cache_data(ttl=600, show_spinner=False)
def _history(months):
    return storage.load_history(months)


if "cfg" not in st.session_state:
    st.session_state.cfg = storage.load_config()
cfg = st.session_state.cfg

if "products" not in st.session_state:
    p, m = _latest()
    st.session_state.products, st.session_state.matches = p, m
    st.session_state.source = "snapshot guardado" if not p.empty else "vacío"


def run_live(fn, *args, label="Escaneando…"):
    """Ejecuta fn en un thread y muestra el progreso en vivo."""
    q, out = queue.Queue(), {}

    def target():
        try:
            out["v"] = fn(*args, progress=q.put)
        except Exception as e:  # noqa: BLE001
            out["e"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    lines = []
    with st.status(label, expanded=True) as status:
        box = st.empty()
        while t.is_alive() or not q.empty():
            try:
                lines.append(str(q.get(timeout=0.4)))
                box.code("\n".join(lines[-18:]), language=None)
            except queue.Empty:
                pass
        t.join()
        if "e" in out:
            status.update(label=f"Error: {out['e']}", state="error")
            raise out["e"]
        status.update(label="Listo ✅", state="complete", expanded=False)
    return out["v"]


def sites_to_df(sites):
    df = pd.DataFrame(sites or [], columns=SITE_COLS)
    df["active"] = df["active"].fillna(True).astype(bool)
    df["platform"] = df["platform"].fillna("auto")
    df["terms"] = df["terms"].fillna("").astype(str)
    df["max_products"] = pd.to_numeric(df["max_products"], errors="coerce").fillna(3000).astype(int)
    return df


def df_to_sites(df):
    df = df.dropna(subset=["url"])
    df = df[df["url"].astype(str).str.strip() != ""]
    out = []
    for r in df.to_dict("records"):
        out.append({
            "name": (r.get("name") or storage_name(r["url"])).strip(),
            "url": str(r["url"]).strip(),
            "platform": r.get("platform") or "auto",
            "terms": str(r.get("terms") or ""),
            "max_products": int(r.get("max_products") or 3000),
            "active": bool(r.get("active", True)),
        })
    return out


def storage_name(url):
    host = url.replace("https://", "").replace("http://", "").split("/")[0]
    return host.replace("www.", "").split(".")[0].title()


def sites_editor(key, sites):
    return st.data_editor(
        sites_to_df(sites), key=key, num_rows="dynamic", width="stretch", hide_index=True,
        column_config={
            "active": st.column_config.CheckboxColumn("Activo", default=True, width="small"),
            "name": st.column_config.TextColumn("Nombre", required=True),
            "url": st.column_config.TextColumn("URL", required=True, help="Ej: https://www.dexter.com.ar"),
            "platform": st.column_config.SelectboxColumn("Plataforma", options=PLATFORMS, default="auto"),
            "terms": st.column_config.TextColumn(
                "Filtrar por (coma)", help="Marcas/términos a buscar. Vacío = todo el catálogo"),
            "max_products": st.column_config.NumberColumn("Máx. productos", min_value=10, step=100, default=3000),
        },
    )


def save_cfg():
    try:
        storage.save_config(st.session_state.cfg)
        st.toast("Configuración guardada ✅" + (" en GitHub" if storage.gh_enabled() else ""))
    except Exception as e:  # noqa: BLE001
        st.error(f"No se pudo guardar: {e}")


def site_tester(sites, key):
    names = [s["name"] for s in sites if s.get("url")]
    if not names:
        return
    c1, c2 = st.columns([3, 1])
    sel = c1.selectbox("Probar un sitio", names, key=f"{key}_sel")
    if c2.button("🔍 Probar", key=f"{key}_btn", width="stretch"):
        s = dict(next(x for x in sites if x["name"] == sel))
        s["max_products"] = min(int(s.get("max_products") or 50), 50)
        df = run_live(lambda progress: fetch_site(s, "test", int(cfg["settings"]["timeout"]), progress),
                      label=f"Probando {sel}…")
        st.success(f"{len(df)} productos · plataforma: {df['platform'].iloc[0] if len(df) else '?'}")
        st.dataframe(df[["title", "brand", "skus", "ref", "eans", "price", "list_price", "available", "url"]],
                     width="stretch", hide_index=True,
                     column_config={"url": st.column_config.LinkColumn("Link", display_text="abrir")})


def safe_detect(url):
    try:
        return f"`{detect_platform(url)}`"
    except Exception as e:  # noqa: BLE001
        return f"❌ {e}"


def to_excel(sheets: dict) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, df in sheets.items():
            df.to_excel(xw, sheet_name=name[:31], index=False)
    return buf.getvalue()


# ----------------------------------------------------------------------------
# Sidebar
# ----------------------------------------------------------------------------
with st.sidebar:
    st.header("💲 Comparador")
    st.caption(f"Persistencia: **{'GitHub · ' + storage.secret('GITHUB_REPO') if storage.gh_enabled() else 'local'}**")
    P, M = st.session_state.products, st.session_state.matches
    last = pd.to_datetime(P["scanned_at"], utc=True, format="ISO8601").max() if not P.empty else None
    st.caption(f"Datos: {st.session_state.source}"
               + (f" · {last.tz_convert('America/Argentina/Buenos_Aires'):%d/%m %H:%M}" if last is not None else ""))

    if st.button("🔄 Escanear todo ahora", type="primary", width="stretch"):
        products, matches, errors = run_live(lambda progress: run_scan(cfg, progress))
        st.session_state.products, st.session_state.matches = products, matches
        st.session_state.source = "escaneo en vivo (sin guardar)"
        st.session_state.scan_errors = errors
        st.rerun()
    for e in st.session_state.get("scan_errors", []):
        st.warning(e)

    if st.button("💾 Guardar snapshot", width="stretch",
                 disabled=st.session_state.products.empty):
        with st.spinner("Guardando…"):
            storage.save_snapshot(st.session_state.products, st.session_state.matches)
            _latest.clear(), _history.clear()
            st.session_state.source = "snapshot guardado"
        st.toast("Snapshot guardado ✅")

    if st.button("↩️ Recargar último snapshot", width="stretch"):
        _latest.clear()
        for k in ("products", "matches", "cfg"):
            st.session_state.pop(k, None)
        st.rerun()

    st.divider()
    st.subheader("Matching")
    s = cfg["settings"]
    s["match_threshold"] = st.slider("Score mínimo de match", 50, 100, int(s["match_threshold"]))
    s["doubt_threshold"] = st.slider("Score mínimo 'dudoso'", 30, int(s["match_threshold"]),
                                     min(int(s["doubt_threshold"]), int(s["match_threshold"])))
    if st.button("🧮 Re-matchear (sin scrapear)", width="stretch",
                 disabled=st.session_state.products.empty):
        with st.spinner("Matcheando…"):
            st.session_state.matches = rematch(st.session_state.products, cfg)
        st.rerun()

P, M = st.session_state.products, st.session_state.matches

tab_cmp, tab_ours, tab_comp, tab_hist, tab_rev = st.tabs(
    ["📊 Comparativa", "🏠 Nuestras webs", "🎯 Competencia", "📈 Histórico", "✅ Revisión de matches"])

# ----------------------------------------------------------------------------
# Comparativa
# ----------------------------------------------------------------------------
with tab_cmp:
    if M is None or M.empty:
        st.info("Todavía no hay datos. Cargá tus webs y las de la competencia y tocá **🔄 Escanear todo ahora**.")
    else:
        f1, f2, f3, f4, f5 = st.columns([2, 2, 2, 2, 3])
        comp_sel = f1.multiselect("Competidor", sorted(M["comp_site"].unique()))
        our_sel = f2.multiselect("Nuestra tienda", sorted(M["our_site"].unique()))
        st_sel = f3.multiselect("Posición", ["Más caro", "Más barato", "Igual", "Sin precio"])
        conf_sel = f4.multiselect("Confianza", ["Alta", "Media", "Dudoso"], default=["Alta", "Media"])
        q = f5.text_input("Buscar producto / SKU")
        g1, g2 = st.columns([1, 3])
        only_stock = g1.toggle("Solo con stock en ambos", value=True)
        min_gap = g2.slider("Brecha mínima (%)", 0, 50, 0)

        D = M.copy()
        if comp_sel:
            D = D[D["comp_site"].isin(comp_sel)]
        if our_sel:
            D = D[D["our_site"].isin(our_sel)]
        if st_sel:
            D = D[D["status"].isin(st_sel)]
        if conf_sel:
            D = D[D["confidence"].isin(conf_sel)]
        if only_stock:
            D = D[D["our_available"].astype(bool) & D["comp_available"].astype(bool)]
        if q:
            ql = q.lower()
            D = D[D["our_title"].str.lower().str.contains(ql, na=False)
                  | D["comp_title"].str.lower().str.contains(ql, na=False)
                  | D["our_skus"].str.lower().str.contains(ql, na=False)]
        if min_gap:
            D = D[D["diff_pct"].abs() >= min_gap]

        ours_total = int((P["role"] == "nuestro").sum()) if not P.empty else 0
        k1, k2, k3, k4, k5 = st.columns(5)
        k1.metric("Productos propios", f"{ours_total:,}".replace(",", "."))
        k2.metric("Con match", f"{D['our_key'].nunique():,}".replace(",", "."))
        k3.metric("Comparaciones", f"{len(D):,}".replace(",", "."))
        pct_exp = (D["status"].eq("Más caro").mean() * 100) if len(D) else 0
        k4.metric("% donde somos más caros", f"{pct_exp:.0f}%")
        k5.metric("Brecha mediana", f"{D['diff_pct'].median():+.1f}%" if len(D) else "–",
                  help="(nuestro precio / precio competidor − 1). Positivo = somos más caros")

        if len(D):
            c1, c2 = st.columns(2)
            agg = D.groupby("comp_site").agg(brecha=("diff_pct", "median"), n=("diff_pct", "size")).reset_index()
            fig = px.bar(agg.sort_values("brecha"), x="brecha", y="comp_site", orientation="h", text="n",
                         labels={"brecha": "Brecha mediana % (+ = somos más caros)", "comp_site": ""},
                         title="Brecha de precio por competidor")
            fig.update_traces(texttemplate="n=%{text}", textposition="outside")
            c1.plotly_chart(fig, width="stretch")
            dist = D.groupby(["comp_site", "status"]).size().reset_index(name="n")
            fig2 = px.bar(dist, x="comp_site", y="n", color="status", title="Posición de precio",
                          color_discrete_map={"Más caro": "#d62728", "Más barato": "#2ca02c",
                                              "Igual": "#7f7f7f", "Sin precio": "#cccccc"},
                          labels={"comp_site": "", "n": "Productos"})
            c2.plotly_chart(fig2, width="stretch")

        st.subheader("Detalle")
        cols = ["our_image", "our_site", "our_title", "our_price", "comp_site", "comp_title", "comp_price",
                "diff_pct", "diff_abs", "status", "comp_discount_pct", "our_discount_pct", "comp_available",
                "score", "method", "confidence", "our_url", "comp_url"]
        if "margin_if_match_pct" in D and D["margin_if_match_pct"].notna().any():
            cols.insert(10, "margin_if_match_pct")
        st.dataframe(
            D.sort_values("diff_pct", ascending=False)[cols], width="stretch", hide_index=True,
            height=520,
            column_config={
                "our_image": st.column_config.ImageColumn("", width="small"),
                "our_site": "Tienda", "our_title": "Nuestro producto",
                "our_price": st.column_config.NumberColumn("Nuestro $", format="localized"),
                "comp_site": "Competidor", "comp_title": "Producto competidor",
                "comp_price": st.column_config.NumberColumn("Competidor $", format="localized"),
                "diff_pct": st.column_config.NumberColumn("Brecha %", format="%+.1f%%"),
                "diff_abs": st.column_config.NumberColumn("Brecha $", format="localized"),
                "status": "Posición",
                "margin_if_match_pct": st.column_config.NumberColumn("Margen si igualo %", format="%.1f%%"),
                "comp_discount_pct": st.column_config.NumberColumn("Dto. comp %", format="%.0f%%"),
                "our_discount_pct": st.column_config.NumberColumn("Dto. nuestro %", format="%.0f%%"),
                "comp_available": st.column_config.CheckboxColumn("Stock comp"),
                "score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%.0f"),
                "method": "Método", "confidence": "Confianza",
                "our_url": st.column_config.LinkColumn("Nuestro", display_text="ver"),
                "comp_url": st.column_config.LinkColumn("Comp.", display_text="ver"),
            },
        )

        st.subheader("Matriz de precios")
        piv = D.pivot_table(index=["our_site", "our_title", "our_price"], columns="comp_site",
                            values="comp_price", aggfunc="min").reset_index()
        comp_cols = [c for c in piv.columns if c not in ("our_site", "our_title", "our_price")]
        if comp_cols:
            piv["mín_competencia"] = piv[comp_cols].min(axis=1)
            piv["brecha_vs_mín_%"] = ((piv["our_price"] / piv["mín_competencia"] - 1) * 100).round(1)
            st.dataframe(
                piv.sort_values("brecha_vs_mín_%", ascending=False), width="stretch", hide_index=True,
                column_config={c: st.column_config.NumberColumn(format="localized")
                               for c in comp_cols + ["our_price", "mín_competencia"]}
                | {"brecha_vs_mín_%": st.column_config.NumberColumn(format="%+.1f%%")},
            )
        st.download_button(
            "⬇️ Descargar Excel", to_excel({"Comparativa": D, "Matriz": piv}),
            file_name=f"comparativa_precios_{datetime.now():%Y%m%d_%H%M}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        with st.expander("Productos propios sin match"):
            if not P.empty:
                ours = P[P["role"] == "nuestro"].copy()
                ours["key"] = ours["site"] + "::" + ours["product_id"].astype(str)
                nm = ours[~ours["key"].isin(M["our_key"])]
                st.caption(f"{len(nm)} productos sin equivalente en la competencia")
                st.dataframe(nm[["site", "title", "skus", "price", "available", "url"]], hide_index=True,
                             width="stretch",
                             column_config={"url": st.column_config.LinkColumn(display_text="ver")})

# ----------------------------------------------------------------------------
# Nuestras webs
# ----------------------------------------------------------------------------
with tab_ours:
    st.subheader("🏠 Nuestras webs")
    st.caption("Cargá la URL de cada tienda propia. Shopify, VTEX y WooCommerce se leen por API pública; "
               "cualquier otra por sitemap + datos estructurados.")
    ed = sites_editor("ed_ours", cfg.get("our_sites"))
    b1, b2, _ = st.columns([1, 1, 3])
    if b1.button("💾 Guardar", key="save_ours", type="primary", width="stretch"):
        cfg["our_sites"] = df_to_sites(ed)
        save_cfg()
    if b2.button("🧭 Detectar plataformas", key="det_ours", width="stretch"):
        with st.spinner("Detectando…"):
            for site in df_to_sites(ed):
                st.write(f"**{site['name']}** → {safe_detect(site['url'])}")
    site_tester(cfg.get("our_sites", []), "t_ours")

    st.divider()
    st.subheader("📦 Catálogo propio (opcional)")
    st.caption("Subí un Excel/CSV con columnas **sku**, **ean** y opcional **costo** (sin IVA). "
               "El EAN mejora muchísimo el matching (VTEX expone EAN) y el costo habilita *Margen si igualo*.")
    up = st.file_uploader("Catálogo", type=["xlsx", "csv"], label_visibility="collapsed")
    if up is not None:
        cat = pd.read_csv(up, dtype=str) if up.name.endswith(".csv") else pd.read_excel(up, dtype=str)
        cat.columns = [c.strip().lower() for c in cat.columns]
        missing = {"sku", "ean"} - set(cat.columns)
        if missing:
            st.error(f"Faltan columnas: {', '.join(missing)}")
        else:
            if "costo" not in cat:
                cat["costo"] = ""
            cat = cat[["sku", "ean", "costo"]].fillna("")
            st.dataframe(cat.head(20), hide_index=True)
            if st.button(f"💾 Guardar catálogo ({len(cat)} filas)"):
                storage.save_csv(storage.CATALOG_PATH, cat, "catálogo propio")
                st.toast("Catálogo guardado. Tocá 🧮 Re-matchear para aplicarlo.")

# ----------------------------------------------------------------------------
# Competencia
# ----------------------------------------------------------------------------
with tab_comp:
    st.subheader("🎯 Webs de referencia (competencia)")
    st.caption("Usá **Filtrar por** con las marcas que vendemos (ej: `crocs, reebok`) para no bajar el catálogo entero.")
    ed2 = sites_editor("ed_comp", cfg.get("competitors"))
    b1, b2, _ = st.columns([1, 1, 3])
    if b1.button("💾 Guardar", key="save_comp", type="primary", width="stretch"):
        cfg["competitors"] = df_to_sites(ed2)
        save_cfg()
    if b2.button("🧭 Detectar plataformas", key="det_comp", width="stretch"):
        with st.spinner("Detectando…"):
            for site in df_to_sites(ed2):
                st.write(f"**{site['name']}** → {safe_detect(site['url'])}")
    site_tester(cfg.get("competitors", []), "t_comp")

    if not P.empty:
        st.divider()
        st.subheader("Cobertura del último escaneo")
        cov = P.groupby(["role", "site", "platform"]).agg(
            productos=("product_id", "size"), con_stock=("available", "sum"),
            precio_mediano=("price", "median")).reset_index()
        st.dataframe(cov, hide_index=True, width="stretch",
                     column_config={"precio_mediano": st.column_config.NumberColumn(format="localized")})

# ----------------------------------------------------------------------------
# Histórico
# ----------------------------------------------------------------------------
with tab_hist:
    months = st.radio("Período", [1, 3, 6, 12], index=1, horizontal=True, format_func=lambda m: f"{m} mes(es)")
    H = _history(months)
    if H.empty:
        st.info("El histórico se arma con cada snapshot guardado (GitHub Actions lo hace 3 veces por día).")
    else:
        H = H.copy()
        H["key"] = H["site"] + "::" + H["product_id"].astype(str)
        H["fecha"] = H["scanned_at"].dt.tz_convert("America/Argentina/Buenos_Aires")

        st.subheader("🔔 Cambios de precio de la competencia (últimos 2 escaneos)")
        HC = H[H["role"] == "competencia"].sort_values("scanned_at")
        scans = HC.groupby("site")["scanned_at"].apply(lambda s: sorted(s.unique())[-2:]).to_dict()
        chg = []
        for site, ts in scans.items():
            if len(ts) < 2:
                continue
            a = HC[(HC["site"] == site) & (HC["scanned_at"] == ts[0])].set_index("product_id")
            b = HC[(HC["site"] == site) & (HC["scanned_at"] == ts[1])].set_index("product_id")
            j = a[["price"]].join(b[["price", "title"]], lsuffix="_antes", how="inner")
            j = j[(j["price_antes"] - j["price"]).abs() > 0.5]
            j["var_%"] = ((j["price"] / j["price_antes"] - 1) * 100).round(1)
            j["site"] = site
            chg.append(j.reset_index())
        if chg:
            C = pd.concat(chg)
            if M is not None and not M.empty:   # marcar los que compiten con productos nuestros
                C["key"] = C["site"] + "::" + C["product_id"].astype(str)
                C = C.merge(M[["comp_key", "our_title", "our_price"]].drop_duplicates("comp_key"),
                            left_on="key", right_on="comp_key", how="left")
                only = st.toggle("Solo productos que compiten con los nuestros", value=True)
                if only:
                    C = C[C["our_title"].notna()]
            st.dataframe(C.drop(columns=[c for c in ("key", "comp_key") if c in C]).sort_values("var_%"),
                         hide_index=True, width="stretch",
                         column_config={"var_%": st.column_config.NumberColumn(format="%+.1f%%"),
                                        "price": st.column_config.NumberColumn("Precio ahora", format="localized"),
                                        "price_antes": st.column_config.NumberColumn("Antes", format="localized")})
        else:
            st.caption("Sin cambios entre los últimos dos escaneos.")

        st.subheader("📈 Evolución de un producto")
        if M is not None and not M.empty:
            opts = M.drop_duplicates("our_key").set_index("our_key")
            sel = st.selectbox("Producto propio", opts.index,
                               format_func=lambda k: f"{opts.at[k, 'our_site']} · {opts.at[k, 'our_title']}")
            keys = {sel: "Nosotros"} | {r.comp_key: r.comp_site for r in M[M["our_key"] == sel].itertuples()}
            L = H[H["key"].isin(keys)].copy()
            L["serie"] = L["key"].map(keys)
            if not L.empty:
                fig = px.line(L.sort_values("fecha"), x="fecha", y="price", color="serie", markers=True,
                              labels={"price": "Precio", "fecha": "", "serie": ""})
                st.plotly_chart(fig, width="stretch")

# ----------------------------------------------------------------------------
# Revisión de matches
# ----------------------------------------------------------------------------
with tab_rev:
    st.subheader("✅ Revisión de matches")
    st.caption("Confirmá (**ok**) o rechazá (**no**) matches. Los confirmados se fijan, los rechazados no se "
               "vuelven a proponer. Se guardan en `data/overrides.csv`.")
    if M is None or M.empty:
        st.info("Sin matches todavía.")
    else:
        lvl = st.multiselect("Mostrar", ["Dudoso", "Media", "Alta"], default=["Dudoso", "Media"], key="rev_lvl")
        R = M[M["confidence"].isin(lvl) & (M["method"] != "Manual")].copy()
        if "ai" in st.session_state:
            R = R.merge(st.session_state.ai, on=["our_key", "comp_key"], how="left")
        else:
            R["ai_same"], R["ai_reason"] = None, ""
        R["decision"] = ""
        c1, c2 = st.columns([1, 3])
        if c1.button("🤖 Verificar con Claude", disabled=not storage.secret("ANTHROPIC_API_KEY"),
                     help="Requiere ANTHROPIC_API_KEY en secrets"):
            from core.ai import verify_pairs
            bar = st.progress(0.0)
            out = verify_pairs(R.head(300), progress=bar.progress)
            st.session_state.ai = out[["our_key", "comp_key", "ai_same", "ai_reason"]]
            st.rerun()
        if c2.button("Aplicar sugerencia de Claude a 'decision'", disabled="ai" not in st.session_state):
            st.session_state.apply_ai = True
        if st.session_state.pop("apply_ai", False):
            R["decision"] = R["ai_same"].map({True: "ok", False: "no"}).fillna("")

        edited = st.data_editor(
            R[["decision", "score", "method", "our_site", "our_title", "our_price", "comp_site", "comp_title",
               "comp_price", "ai_same", "ai_reason", "our_url", "comp_url", "our_key", "comp_key"]],
            hide_index=True, width="stretch", height=520, key="rev_editor",
            disabled=[c for c in R.columns if c != "decision"],
            column_config={
                "decision": st.column_config.SelectboxColumn("Decisión", options=["", "ok", "no"], width="small"),
                "score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%.0f"),
                "ai_same": st.column_config.CheckboxColumn("Claude: mismo"),
                "our_url": st.column_config.LinkColumn("Nuestro", display_text="ver"),
                "comp_url": st.column_config.LinkColumn("Comp.", display_text="ver"),
                "our_key": None, "comp_key": None,
            },
        )
        dec = edited[edited["decision"].isin(["ok", "no"])]
        if st.button(f"💾 Guardar {len(dec)} decisiones y re-matchear", type="primary", disabled=dec.empty):
            ov = pd.concat([storage.load_overrides(),
                            dec[["our_key", "comp_key"]].assign(status=dec["decision"])], ignore_index=True)
            storage.save_overrides(ov)
            st.session_state.matches = rematch(P, cfg)
            st.session_state.pop("ai", None)
            st.toast("Guardado ✅")
            st.rerun()
