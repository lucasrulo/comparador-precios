"""Verificación de matches dudosos con Claude (opcional, requiere ANTHROPIC_API_KEY)."""
from __future__ import annotations

import json
import re

import pandas as pd

from .storage import secret

PROMPT = """Sos un experto en catálogo de indumentaria y calzado deportivo en Argentina.
Te paso pares de productos: A es de nuestra tienda, B de un competidor.
Decidí si son EXACTAMENTE el mismo producto (mismo modelo y mismo color; el talle no importa).
Respondé SOLO un JSON: [{{"i": <índice>, "same": true|false, "reason": "<máx 8 palabras>"}}]

Pares:
{pairs}"""


def verify_pairs(df: pd.DataFrame, batch: int = 25, progress=None) -> pd.DataFrame:
    """Devuelve df con columnas ai_same (bool) y ai_reason."""
    import anthropic

    key = secret("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("Falta ANTHROPIC_API_KEY en secrets")
    model = secret("ANTHROPIC_MODEL", "claude-sonnet-4-5")
    client = anthropic.Anthropic(api_key=key)
    df = df.reset_index(drop=True).copy()
    df["ai_same"], df["ai_reason"] = None, ""
    for start in range(0, len(df), batch):
        chunk = df.iloc[start:start + batch]
        lines = [
            f'{i}. A: "{r.our_title}" ({r.our_brand}, SKU {str(r.our_skus)[:60]}) | '
            f'B: "{r.comp_title}" ({r.comp_brand})'
            for i, r in zip(chunk.index, chunk.itertuples())
        ]
        msg = client.messages.create(
            model=model, max_tokens=2000,
            messages=[{"role": "user", "content": PROMPT.format(pairs="\n".join(lines))}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        m = re.search(r"\[.*\]", text, re.S)
        for item in json.loads(m.group(0)) if m else []:
            i = int(item.get("i", -1))
            if i in df.index:
                df.at[i, "ai_same"] = bool(item.get("same"))
                df.at[i, "ai_reason"] = item.get("reason", "")
        if progress:
            progress(min(start + batch, len(df)) / len(df))
    return df
