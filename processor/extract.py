# processor/extract.py
"""
FASE 1: Estrazione dei toponimi dal PDF con spaCy + marcatura/attestazioni.

Novità rispetto alla vecchia phase_extract():
- Generiamo anche:
  1) annale_attestazioni.json
     {
       "<norm(term)>": {
         "term_display": "Cerignola",
         "occurrences": [
           {
             "page_label": 52,         # numero pagina visibile nel footer
             "pdf_page_index": 51,     # indice 0-based nel PDF
             "boxes": [[x0,y0,x1,y1], ...],  # bbox per highlight sul PDF
             "snippet": "…contesto testo…"
           },
           ...
         ]
       },
       ...
     }

  2) annale_tagged.json
     [
       {
         "page": 52,
         "attestations": [
           {"term": "Cerignola", "snippet": "…contesto testo…"},
           ...
         ]
       },
       ...
     ]

Questi file servono per:
- espandere un toponimo in tutte le sue attestazioni (pagine),
- cliccare una singola attestazione e saltare alla pagina giusta nel PDF,
- più avanti: consentire l'esclusione di Cerignola solo in p.53 ma non altrove.

Manteniamo comunque:
- annale_marked.pdf (con highlight e asterischi, per retro-compatibilità)
- annale_toponimi.csv
- annale_toponimi_esclusi.csv
"""

from __future__ import annotations

import os
import re
import csv
import json
import logging
from typing import List, Dict, Tuple, Optional

import fitz  # PyMuPDF

from .utils import (
    normalize_name,
    ordered_unique,
    parse_include_ranges,
    index_in_includes,
    HEADER_FALLBACK_RATIO,
    FOOTER_FALLBACK_RATIO,
    SIDE_MARGIN_PT,
)

logger = logging.getLogger(__name__)

# ---------------- spaCy loader ----------------
def try_load_spacy():
    """
    Carica un modello spaCy italiano disponibile tra:
    it_core_news_lg, it_core_news_md, it_core_news_sm.
    """
    try:
        import spacy
    except Exception:
        raise RuntimeError(
            "spaCy assente. Installa con: pip install spacy; "
            "python -m spacy download it_core_news_sm"
        )

    for model in ("it_core_news_lg", "it_core_news_md", "it_core_news_sm"):
        try:
            return spacy.load(model)
        except Exception:
            continue
    raise RuntimeError(
        "Nessun modello it_core_news_* trovato. "
        "Esegui: python -m spacy download it_core_news_sm"
    )


# ---------------- Euristiche / regex varie ----------------

ID_REGEX = re.compile(r"\b(19[0-9]{2})\s*/\s*([0-9]+)\b")
OGGETTO_PAT = re.compile(r"\boggetto\b", re.IGNORECASE)
BODY_END_PAT = re.compile(
    r"\b(Il\s+Prefetto|Il\s+Questore|Il\s+Capo\s+di\s+Gabinetto|F\.?to|Firma|Allegat[oi]|Il\s+Direttore|Il\s+Ministro|Sottosegretario)\b",
    re.IGNORECASE
)
SIGLA_NOME_PAT = re.compile(r"^[A-Z]\.\s*[A-ZÀ-Ü][a-zà-ü]+$")

DROP_IF_EXACT = set(map(str.lower, """
Ministero Interno Direzione Generale Pubblica Sicurezza Servizio
Governo Stato Regno Capitale Prefettura Questura
""".strip().split()))

PERSON_TITLES = {
    "sig", "sig.", "sig.ra", "sig.na", "on.", "onorevole", "dott.", "dott", "prof.", "prof",
    "ing.", "ing", "avv.", "avv", "mons.", "mons", "cav.", "cav", "prefetto", "questore",
    "ministro", "direttore", "sottosegretario"
}

LOC_VERBS = {
    "risiedere", "risiede", "risiedono", "domiciliare", "domiciliato", "domiciliata",
    "abitare", "abita", "abitano", "nascere", "nato", "nata", "provenire", "proveniente",
    "recarsi", "recatosi", "tornare", "tornato", "emigrare", "emigrato", "oriundo", "residente",
    "resiedeva", "dimorare", "dimora", "trasferirsi", "trasferito", "fermare", "arrestato", "arrestato a"
}

