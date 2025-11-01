# processor/geocode.py
"""
FASE 2: Geocoding (Nominatim / OSM) + produzione GeoJSON.

Offriamo due modalità:

1. phase_geocode(out_dir)  [LEGACY]
   - legge annale_toponimi.csv "grezzo" (non filtrato)
   - geocoda toponimo per ogni pagina/id
   - produce annale_toponimi.ndjson e annale_toponimi.geojson
   - produce annale_toponimi_osm_rejects.csv

2. phase_geocode_grouped(out_dir, progress_cb=None)  [NUOVA]
   - usa il CSV attivo (filtrato se l'utente ha escluso dei toponimi o certe
     attestazioni)
   - geocoda ogni toponimo una sola volta
   - costruisce annale_toponimi_grouped.geojson
   - produce annale_toponimi_grouped_rejects.csv
   - supporta un callback progress_cb(done, total, current_term)
"""

from __future__ import annotations

import os
import json
import time
import csv
import logging
from typing import Dict, List, Tuple, Optional, Set

import requests

from .utils import (
    _norm,
    ordered_unique,
)
from .exclusions import choose_active_csv, load_user_exclusions_full

logger = logging.getLogger(__name__)

# ---------------- Config geocoding ----------------
NOMINATIM_EMAIL = "erasmo.difonso@libero.it"
NOMINATIM_BASE_URL = os.environ.get("NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org")
NOMINATIM_COUNTRYCODES = "it"
GEOCODING_SLEEP_SECONDS = 1.0

PRIMARY_PLACE_TYPES = {
    "city","town","village","hamlet","municipality",
    "city_district","borough","quarter","suburb","neighbourhood","neighborhood","locality"
}
SECONDARY_PLACE_TYPES = {
    "county","province","region","state","country","island",
    "archipelago","state_district","department"
}
ACCEPT_BOUNDARY_TYPES = {"administrative"}

# esonimi IT → EN minimi
EXONYMS_IT = {
    "parigi": "paris",
    "berna": "bern",
    "francia": "france",
    "spagna": "spain",
    "brasile": "brazil",
    "svizzera": "switzerland",
    "libia": "libya",
    "puglie": "puglia",
    "regno unito": "united kingdom",
    "stati uniti": "united states",
    "paesi bassi": "netherlands",
}

# ---------------- Helpers ranking/normalizzazione ----------------

def _safe_float(x):
    try:
        return float(x)
    except Exception:
        return 0.0


def _split_semicolon(s: str) -> list:
    return [t.strip() for t in (s or "").split(";") if t.strip()]


def _collect_names_for_match(hit: dict) -> set:
    """
    Raccoglie tutte le varianti nome utili per confronto con la query.
    """
    names = set()
    nd = hit.get("namedetails") or {}
    disp = (hit.get("display_name") or "").split(",")[0]

    # base
    for k in ("name","official_name","short_name"):
        if nd.get(k):
            names.add(_norm(nd.get(k)))

    # multivalori
    for k in ("alt_name","old_name","loc_name"):
        for item in _split_semicolon(nd.get(k) or ""):
            names.add(_norm(item))

    # localizzazioni (name:xx)
    for k, v in nd.items():
        if k.startswith("name:") and v:
            names.add(_norm(v))

    # prima parte del display_name
    if disp:
        names.add(_norm(disp))

    return names


def _admin_level(hit: dict) -> int:
    """
    Prova a leggere admin_level (come intero).
    """
    for container in (hit.get("extratags") or {}, hit):
        al = container.get("admin_level")
        if al is not None:
            try:
                return int(al)
            except Exception:
                pass
    return 0


