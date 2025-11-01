// static/app.js

// ================== STATO GLOBALE ==================
let JOB_ID = null;
let PDF_URL = null;
let MAP = null;
let GEOJSON_LAYER = null;

let PROG_TIMER = null;

// cache attestazioni per il toponimo incluso aperto
let ATTEST_CACHE = {};

// selezioni per esclusione (nella lista inclusi)
const SELECT_EXCLUDE_TOPOS = new Set();     // toponimi da escludere globalmente
const SELECT_EXCLUDE_ATTEST = new Set();    // attestazioni da escludere "term|page"

// selezioni per reinclusione (nella lista esclusi)
const SELECT_INCLUDE_TOPOS = new Set();     // toponimi da reincludere globalmente
const SELECT_INCLUDE_ATTEST = new Set();    // attestazioni da reincludere "term|page"

// per drag della finestra flottante
let DRAGGING = false;
let DRAG_START_X = 0;
let DRAG_START_Y = 0;
let DRAG_ORIG_LEFT = 0;
let DRAG_ORIG_TOP = 0;
let DRAG_EL = null;


// ================== UTILS UI ==================
function toast(msg, ms=2500){
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.display = 'block';
  setTimeout(()=>{ t.style.display='none'; }, ms);
}

function currentLayout(){
  const content = document.getElementById('content');
  if(!content) return 'split';
  return content.getAttribute('data-layout') || 'split';
}

function clearFloatingStyles(){
  // rimuove dimensioni/posizioni inline così tornano responsive
  ['pdfContainer','mapContainer'].forEach(id=>{
    const el = document.getElementById(id);
    if(!el) return;
    el.style.removeProperty('left');
    el.style.removeProperty('top');
    el.style.removeProperty('width');
    el.style.removeProperty('height');
  });
}

function setLayout(mode){
  const content = document.getElementById('content');
  if(!content) return;
  // se non è float, puliamo gli inline style
  if(!mode.startsWith('float-')){
    clearFloatingStyles();
  }

  content.setAttribute('data-layout', mode);

  // se siamo in modalità floating, diamo dimensione/posizione iniziale alla finestra mobile
  if(mode === 'float-pdf'){
    const pdfEl = document.getElementById('pdfContainer');
    if(pdfEl){
      pdfEl.style.left = '16px';
      pdfEl.style.top = '16px';
      pdfEl.style.width = '40%';
      pdfEl.style.height = '40%';
    }
  } else if(mode === 'float-map'){
    const mapEl = document.getElementById('mapContainer');
    if(mapEl){
      mapEl.style.left = '16px';
      mapEl.style.top = '16px';
      mapEl.style.width = '40%';
      mapEl.style.height = '40%';
    }
  }

  // forziamo Leaflet ad aggiornare le dimensioni quando cambia layout
  if(MAP){
    setTimeout(()=>{
      MAP.invalidateSize();
    }, 0);
  }
}


// ================== DOWNLOADS / PDF ==================
function setDownloads(files){
  const box = document.getElementById('downloads');
  box.innerHTML = '';
  const keys = Object.keys(files || {}).sort();
  for(const k of keys){
    const a = document.createElement('a');
    a.href = files[k];
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = '⬇ ' + k;
    box.appendChild(a);
  }
  updatePdfUrlFromFiles(files || {});
}

function updatePdfUrlFromFiles(files){
  if(!PDF_URL && files["annale_marked.pdf"]){
    PDF_URL = files["annale_marked.pdf"];
  }
}

function loadPdfInFrame(pageLabel){
  if(!PDF_URL) return;
  const frame = document.getElementById('pdfFrame');
  const targetPage = pageLabel || 1;
  const newSrc = PDF_URL + '#page=' + targetPage;

  // hack per forzare salto di pagina
  frame.src = 'about:blank';
  setTimeout(()=>{
    frame.src = newSrc;
  }, 0);
}


