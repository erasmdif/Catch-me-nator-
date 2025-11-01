// static/pdfviewer.js
// Viewer PDF interno all'iframe

(function(){
  const urlParams = new URLSearchParams(window.location.search);
  const pdfUrl = urlParams.get("pdf");

  const RENDER_SCALE = 1.25;
  const scrollHost = document.getElementById('scrollHost');

  // pageMeta[i] = { wrapper, canvas, scale }
  const pageMeta = [];
  window._pageMeta = pageMeta; // debug opzionale

  // setup pdf.js
  const pdfjsLib = window.pdfjsLib;
  if (!pdfjsLib) {
    console.error("pdfjsLib non caricato");
  } else {
    // assicuriamoci che il worker sia noto al lib
    pdfjsLib.GlobalWorkerOptions.workerSrc =
      "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.2.67/pdf.worker.min.js";
  }

  async function renderPDF() {
    if(!pdfUrl){
        console.warn("Nessun pdfUrl fornito");
        return;
    }

    const loadingTask = pdfjsLib.getDocument(pdfUrl);
    const pdf = await loadingTask.promise;
    const numPages = pdf.numPages;

    for(let pageNum = 1; pageNum <= numPages; pageNum++){
      const page = await pdf.getPage(pageNum);
      const viewport = page.getViewport({ scale: RENDER_SCALE });

      const wrapper = document.createElement('div');
      wrapper.className = 'pageWrapper';

      const canvas = document.createElement('canvas');
      const ctx = canvas.getContext('2d', {alpha:false});
      canvas.width = viewport.width;
      canvas.height = viewport.height;

      wrapper.appendChild(canvas);
      scrollHost.appendChild(wrapper);

      pageMeta[pageNum - 1] = {
        wrapper,
        canvas,
        scale: RENDER_SCALE
      };

      await page.render({
        canvasContext: ctx,
        viewport
      }).promise;
    }

    // avvisa il parent che il viewer è pronto
    try{
      window.parent.postMessage({cmd:"viewer_ready"}, "*");
    }catch(_){}
  }

  function clearHighlight(){
    const boxes = scrollHost.querySelectorAll('.hl-box');
    boxes.forEach(b => b.remove());
  }

  function drawHighlight(meta, box){
    if(!box || box.length !== 4) return;
    const [x0,y0,x1,y1] = box;
    const left = x0 * meta.scale;
    const top  = y0 * meta.scale;
    const w    = (x1 - x0) * meta.scale;
    const h    = (y1 - y0) * meta.scale;

    const hl = document.createElement('div');
    hl.className = 'hl-box';
    hl.style.left   = left + 'px';
    hl.style.top    = top + 'px';
    hl.style.width  = w + 'px';
    hl.style.height = h + 'px';
    meta.wrapper.appendChild(hl);
  }

  function jumpTo(pageIndex, box){
    const meta = pageMeta[pageIndex];
    if(!meta){
      console.warn("jumpTo: pagina non trovata", pageIndex);
      return;
    }

    clearHighlight();
    drawHighlight(meta, box);

    let y = meta.wrapper.offsetTop;
    if(box && box.length === 4){
      const [,y0, , ] = box;
      y = y + (y0 * meta.scale) - 40;
    } else {
      y = y - 40;
    }

    scrollHost.scrollTo({
      top: Math.max(0, y),
      behavior: 'smooth'
    });
  }

  // ascolta comandi dal parent
  window.addEventListener('message', (e)=>{
    const data = e.data;
    if(!data || typeof data !== 'object') return;
    if(data.cmd === 'jump'){
      const pageIndex = data.page_index;
      const box = data.box || null;
      jumpTo(pageIndex, box);
    }
  });

  renderPDF().catch(err=>{
    console.error("Errore rendering PDF:", err);
  });

})();
