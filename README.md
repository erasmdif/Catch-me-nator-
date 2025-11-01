# Toponimi PDF — estrazione & geocoding (spaCy + OSM)

[**→ Link al video**](#video)  

Applicazione web locale per:
- estrarre **toponimi** da PDF con **spaCy** (italiano),
- generare un **PDF marcato** con evidenziazioni,
- gestire **inclusioni/esclusioni** (anche per singola pagina/attestazione),
- (facoltativo) ottenere **geometrie** da **OpenStreetMap / Nominatim** e produrre **GeoJSON**,
- navigare **PDF** e **mappa** in modalità **split**, **focus** o **finestra flottante**.

---

## Come lavora (in breve)

1. **Upload PDF** → il file resta in locale (cartella `workspace/…`).
2. **Estrazione** → PyMuPDF isola il corpo del testo; spaCy (it_core_news_*) individua LOC/GPE; regole contestuali filtrano rumore.  
   - Output:  
     - `annale_toponimi.csv` (toponimi per pagina)  
     - `annale_toponimi_esclusi.csv` (candidati scartati automaticamente)  
     - `annale_marked.pdf` (PDF con highlight).
3. **Cura editoriale** (UI) → scegli cosa **escludere** o **re-includere** (per toponimo o **per singola pagina**).  
   - Stato salvato in `annale_user_state.json`.  
   - Viene rigenerato `annale_toponimi_filtered.csv` (base per il geocoding).
4. **Geocoding (opzionale)** → Nominatim (OSM) risolve i nomi (con cache e ranking robusto).  
   - Output: `annale_toponimi_grouped.geojson` (+ eventuali rejects).
5. **Esplorazione** → mappa Leaflet con popup, link “Vai” che sincronizza mappa + PDF.

---

## Requisiti

- **Python 3.10+**
- Sistema testato su **Ubuntu** (funziona anche su macOS/Windows).
- Connessione internet **solo se** esegui il geocoding (Nominatim).
- Dipendenze Python (vedi `requirements.txt`), inclusi:
  - `spacy`, `pymupdf` (PyMuPDF), `flask`, `requests`, ecc.
- Modello spaCy italiano (almeno **`it_core_news_sm`**; meglio `md`/`lg` se disponibili).

---

## Avvio (locale)

```bash
# 1) Clona il repository
git clone <questo-repo> toponimi-pdf
cd toponimi-pdf

# 2) Crea (consigliato) un virtualenv
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows (PowerShell)
# .\.venv\Scripts\Activate.ps1

# 3) Installa le dipendenze
pip install -r requirements.txt

# 4) Installa un modello spaCy italiano
python -m spacy download it_core_news_sm
# (se li hai, puoi usare anche it_core_news_md / it_core_news_lg)

# 5) (Opzionale) imposta la tua email per Nominatim
#    (rispetta i ToS: l'email viene inviata come User-Agent)
export NOMINATIM_EMAIL="tua_email@example.com"     # Linux/macOS
# $env:NOMINATIM_EMAIL="tua_email@example.com"     # Windows PowerShell

# 6) Avvia il server Flask
python server.py
```

Apri il browser su **http://127.0.0.1:5000**.

> Porta diversa?  
> Avvia con `FLASK_RUN_PORT=5050 python server.py` (o usa un reverse proxy).  

---

## Come funziona (passo–passo)

> Hai fretta? [Guarda il video](#video).

### 1) Inserisci PDF → “Estrai CSV”
- Trascina un PDF nella **sidebar** (o clicca per selezionarlo).  
- (Opzionale) indica un **range** di pagine: es. `51-104,115-136`.  
- Clicca **“Estrai CSV”**:
  - `annale_toponimi.csv`: toponimi trovati per pagina.
  - `annale_toponimi_esclusi.csv`: candidati scartati automaticamente (p.es. mancanza di contesto spaziale).
  - `annale_marked.pdf`: PDF con evidenziazioni degli hit.

Il PDF marcato appare nel viewer a destra.

### 2) Liste “Toponimi inclusi” / “Toponimi esclusi”
Le due liste lavorano in modo **simmetrico** e **gerarchico**.

- **Espandi (▶)** un toponimo per vedere le **attestazioni (pagine)**:
  - **Vai**: apre la pagina corrispondente nel PDF e centra la mappa sul toponimo.
- **Inclusi**:
  - Spunta un **toponimo** → **Escludi toponimo/i** (tutte le sue attestazioni).
  - Spunta **pagine** specifiche → **Escludi attestazioni** (esclude solo quelle).
- **Esclusi**:
  - Spunta un **toponimo** → **Re-includi toponimo/i** (rimuove l’esclusione globale).
  - Spunta **pagine** → **Re-includi attestazioni** (rientrano anche quelle scartate automaticamente).
- La UI mostra sempre la situazione **corrente**: ciò che resta **incluso** determina il CSV **filtrato** usato per il geocoding (`annale_toponimi_filtered.csv`).

> **Ratio dell’esclusione**  
> Si tende a escludere:
> - falsi positivi (es. termini istituzionali generici senza contesto),
> - citazioni non-geografiche,
> - errori OCR.  
> Ma puoi **sempre** reincludere a piacimento, anche singole pagine.

### 3) Avvia geocoding (facoltativo)
- Clicca **“Avvia geocoding”**.  
- Il sistema consulta **Nominatim** (OSM) con una strategia “name-aware + admin-aware”, cache locale e backoff soft:
  - output principale: **`annale_toponimi_grouped.geojson`** (un feature per toponimo, con conteggi e pagine),
  - eventuali rifiutati: `annale_toponimi_grouped_rejects.csv`.

> Non serve per lavorare con il solo PDF.  
> Ricorda: il geocoding usa la **rete**.

### 4) Moduli **Mappa** e **Lettore PDF**
- **PDF**: mostra `annale_marked.pdf`; “Vai” salta alla pagina (`#page=N`).
- **Mappa**: layer OSM + GeoJSON generato; popup con **display_name**, **attestazioni** e **pagine**.
- **Modalità di visualizzazione** (controlli nella testata di ogni modulo):
  - **⛶ Focus**: il modulo occupa tutta l’area di destra (l’altro si nasconde).
  - **📌 Finestra flottante**: il modulo diventa una **finestra mobile** (trascinabile e ridimensionabile) sopra l’altro, che passa in modalità **a tutto schermo**.
  - **↺ Ripristina**: torna alla modalità **split** (PDF sopra, mappa sotto).

### 5) Download
In alto a destra trovi i link ai file prodotti:
- `annale_marked.pdf` — PDF evidenziato
- `annale_toponimi.csv` — estratto “grezzo”
- `annale_toponimi_filtered.csv` — **usato per geocoding** dopo le scelte utente
- `annale_toponimi_esclusi.csv` — scarti automatici (fallback)
- `annale_toponimi.ndjson` / `annale_toponimi.geojson` — (pipeline legacy)
- `annale_toponimi_grouped.geojson` — **geocoding raggruppato**
- `annale_toponimi_grouped_rejects.csv` — non risolti
- `geocache_toponyms.json` — cache Nominatim
- `annale_user_state.json` — tue scelte (globali e per pagina)
- `geocode_progress.json` — stato avanzamento

---

## Struttura dei file (per job)

I file vivono in `workspace/<job_id>/`:

| File | Descrizione |
|---|---|
| `annale.pdf` | PDF caricato dall’utente |
| `annale_marked.pdf` | PDF con evidenziazioni degli hit |
| `annale_toponimi.csv` | Toponimi per pagina (post-estrazione) |
| `annale_toponimi_esclusi.csv` | Candidati scartati automaticamente |
| `annale_toponimi_filtered.csv` | Toponimi effettivamente **inclusi** dopo le scelte |
| `annale_user_state.json` | Stato esclusioni/reinclusioni (globali e per pagina) |
| `annale_toponimi_grouped.geojson` | Geometrie raggruppate per toponimo |
| `annale_toponimi_grouped_rejects.csv` | Toponimi non risolti |
| `geocache_toponyms.json` | Cache delle risposte Nominatim |
| `annale_toponimi.ndjson` / `annale_toponimi.geojson` | Output legacy (per compatibilità) |
| `geocode_progress.json` | Avanzamento del geocoding |

---

## Suggerimenti & limiti

- **OCR**: la qualità del PDF influenza l’estrazione. Rumore tipografico e testi scansionati male riducono il recall.
- **Contesto**: il filtro privilegia attestazioni con segnali “spaziali” (preposizioni, verbi di residenza, indirizzi).  
  Puoi sempre reincludere manualmente.
- **Nominatim**: rispetta i ToS (User-Agent con email, richieste moderate).  
  La cache riduce le chiamate ripetute (`geocache_toponyms.json`).
- **Modelli spaCy**: se possibile usa `it_core_news_md` o `lg` per una NER più robusta.

---

## Licenza

**Creative Commons Attribution – NonCommercial – ShareAlike 4.0 International**  
(**CC BY-NC-SA 4.0**)

Puoi usare, condividere e adattare l’opera **senza scopi commerciali**, citando l’autore e **condividendo allo stesso modo**.

---

## Crediti

- **spaCy** (NER italiano)  
- **PyMuPDF** (estrazione testo / highlight PDF)  
- **Leaflet** (mappa)  
- **OpenStreetMap / Nominatim** (geocoding & confini)  
- **Flask** (server)

---

## <a id="video"></a>📹 Video dimostrativo

![Demo](media/video_example.gif)

*(Il video mostra: upload PDF, estrazione, gestione inclusi/esclusi per toponimo e per pagina, modalità focus/flottante dei moduli, avvio geocoding e lettura del GeoJSON in mappa.)*