LOC_PREPS = {
    "a", "ad", "da", "di", "in", "nel", "nella", "nelle", "nello", "nei", "degli", "dei",
    "della", "dello", "delle", "sul", "sulla", "sullo", "sui", "sulle", "presso", "tra", "fra"
}

ADDR_WORDS = {
    "via", "viale", "piazza", "corso", "largo", "strada", "vicolo", "piazzale"
}

COMMON_FIRST_NAMES = {
    "alberto","angelica","angel","antonio","antonietta","anzlovar","aristide","auriti","ausenda",
    "bagnoli","balduini","balestri","ballarini","bancone","barbato","barbusse","bartocci","bartoli","basile",
    "celeste","duilio","feltre","franca","giacinto","gigante","luigi","nullo","renato","vincenzo","vincent",
    "benigni","cerruti","azzi","aprato","alessandro","giuseppe","giovanni","maria","mario",
    "enzo","pasquale","francesco","paolo","pietro","salvatore","roberto","giorgio","grazia","graziano",
    "carlo","claudio","fabrizio","giulia","valentina","stefano","simone","riccardo","chiara","celestino"
}

ALWAYS_ALLOW = {
    "roma","milano","torino","napoli","genova","bologna","firenze","venezia","palermo","catania",
    "bari","foggia","cerignola","lecce","taranto","brindisi","andria","barletta","trani",
    "verona","padova","treviso","vicenza","udine","trieste","trento","bolzano","brescia","bergamo",
    "como","varese","monza","pavia","piacenza","parma","modena","reggio emilia","ravenna","rimini",
    "forlì","cesena","ancona","pesaro","urbino","perugia","terni","l'aquila","pescara","chieti",
    "campobasso","potenza","catanzaro","reggio calabria","cagliari","sassari","aosta","matera","latina",
    "viterbo","rieti","frosinone","novara","alessandria","asti","biella","cuneo","savona","la spezia",
    "prato","pisa","lucca","arezzo","siena","grosseto","livorno"
}


# ---------------- PDF helpers ----------------

def get_footer_page_number(page: fitz.Page) -> int:
    h = page.rect.height
    w = page.rect.width
    footer_rect = fitz.Rect(0, h*(1-FOOTER_FALLBACK_RATIO), w, h)
    txt = page.get_text("text", clip=footer_rect) or ""
    nums = re.findall(r"(\d{1,4})", txt)
    if nums:
        try:
            return int(nums[-1])
        except Exception:
            pass
    return page.number + 1  # fallback


def extract_id_year(page: fitz.Page, last_id=None, last_year=None):
    """
    Recupera l'ID tipo '1937/12345' e l'anno corrente dalla fascia alta.
    """
    top_rect = fitz.Rect(0, 0, page.rect.width, page.rect.height * 0.35)
    text = page.get_text("text", clip=top_rect) or ""
    text = re.sub(r"[\u00AD\-]\s*\n\s*", "", text)
    text = re.sub(r"\s+", " ", text)
    m = ID_REGEX.search(text)
    if m:
        year = m.group(1)
        number = m.group(2)
        return f"{year}/{number}", year
    return last_id, last_year


def compute_body_rect(page: fitz.Page):
    """
    Cerca di delimitare il corpo della lettera/rapporto escludendo intestazioni e firme.
    """
    blocks = page.get_text("blocks", sort=True)
    page_w, page_h = page.rect.width, page.rect.height
    header_end_y = None
    first_signature_y = None
    for b in blocks:
        x0, y0, x1, y1, text = b[:5]
        if not isinstance(text, str):
            continue
        if OGGETTO_PAT.search(text):
            header_end_y = max(header_end_y or 0, y1)
        if (BODY_END_PAT.search(text) or SIGLA_NOME_PAT.search(text.strip())):
            if first_signature_y is None or y0 < first_signature_y:
                first_signature_y = y0
    if header_end_y is None:
        header_end_y = page_h * HEADER_FALLBACK_RATIO
    if first_signature_y is None:
        first_signature_y = page_h * (1 - FOOTER_FALLBACK_RATIO)

    x0 = SIDE_MARGIN_PT
    x1 = page_w - SIDE_MARGIN_PT
    y0 = min(max(header_end_y + 2, 0), page_h)
    y1 = max(min(first_signature_y - 2, page_h), y0 + 1)
    return fitz.Rect(x0, y0, x1, y1)