def _rank_tier(hit: dict) -> int:
    """
    Tier più basso = migliore.
    0 = città/centri abitati o boundary amministrativi nazionali/regionali,
    1 = boundary amministrativi provinciali/comunali,
    2 = region/province/county 'place',
    3 = POI utili (stazioni, aeroporti, ecc.),
    8 = fallback (solo punto),
    9 = scartabile.
    """
    cls = (hit.get("class") or "").lower()
    typ = (hit.get("type") or "").lower()

    if cls == "place" and typ in PRIMARY_PLACE_TYPES:
        return 0

    if cls == "boundary" and typ in ACCEPT_BOUNDARY_TYPES:
        al = _admin_level(hit)
        if 0 < al <= 4:
            return 0  # country / macroregioni
        if 5 <= al <= 6:
            return 1  # province / comuni
        return 2

    if cls == "place" and typ in SECONDARY_PLACE_TYPES:
        return 2

    if (cls, typ) in {
        ("railway","station"), ("aeroway","aerodrome"), ("aeroway","airport"),
        ("tourism","attraction"), ("natural","peak"), ("natural","bay")
    }:
        return 3

    if "lat" in hit and "lon" in hit:
        return 8

    return 9


def _name_match_strength(q_norm: str, hit: dict) -> int:
    """
    2 = match esatto (o quasi) nel nome o varianti,
    1 = match parziale forte o nell'address.*,
    0 = niente di convincente.
    """
    names = _collect_names_for_match(hit)
    if q_norm in names:
        return 2

    for n in names:
        if len(q_norm) >= 4 and (q_norm in n or n in q_norm):
            return 1

    addr = hit.get("address") or {}
    for k in ("country","state","region","province","county","city","town","village"):
        v = addr.get(k)
        if v and len(q_norm) >= 4 and q_norm in _norm(v):
            return 1

    return 0


def _rank_key(hit: dict, q_norm: str) -> tuple:
    """
    Ordiniamo i risultati Nominatim con:
    (tier asc, name_match desc, importance desc)
    """
    tier = _rank_tier(hit)
    imp  = _safe_float(hit.get("importance"))
    nm   = _name_match_strength(q_norm, hit)
    return (tier, -nm, -imp)


def _normalize_hit_with_geom(hit: dict, raw_name: str) -> dict:
    """
    Normalizza un risultato di Nominatim in un dizionario (con geojson se esiste).
    """
    out = {
        "lat": _safe_float(hit.get("lat")),
        "lon": _safe_float(hit.get("lon")),
        "display_name": hit.get("display_name", raw_name),
        "class": hit.get("class"),
        "type": hit.get("type"),
        "importance": hit.get("importance"),
        "osm_id": hit.get("osm_id"),
        "osm_type": hit.get("osm_type"),
        "raw": raw_name,
        "address": hit.get("address"),
        "namedetails": hit.get("namedetails"),
        "admin_level": _admin_level(hit) or None,
    }

    if "geojson" in hit and isinstance(hit["geojson"], dict):
        out["geometry"] = hit["geojson"]
        out["geometry_source"] = "polygon"
    else:
        out["geometry"] = {
            "type": "Point",
            "coordinates": [out["lon"], out["lat"]],
        }
        out["geometry_source"] = "centroid"

    return out


# ---------------- Chiamata a Nominatim ----------------

def _dump_debug(obj: dict, out_dir: str):
    """
    Scrive info di debug su annale_osm_debug_last.json (best-effort).
    """
    try:
        with open(
            os.path.join(out_dir, "annale_osm_debug_last.json"),
            "w",
            encoding="utf-8"
        ) as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def nominatim_search(session: requests.Session, q: str, countrycodes: Optional[str],
                     debug_label: str, out_dir: str) -> list:
    """
    Query grezza a Nominatim /search.
    """
    url = NOMINATIM_BASE_URL.rstrip("/") + "/search"
    headers = {"User-Agent": f"annale-toponimi/3.2 ({NOMINATIM_EMAIL})"}
    params = {
        "q": q,
        "format": "jsonv2",
        "limit": 15,
        "addressdetails": 1,
        "namedetails": 1,
        "extratags": 1,
        "polygon_geojson": 1,
        "polygon_threshold": 0.005,
        "accept-language": "it",
        "dedupe": 1,
    }
    if countrycodes:
        params["countrycodes"] = countrycodes

    try:
        r = session.get(url, headers=headers, params=params, timeout=25)
    except Exception as e:
        _dump_debug(
            {"stage": "network_exception", "query": q,
             "label": debug_label, "error": str(e)},
            out_dir
        )
        return [{"__http_error__": "network"}]

    if r.status_code != 200:
        _dump_debug(
            {"stage": "http_error", "status": r.status_code, "query": q,
             "label": debug_label, "text": r.text[:8000]},
            out_dir
        )
        return [{"__http_error__": r.status_code}]

    try:
        arr = r.json()
        if isinstance(arr, list):
            return arr
        else:
            _dump_debug(
                {"stage": "not_list_json", "label": debug_label,
                 "query": q, "data": arr},
                out_dir
            )
            return []
    except Exception:
        _dump_debug(
            {"stage": "not_json", "label": debug_label,
             "query": q, "text": r.text[:8000]},
            out_dir
        )
        return []


