# processor/utils.py
# Shared utilities and lightweight helpers used across the processor package.
# Questo modulo non dipende da spaCy / fitz / requests, per evitare cicli.

from __future__ import annotations

import os
import re
import csv
import json
import unicodedata
import logging
from collections import OrderedDict
from typing import Dict, List, Tuple, Optional

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# -------------------------------------------------
# Costanti comuni per la logica PDF
# -------------------------------------------------

HEADER_FALLBACK_RATIO = 0.15
FOOTER_FALLBACK_RATIO = 0.10
SIDE_MARGIN_PT = 36

# -------------------------------------------------
# Helper testuali
# -------------------------------------------------

def normalize_name(s: str) -> str:
    """
    Usata durante l'estrazione per normalizzare in modo "umano": rimuove accenti,
    comprime spazi e porta a minuscolo.
    """
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()


def _norm(s: str) -> str:
    """
    Variante usata nel geocoding/cache. È sostanzialmente normalize_name,
    ma teniamo il nome separato per compatibilità col codice originale.
    """
    return normalize_name(s)


def ordered_unique(seq):
    """Preserva l'ordine della prima occorrenza."""
    return list(OrderedDict.fromkeys(seq).keys())


# -------------------------------------------------
# Gestione range pagine
# -------------------------------------------------

def parse_include_ranges(ranges: str, total_pages: int) -> Optional[List[Tuple[int,int]]]:
    """
    Parsea input utente come range 1-based (es. '51-104,115-136').
    Ritorna lista di tuple 0-based inclusive ([(50,103),(114,135)] ...).
    """
    if not ranges:
        return None
    out: List[Tuple[int,int]] = []
    for chunk in re.split(r"[,\s]+", ranges.strip()):
        if not chunk:
            continue
        if "-" in chunk:
            a, b = chunk.split("-", 1)
            try:
                a = max(1, int(a))
                b = min(total_pages, int(b))
            except Exception:
                continue
            if a > b:
                a, b = b, a
            out.append((a-1, b-1))
        else:
            try:
                k = max(1, min(total_pages, int(chunk)))
                out.append((k-1, k-1))
            except Exception:
                continue
    # merge contigui
    out.sort()
    merged: List[Tuple[int,int]] = []
    for a,b in out:
        if not merged or a > merged[-1][1] + 1:
            merged.append((a,b))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
    return merged


def index_in_includes(idx: int, includes: Optional[List[Tuple[int,int]]]) -> bool:
    """True se idx (0-based) rientra nei range dati."""
    if not includes:
        return True
    for a,b in includes:
        if a <= idx <= b:
            return True
    return False


# -------------------------------------------------
# Output helper: elenco file prodotti da una sessione
# -------------------------------------------------

def list_outputs(job_dir: str) -> Dict[str, str]:
    """
    Restituisce i file disponibili con il relativo path scaricabile dal server.
    Inclusi i nuovi file annale_attestazioni.json e annale_tagged.json.
    """
    out: Dict[str, str] = {}
    candidate_names = [
        "annale_marked.pdf",
        "annale_toponimi.csv",
        "annale_toponimi_filtered.csv",          # CSV filtrato tramite esclusioni
        "annale_toponimi_esclusi.csv",
        "annale_toponimi.ndjson",
        "annale_toponimi.geojson",
        "annale_toponimi_grouped.geojson",       # geocoding raggruppato
        "annale_toponimi_osm_rejects.csv",
        "annale_toponimi_grouped_rejects.csv",   # reject raggruppato
        "geocache_toponyms.json",
        "annale_osm_debug_last.json",
        "geocode_progress.json",
        "annale_attestazioni.json",              # NEW: indice attestazioni per pagina
        "annale_tagged.json",                    # NEW: snippet testuali/tagging
        "annale_user_exclusions.json",           # stato esclusioni utente
    ]
    for name in candidate_names:
        p = os.path.join(job_dir, name)
        if os.path.exists(p):
            jid = os.path.basename(job_dir)
            out[name] = f"/files/{jid}/{name}"
    return out


# -------------------------------------------------
# Aggregazione / conteggio toponimi
# -------------------------------------------------

def group_toponyms(csv_path: str) -> List[Dict]:
    """
    Legge un CSV stile annale_toponimi*.csv (pagina,anno,id,luogo)
    e restituisce una lista ordinata di {name,count}.
    Serve a popolare la lista "Toponimi estratti".
    """
    if not os.path.exists(csv_path):
        return []
    counts: Dict[str,int] = {}
    with open(csv_path, "r", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        for r in rdr:
            luoghi = (r.get("luogo") or "").strip()
            if not luoghi:
                continue
            for t in [x.strip() for x in luoghi.split(";") if x.strip()]:
                counts[t] = counts.get(t, 0) + 1

    items = [{"name": k, "count": v} for k, v in counts.items()]
    # ordina prima per frequenza desc, poi alfabetico
    items.sort(key=lambda x: (-x["count"], x["name"].lower()))
    return items