def text_for_nlp(page: fitz.Page, rect: fitz.Rect) -> str:
    """
    Estrae il testo 'continuo' dalla zona corpo, togliendo sillabazioni tipo "foo-\nbar".
    """
    txt = page.get_text("text", clip=rect) or ""
    txt = re.sub(r"-\s*\n\s*", "", txt)
    return txt


# ---------------- NER + euristiche di contesto ----------------

def detect_candidates_with_context(nlp, text: str) -> Tuple[List[str], List[Tuple[str, str]]]:
    """
    Ritorna:
      selected: lista di candidati toponimi 'buoni'
      excluded: [(termine, motivo_scarto), ...]
    Applica euristiche sintattiche e semantiche sul contesto (preposizioni, verbi tipo "risiede a ...", ecc.).
    """
    selected: List[str] = []
    excluded: List[Tuple[str,str]] = []

    if not text.strip():
        return selected, excluded

    doc = nlp(text)

    def lemma(tok):
        return (tok.lemma_ or tok.text).lower()

    seen_norm = set()
    months = r"gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|novembre|dicembre"

    for ent in doc.ents:
        if ent.label_ not in ("LOC", "GPE"):
            continue

        raw = re.sub(r"\s+", " ", ent.text).strip(" ’'\",.;:()[]")
        if not raw:
            excluded.append((ent.text, "empty"))
            continue
        if any(ch.isdigit() for ch in raw):
            excluded.append((raw, "has_digit"))
            continue

        norm = normalize_name(raw)
        if norm in seen_norm:
            continue
        if norm in DROP_IF_EXACT:
            excluded.append((raw, "institution_term"))
            continue

        tokens = [t for t in ent]
        if len(tokens) > 4:
            excluded.append((raw, "too_many_tokens"))
            continue

        if all(normalize_name(t.text) in COMMON_FIRST_NAMES for t in tokens):
            excluded.append((raw, "all_common_first_names"))
            continue

        prev_tok = doc[ent.start - 1] if ent.start > 0 else None
        if prev_tok is not None and prev_tok.text.endswith(".") and len(prev_tok.text) <= 3:
            excluded.append((raw, "prev_initial"))
            continue

        left = doc[max(0, ent.start - 4):ent.start]
        if any(normalize_name(t.text.strip(".’'")) in PERSON_TITLES for t in left):
            excluded.append((raw, "left_person_title"))
            continue

        ok_context = False

        left_words = [normalize_name(t.text) for t in left if t.is_alpha]
        if any(w in LOC_PREPS for w in left_words[-4:]):
            ok_context = True

        if not ok_context:
            left10 = doc[max(0, ent.start - 10):ent.start]
            if any(lemma(t) in LOC_VERBS for t in left10 if t.pos_ in {"VERB", "AUX"}):
                ok_context = True

        if not ok_context:
            right6 = doc[ent.end: min(len(doc), ent.end + 6)]
            right_words = [normalize_name(t.text) for t in right6 if t.is_alpha]
            if (any(w in {"in", "a"} for w in left_words[-4:])
                and any(w in ADDR_WORDS for w in right_words[:4])):
                ok_context = True

        if not ok_context:
            sent_txt = re.sub(r"\s+", " ", ent.sent.text)
            verb_pat = (
                r"(risied\w+|resident\w+|domicili\w+|abit\w+|nato|nata|provenient\w+|"
                r"recat\w+|tornat\w+|emigrat\w+|oriundo|dimor\w+|trasferit\w+|arrestat\w+)"
            )
            if re.search(verb_pat + r".{0,80}\b" + re.escape(raw) + r"\b",
                         sent_txt, flags=re.IGNORECASE):
                ok_context = True

        if not ok_context:
            sent_txt = ent.sent.text
            if re.search(
                rf"\b{re.escape(raw)}\s*,\s*(?:add[iì]|li|\d{{1,2}}\s+({months}))",
                sent_txt,
                flags=re.IGNORECASE
            ):
                ok_context = True

        if not ok_context and norm in ALWAYS_ALLOW:
            ok_context = True

        if not ok_context:
            excluded.append((raw, "no_spatial_context"))
            continue

        seen_norm.add(norm)
        selected.append(raw)

    return selected, excluded