// ================== MAPPA ==================
function focusMapOnTerm(termDisplay){
  if(!GEOJSON_LAYER) return;
  let targetLayer = null;
  GEOJSON_LAYER.eachLayer(layer=>{
    if(!layer || !layer.feature || !layer.feature.properties) return;
    const props = layer.feature.properties;
    const luogo = (props.luogo || "").toLowerCase();
    if(luogo === termDisplay.toLowerCase()){
      targetLayer = layer;
    }
  });
  if(targetLayer){
    try{
      if(targetLayer.getBounds){
        MAP.fitBounds(targetLayer.getBounds(), {padding:[20,20]});
      }else if(targetLayer.getLatLng){
        MAP.setView(targetLayer.getLatLng(), 10);
      }
      if(targetLayer.openPopup) targetLayer.openPopup();
    }catch(_){}
  }
}


// ================== LISTA TOPONIMI INCLUSI ==================
async function toggleTermExpansion(term, sublistDiv, expandBtn){
  // se già aperto => chiudi
  if(!sublistDiv.classList.contains('hidden-sublist')){
    sublistDiv.classList.add('hidden-sublist');
    expandBtn.textContent = "▶";
    return;
  }

  // lazy load attestazioni
  if(!ATTEST_CACHE[term]){
    if(!JOB_ID){
      toast("Nessun job attivo");
      return;
    }
    const url = `/api/attestations?job_id=${encodeURIComponent(JOB_ID)}&term=${encodeURIComponent(term)}`;
    const r = await fetch(url);
    const j = await r.json();
    if(!j.ok){
      toast(j.error || "Errore attestazioni");
      return;
    }
    ATTEST_CACHE[term] = j;
  }

  renderAttestations(term, ATTEST_CACHE[term], sublistDiv);

  sublistDiv.classList.remove('hidden-sublist');
  expandBtn.textContent = "▼";
}

function renderAttestations(term, attData, sublistDiv){
  sublistDiv.innerHTML = "";

  const { occurrences = [], excluded_global = false } = attData;

  occurrences.forEach(occ=>{
    const pageLabel = occ.page_label;
    const key = `${term}|${pageLabel}`;

    const row = document.createElement('div');
    row.className = "att-row";
    if(excluded_global || occ.excluded_specific){
      row.classList.add("excluded");
    }

    // checkbox per escludere questa attestazione
    const lblCheck = document.createElement('label');
    lblCheck.className = "att-check";

    const chk = document.createElement('input');
    chk.type = "checkbox";
    chk.className = "chk-att";
    chk.dataset.term = term;
    chk.dataset.page = pageLabel;
    chk.checked = SELECT_EXCLUDE_ATTEST.has(key);

    chk.addEventListener('change', e=>{
      if(e.target.checked){
        SELECT_EXCLUDE_ATTEST.add(key);
      } else {
        SELECT_EXCLUDE_ATTEST.delete(key);
      }
    });

    lblCheck.appendChild(chk);

    const pageSpan = document.createElement('span');
    pageSpan.className = "att-page";
    pageSpan.textContent = `p. ${pageLabel}`;
    lblCheck.appendChild(pageSpan);

    row.appendChild(lblCheck);

    const gotoBtn = document.createElement('button');
    gotoBtn.className = "goto-btn";
    gotoBtn.textContent = "Vai";
    gotoBtn.title = occ.snippet || "";
    gotoBtn.addEventListener('click', ()=>{
      loadPdfInFrame(pageLabel);
      focusMapOnTerm(term);
    });
    row.appendChild(gotoBtn);

    const statusSpan = document.createElement('span');
    statusSpan.className = "att-status";
    if(excluded_global){
      statusSpan.textContent = "(escluso globalmente)";
    } else if(occ.excluded_specific){
      statusSpan.textContent = "(escluso qui)";
    } else {
      statusSpan.textContent = "";
    }
    row.appendChild(statusSpan);

    sublistDiv.appendChild(row);
  });
}