def geocode_name_robust(
    name: str,
    session: requests.Session,
    out_dir: str
) -> Tuple[Optional[dict], Optional[str]]:
    """
    Tenta:
    - bias Italia
    - "<name>, Italia"
    - globale
    - eventuale esonimo (Parigi->Paris)
    Poi seleziona il best candidate tramite ranking.
    """
    q_norm = _norm(name)
    variants = [
        ("it", name, NOMINATIM_COUNTRYCODES or None),
        ("it_suffix", f"{name}, Italia", "it"),
        ("global", name, None),
    ]

    alias = EXONYMS_IT.get(q_norm)
    if alias and alias != name:
        variants.append(("exonym", alias, None))

    all_hits: List[dict] = []
    seen_keys = set()

    for label, q, cc in variants:
        arr = nominatim_search(session, q, cc, label, out_dir)
        time.sleep(GEOCODING_SLEEP_SECONDS)

        if arr and "__http_error__" in arr[0]:
            continue

        for h in arr or []:
            if not isinstance(h, dict):
                continue
            key = (h.get("osm_type"), h.get("osm_id"))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            all_hits.append(h)

    if not all_hits:
        return None, "no_results"

    all_hits.sort(key=lambda h: _rank_key(h, q_norm))
    best = all_hits[0]

    if _rank_tier(best) >= 9:
        _dump_debug(
            {"stage": "final_reject_all", "name": name, "top": best},
            out_dir
        )
        return None, "no_accepted_candidate"

    return _normalize_hit_with_geom(best, name), None


def geocode_with_cache(
    name: str,
    cache: dict,
    session: requests.Session,
    out_dir: str
) -> Tuple[Optional[dict], Optional[str], dict]:
    """
    Geocoding con cache (geocache_toponyms.json).
    """
    norm_key = _norm(name)
    cache_path = os.path.join(out_dir, "geocache_toponyms.json")

    if norm_key in cache:
        entry = cache[norm_key]
        if isinstance(entry, dict) and entry.get("ok") is True:
            return entry["data"], None, cache
        if isinstance(entry, dict) and entry.get("ok") is False:
            return None, entry.get("reason","cached_reject"), cache
        if isinstance(entry, dict) and ("class" in entry or "lat" in entry):
            return entry, None, cache
        if entry is None:
            return None, "cached_none", cache

    data, reason = geocode_name_robust(name, session, out_dir)
    if data is not None:
        cache[norm_key] = {"ok": True, "data": data}
    else:
        cache[norm_key] = {"ok": False, "reason": reason or "rejected"}

    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

    return (data if data is not None else None), reason, cache


# ---------------- Gestione NDJSON/GeoJSON legacy ----------------

def read_processed_keys_from_ndjson(path: str) -> Set[str]:
    """
    Ritorna le chiavi già processate (pagina|id|norm(termine))
    dal file annale_toponimi.ndjson usato in modalità legacy.
    """
    if os.path.exists(path) is False:
        return set()

    processed: Set[str] = set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                feat = json.loads(line)
                props = feat.get("properties", {})
                key = (
                    f"{props.get('pagina','')}|{props.get('id','')}|"
                    f"{_norm(str(props.get('luogo','')))}"
                )
                processed.add(key)
            except Exception:
                continue
    return processed