# ---------------- Localizzazione rettangoli nel PDF ----------------

def union_rect(rects: List[fitz.Rect]) -> Optional[fitz.Rect]:
    """Restituisce il bounding box unificato di più rettangoli."""
    if not rects:
        return None
    r = fitz.Rect(rects[0])
    for rr in rects[1:]:
        r |= rr
    return r


def locate_term_occurrences(page: fitz.Page, body_rect: fitz.Rect, term: str) -> List[fitz.Rect]:
    """
    Trova le occorrenze (visive) di `term` all'interno di body_rect.
    Ritorna una lista di fitz.Rect, uno per occorrenza.
    """
    words = page.get_text("words")
    filtered = []
    for w in words:
        rect = fitz.Rect(w[0], w[1], w[2], w[3])
        inter = rect & body_rect
        if inter.get_area() > 0.5 * rect.get_area():
            filtered.append(w)

    term_norm = re.sub(r"[\W_]+", "", term, flags=re.UNICODE).lower()
    MAX_WIN = 4
    hits: List[fitz.Rect] = []
    n = len(filtered)
    i = 0
    while i < n:
        matched = False
        for win in range(1, min(MAX_WIN, n - i) + 1):
            toks = [filtered[i + k][4] for k in range(win)]
            cand = "".join(
                re.sub(r"[\W_]+", "", t, flags=re.UNICODE) for t in toks
            ).lower()
            if cand == term_norm:
                rects_here = [
                    fitz.Rect(filtered[i + k][0], filtered[i + k][1],
                              filtered[i + k][2], filtered[i + k][3])
                    for k in range(win)
                ]
                hits.append(union_rect(rects_here))
                i += win
                matched = True
                break
        if not matched:
            i += 1
    return hits


def add_highlight_and_star(page: fitz.Page, rect: fitz.Rect):
    """
    Evidenzia nel PDF e aggiunge un asterisco accanto.
    Manteniamo questo output visivo legacy,
    ma in parallelo ora generiamo anche annale_tagged.json
    per una marcatura testuale più pulita.
    """
    try:
        a = page.add_highlight_annot(rect)
        a.set_colors({"stroke": (1, 1, 0), "fill": (1, 1, 0)})
        a.update()
    except Exception:
        pass

    try:
        h = rect.y1 - rect.y0
        fs = max(8, min(12, h))
        x = rect.x1 + 1.5
        y = rect.y0 + max(fs * 0.85, h * 0.8)
        page.insert_text(
            fitz.Point(x, y), "*",
            fontsize=fs, color=(0, 0, 0), overlay=True
        )
    except Exception:
        pass


def _make_snippets_for_term(body_text: str, term: str, max_ctx: int = 60) -> List[str]:
    """
    Restituisce piccoli 'contesti' in cui compare il toponimo nel testo pagina,
    per costruire annale_tagged.json.
    """
    snippets = []
    if not body_text:
        return snippets
    clean_txt = re.sub(r"\s+", " ", body_text)
    pat = re.compile(re.escape(term), re.IGNORECASE)
    for m in pat.finditer(clean_txt):
        start = max(0, m.start() - max_ctx)
        end = min(len(clean_txt), m.end() + max_ctx)
        snip = clean_txt[start:end].strip()
        if snip not in snippets:
            snippets.append(snip)
    return snippets


# ---------------- Public API: phase_extract ----------------