function renderIncludedToponyms(list){
  const box = document.getElementById('toponymsList');
  if(!box) return;
  box.innerHTML = '';

  if(!list || !list.length){
    box.innerHTML = '<div class="item"><em>Nessun elemento</em></div>';
    return;
  }

  list.forEach(it=>{
    const term = it.name || "";
    const count = it.count || 0;

    const block = document.createElement('div');
    block.className = "topo-block";

    const header = document.createElement('div');
    header.className = "topo-header";

    const expandBtn = document.createElement('button');
    expandBtn.className = "expand-btn";
    expandBtn.textContent = "▶";

    const sublistDiv = document.createElement('div');
    sublistDiv.className = "sublist hidden-sublist";

    expandBtn.addEventListener('click', async ()=>{
      await toggleTermExpansion(term, sublistDiv, expandBtn);
    });

    header.appendChild(expandBtn);

    const topoLabel = document.createElement('label');
    topoLabel.className = "sel-topo";

    const chkTopo = document.createElement('input');
    chkTopo.type = "checkbox";
    chkTopo.className = "chk-topo";
    chkTopo.dataset.term = term;
    chkTopo.checked = SELECT_EXCLUDE_TOPOS.has(term);
    chkTopo.addEventListener('change', e=>{
      if(e.target.checked){
        SELECT_EXCLUDE_TOPOS.add(term);
      } else {
        SELECT_EXCLUDE_TOPOS.delete(term);
      }
    });
    topoLabel.appendChild(chkTopo);

    const nameSpan = document.createElement('span');
    nameSpan.className = "term-label";
    nameSpan.textContent = term;
    topoLabel.appendChild(nameSpan);

    const cntSpan = document.createElement('span');
    cntSpan.className = 'count';
    cntSpan.textContent = `(${count})`;
    topoLabel.appendChild(cntSpan);

    header.appendChild(topoLabel);

    block.appendChild(header);
    block.appendChild(sublistDiv);

    box.appendChild(block);
  });
}


// ================== LISTA TOPONIMI ESCLUSI ==================
function renderExcludedToponyms(excluded_state){
  const box = document.getElementById('excludedList');
  if(!box) return;
  box.innerHTML = '';

  const glob = excluded_state.global || [];
  const perPage = excluded_state.per_page || [];

  const groups = {};

  // globalmente esclusi
  glob.forEach(row=>{
    const disp = row.display || row.name_norm || "";
    if(!groups[disp]){
      groups[disp] = { isGlobal: true, pages: [] };
    } else {
      groups[disp].isGlobal = true;
    }
  });

  // pagine escluse (manuali o fallback)
  perPage.forEach(row=>{
    const disp = row.display || row.name_norm || "";
    const page = row.page;
    if(!groups[disp]){
      groups[disp] = { isGlobal: false, pages: [] };
    }
    if(page != null && page !== ""){
      if(!groups[disp].pages.includes(page)){
        groups[disp].pages.push(page);
      }
    }
  });

  const names = Object.keys(groups).sort((a,b)=>a.localeCompare(b,'it'));

  if(!names.length){
    box.innerHTML = '<div class="item"><em>Nessun elemento</em></div>';
    return;
  }

  names.forEach(disp=>{
    const g = groups[disp];

    g.pages.sort((a,b)=>{
      const pa = parseInt(a,10), pb = parseInt(b,10);
      if(isNaN(pa)||isNaN(pb)) return String(a).localeCompare(String(b),'it');
      return pa-pb;
    });

    const block = document.createElement('div');
    block.className = "topo-block excluded-block";

    const header = document.createElement('div');
    header.className = "topo-header";

    const expandBtn = document.createElement('button');
    expandBtn.className = "expand-btn";
    expandBtn.textContent = "▶";
    header.appendChild(expandBtn);

    // checkbox per RE-includere tutto quel toponimo
    const topoLabel = document.createElement('label');
    topoLabel.className = "sel-topo";

    const chkTopo = document.createElement('input');
    chkTopo.type = "checkbox";
    chkTopo.className = "chk-inc-topo";
    chkTopo.dataset.term = disp;
    chkTopo.checked = SELECT_INCLUDE_TOPOS.has(disp);
    chkTopo.addEventListener('change', e=>{
      if(e.target.checked){
        SELECT_INCLUDE_TOPOS.add(disp);
      } else {
        SELECT_INCLUDE_TOPOS.delete(disp);
      }
    });
    topoLabel.appendChild(chkTopo);

    const nameSpan = document.createElement('span');
    nameSpan.className = "term-label";
    nameSpan.textContent = disp;
    topoLabel.appendChild(nameSpan);

    const cntSpan = document.createElement('span');
    cntSpan.className = 'count';
    if(g.pages.length > 0){
      cntSpan.textContent = `(${g.pages.length})`;
    } else if(g.isGlobal){
      cntSpan.textContent = `(tutto)`;
    } else {
      cntSpan.textContent = `(0)`;
    }
    topoLabel.appendChild(cntSpan);

    header.appendChild(topoLabel);

    const sublistDiv = document.createElement('div');
    sublistDiv.className = "sublist hidden-sublist";

    g.pages.forEach(page=>{
      const key = `${disp}|${page}`;

      const rowDiv = document.createElement('div');
      rowDiv.className = 'att-row';

      const lbl = document.createElement('label');
      lbl.className = "att-check";

      const chk = document.createElement('input');
      chk.type = 'checkbox';
      chk.className = 'chk-inc-att';
      chk.dataset.term = disp;
      chk.dataset.page = page;
      chk.checked = SELECT_INCLUDE_ATTEST.has(key);

      chk.addEventListener('change', e=>{
        if(e.target.checked){
          SELECT_INCLUDE_ATTEST.add(key);
        } else {
          SELECT_INCLUDE_ATTEST.delete(key);
        }
      });
      lbl.appendChild(chk);

      const pgSpan = document.createElement('span');
      pgSpan.className = "att-page";
      pgSpan.textContent = `p. ${page}`;
      lbl.appendChild(pgSpan);

      rowDiv.appendChild(lbl);

      const gotoBtn = document.createElement('button');
      gotoBtn.className = 'goto-btn';
      gotoBtn.textContent = 'Vai';
      gotoBtn.addEventListener('click', ()=>{
        loadPdfInFrame(page);
        focusMapOnTerm(disp);
      });
      rowDiv.appendChild(gotoBtn);

      sublistDiv.appendChild(rowDiv);
    });

    expandBtn.addEventListener('click', ()=>{
      if(sublistDiv.classList.contains('hidden-sublist')){
        sublistDiv.classList.remove('hidden-sublist');
        expandBtn.textContent = "▼";
      } else {
        sublistDiv.classList.add('hidden-sublist');
        expandBtn.textContent = "▶";
      }
    });

    block.appendChild(header);
    block.appendChild(sublistDiv);

    box.appendChild(block);
  });
}


