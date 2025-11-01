# processor/exclusions.py
"""
Gestione delle esclusioni / reinclusioni utente e delle esclusioni automatiche.

Novità:
- Supportiamo tre cose:
  1. Esclusioni globali di un toponimo ("exclude all").
  2. Esclusioni pagina-specifiche ("Cerignola p.53 va esclusa").
  3. Reinclusioni forzate di toponimi che sarebbero stati scartati
     automaticamente nella fase di estrazione preliminare (pre_filter),
     o che sarebbero esclusi da 1/2.

Stato persistito in:
  annale_user_exclusions.json
    {
      "excluded_toponyms": ["Cerignola", "Bari"],
      "excluded_attestations": [
        {"name_norm":"cerignola","page":53},
        {"name_norm":"cerignola","page":54}
      ],
      "forced_include_attestations": [
        {"name_norm":"cerignola","page":53}
      ]
    }

Compat con vecchi formati:
- se troviamo {"excluded": [...]} lo leggiamo come excluded_toponyms
- se mancano i campi nuovi li creiamo vuoti.

apply_exclusions_to_csv(out_dir):
- parte da annale_toponimi.csv (candidati buoni)
- recupera annale_toponimi_esclusi.csv (candidati "scartati" a monte)
- se l'utente forza l'inclusione di "Cerignola" a p.53,
  allora quel termine viene inserito nel CSV filtrato per quella pagina.

choose_active_csv(out_dir):
- se esiste annale_toponimi_filtered.csv, usa quello
- altrimenti annale_toponimi.csv
"""

from __future__ import annotations

import os
import json
import csv
from typing import List, Tuple, Set, Optional, Dict, Any
from .utils import _norm, ordered_unique


def _exclusions_path(out_dir: str) -> str:
    return os.path.join(out_dir, "annale_user_exclusions.json")


def _load_raw_exclusions(out_dir: str) -> Dict[str, Any]:
    """
    Ritorna un dict normalizzato con campi:
      excluded_toponyms: List[str] (raw display)
      excluded_attestations: List[{"name_norm":str,"page":int}]
      forced_include_attestations: List[{"name_norm":str,"page":int}]
    """
    p = _exclusions_path(out_dir)
    if not os.path.exists(p):
        return {
            "excluded_toponyms": [],
            "excluded_attestations": [],
            "forced_include_attestations": [],
        }

    try:
        data = json.load(open(p, "r", encoding="utf-8"))
    except Exception:
        return {
            "excluded_toponyms": [],
            "excluded_attestations": [],
            "forced_include_attestations": [],
        }

    # retrocompatibilità col vecchio formato {"excluded":[...]}
    if "excluded_toponyms" not in data and "excluded" in data:
        data["excluded_toponyms"] = list(data.get("excluded") or [])

    if "excluded_attestations" not in data:
        data["excluded_attestations"] = []
    if "forced_include_attestations" not in data:
        data["forced_include_attestations"] = []

    # pulizia liste
    excl_topo = data.get("excluded_toponyms") or []
    excl_att = data.get("excluded_attestations") or []
    incl_att = data.get("forced_include_attestations") or []

    # sanitizza attestazioni in/out (devono avere name_norm e page int)
    def _clean_pairs(arr):
        cleaned = []
        for row in arr:
            if not isinstance(row, dict):
                continue
            nm = row.get("name_norm")
            pg = row.get("page")
            try:
                pg_int = int(pg)
            except Exception:
                continue
            if isinstance(nm, str) and nm.strip():
                cleaned.append({"name_norm": _norm(nm), "page": pg_int})
        return cleaned

    excl_att = _clean_pairs(excl_att)
    incl_att = _clean_pairs(incl_att)

    return {
        "excluded_toponyms": [str(x) for x in excl_topo if isinstance(x, str)],
        "excluded_attestations": excl_att,
        "forced_include_attestations": incl_att,
    }


def load_user_exclusions(out_dir: str) -> Set[str]:
    """
    Versione "semplice" storica: ritorna l'insieme normalizzato dei
    toponimi esclusi globalmente.
    """
    raw = _load_raw_exclusions(out_dir)
    return set(_norm(x) for x in raw["excluded_toponyms"])


