#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import csv
import uuid
import json
import time
import unicodedata
import threading
from typing import Dict, List, Tuple, Any, Set

from flask import Flask, request, send_from_directory, jsonify, abort
from werkzeug.utils import secure_filename

# importiamo le funzioni "pesanti" dalla pipeline esistente
# (queste dovresti già averle dal tuo codice originale / refactor)
from processor import (
    phase_extract,
    phase_geocode_grouped,
    list_outputs,
)

# =====================================================
# CONFIG FLASK / PATH
# =====================================================

UPLOAD_ROOT = os.path.abspath("workspace")
os.makedirs(UPLOAD_ROOT, exist_ok=True)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

ALLOWED_EXTENSIONS = {"pdf"}

app = Flask(
    __name__,
    static_folder=os.path.join(BASE_DIR, "static"),
    template_folder=os.path.join(BASE_DIR, "static"),  # serve index.html da /static
)


# =====================================================
# UTILS DI BASE
# =====================================================

def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def make_job_dir() -> Tuple[str, str]:
    jid = uuid.uuid4().hex[:12]
    job_dir = os.path.join(UPLOAD_ROOT, jid)
    os.makedirs(job_dir, exist_ok=True)
    return jid, job_dir


def _progress_path(job_dir: str) -> str:
    return os.path.join(job_dir, "geocode_progress.json")


def _write_progress(job_dir: str, done: int, total: int,
                    current: str = None, status: str = "running"):
    prog = {
        "status": status,               # starting | running | done | error
        "done": int(done),
        "total": int(total),
        "pct": (0 if total <= 0 else round(done * 100.0 / total, 1)),
        "current": current
    }
    with open(_progress_path(job_dir), "w", encoding="utf-8") as f:
        json.dump(prog, f, ensure_ascii=False, indent=2)


def _geocode_worker(job_dir: str):
    """Thread worker per geocoding raggruppato con callback di progresso."""
    def cb(done, total, current_term):
        _write_progress(job_dir, done, total, current_term, "running")

    try:
        _write_progress(job_dir, 0, 0, None, "starting")
        phase_geocode_grouped(out_dir=job_dir, progress_cb=cb)
        _write_progress(job_dir, 1, 1, None, "done")
    except Exception as e:
        _write_progress(job_dir, 0, 0,
                        f"error: {type(e).__name__}: {e}", "error")


# =====================================================
# NORMALIZZAZIONE NOMI / SUPPORTO CSV
# =====================================================