// ================== REFRESH TOPONIMI ==================
async function refreshToponyms(){
  if(!JOB_ID) return;
  const r = await fetch(`/api/toponyms?job_id=${encodeURIComponent(JOB_ID)}`);
  const j = await r.json();
  if(!j.ok){
    toast(j.error || 'Errore toponimi');
    return;
  }

  setDownloads(j.files || {});
  renderIncludedToponyms(j.included_summary || []);
  renderExcludedToponyms(j.excluded_state || {});
}


// ================== UPLOAD / ESTRARRE ==================
async function uploadPDF(file){
  const fd = new FormData();
  fd.append('pdf', file);
  const r = await fetch('/api/upload', {method:'POST', body:fd});
  const j = await r.json();
  if(!j.ok) throw new Error(j.error || 'Upload fallito');

  JOB_ID = j.job_id;
  document.getElementById('jobId').value = JOB_ID;
  toast('PDF caricato');

  // reset stato
  setDownloads({});
  PDF_URL = null;
  ATTEST_CACHE = {};
  SELECT_EXCLUDE_TOPOS.clear();
  SELECT_EXCLUDE_ATTEST.clear();
  SELECT_INCLUDE_TOPOS.clear();
  SELECT_INCLUDE_ATTEST.clear();
  clearFloatingStyles();
  setLayout('split');

  const frame = document.getElementById('pdfFrame');
  frame.src = '';
  if(GEOJSON_LAYER){ GEOJSON_LAYER.remove(); GEOJSON_LAYER = null; }

  await refreshToponyms();
}

async function doExtract(){
  if(!JOB_ID){ toast('Carica prima un PDF'); return; }
  const ranges = document.getElementById('ranges').value.trim();
  const r = await fetch('/api/extract', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ job_id: JOB_ID, ranges })
  });
  const j = await r.json();
  if(!j.ok){
    toast(j.error || 'Errore estrazione');
    return;
  }

  setDownloads(j.files || {});
  if(j.marked_pdf_url){
    PDF_URL = j.marked_pdf_url;
    loadPdfInFrame(1);
  }

  ATTEST_CACHE = {};
  await refreshToponyms();
  toast('Estrazione completata');
}