def load_user_exclusions_full(out_dir: str) -> Tuple[Set[str], Set[Tuple[str,int]], Set[Tuple[str,int]]]:
    """
    Ritorna tre insiemi:
    - global_excl: set di name_norm esclusi globalmente
    - page_pairs_excl: set di (name_norm, page_int) esclusi solo per quella pagina
    - forced_include_pairs: set di (name_norm, page_int) che l'utente vuole
      INCLUDERE anche se normalmente sarebbero esclusi (es. erano pre_filter)
    """
    raw = _load_raw_exclusions(out_dir)

    # globali
    global_set = set(_norm(x) for x in raw["excluded_toponyms"])

    # esclusi pagina-specifica
    page_pairs_excl: Set[Tuple[str,int]] = set()
    for row in raw["excluded_attestations"]:
        nm = _norm(row.get("name_norm",""))
        try:
            pg = int(row.get("page"))
        except Exception:
            continue
        if nm and pg:
            page_pairs_excl.add((nm, pg))

    # forzati dentro (override)
    forced_include_pairs: Set[Tuple[str,int]] = set()
    for row in raw["forced_include_attestations"]:
        nm = _norm(row.get("name_norm",""))
        try:
            pg = int(row.get("page"))
        except Exception:
            continue
        if nm and pg:
            forced_include_pairs.add((nm, pg))

    return global_set, page_pairs_excl, forced_include_pairs


def save_user_exclusions(
    out_dir: str,
    excluded_toponyms: List[str],
    excluded_attestations: Optional[List[Dict[str,Any]]] = None,
    forced_include_attestations: Optional[List[Dict[str,Any]]] = None,
):
    """
    Scrive annale_user_exclusions.json aggiornato.
    - excluded_toponyms: lista di stringhe (grezze, leggibili) per gli "exclude all"
    - excluded_attestations: [{name_norm, page}, ...] (pagina esclusa)
    - forced_include_attestations: [{name_norm, page}, ...] (pagina reinclusa)
    Se uno dei parametri *_attestations è None, mantenere quello già salvato.
    """
    path = _exclusions_path(out_dir)
    prev = _load_raw_exclusions(out_dir)

    if excluded_attestations is None:
        excluded_attestations = prev["excluded_attestations"]
    if forced_include_attestations is None:
        forced_include_attestations = prev["forced_include_attestations"]

    uniq_topo = ordered_unique([str(x) for x in excluded_toponyms if str(x).strip()])

    def _clean_pairs(arr):
        clean_list = []
        seen = set()
        for row in (arr or []):
            if not isinstance(row, dict):
                continue
            nm = row.get("name_norm")
            pg = row.get("page")
            try:
                pg_int = int(pg)
            except Exception:
                continue
            if isinstance(nm, str) and nm.strip():
                key = (_norm(nm), pg_int)
                if key in seen:
                    continue
                seen.add(key)
                clean_list.append({"name_norm": _norm(nm), "page": pg_int})
        return clean_list

    excl_att_clean = _clean_pairs(excluded_attestations)
    incl_att_clean = _clean_pairs(forced_include_attestations)

    to_write = {
        "excluded_toponyms": uniq_topo,
        "excluded_attestations": excl_att_clean,
        "forced_include_attestations": incl_att_clean,
    }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(to_write, f, ensure_ascii=False, indent=2)


def _filtered_csv_path(out_dir: str) -> str:
    return os.path.join(out_dir, "annale_toponimi_filtered.csv")