def phase_extract(pdf_path: str, out_dir: str, include_ranges: str = "") -> Dict:
    """
    Esegue la 'FASE 1':
    - Estrae toponimi pagina per pagina usando spaCy e le euristiche di contesto
    - Evidenzia i toponimi nel PDF marcato
    - Salva:
        * annale_marked.pdf
        * annale_toponimi.csv
        * annale_toponimi_esclusi.csv
    - NUOVO:
        * annale_attestazioni.json  (per click -> pagina/bbox)
        * annale_tagged.json        (snippet di contesto testuale)

    Ritorna un dict con i path principali.
    """
    nlp = try_load_spacy()

    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF non trovato: {pdf_path}")

    doc = fitz.open(pdf_path)
    total_pages = doc.page_count
    includes = parse_include_ranges(include_ranges, total_pages)

    excluded_rows: List[Tuple[str,str,str,str,str,str]] = []
    attest_index: Dict[str, Dict] = {}  # norm(term) -> {term_display, occurrences:[...]}
    tagged_pages: List[Dict] = []

    last_id = None
    last_year = None

    csv_path = os.path.join(out_dir, "annale_toponimi.csv")
    pdf_out_path = os.path.join(out_dir, "annale_marked.pdf")
    excl_csv = os.path.join(out_dir, "annale_toponimi_esclusi.csv")
    attest_json_path = os.path.join(out_dir, "annale_attestazioni.json")
    tagged_json_path = os.path.join(out_dir, "annale_tagged.json")

    with open(csv_path, "w", newline="", encoding="utf-8") as csvf:
        writer = csv.writer(csvf)
        writer.writerow(["pagina", "anno", "id", "luogo"])

        for idx in range(total_pages):
            if not index_in_includes(idx, includes):
                continue

            page = doc.load_page(idx)
            pg_num = get_footer_page_number(page)

            page_id, page_year = extract_id_year(page, last_id, last_year)
            last_id, last_year = page_id, page_year

            body_rect = compute_body_rect(page)
            body_text = text_for_nlp(page, body_rect)

            # step NER + filtraggio
            candidates, pre_excluded = detect_candidates_with_context(nlp, body_text)

            # salva info su esclusi preliminari
            for term, reason in pre_excluded:
                excluded_rows.append((
                    pg_num, page_year or "", page_id or "",
                    term, "pre_filter", reason
                ))

            unique_candidates = ordered_unique(candidates)

            # per annale_tagged.json
            page_tag_attest: List[Dict] = []

            for term in unique_candidates:
                # individua bounding boxes di quel termine su questa pagina
                rects_here = locate_term_occurrences(page, body_rect, term)
                for r in rects_here:
                    if r is None:
                        continue
                    add_highlight_and_star(page, r)

                # snippet testuali
                snippets = _make_snippets_for_term(body_text, term)
                snippet_preview = snippets[0] if snippets else ""

                # aggiorna indice attestazioni
                norm = normalize_name(term)
                entry = attest_index.get(norm)
                if not entry:
                    entry = {
                        "term_display": term,
                        "occurrences": []
                    }

                boxes = []
                for rr in rects_here:
                    if rr is None:
                        continue
                    boxes.append([
                        float(rr.x0), float(rr.y0), float(rr.x1), float(rr.y1)
                    ])

                entry["term_display"] = (
                    entry["term_display"]
                    if len(entry["term_display"]) <= len(term)
                    else term
                )
                entry["occurrences"].append({
                    "page_label": pg_num,
                    "pdf_page_index": idx,
                    "boxes": boxes,
                    "snippet": snippet_preview,
                })
                attest_index[norm] = entry

                # aggiorna vista "tagged" per la pagina
                page_tag_attest.append({
                    "term": term,
                    "snippet": snippet_preview
                })

            # CSV principale: tutti i candidati trovati su questa pagina
            writer.writerow([
                pg_num,
                page_year or "",
                page_id or "",
                ";".join(unique_candidates)
            ])

            tagged_pages.append({
                "page": pg_num,
                "attestations": page_tag_attest
            })

    # salva PDF marcato
    doc.save(pdf_out_path)
    doc.close()

    # salva CSV degli esclusi preliminari
    with open(excl_csv, "w", newline="", encoding="utf-8") as exf:
        w = csv.writer(exf)
        w.writerow(["pagina", "anno", "id", "termine", "stadio", "ragione"])
        for row in excluded_rows:
            w.writerow(row)

    # salva indice attestazioni
    with open(attest_json_path, "w", encoding="utf-8") as f_att:
        json.dump(attest_index, f_att, ensure_ascii=False, indent=2)

    # salva versione "tagged"
    with open(tagged_json_path, "w", encoding="utf-8") as f_tag:
        json.dump(tagged_pages, f_tag, ensure_ascii=False, indent=2)

    return {
        "csv": csv_path,
        "pdf": pdf_out_path,
        "exclusions": excl_csv,
        "attestazioni": attest_json_path,
        "tagged": tagged_json_path,
        "total_pages": total_pages,
        "include_ranges": includes,
    }