// ================== ESCLUSIONI / REINCLUSIONI ==================
async function excludeSelectedAll(){
  if(!JOB_ID){ toast('Nessun job'); return; }
  const exclude_toponyms = Array.from(SELECT_EXCLUDE_TOPOS);
  if(!exclude_toponyms.length){
    toast('Seleziona almeno un toponimo nella lista inclusi');
    return;
  }
  const r = await fetch('/api/exclusions', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      job_id: JOB_ID,
      exclude_toponyms
    })
  });
  const j = await r.json();
  if(!j.ok){
    toast(j.error || 'Errore esclusione');
    return;
  }
  setDownloads(j.files || {});
  SELECT_EXCLUDE_TOPOS.clear();
  await refreshToponyms();
  toast('Toponimi esclusi globalmente');
}

async function excludeSelectedAtt(){
  if(!JOB_ID){ toast('Nessun job'); return; }
  const exclude_attestations = Array.from(SELECT_EXCLUDE_ATTEST).map(k=>{
    const [term,page] = k.split('|');
    return {term, page: parseInt(page,10)};
  });
  if(!exclude_attestations.length){
    toast('Seleziona almeno una attestazione nella lista inclusi');
    return;
  }
  const r = await fetch('/api/exclusions', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      job_id: JOB_ID,
      exclude_attestations
    })
  });
  const j = await r.json();
  if(!j.ok){
    toast(j.error || 'Errore esclusione attestazioni');
    return;
  }
  setDownloads(j.files || {});
  SELECT_EXCLUDE_ATTEST.clear();
  await refreshToponyms();
  toast('Attestazioni escluse');
}

async function includeSelectedAll(){
  if(!JOB_ID){ toast('Nessun job'); return; }
  const include_toponyms = Array.from(SELECT_INCLUDE_TOPOS);
  if(!include_toponyms.length){
    toast('Seleziona almeno un toponimo nella lista esclusi');
    return;
  }
  const r = await fetch('/api/exclusions', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      job_id: JOB_ID,
      include_toponyms
    })
  });
  const j = await r.json();
  if(!j.ok){
    toast(j.error || 'Errore reinclusione globale');
    return;
  }
  setDownloads(j.files || {});
  SELECT_INCLUDE_TOPOS.clear();
  await refreshToponyms();
  toast('Toponimi reinclusi');
}

async function includeSelectedAtt(){
  if(!JOB_ID){ toast('Nessun job'); return; }
  const include_attestations = Array.from(SELECT_INCLUDE_ATTEST).map(k=>{
    const [term,page] = k.split('|');
    return {term, page: parseInt(page,10)};
  });
  if(!include_attestations.length){
    toast('Seleziona attestazioni nella lista esclusi');
    return;
  }
  const r = await fetch('/api/exclusions', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      job_id: JOB_ID,
      include_attestations
    })
  });
  const j = await r.json();
  if(!j.ok){
    toast(j.error || 'Errore reinclusione attestazioni');
    return;
  }
  setDownloads(j.files || {});
  SELECT_INCLUDE_ATTEST.clear();
  await refreshToponyms();
  toast('Attestazioni reincluse');
}


// ================== GEOCODING / MAPPA ==================
function showProgress(show){
  const p = document.getElementById('progress');
  p.classList.toggle('hidden', !show);
}

function setProgress(pct, txt){
  const bar = document.getElementById('progressBar');
  const t = document.getElementById('progressText');
  bar.style.width = `${Math.max(0, Math.min(100, pct))}%`;
  t.textContent = txt || `${pct}%`;
}