def _norm(s: str) -> str:
    """normalizzazione stile processor._norm."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s).strip().lower()


def _safe_int(x):
    try:
        return int(str(x).strip())
    except Exception:
        return None


def _best_display(a: str, b: str) -> str:
    """Scegli la forma più breve / semplice tra due versioni del toponimo."""
    if not a:
        return b
    if not b:
        return a
    return a if len(a) <= len(b) else b


def _state_path(job_dir: str) -> str:
    return os.path.join(job_dir, "annale_user_state.json")


def _load_user_state(job_dir: str) -> Dict[str, Any]:
    """
    Stato persistente dell'utente:
    {
      "exclude_global": [norm1, norm2, ...],
      "exclude_pages": [ {"norm":norm, "page":52}, ...],
      "include_pages": [ {"norm":norm, "page":52, "raw":"Forma Originale"}, ...]
    }

    - exclude_global    = toponimo escluso ovunque
    - exclude_pages     = singole attestazioni da togliere
    - include_pages     = reinclusioni forzate (anche se quel toponimo è stato
                          scartato dal filtro automatico o globalmente escluso)
    """
    p = _state_path(job_dir)
    if not os.path.exists(p):
        return {
            "exclude_global": [],
            "exclude_pages": [],
            "include_pages": [],
        }
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    return {
        "exclude_global": data.get("exclude_global", []) or [],
        "exclude_pages": data.get("exclude_pages", []) or [],
        "include_pages": data.get("include_pages", []) or [],
    }


def _save_user_state(job_dir: str, st: Dict[str, Any]):
    # dedup e normalizza un minimo
    eg = []
    seen_eg = set()
    for n in st.get("exclude_global", []):
        nn = _norm(n)
        if nn and nn not in seen_eg:
            eg.append(nn)
            seen_eg.add(nn)

    ep = []
    seen_ep = set()
    for rec in st.get("exclude_pages", []):
        nn = _norm(rec.get("norm", ""))
        pp = rec.get("page", None)
        if nn and (pp is not None):
            key = (nn, pp)
            if key not in seen_ep:
                ep.append({"norm": nn, "page": pp})
                seen_ep.add(key)

    ip = []
    seen_ip = set()
    for rec in st.get("include_pages", []):
        nn = _norm(rec.get("norm", ""))
        pp = rec.get("page", None)
        raw = rec.get("raw", "")
        if nn and (pp is not None):
            key = (nn, pp)
            if key not in seen_ip:
                ip.append({"norm": nn, "page": pp, "raw": raw})
                seen_ip.add(key)

    data = {
        "exclude_global": eg,
        "exclude_pages": ep,
        "include_pages": ip,
    }
    with open(_state_path(job_dir), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _read_base_csv(job_dir: str) -> Tuple[Dict[str, Any], Dict[Any, Dict[str, str]]]:
    """
    Ritorna:
      base_included = {
        norm: { "display": "Cerignola", "pages": {52,53,...} }
      }

    e page_meta:
      { 52: {"anno":"1937","id":"1937/12"}, ... }

    Fonte: annale_toponimi.csv
    """
    base_path = os.path.join(job_dir, "annale_toponimi.csv")
    out = {}
    page_meta = {}
    if not os.path.exists(base_path):
        return out, page_meta

    with open(base_path, "r", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            page_raw = str(row.get("pagina", "")).strip()
            ano = str(row.get("anno", "")).strip()
            pid = str(row.get("id", "")).strip()
            luoghi = (row.get("luogo") or "").strip()

            if not page_raw:
                continue

            page_i = _safe_int(page_raw)
            page_key = page_i if page_i is not None else page_raw

            # meta pagina (tieni la prima che trovi)
            if page_key not in page_meta:
                page_meta[page_key] = {"anno": ano, "id": pid}

            if not luoghi:
                continue

            for t in [x.strip() for x in luoghi.split(";") if x.strip()]:
                nm = _norm(t)
                if nm not in out:
                    out[nm] = {"display": t, "pages": set()}
                else:
                    out[nm]["display"] = _best_display(out[nm]["display"], t)
                out[nm]["pages"].add(page_key)

    return out, page_meta


def _read_fallback_excluded(job_dir: str) -> Tuple[Dict[str, Any], Dict[Any, Dict[str, str]]]:
    """
    Ritorna:
      auto_excl = {
        norm: { "display": "Circolo giovanile", "pages": {52,...} }
      }

    e page_meta_fb (può aiutarci a costruire CSV filtrato per pagine che
    non comparivano proprio in annale_toponimi.csv)

    Fonte: annale_toponimi_esclusi.csv
    """
    excl_path = os.path.join(job_dir, "annale_toponimi_esclusi.csv")
    out = {}
    page_meta_fb = {}
    if not os.path.exists(excl_path):
        return out, page_meta_fb

    with open(excl_path, "r", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            page_raw = str(row.get("pagina", "")).strip()
            ano = str(row.get("anno", "")).strip()
            pid = str(row.get("id", "")).strip()
            term = str(row.get("termine", "")).strip()

            if not page_raw or not term:
                continue

            page_i = _safe_int(page_raw)
            page_key = page_i if page_i is not None else page_raw

            if page_key not in page_meta_fb:
                page_meta_fb[page_key] = {"anno": ano, "id": pid}

            nm = _norm(term)
            if nm not in out:
                out[nm] = {"display": term, "pages": set()}
            else:
                out[nm]["display"] = _best_display(out[nm]["display"], term)
            out[nm]["pages"].add(page_key)

    return out, page_meta_fb


def _compute_model(job_dir: str):
    """
    Combina:
    - annale_toponimi.csv  (base_included)
    - annale_toponimi_esclusi.csv (auto_excl: ciò che il filtro ha scartato)
    - annale_user_state.json (scelte utente)

    e produce:
    - final_included[norm] = { "display":..., "pages":[...] }
    - final_excluded[norm] = { "display":..., "is_global":bool, "pages":[...] }
    - page_meta = {page: {"anno":..,"id":..}}
    - state = stato utente (per eventuali debug/ricalcoli)
    """
    base_included, page_meta_base = _read_base_csv(job_dir)
    auto_excl, page_meta_fb = _read_fallback_excluded(job_dir)

    # unisci i metadati di pagina (fallback prima, poi base sovrascrive)
    page_meta = {}
    page_meta.update(page_meta_fb)
    page_meta.update(page_meta_base)

    state = _load_user_state(job_dir)
    exclude_global = set(state.get("exclude_global", []))
    exclude_pages_list = state.get("exclude_pages", [])
    include_pages_list = state.get("include_pages", [])

    # set rapidi per test
    ex_pages_set = set()
    for rec in exclude_pages_list:
        nn = _norm(rec.get("norm", ""))
        pp = rec.get("page", None)
        if nn and (pp is not None):
            ex_pages_set.add((nn, pp))

    in_pages_set = set()
    in_pages_raw = {}
    for rec in include_pages_list:
        nn = _norm(rec.get("norm", ""))
        pp = rec.get("page", None)
        raw = rec.get("raw", "")
        if nn and (pp is not None):
            in_pages_set.add((nn, pp))
            if raw:
                in_pages_raw[(nn, pp)] = raw

    # union di tutti i nomi possibili che dobbiamo considerare
    all_norms: Set[str] = (
        set(base_included.keys())
        | set(auto_excl.keys())
        | set(nn for (nn, _) in in_pages_set)
        | exclude_global
        | set(nn for (nn, _) in ex_pages_set)
    )

    final_included = {}
    final_excluded = {}

    for norm in all_norms:
        # scegli la forma "più carina" da mostrare
        disp_candidates = []
        if norm in base_included:
            disp_candidates.append(base_included[norm]["display"])
        if norm in auto_excl:
            disp_candidates.append(auto_excl[norm]["display"])
        for (nn, pp), raw in in_pages_raw.items():
            if nn == norm and raw:
                disp_candidates.append(raw)

        disp_candidates = [d for d in disp_candidates if d]
        if disp_candidates:
            display = min(disp_candidates, key=len)
        else:
            display = norm

        # 1. Pagine incluse
        pages_inc = set()
        # di base
        if norm in base_included:
            pages_inc |= set(base_included[norm]["pages"])
        # forzate dall'utente (anche se erano escluse dal filtro)
        for (nn, pp) in in_pages_set:
            if nn == norm:
                pages_inc.add(pp)
        # togli pagine escluse manualmente (se non sono state re-incluse)
        for (nn, pp) in ex_pages_set:
            if nn == norm and (nn, pp) not in in_pages_set:
                pages_inc.discard(pp)
        # se è escluso globalmente, tieni SOLO le pagine che l'utente ha forzato con include_pages
        if norm in exclude_global:
            forced = {pp for (nn, pp) in in_pages_set if nn == norm}
            pages_inc = forced

        # 2. Pagine escluse
        pages_exc = set()
        # tutte quelle di base che NON sono rimaste incluse
        if norm in base_included:
            for p in base_included[norm]["pages"]:
                if p not in pages_inc:
                    pages_exc.add(p)
        # tutte quelle scartate dal filtro automatico (auto_excl)
        if norm in auto_excl:
            for p in auto_excl[norm]["pages"]:
                # se l'utente le ha re-incluse, NON vanno in esclusi
                if (norm, p) not in in_pages_set:
                    if p not in pages_inc:
                        pages_exc.add(p)

        # salva final_included (solo se rimane almeno 1 pagina viva)
        if pages_inc:
            final_included[norm] = {
                "display": display,
                "pages": sorted(pages_inc, key=lambda x: (not isinstance(x, int), x)),
            }

        # salva final_excluded se globalmente escluso o se ha pagine escluse
        is_glob = (norm in exclude_global)
        if is_glob or pages_exc:
            final_excluded[norm] = {
                "display": display,
                "is_global": is_glob,
                "pages": sorted(pages_exc, key=lambda x: (not isinstance(x, int), x)),
            }

    return final_included, final_excluded, state, page_meta


def _write_filtered_csv(job_dir: str,
                        final_included: Dict[str, Any],
                        page_meta: Dict[Any, Dict[str, str]]):
    """
    Rigenera annale_toponimi_filtered.csv in base allo stato corrente.
    Ogni riga = una pagina. 'luogo' = lista di toponimi che restano inclusi
    in QUELLA pagina, dopo tutte le esclusioni/reenclusioni.
    """
    # 1) raccogli quali toponimi vanno su quale pagina
    page_map: Dict[Any, List[str]] = {}
    for norm, info in final_included.items():
        disp = info["display"]
        for p in info["pages"]:
            page_map.setdefault(p, []).append(disp)

    # ordina le pagine per scriverle in maniera stabile
    all_pages_sorted = sorted(page_map.keys(), key=lambda x: (not isinstance(x, int), x))

    dst = os.path.join(job_dir, "annale_toponimi_filtered.csv")
    with open(dst, "w", newline="", encoding="utf-8") as f_out:
        w = csv.writer(f_out)
        w.writerow(["pagina", "anno", "id", "luogo"])
        for p in all_pages_sorted:
            terms_here = page_map.get(p, [])
            # ordina alfabeticamente per avere output deterministico
            # e dedup
            uniq_terms = sorted(set(terms_here), key=lambda x: x.lower())

            meta = page_meta.get(p, {"anno": "", "id": ""})
            anno_val = meta.get("anno", "")
            id_val = meta.get("id", "")

            w.writerow([p, anno_val, id_val, ";".join(uniq_terms)])


def _rebuild_filtered_csv(job_dir: str):
    """Ricalcola lo stato incluso/escluso e riscrive il CSV filtrato."""
    final_included, _final_excluded, _state, page_meta = _compute_model(job_dir)
    _write_filtered_csv(job_dir, final_included, page_meta)


# =====================================================
# FLASK ROUTES
# =====================================================

@app.route("/")
def index():
    # l'index.html vive in static/
    return send_from_directory(app.template_folder, "index.html")


# ---------------- UPLOAD PDF ----------------
@app.post("/api/upload")
def api_upload():
    if "pdf" not in request.files:
        return jsonify({"ok": False, "error": "Nessun file 'pdf' nel form-data"}), 400
    f = request.files["pdf"]
    if f.filename == "":
        return jsonify({"ok": False, "error": "Nessun file selezionato"}), 400
    if not allowed_file(f.filename):
        return jsonify({"ok": False, "error": "Formato non supportato (usa .pdf)"}), 400

    jid, job_dir = make_job_dir()
    filename = "annale.pdf"
    path = os.path.join(job_dir, secure_filename(filename))
    f.save(path)

    # reset stato utente
    _save_user_state(job_dir, {
        "exclude_global": [],
        "exclude_pages": [],
        "include_pages": [],
    })

    return jsonify({
        "ok": True,
        "job_id": jid,
        "pdf": f"/files/{jid}/{filename}"
    })


# ---------------- ESTRAZIONE PDF -> CSV/PDF MARCATO ----------------
@app.post("/api/extract")
def api_extract():
    data = request.get_json(silent=True) or {}
    jid = (data.get("job_id") or "").strip()
    ranges = (data.get("ranges") or "").strip()

    if not jid:
        return jsonify({"ok": False, "error": "job_id mancante"}), 400
    job_dir = os.path.join(UPLOAD_ROOT, jid)
    pdf_path = os.path.join(job_dir, "annale.pdf")
    if not os.path.exists(pdf_path):
        return jsonify({"ok": False, "error": "PDF non trovato per questo job_id"}), 404

    try:
        phase_extract(pdf_path=pdf_path, out_dir=job_dir, include_ranges=ranges)
    except Exception as e:
        return jsonify({"ok": False, "error": f"extract failed: {type(e).__name__}: {e}"}), 500

    # dopo aver estratto, rigenera il CSV filtrato coerente con lo stato utente
    _rebuild_filtered_csv(job_dir)

    files = list_outputs(job_dir)
    marked_pdf_url = None
    if os.path.exists(os.path.join(job_dir, "annale_marked.pdf")):
        marked_pdf_url = f"/files/{jid}/annale_marked.pdf"

    return jsonify({
        "ok": True,
        "files": files,
        "marked_pdf_url": marked_pdf_url
    })


# ---------------- LISTA TOPONIMI (INCLUSI + ESCLUSI) ----------------
@app.get("/api/toponyms")
def api_toponyms():
    jid = (request.args.get("job_id") or "").strip()
    if not jid:
        return jsonify({"ok": False, "error": "job_id mancante"}), 400

    job_dir = os.path.join(UPLOAD_ROOT, jid)
    # costruisci il modello stato finale (inclusi/esclusi)
    final_included, final_excluded, state, page_meta = _compute_model(job_dir)

    # included_summary -> [{name:"Cerignola", count:6}, ...]
    included_summary = []
    for norm, info in final_included.items():
        included_summary.append({
            "name": info["display"],
            "count": len(info["pages"]),
        })
    # ordina alfabeticamente
    included_summary.sort(key=lambda x: x["name"].lower())

    # excluded_state -> { global:[{display,...}], per_page:[{display,...,page}] }
    excl_global = []
    excl_perpage = []
    for norm, info in final_excluded.items():
        if info.get("is_global"):
            excl_global.append({
                "display": info["display"],
                "name_norm": norm,
            })
        for p in info.get("pages", []):
            excl_perpage.append({
                "display": info["display"],
                "name_norm": norm,
                "page": p,
            })

    excl_global.sort(key=lambda x: x["display"].lower())
    excl_perpage.sort(key=lambda x: (x["display"].lower(), (not isinstance(x["page"], int), x["page"])))

    files = list_outputs(job_dir)

    return jsonify({
        "ok": True,
        "included_summary": included_summary,
        "excluded_state": {
            "global": excl_global,
            "per_page": excl_perpage
        },
        "files": files
    })


# ---------------- DETTAGLIO ATTESTAZIONI DI UN TOPONIMO INCLUSO ----------------
@app.get("/api/attestations")
def api_attestations():
    jid = (request.args.get("job_id") or "").strip()
    term = (request.args.get("term") or "").strip()
    if not jid:
        return jsonify({"ok": False, "error": "job_id mancante"}), 400
    if not term:
        return jsonify({"ok": False, "error": "term mancante"}), 400

    job_dir = os.path.join(UPLOAD_ROOT, jid)
    final_included, final_excluded, state, page_meta = _compute_model(job_dir)

    nm = _norm(term)
    info = final_included.get(nm)
    if not info:
        # se non è incluso, ritorno lista vuota;
        # la UI non lo mostrerà sotto "inclusi" comunque
        return jsonify({
            "ok": True,
            "excluded_global": False,
            "occurrences": []
        })

    occs = []
    for p in info["pages"]:
        occs.append({
            "page_label": p,
            "snippet": "",
            "excluded_specific": False,
        })

    # nel blocco "inclusi" consideriamo queste pagine effettivamente vive,
    # quindi non marchiamo excluded_global
    return jsonify({
        "ok": True,
        "excluded_global": False,
        "occurrences": occs,
    })


# ---------------- SALVATAGGIO ESCLUSIONI / RE-INCLUSIONI ----------------
@app.post("/api/exclusions")
def api_exclusions():
    data = request.get_json(silent=True) or {}
    jid = (data.get("job_id") or "").strip()
    if not jid:
        return jsonify({"ok": False, "error": "job_id mancante"}), 400

    job_dir = os.path.join(UPLOAD_ROOT, jid)
    st = _load_user_state(job_dir)

    # 1. Escludi interamente questi toponimi
    for term in data.get("exclude_toponyms", []) or []:
        nm = _norm(term)
        if nm and nm not in st["exclude_global"]:
            st["exclude_global"].append(nm)
        # se lo sto escludendo globalmente, rimuovo eventuali include_pages su quel norm
        st["include_pages"] = [
            rec for rec in st["include_pages"]
            if _norm(rec.get("norm","")) != nm
        ]

    # 2. Escludi attestazioni specifiche (pagina)
    for att in data.get("exclude_attestations", []) or []:
        term = str(att.get("term",""))
        page = att.get("page", None)
        if page is None: continue
        page = _safe_int(page) if _safe_int(page) is not None else page
        nm = _norm(term)
        if not nm: continue

        # togli eventuale include esplicito
        st["include_pages"] = [
            rec for rec in st["include_pages"]
            if not (_norm(rec.get("norm","")) == nm and rec.get("page")==page)
        ]

        # aggiungi a exclude_pages se non già presente
        already = any(
            (_norm(rec.get("norm","")) == nm and rec.get("page")==page)
            for rec in st["exclude_pages"]
        )
        if not already:
            st["exclude_pages"].append({"norm": nm, "page": page})

    # 3. Re-includi globalmente i toponimi
    for term in data.get("include_toponyms", []) or []:
        nm = _norm(term)
        if not nm:
            continue
        # togli dal global-exclude
        st["exclude_global"] = [x for x in st["exclude_global"] if _norm(x) != nm]
        # togli dalle exclude_pages tutte le pagine di quel norm
        st["exclude_pages"] = [
            rec for rec in st["exclude_pages"]
            if _norm(rec.get("norm","")) != nm
        ]
        # NB: non aggiungiamo forzature include_pages qui; l'utente può farlo
        # con include_attestations sulle singole pagine.

    # 4. Re-includi attestazioni specifiche
    for att in data.get("include_attestations", []) or []:
        term = str(att.get("term",""))
        page = att.get("page", None)
        if page is None:
            continue
        page = _safe_int(page) if _safe_int(page) is not None else page
        nm = _norm(term)
        if not nm:
            continue

        # togli da exclude_pages (se c'era)
        st["exclude_pages"] = [
            rec for rec in st["exclude_pages"]
            if not (_norm(rec.get("norm","")) == nm and rec.get("page")==page)
        ]

        # aggiungi a include_pages per forzare l'inclusione di questa pagina
        already = any(
            (_norm(rec.get("norm","")) == nm and rec.get("page")==page)
            for rec in st["include_pages"]
        )
        if not already:
            st["include_pages"].append({"norm": nm, "page": page, "raw": term})

    # salva lo stato aggiornato
    _save_user_state(job_dir, st)

    # rigenera CSV filtrato coerente
    _rebuild_filtered_csv(job_dir)

    files = list_outputs(job_dir)
    return jsonify({"ok": True, "files": files})


# ---------------- GEOcoding START / PROGRESS ----------------
@app.post("/api/geocode_start")
def api_geocode_start():
    data = request.get_json(silent=True) or {}
    jid = (data.get("job_id") or "").strip()
    if not jid:
        return jsonify({"ok": False, "error": "job_id mancante"}), 400

    job_dir = os.path.join(UPLOAD_ROOT, jid)
    # prima di lanciare il geocoding, assicuriamoci che il CSV filtrato
    # sia coerente con lo stato
    _rebuild_filtered_csv(job_dir)

    # previene doppio worker
    prog_path = _progress_path(job_dir)
    if os.path.exists(prog_path):
        try:
            cur = json.load(open(prog_path, "r", encoding="utf-8"))
            if cur.get("status") in {"starting","running"}:
                return jsonify({"ok": False, "error": "Geocoding già in esecuzione"}), 400
        except Exception:
            pass

    t = threading.Thread(target=_geocode_worker, args=(job_dir,), daemon=True)
    t.start()

    return jsonify({"ok": True})


@app.get("/api/geocode_progress")
def api_geocode_progress():
    jid = (request.args.get("job_id") or "").strip()
    if not jid:
        return jsonify({"ok": False, "error": "job_id mancante"}), 400
    job_dir = os.path.join(UPLOAD_ROOT, jid)

    prog_path = _progress_path(job_dir)
    if not os.path.exists(prog_path):
        return jsonify({
            "ok": True,
            "status": "idle",
            "done": 0,
            "total": 0,
            "pct": 0.0,
            "current": None,
            "files": list_outputs(job_dir)
        })

    with open(prog_path, "r", encoding="utf-8") as f:
        prog = json.load(f)
    prog["ok"] = True
    prog["files"] = list_outputs(job_dir)
    return jsonify(prog)


# ---------------- LISTA FILE DISPONIBILI ----------------
@app.get("/api/list")
def api_list():
    jid = (request.args.get("job_id") or "").strip()
    if not jid:
        return jsonify({"ok": False, "error": "job_id mancante"}), 400
    job_dir = os.path.join(UPLOAD_ROOT, jid)
    if not os.path.isdir(job_dir):
        return jsonify({"ok": False, "error": "job non trovato"}), 404
    return jsonify({"ok": True, "files": list_outputs(job_dir)})


# ---------------- SERVE FILE STATICI JOB ----------------
@app.get("/files/<job_id>/<path:filename>")
def files(job_id, filename):
    job_dir = os.path.join(UPLOAD_ROOT, job_id)
    if not os.path.isdir(job_dir):
        abort(404)
    return send_from_directory(job_dir, filename, as_attachment=False)


# ---------------- STATIC (css/js) ----------------
@app.route("/static/<path:path>")
def serve_static(path):
    return send_from_directory("static", path)


# =====================================================
# MAIN
# =====================================================

if __name__ == "__main__":
    # debug=True va bene in sviluppo
    app.run(debug=True)