def ndjson_append_feature(path: str, feature: dict):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(feature, ensure_ascii=False) + "\n")


def finalize_ndjson_to_geojson(ndjson_path: str, out_geojson: str):
    """
    Converte annale_toponimi.ndjson in una FeatureCollection GeoJSON unica.
    """
    features = []
    if os.path.exists(ndjson_path):
        with open(ndjson_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    feat = json.loads(line)
                    if isinstance(feat, dict) and feat.get("type") == "Feature":
                        features.append(feat)
                except Exception:
                    continue

    fc = {"type": "FeatureCollection", "features": features}
    with open(out_geojson, "w", encoding="utf-8") as f:
        json.dump(fc, f, ensure_ascii=False, indent=2)


def make_feature_from_hit(data: dict, props: dict) -> dict:
    """
    Crea una Feature GeoJSON usando la geometria (poligono se presente).
    """
    geom = data.get("geometry")
    if not isinstance(geom, dict):
        geom = {
            "type": "Point",
            "coordinates": [data.get("lon"), data.get("lat")]
        }

    return {
        "type": "Feature",
        "geometry": geom,
        "properties": props
    }


# ---------------- Public API legacy: phase_geocode ----------------

def phase_geocode(out_dir: str):
    """
    Modalità legacy:
    - legge annale_toponimi.csv (non filtrato)
    - per ogni riga e toponimo, chiama Nominatim (rispettando cache)
    - genera annale_toponimi.ndjson e annale_toponimi.geojson
    - salva reject su annale_toponimi_osm_rejects.csv
    """
    csv_path = os.path.join(out_dir, "annale_toponimi.csv")
    if not os.path.exists(csv_path):
        raise FileNotFoundError(
            "annale_toponimi.csv non trovato – eseguire Fase 1"
        )

    ndjson_path = os.path.join(out_dir, "annale_toponimi.ndjson")
    geojson_path = os.path.join(out_dir, "annale_toponimi.geojson")
    rejects_path = os.path.join(out_dir, "annale_toponimi_osm_rejects.csv")
    cache_path = os.path.join(out_dir, "geocache_toponyms.json")

    cache = {}
    if os.path.exists(cache_path):
        try:
            cache = json.load(open(cache_path, "r", encoding="utf-8"))
        except Exception:
            cache = {}

    session = requests.Session()
    processed_keys = read_processed_keys_from_ndjson(ndjson_path)
    rejects_rows: List[Tuple[str,str,str,str,str]] = []

    rows = []
    with open(csv_path, "r", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        for r in rdr:
            rows.append(r)

    for r in rows:
        pagina = str(r.get("pagina","")).strip()
        anno = str(r.get("anno","")).strip()
        pid = str(r.get("id","")).strip()
        luoghi = str(r.get("luogo","")).strip()
        if not luoghi:
            continue

        terms = ordered_unique(
            [x.strip() for x in luoghi.split(";") if x.strip()]
        )
        for term in terms:
            key = f"{pagina}|{pid}|{_norm(term)}"
            if key in processed_keys:
                continue

            data, reason, cache = geocode_with_cache(term, cache, session, out_dir)
            if data is None:
                rejects_rows.append((pagina, anno, pid, term, reason or "rejected"))
                continue

            props = {
                "pagina": pagina,
                "anno": anno,
                "id": pid,
                "luogo": term,
                "display_name": data.get("display_name"),
                "class": data.get("class"),
                "type": data.get("type"),
                "geometry_source": data.get("geometry_source"),
                "admin_level": data.get("admin_level"),
            }
            feat = make_feature_from_hit(data, props)
            ndjson_append_feature(ndjson_path, feat)
            processed_keys.add(key)

    session.close()

    if rejects_rows:
        with open(rejects_path, "w", newline="", encoding="utf-8") as rej:
            w = csv.writer(rej)
            w.writerow(["pagina","anno","id","luogo","reason"])
            for row in rejects_rows:
                w.writerow(row)

    finalize_ndjson_to_geojson(ndjson_path, geojson_path)


# ---------------- Modalità raggruppata (FASE 2 nuova) ----------------

def _collect_occurrences_by_term(csv_path: str) -> Dict[str, Dict]:
    """
    Legge il CSV attivo e costruisce:
        { term_norm: { 'raw': forma_preferita, 'pages': set([...]) } }

    Serve per geocodare ogni toponimo una sola volta e annotare in quante/qual pagine compare.
    """
    occ: Dict[str, Dict] = {}
    with open(csv_path, "r", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        for r in rdr:
            pagina = str(r.get("pagina","")).strip()
            luoghi = (r.get("luogo") or "").strip()
            if not luoghi:
                continue

            for t in [x.strip() for x in luoghi.split(";") if x.strip()]:
                k = _norm(t)
                d = occ.get(k)
                if not d:
                    d = {"raw": t, "pages": set()}
                # scegli come 'raw' la forma più breve
                d["raw"] = d["raw"] if len(d["raw"]) <= len(t) else t
                d["pages"].add(pagina)
                occ[k] = d
    return occ


def _sorted_pages_str(pages: set) -> str:
    """
    Restituisce le pagine ordinate numericamente quando possibile.
    """
    def _key(x):
        try:
            return (0, int(x))
        except Exception:
            return (1, x)
    return ",".join(str(x) for x in sorted(pages, key=_key))


def phase_geocode_grouped(out_dir: str, progress_cb=None):
    """
    Geocoding raggruppato (rispetta le esclusioni):
    - Usa il CSV attivo (filtrato se esiste, originale altrimenti)
    - Geocoda ogni toponimo una sola volta
    - Scrive annale_toponimi_grouped.geojson
    - Scrive annale_toponimi_grouped_rejects.csv
    - Aggiorna progress_cb(done, total, current_term) durante il loop
    """
    csv_path = choose_active_csv(out_dir)
    occ = _collect_occurrences_by_term(csv_path)
    terms = list(occ.values())
    total = len(terms)

    cache_path = os.path.join(out_dir, "geocache_toponyms.json")
    cache = {}
    if os.path.exists(cache_path):
        try:
            cache = json.load(open(cache_path, "r", encoding="utf-8"))
        except Exception:
            cache = {}

    session = requests.Session()
    features = []
    rejects_rows: List[Tuple[str,str,str,str,str]] = []

    for i, item in enumerate(terms, start=1):
        if progress_cb:
            progress_cb(i-1, total, item["raw"])

        data, reason, cache = geocode_with_cache(item["raw"], cache, session, out_dir)
        if data is None:
            rejects_rows.append(("", "", "", item["raw"], reason or "rejected"))
            continue

        props = {
            "pagine": _sorted_pages_str(item["pages"]),
            "mentions": len(item["pages"]),
            "anno": "",
            "id": "",
            "luogo": item["raw"],
            "display_name": data.get("display_name"),
            "class": data.get("class"),
            "type": data.get("type"),
            "geometry_source": data.get("geometry_source"),
            "admin_level": data.get("admin_level"),
        }
        feat = make_feature_from_hit(data, props)
        features.append(feat)

    session.close()

    grouped_path = os.path.join(out_dir, "annale_toponimi_grouped.geojson")
    with open(grouped_path, "w", encoding="utf-8") as f:
        json.dump(
            {"type": "FeatureCollection", "features": features},
            f,
            ensure_ascii=False,
            indent=2
        )

    if rejects_rows:
        rej_path = os.path.join(out_dir, "annale_toponimi_grouped_rejects.csv")
        with open(rej_path, "w", newline="", encoding="utf-8") as rej:
            w = csv.writer(rej)
            w.writerow(["pagina","anno","id","luogo","reason"])
            for row in rejects_rows:
                w.writerow(row)

    if progress_cb:
        progress_cb(total, total, None)