async function pollProgressAndMaybeLoad(){
  if(!JOB_ID) return;
  const r = await fetch(`/api/geocode_progress?job_id=${encodeURIComponent(JOB_ID)}`);
  const j = await r.json();
  if(!j.ok){ return; }

  if(j.status === 'starting' || j.status === 'running'){
    showProgress(true);
    setProgress(j.pct || 0, `${j.pct || 0}% ${j.current ? `– ${j.current}` : ''}`);
  } else if(j.status === 'done'){
    setProgress(100, '100% – completato');
    clearInterval(PROG_TIMER); PROG_TIMER = null;
    setDownloads(j.files || {});

    const files = j.files || {};
    const gjUrl = files['annale_toponimi_grouped.geojson'] || files['annale_toponimi.geojson'];
    if(gjUrl){
      const resp = await fetch(gjUrl);
      const gj = await resp.json();

      if(GEOJSON_LAYER){ GEOJSON_LAYER.remove(); GEOJSON_LAYER = null; }

      GEOJSON_LAYER = L.geoJSON(gj, {
        onEachFeature: (f, layer)=>{
          const p = f.properties || {};
          const name = p.luogo || '(sconosciuto)';
          const disp = p.display_name ? `<div><small>${p.display_name}</small></div>` : '';
          const mentions = (p.mentions != null) ? p.mentions : 1;
          const pages = p.pagine || p.pagina || '';
          const html = `<strong>${name}</strong>${disp}<div>Attestazioni: ${mentions}</div>${pages? `<div>Pagine: ${pages}</div>`:''}`;
          layer.bindPopup(html);
        }
      }).addTo(MAP);

      try{
        if(GEOJSON_LAYER.getBounds){
          MAP.fitBounds(GEOJSON_LAYER.getBounds(), {padding:[20,20]});
        }
      }catch(_){}
    }

    if(MAP){
      setTimeout(()=>MAP.invalidateSize(),0);
    }

    toast('GeoJSON pronto');
  } else if(j.status === 'error'){
    clearInterval(PROG_TIMER); PROG_TIMER = null;
    toast(j.current || 'Errore geocoding');
    showProgress(false);
  } else {
    // idle
  }
}

async function doGeocode(){
  if(!JOB_ID){ toast('Carica prima un PDF'); return; }
  const r = await fetch('/api/geocode_start', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ job_id: JOB_ID })
  });
  const j = await r.json();
  if(!j.ok){
    toast(j.error || 'Errore geocoding');
    return;
  }
  showProgress(true);
  setProgress(0, '0% – inizio');
  if(PROG_TIMER) clearInterval(PROG_TIMER);
  PROG_TIMER = setInterval(pollProgressAndMaybeLoad, 900);
}


// ================== DRAG DELLA FINESTRA FLOTTANTE ==================
function setupDraggable(containerId){
  const el = document.getElementById(containerId);
  if(!el) return;
  const header = el.querySelector('.viewer-header');
  if(!header) return;

  header.addEventListener('mousedown', (e)=>{
    // se clicco sui bottoni di controllo non devo trascinare
    if(e.target.closest('.viewer-controls')){
      return;
    }
    const layout = currentLayout();
    const isPdfFloat = (layout === 'float-pdf' && containerId === 'pdfContainer');
    const isMapFloat = (layout === 'float-map' && containerId === 'mapContainer');
    if(!isPdfFloat && !isMapFloat){
      return; // drag attivo solo in modalità floating
    }

    DRAGGING = true;
    DRAG_EL = el;
    DRAG_START_X = e.clientX;
    DRAG_START_Y = e.clientY;
    DRAG_ORIG_LEFT = parseFloat(el.style.left || '0');
    DRAG_ORIG_TOP  = parseFloat(el.style.top  || '0');
    e.preventDefault();
  });
}

function handleDragMove(e){
  if(!DRAGGING || !DRAG_EL) return;
  const dx = e.clientX - DRAG_START_X;
  const dy = e.clientY - DRAG_START_Y;
  DRAG_EL.style.left = (DRAG_ORIG_LEFT + dx) + 'px';
  DRAG_EL.style.top  = (DRAG_ORIG_TOP  + dy) + 'px';
}

function handleDragEnd(){
  DRAGGING = false;
  DRAG_EL = null;
}


// ================== INIT MAPPA / INIT UI ==================
function initMap(){
  MAP = L.map('map', {zoomControl:true}).setView([41.1, 16.87], 6);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap'
  }).addTo(MAP);
}

