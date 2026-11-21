// Barcode scanning: camera (native BarcodeDetector, ZXing as fallback) and
// keyboard-wedge / handheld scanners via the text field.
(function () {
  "use strict";
  const root = document.getElementById("scanner");
  if (!root) return;
  const lid = root.dataset.location;
  const $ = (id) => document.getElementById(id);
  const video = $("video"), videoWrap = $("videoWrap"), camOff = $("camOff"), camNote = $("camNote");
  const camToggle = $("camToggle"), camSelect = $("camSelect"), camStatus = $("camStatus");
  const zoomWrap = $("zoomWrap"), zoomInput = $("zoom"), torchBtn = $("torch");
  const codeInput = $("code"), qtyInput = $("qty"), feed = $("feed"), toast = $("toast");

  let mode = "add";
  let running = false, stream = null, engine = null, loopTimer = null, torchOn = false;
  let lastCode = "", lastTime = 0, scans = 0, frames = 0, statusTimer = null;
  // After one bottle the camera pauses until you tap "Volgende fles scannen": no double scans.
  let paused = false;
  const nextBtn = $("nextScan"), nextInfo = $("nextInfo");
  const FORMATS = ["ean_13", "ean_8", "upc_a", "upc_e", "code_128", "code_39", "itf"];

  // ---- mode ----------------------------------------------------------------
  document.querySelectorAll(".seg-btn").forEach((b) => b.addEventListener("click", () => {
    mode = b.dataset.mode;
    document.querySelectorAll(".seg-btn").forEach((x) => {
      x.classList.toggle("on", x === b);
      x.setAttribute("aria-pressed", x === b ? "true" : "false");
    });
  }));

  // ---- feedback ---------------------------------------------------------------
  // Phones only allow sound that was switched on by a tap. So the audio is created/resumed
  // on every tap (camera button, "next bottle", anywhere on the page) and reused for the beep.
  let audio = null;
  function unlockAudio() {
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      if (audio.state === "suspended") audio.resume();
      // iOS: play one silent sample inside the tap to really unlock the output.
      const b = audio.createBuffer(1, 1, 22050), s = audio.createBufferSource();
      s.buffer = b; s.connect(audio.destination); s.start(0);
    } catch (e) { /* no audio */ }
  }
  document.addEventListener("pointerdown", unlockAudio, { passive: true });
  document.addEventListener("keydown", unlockAudio);
  function tone(freq, start, dur) {
    const o = audio.createOscillator(), g = audio.createGain();
    o.type = "square";
    o.frequency.value = freq;
    const t = audio.currentTime + start;
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(0.35, t + 0.01);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    o.connect(g); g.connect(audio.destination);
    o.start(t); o.stop(t + dur + 0.02);
  }
  function beep(ok) {
    try {
      if (audio) {
        if (audio.state === "suspended") audio.resume();
        if (ok) tone(1800, 0, 0.12);
        else { tone(400, 0, 0.18); tone(300, 0.22, 0.25); }
      }
    } catch (e) { /* no audio */ }
    if (navigator.vibrate) navigator.vibrate(ok ? 120 : [120, 80, 120]);
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
  function submit(code, done) {
    code = (code || "").trim();
    if (!code) return;
    const qty = Math.max(1, Math.min(1000, parseInt(qtyInput.value, 10) || 1));
    busy = busy.then(async () => {
      try {
        const p = await window.BM.api(`/api/locaties/${lid}/scan`, { barcode: code, mode, qty });
        beep(!p.warning);
        if (done) done(p);
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
  });

  function onDetected(code) {
    if (paused) return;
    paused = true;
    videoWrap.classList.add("hit", "paused");
    setTimeout(() => videoWrap.classList.remove("hit"), 600);
    nextInfo.textContent = code;
    nextBtn.hidden = false;
    submit(code, (p) => { nextInfo.textContent = p.display_name || code; });
  }
  function resume() {
    paused = false;
    nextBtn.hidden = true;
    videoWrap.classList.remove("paused");
  }
  nextBtn.addEventListener("click", resume);

  // ---- decoders ----------------------------------------------------------------------
  // Both decoders work on a canvas with the centre of the camera image, which we draw
  // ourselves; this is far more reliable than handing the <video> to a library.
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d", { willReadFrequently: true });

  async function nativeEngine() {
    if (!("BarcodeDetector" in window)) return null;
    try {
      const supported = await window.BarcodeDetector.getSupportedFormats();
      const formats = FORMATS.filter((f) => supported.includes(f));
      if (!formats.includes("ean_13")) return null;
      const det = new window.BarcodeDetector({ formats });
      return {
        name: "ingebouwde scanner",
        // The native detector finds codes in any orientation, so give it the whole frame.
        decode: async () => {
          const r = await det.detect(video);
          return r.length ? r[0].rawValue : null;
        },
      };
    } catch (e) { return null; }
  }

  function loadZxing() {
    if (window.ZXing) return Promise.resolve(window.ZXing);
    return new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = "/static/vendor/zxing.min.js?v=0.21.3";
      s.onload = () => resolve(window.ZXing);
      s.onerror = () => reject(new Error("Scanbibliotheek kon niet laden"));
      document.head.appendChild(s);
    });
  }

  async function zxingEngine() {
    const Z = await loadZxing();
    const hints = new Map();
    hints.set(Z.DecodeHintType.POSSIBLE_FORMATS, [Z.BarcodeFormat.EAN_13, Z.BarcodeFormat.EAN_8,
      Z.BarcodeFormat.UPC_A, Z.BarcodeFormat.UPC_E, Z.BarcodeFormat.CODE_128, Z.BarcodeFormat.CODE_39,
      Z.BarcodeFormat.ITF]);
    hints.set(Z.DecodeHintType.TRY_HARDER, true);
    const reader = new Z.MultiFormatReader();
    reader.setHints(hints);
    let turn = 0;
    return {
      name: "ZXing",
      decode: async () => {
        const vw = video.videoWidth, vh = video.videoHeight;
        // ZXing only reads codes whose bars run (roughly) vertically in the image, and its own
        // rotation does not work on canvas input. Barcodes on bottles are often upright, so
        // cycle through: aiming box, whole frame, and both of those turned 90°.
        turn = (turn + 1) % 4;
        const rotate = turn >= 2;
        const box = turn % 2 === 0;
        // The aiming box (see .aim in the CSS); turned 90° it becomes a tall box.
        const fw = box ? (rotate ? 0.4 : 0.76) : 1, fh = box ? (rotate ? 0.8 : 0.36) : 1;
        const sw = vw * fw, sh = vh * fh;
        const sx = (vw - sw) / 2, sy = (vh - sh) / 2;
        const scale = Math.min(1, 1280 / Math.max(sw, sh));
        const dw = Math.round(sw * scale), dh = Math.round(sh * scale);
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        if (rotate) {
          canvas.width = dh; canvas.height = dw;
          ctx.translate(dh / 2, dw / 2);
          ctx.rotate(Math.PI / 2);
          ctx.drawImage(video, sx, sy, sw, sh, -dw / 2, -dh / 2, dw, dh);
          ctx.setTransform(1, 0, 0, 1, 0, 0);
        } else {
          canvas.width = dw; canvas.height = dh;
          ctx.drawImage(video, sx, sy, sw, sh, 0, 0, dw, dh);
        }
        try {
          const lum = new Z.HTMLCanvasElementLuminanceSource(canvas);
          const result = reader.decode(new Z.BinaryBitmap(new Z.HybridBinarizer(lum)));
          return result.getText();
        } catch (e) {
          return null;   // NotFound / Checksum / Format: no code in this frame
        } finally {
          reader.reset();
        }
      },
    };
  }

  // ---- camera ------------------------------------------------------------------------
  if (!window.isSecureContext) {
    camNote.textContent = "De camera werkt alleen via https. Open BarManager via je https-adres, of gebruik het tekstveld met een handscanner.";
  } else if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    camNote.textContent = "Deze browser geeft geen toegang tot de camera.";
    camToggle.disabled = true;
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

  function setupTrack() {
    const track = stream && stream.getVideoTracks()[0];
    zoomWrap.hidden = true; torchBtn.hidden = true;
    if (!track || !track.getCapabilities) return;
    const caps = track.getCapabilities();
    const adv = {};
    if (caps.focusMode && caps.focusMode.includes("continuous")) adv.focusMode = "continuous";
    if (Object.keys(adv).length) track.applyConstraints({ advanced: [adv] }).catch(() => {});
    if (caps.zoom && caps.zoom.max > caps.zoom.min) {
      zoomInput.min = caps.zoom.min; zoomInput.max = Math.min(caps.zoom.max, 8);
      zoomInput.step = caps.zoom.step || 0.1;
      // A little zoom lets you hold the phone further away, where the camera can focus.
      const start = Math.min(Math.max(caps.zoom.min, 1.6), zoomInput.max);
      zoomInput.value = start;
      track.applyConstraints({ advanced: [{ zoom: start }] }).catch(() => {});
      zoomWrap.hidden = false;
    }
    if (caps.torch) torchBtn.hidden = false;
  }
  zoomInput.addEventListener("input", () => {
    const track = stream && stream.getVideoTracks()[0];
    if (track) track.applyConstraints({ advanced: [{ zoom: parseFloat(zoomInput.value) }] }).catch(() => {});
  });
  torchBtn.addEventListener("click", () => {
    const track = stream && stream.getVideoTracks()[0];
    if (!track) return;
    torchOn = !torchOn;
    track.applyConstraints({ advanced: [{ torch: torchOn }] }).catch(() => {});
    torchBtn.classList.toggle("on", torchOn);
  });

  async function start(deviceId) {
    camToggle.disabled = true;
    camNote.textContent = "";
    try {
      const size = { width: { ideal: 1920 }, height: { ideal: 1080 } };
      const constraints = {
        audio: false,
        video: deviceId ? { deviceId: { exact: deviceId }, ...size } : { facingMode: { ideal: "environment" }, ...size },
      };
      stream = await navigator.mediaDevices.getUserMedia(constraints);
      videoWrap.hidden = false; camOff.hidden = true;   // visible before play(), iOS needs that
      video.setAttribute("playsinline", ""); video.muted = true;
      video.srcObject = stream;
      await video.play();
      running = true;
      resume();
      camToggle.textContent = "Camera stoppen";
      try { localStorage.setItem("bm-camera", "on"); } catch (e) { /* private mode */ }
      listCameras();
      setupTrack();

      engine = engine || await nativeEngine() || await zxingEngine();
      frames = 0;
      clearInterval(statusTimer);
      statusTimer = setInterval(() => {
        camStatus.textContent = `${engine.name} · ${video.videoWidth}×${video.videoHeight} · ${frames} beelden/s`;
        frames = 0;
      }, 1000);
      const tick = async () => {
        if (!running) return;
        if (!paused && video.readyState >= 2 && video.videoWidth) {
          try {
            const code = await engine.decode();
            frames += 1;
            if (code) onDetected(code);
          } catch (e) { /* frame not ready */ }
        }
        loopTimer = setTimeout(tick, 60);
      };
      tick();
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
    clearInterval(statusTimer);
    camStatus.textContent = "";
    if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
    video.srcObject = null;
    videoWrap.hidden = true; camOff.hidden = false;
    zoomWrap.hidden = true; torchBtn.hidden = true; torchOn = false;
    camToggle.textContent = "Camera starten";
    if (remember) { try { localStorage.setItem("bm-camera", "off"); } catch (e) { /* ignore */ } }
  }

  camToggle.addEventListener("click", () => (running ? stop(true) : start()));
  camSelect.addEventListener("change", () => { stop(); start(camSelect.value); });
  document.addEventListener("visibilitychange", () => { if (document.hidden && running) stop(); });

  // Start the camera straight away when it was on last time (phones). The text field is
  // not focused automatically on touch devices, so the keyboard does not pop up.
  let wasOn = false;
  try { wasOn = localStorage.getItem("bm-camera") === "on"; } catch (e) { /* ignore */ }
  if (wasOn && window.isSecureContext && navigator.mediaDevices) start();
  else if (!window.matchMedia("(pointer: coarse)").matches) codeInput.focus();
})();
