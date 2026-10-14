# 💲 Comparador de precios vs competencia

App Streamlit + GitHub Actions que lee el catálogo de **nuestras tiendas** y de **webs de la competencia**, empareja los productos (EAN → código de estilo → nombre) y muestra quién es más caro, por cuánto, y cómo se movieron los precios en el tiempo.

## Qué hace

| Pestaña | Qué hacés |
|---|---|
| 📊 Comparativa | KPIs, brecha por competidor, detalle producto a producto, matriz de precios y descarga a Excel |
| 🏠 Nuestras webs | Cargás las URLs de nuestras tiendas. También podés subir el catálogo (sku, ean, costo) |
| 🎯 Competencia | Cargás las webs de referencia y un filtro por marca (ej. `crocs, reebok`) |
| 📈 Histórico | Cambios de precio de la competencia entre escaneos y evolución de cada producto |
| ✅ Revisión | Confirmás o rechazás matches dudosos (opcional: Claude los verifica por vos) |

**Plataformas que lee sin token:** Shopify (`/products.json`), VTEX (API pública de catálogo, que expone EAN y referencia), WooCommerce (Store API) y cualquier otra web vía `sitemap.xml` + JSON-LD (Tiendanube, Magento, sitios a medida). La plataforma se detecta sola (`auto`).

**Cómo empareja productos** (`core/matcher.py`):
1. **Manual**: lo que confirmaste en Revisión
2. **EAN** en común (subí `catalogo.csv` con los EAN para aprovecharlo con sitios VTEX)
3. **Código de estilo**: SKU sin el talle o referencia (ej. `10001-001-M4W6` ↔ `10001-001`)
4. **Nombre**: similitud difusa dentro de la misma marca, con penalización si el color no coincide

## Setup (10 min)

```bash
git clone https://github.com/lucasrulo/comparador-precios && cd comparador-precios
pip install -r requirements.txt
streamlit run app.py          # local: guarda en disco
python scan.py                # escaneo batch por consola
```

### Deploy en Streamlit Cloud
1. Subí el repo a GitHub y creá la app en share.streamlit.io con `app.py` como archivo principal.
2. En **Settings → Secrets** pegá esto (está en `.streamlit/secrets.toml.example`):
   ```toml
   APP_PASSWORD = "..."
   GITHUB_TOKEN = "github_pat_..."   # fine-grained PAT, Contents: Read & write sobre este repo
   GITHUB_REPO  = "lucasrulo/comparador-precios"
   GITHUB_BRANCH = "main"
   ANTHROPIC_API_KEY = "sk-ant-..."  # opcional
   ```
   Con `GITHUB_TOKEN` todo lo que cargues en la web (sitios, catálogo, decisiones de matching y snapshots) se commitea al repo, así no se pierde cuando Streamlit Cloud reinicia la app.

### Escaneo automático
`.github/workflows/scan.yml` corre `scan.py` a las **07, 13 y 19 hs (AR)** y commitea en `data/` el snapshot y el histórico mensual (`data/history/YYYY-MM.parquet`). Para correrlo a mano: **Actions → Escaneo de precios → Run workflow**. Necesita **Settings → Actions → General → Workflow permissions: Read and write**.

Además, en la app tenés **🔄 Escanear todo ahora** (en vivo) y **💾 Guardar snapshot**.

## Archivos

```
app.py               UI Streamlit
scan.py              escaneo batch (GitHub Actions)
config.yaml          webs propias / competencia / umbrales (editable desde la app)
core/fetchers.py     extractores Shopify / VTEX / Woo / genérico
core/matcher.py      emparejamiento
core/pipeline.py     orquestación + catálogo propio + margen
core/storage.py      persistencia local o GitHub
core/ai.py           verificación con Claude (opcional)
data/                snapshots, histórico, overrides.csv, catalogo.csv
```

## Catálogo propio (opcional, recomendado)
CSV/Excel con columnas `sku, ean, costo` (costo sin IVA, por SKU de variante). Con eso:
- los matches por **EAN** contra sitios VTEX salen exactos
- aparece **Margen si igualo %** = 1 − costo / (precio competidor / 1,21)

## Limitaciones
- Toma el **precio de lista web** (precio de venta del producto). No contempla descuentos por medio de pago, cuotas o cupones.
- VTEX corta la búsqueda en 2.500 resultados por término: usá términos más finos (`crocs clog, crocs sandalia`) si una marca tiene más productos.
- Sitios con anti-bot fuerte (Cloudflare challenge) pueden devolver 0 productos en modo genérico. Usá **Probar** en la pestaña Competencia para validar cada sitio antes de dejarlo activo.
- MercadoLibre no está incluido: su búsqueda pública hoy requiere token. Se puede sumar reutilizando tu app de MeLi.