function initUI(){
  // Drag globale mousemove/mouseup
  document.addEventListener('mousemove', handleDragMove);
  document.addEventListener('mouseup', handleDragEnd);

  // attiva draggable sulle due viewer
  setupDraggable('pdfContainer');
  setupDraggable('mapContainer');

  // controlli layout nelle header
  const fullBtns = document.querySelectorAll('.ctrl-full');
  const floatBtns = document.querySelectorAll('.ctrl-float');
  const resetBtns = document.querySelectorAll('.ctrl-reset');

  fullBtns.forEach(btn=>{
    btn.addEventListener('click', ()=>{
      const target = btn.dataset.target;
      if(target === 'pdf'){
        setLayout('focus-pdf');
      }else if(target === 'map'){
        setLayout('focus-map');
        if(MAP){
          setTimeout(()=>MAP.invalidateSize(),0);
        }
      }
    });
  });

  floatBtns.forEach(btn=>{
    btn.addEventListener('click', ()=>{
      const target = btn.dataset.target;
      if(target === 'pdf'){
        setLayout('float-pdf');
      }else if(target === 'map'){
        setLayout('float-map');
        if(MAP){
          setTimeout(()=>MAP.invalidateSize(),0);
        }
      }
    });
  });

  resetBtns.forEach(btn=>{
    btn.addEventListener('click', ()=>{
      setLayout('split');
      if(MAP){
        setTimeout(()=>MAP.invalidateSize(),0);
      }
    });
  });

  // drag&drop upload
  const drop = document.getElementById('dropzone');
  const fileinput = document.getElementById('fileinput');

  ['dragenter','dragover'].forEach(ev=>drop.addEventListener(ev, e=>{
    e.preventDefault(); drop.classList.add('hover');
  }));
  ['dragleave','drop'].forEach(ev=>drop.addEventListener(ev, e=>{
    e.preventDefault(); drop.classList.remove('hover');
  }));

  drop.addEventListener('drop', async e=>{
    const f = e.dataTransfer.files[0];
    if(f){
      try { await uploadPDF(f); }
      catch(err){ toast(err.message || 'Errore upload'); }
    }
  });

  fileinput.addEventListener('change', async e=>{
    const f = e.target.files[0];
    if(f){
      try { await uploadPDF(f); }
      catch(err){ toast(err.message || 'Errore upload'); }
    }
  });

  // bottoni estrazione/geocoding
  document.getElementById('btnExtract').addEventListener('click', async ()=>{
    try { await doExtract(); }
    catch(err){ toast(err.message || 'Errore estrazione'); }
  });
  document.getElementById('btnGeocode').addEventListener('click', async ()=>{
    try { await doGeocode(); }
    catch(err){ toast(err.message || 'Errore geocoding'); }
  });

  // bottoni inclusi/esclusi
  const btnExcludeAll = document.getElementById('btnExcludeAll');
  const btnExcludeAtt = document.getElementById('btnExcludeAtt');
  const btnIncludeAll = document.getElementById('btnIncludeAll');
  const btnIncludeAtt = document.getElementById('btnIncludeAtt');

  if(btnExcludeAll){
    btnExcludeAll.addEventListener('click', async ()=>{
      try { await excludeSelectedAll(); }
      catch(err){ toast(err.message || 'Errore esclusione'); }
    });
  }
  if(btnExcludeAtt){
    btnExcludeAtt.addEventListener('click', async ()=>{
      try { await excludeSelectedAtt(); }
      catch(err){ toast(err.message || 'Errore esclusione attestazioni'); }
    });
  }
  if(btnIncludeAll){
    btnIncludeAll.addEventListener('click', async ()=>{
      try { await includeSelectedAll(); }
      catch(err){ toast(err.message || 'Errore reinclusione globale'); }
    });
  }
  if(btnIncludeAtt){
    btnIncludeAtt.addEventListener('click', async ()=>{
      try { await includeSelectedAtt(); }
      catch(err){ toast(err.message || 'Errore reinclusione attestazioni'); }
    });
  }

  // stato iniziale layout
  setLayout('split');
}

window.addEventListener('DOMContentLoaded', async ()=>{
  initMap();
  initUI();
});
