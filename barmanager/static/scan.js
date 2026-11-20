// Barcode scanning: camera (native BarcodeDetector, ZXing as fallback) and
// keyboard-wedge / handheld scanners via the text field.
(function () {
  "use strict";
  const root = document.getElementById("scanner");
  if (!root) return;
  const lid = root.dataset.location;
  const $ = (id) => document.getElementById(id);
  const video = $("video"), videoWrap = $("videoWrap"), camOff = $("camOff"), camNote = $("camNote");
  const camToggle = $("camToggle"), camSelect = $("camSelect");
  const codeInput = $("code"), qtyInput = $("qty"), feed = $("feed"), toast = $("toast");

  let mode = "add";
  let running = false, stream = null, detector = null, zxing = null, loopTimer = null;
  let lastCode = "", lastTime = 0, scans = 0;
  const FORMATS = ["ean_13", "ean_8", "upc_a", "upc_e", "code_128", "code_39", "itf"];

  // ---- mode ----------------------------------------------------------------
  document.querySelectorAll(".seg-btn").forEach((b) => b.addEventListener("click", () => {
    mode = b.dataset.mode;
    document.querySelectorAll(".seg-btn").forEach((x) => {
      x.classList.toggle("on", x === b);
      x.setAttribute("aria-pressed", x === b ? "true" : "false");
    });
    codeInput.focus();
  }));

  // ---- feedback ---------------------------------------------------------------
  let audio = null;
  function beep(ok) {
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      const o = audio.createOscillator(), g = audio.createGain();
      o.frequency.value = ok ? 1250 : 330;
      g.gain.value = 0.08;
      o.connect(g); g.connect(audio.destination);
      o.start(); o.stop(audio.currentTime + (ok ? 0.09 : 0.25));
    } catch (e) { /* no audio */ }
    if (navigator.vibrate) navigator.vibrate(ok ? 60 : [80, 60, 80]);
  }
  let toastTimer = null;
  function showToast(text) {
    toast.textContent = text;
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.hidden = true; }, 2600);
  }

  // ---- feed --------------------------------------------------------------------
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function statusText(p) {
    if (p.warning) return p.warning;
    switch (p.status) {
      case "pending": return "Zoeken op internet…";
      case "found": return "Gevonden" + (p.source ? " via " + p.source : "");
      case "guess": return "Gevonden via webzoektocht, even controleren";
      case "not_found": return p.name ? "Handmatig ingevuld" : "Niet gevonden, vul zelf in";
      default: return p.created ? "Nieuw" : "Bekend product";
    }
  }
  function renderItem(el, p) {
    const delta = p.applied || 0;
    el.innerHTML = `
      <a class="card-img" href="${esc(p.url)}">${p.image
        ? `<img src="${esc(p.image)}" alt="">` : `<img src="/static/bottle.svg" alt="" class="ph">`}</a>
      <div class="card-body">
        <a class="card-title" href="${esc(p.url)}">${esc(p.display_name)}</a>
        <div class="card-meta">
          ${(p.categories || []).map((c) => `<span class="badge" style="--c:${esc(c.color)}">${esc(c.label)}</span>`).join("")}
          <span class="mono">${esc(p.barcode)}</span>
        </div>
        <div class="status ${p.status === "pending" ? "busy" : ""}">${esc(statusText(p))}</div>
      </div>
      <div class="delta ${delta > 0 ? "pos" : delta < 0 ? "neg" : ""}">
        ${delta > 0 ? "+" + delta : delta}
        <small>nu ${esc(p.quantity)}</small>
        ${delta ? `<button type="button" class="ghost undo">Ongedaan</button>` : ""}
      </div>`;
    const undo = el.querySelector(".undo");
    if (undo) undo.addEventListener("click", async () => {
      undo.disabled = true;
      try {
        const q = await window.BM.api(`/api/locaties/${lid}/items/${p.id}/aanpassen`, { delta: -delta });
        el.querySelector(".delta").innerHTML = `0 <small>nu ${esc(q.quantity)} · ongedaan</small>`;
      } catch (err) { showToast(err.message); undo.disabled = false; }
    });
  }
  function poll(el, p, tries) {
    if (p.status !== "pending" || tries > 40) return;
    setTimeout(async () => {
      try {
        const q = await window.BM.api(`/api/producten/${p.id}?loc=${lid}`);
        Object.assign(p, q);
        renderItem(el, p);
        if (q.status === "pending") poll(el, p, tries + 1);
        else if (q.status === "not_found" && !q.name) showToast("Onbekend product, tik erop om het in te vullen");
        else if (!q.image && q.images === 0) setTimeout(() => refreshImage(el, p), 3000);
      } catch (e) { poll(el, p, tries + 1); }
    }, 1500);
  }
  async function refreshImage(el, p) {
    try {
      const q = await window.BM.api(`/api/producten/${p.id}?loc=${lid}`);
      if (q.image) { Object.assign(p, q); renderItem(el, p); }
    } catch (e) { /* ignore */ }
  }
  function addItem(p, isError) {
    const empty = $("feedEmpty");
    if (empty) empty.remove();
    const el = document.createElement("div");
    el.className = "scan-item" + (isError ? " err" : "");
    feed.prepend(el);
    if (isError) {
      el.innerHTML = `<div class="card-img"><img src="/static/bottle.svg" alt="" class="ph"></div>
        <div class="card-body"><div class="card-title">${esc(p.barcode)}</div>
        <div class="status bad">${esc(p.message)}</div></div><div></div>`;
    } else {
      renderItem(el, p);
      poll(el, p, 0);
    }
    scans += 1;
    $("scanCount").textContent = `(${scans})`;
    while (feed.children.length > 50) feed.lastChild.remove();
  }

  // ---- submit a code --------------------------------------------------------------
  let busy = Promise.resolve();
  function submit(code) {
    code = (code || "").trim();
    if (!code) return;
    const qty = Math.max(1, Math.min(1000, parseInt(qtyInput.value, 10) || 1));
    busy = busy.then(async () => {
      try {
        const p = await window.BM.api(`/api/locaties/${lid}/scan`, { barcode: code, mode, qty });
        beep(!p.warning);
        addItem(p, false);
        if (p.warning) showToast(p.warning);
      } catch (err) {
        beep(false);
        addItem({ barcode: code, message: err.message }, true);
      }
    });
  }
  $("manual").addEventListener("submit", (e) => {
    e.preventDefault();
    submit(codeInput.value);
    codeInput.value = "";
    codeInput.focus();
  });

  function onDetected(code) {
    const now = Date.now();
    if (code === lastCode && now - lastTime < 2500) return;   // same bottle still in view
    lastCode = code; lastTime = now;
    videoWrap.classList.add("hit");
    setTimeout(() => videoWrap.classList.remove("hit"), 300);
    submit(code);
  }

  // ---- camera ------------------------------------------------------------------------
  if (!window.isSecureContext) {
    camNote.textContent = "De camera werkt alleen via https. Open BarManager via je https-adres, of gebruik het tekstveld met een handscanner.";
  } else if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    camNote.textContent = "Deze browser geeft geen toegang tot de camera.";
    camToggle.disabled = true;
  }

  async function nativeDetector() {
    if (!("BarcodeDetector" in window)) return null;
    try {
      const supported = await window.BarcodeDetector.getSupportedFormats();
      const formats = FORMATS.filter((f) => supported.includes(f));
      if (!formats.includes("ean_13")) return null;
      return new window.BarcodeDetector({ formats });
    } catch (e) { return null; }
  }

  function loadZxing() {
    if (window.ZXing) return Promise.resolve(window.ZXing);
    return new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = "/static/vendor/zxing.min.js";
      s.onload = () => resolve(window.ZXing);
      s.onerror = () => reject(new Error("Scanbibliotheek kon niet laden"));
      document.head.appendChild(s);
    });
  }

  async function listCameras() {
    try {
      const devices = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "videoinput");
      if (devices.length < 2) { camSelect.hidden = true; return; }
      const current = stream && stream.getVideoTracks()[0] && stream.getVideoTracks()[0].getSettings().deviceId;
      camSelect.innerHTML = devices.map((d, i) =>
        `<option value="${esc(d.deviceId)}" ${d.deviceId === current ? "selected" : ""}>${esc(d.label || "Camera " + (i + 1))}</option>`).join("");
      camSelect.hidden = false;
    } catch (e) { camSelect.hidden = true; }
  }

  async function start(deviceId) {
    camToggle.disabled = true;
    try {
      const constraints = {
        audio: false,
        video: deviceId ? { deviceId: { exact: deviceId } }
          : { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 720 } },
      };
      stream = await navigator.mediaDevices.getUserMedia(constraints);
      video.srcObject = stream;
      await video.play();
      videoWrap.hidden = false; camOff.hidden = true;
      running = true;
      camToggle.textContent = "Camera stoppen";
      try { localStorage.setItem("bm-camera", "on"); } catch (e) { /* private mode */ }
      listCameras();

      detector = detector || await nativeDetector();
      if (detector) {
        const tick = async () => {
          if (!running) return;
          try {
            if (video.readyState >= 2) {
              const codes = await detector.detect(video);
              if (codes.length) onDetected(codes[0].rawValue);
            }
          } catch (e) { /* frame not ready */ }
          loopTimer = setTimeout(tick, 150);
        };
        tick();
      } else {
        const Z = await loadZxing();
        const hints = new Map();
        hints.set(Z.DecodeHintType.POSSIBLE_FORMATS, [Z.BarcodeFormat.EAN_13, Z.BarcodeFormat.EAN_8,
          Z.BarcodeFormat.UPC_A, Z.BarcodeFormat.UPC_E, Z.BarcodeFormat.CODE_128, Z.BarcodeFormat.CODE_39, Z.BarcodeFormat.ITF]);
        hints.set(Z.DecodeHintType.TRY_HARDER, true);
        zxing = new Z.BrowserMultiFormatReader(hints, 200);
        zxing.decodeFromStream(stream, video, (result) => { if (result) onDetected(result.getText()); });
      }
    } catch (err) {
      stop();
      camNote.textContent = err && err.name === "NotAllowedError"
        ? "Geen toestemming voor de camera. Sta camera-toegang toe in je browser en probeer opnieuw."
        : "Camera starten mislukt: " + (err && err.message ? err.message : err);
    } finally {
      camToggle.disabled = false;
    }
  }

  function stop(remember) {
    running = false;
    clearTimeout(loopTimer);
    if (zxing) { try { zxing.reset(); } catch (e) { /* ignore */ } zxing = null; }
    if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
    video.srcObject = null;
    videoWrap.hidden = true; camOff.hidden = false;
    camToggle.textContent = "Camera starten";
    if (remember) { try { localStorage.setItem("bm-camera", "off"); } catch (e) { /* ignore */ } }
  }

  camToggle.addEventListener("click", () => (running ? stop(true) : start()));
  camSelect.addEventListener("change", () => { stop(); start(camSelect.value); });
  document.addEventListener("visibilitychange", () => { if (document.hidden && running) stop(); });

  // Start the camera straight away when it was on last time (phones), otherwise
  // focus the text field so a handheld scanner works immediately.
  let wasOn = false;
  try { wasOn = localStorage.getItem("bm-camera") === "on"; } catch (e) { /* ignore */ }
  if (wasOn && window.isSecureContext && navigator.mediaDevices) start();
  else codeInput.focus();
})();