def _collect_prefilter_forced_inclusions(
    out_dir: str,
    forced_include_pairs: Set[Tuple[str,int]]
) -> Dict[str, List[str]]:
    """
    Ritorna { pagina_label(str) : [term1, term2, ...] }
    con i termini che erano stati esclusi in fase di pre_filter
    ma che l'utente ha chiesto di reincludere per quella pagina.

    Legge annale_toponimi_esclusi.csv:
      pagina, anno, id, termine, stadio, ragione
    e considera solo stadio == "pre_filter".
    """
    path = os.path.join(out_dir, "annale_toponimi_esclusi.csv")
    if not os.path.exists(path):
        return {}

    out: Dict[str, List[str]] = {}
    with open(path, "r", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            stadio = (row.get("stadio") or "").strip().lower()
            if stadio != "pre_filter":
                continue
            term = (row.get("termine") or "").strip()
            pagina_label = str(row.get("pagina","")).strip()
            if not term or not pagina_label:
                continue

            nm = _norm(term)
            # cerco la coppia (nm, pagina_int) tra i forced_include_pairs
            try:
                pagina_int = int(pagina_label)
            except Exception:
                pagina_int = None

            if pagina_int is None:
                continue

            if (nm, pagina_int) in forced_include_pairs:
                # reincluso -> aggiungilo
                out.setdefault(pagina_label, []).append(term)

    return out


def apply_exclusions_to_csv(out_dir: str) -> str:
    """
    Rigenera annale_toponimi_filtered.csv combinando:
    - annale_toponimi.csv (estratti 'buoni')
    - inclusioni forzate dall'utente di termini pre-filtrati
      (forced_include_attestations)
    - esclusioni globali e pagina-specifiche

    In pratica:
      1. Parto dai termini 'buoni' trovati su ogni pagina.
      2. Aggiungo i termini che erano stati scartati in pre_filter
         ma che l'utente ha forzato a includere.
      3. Tolgo quelli esclusi globalmente o per pagina.
    """
    src = os.path.join(out_dir, "annale_toponimi.csv")
    if not os.path.exists(src):
        raise FileNotFoundError("annale_toponimi.csv non trovato – eseguire Fase 1")

    dst = _filtered_csv_path(out_dir)

    # carica preferenze utente
    global_excl, page_pairs_excl, forced_include_pairs = load_user_exclusions_full(out_dir)

    # (pagina_label -> [termini pre-filter reinclusi])
    forced_prefilter_map = _collect_prefilter_forced_inclusions(out_dir, forced_include_pairs)

    # carichiamo righe originali
    base_rows = []
    with open(src, "r", encoding="utf-8") as f_in:
        rdr = csv.DictReader(f_in)
        for r in rdr:
            pagina_label = str(r.get("pagina","")).strip()
            anno = (r.get("anno") or "").strip()
            pid = (r.get("id") or "").strip()
            luoghi_field = (r.get("luogo") or "").strip()
            base_terms = [t.strip() for t in luoghi_field.split(";") if t.strip()]
            base_rows.append({
                "pagina_label": pagina_label,
                "anno": anno,
                "id": pid,
                "base_terms": base_terms,
            })

    with open(dst, "w", newline="", encoding="utf-8") as f_out:
        w = csv.writer(f_out)
        w.writerow(["pagina", "anno", "id", "luogo"])

        for row in base_rows:
            pagina_label = row["pagina_label"]
            anno = row["anno"]
            pid = row["id"]

            try:
                pagina_int = int(pagina_label)
            except Exception:
                pagina_int = None

            # 1. termini base
            working_terms = set(row["base_terms"])

            # 2. aggiungi eventuali termini forzati (pre-filter reincluso)
            extra_terms = forced_prefilter_map.get(pagina_label, [])
            for t in extra_terms:
                working_terms.add(t)

            # 3. applica esclusioni
            final_terms: List[str] = []
            seen_norms = set()
            for t in working_terms:
                nm = _norm(t)
                if nm in seen_norms:
                    continue
                seen_norms.add(nm)

                # global exclude
                if nm in global_excl:
                    # NOTA: l'utente potrebbe avere forced_include su questa pagina.
                    # forced_include_pairs vince sull'esclusione globale.
                    if pagina_int is not None and (nm, pagina_int) in forced_include_pairs:
                        pass
                    else:
                        continue

                # page-specific exclude
                if pagina_int is not None and (nm, pagina_int) in page_pairs_excl:
                    # se è escluso su questa pagina ma forzato a includere,
                    # l'inclusione vince. Quindi controlliamo:
                    if (nm, pagina_int) in forced_include_pairs:
                        pass
                    else:
                        continue

                final_terms.append(t)

            w.writerow([
                pagina_label,
                anno,
                pid,
                ";".join(final_terms)
            ])

    return dst


def choose_active_csv(out_dir: str) -> str:
    """
    Se esiste il CSV filtrato (che adesso è l'insieme finale
    dopo esclusioni e reinclusioni forzate), usalo;
    altrimenti usa l'originale annale_toponimi.csv.
    """
    filt = _filtered_csv_path(out_dir)
    if os.path.exists(filt):
        return filt
    return os.path.join(out_dir, "annale_toponimi.csv")
