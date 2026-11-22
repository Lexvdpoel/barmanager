// Language toggle (EN / NL) and the copy button.
(function () {
  "use strict";
  const html = document.documentElement;
  const titles = {
    en: "BarManager · Your home bar, always in stock",
    nl: "BarManager · Je thuisbar, altijd op voorraad",
  };

  function setLang(lang, save) {
    html.dataset.lang = lang;
    html.lang = lang;
    document.title = titles[lang];
    document.querySelectorAll(".lang button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.lang === lang)));
    if (save) { try { localStorage.setItem("bm-lang", lang); } catch (e) { /* private mode */ } }
  }

  let lang = "en";
  try { lang = localStorage.getItem("bm-lang") || ""; } catch (e) { lang = ""; }
  if (!lang) lang = (navigator.language || "").toLowerCase().startsWith("nl") ? "nl" : "en";
  setLang(lang, false);

  document.addEventListener("DOMContentLoaded", () => {
    setLang(html.dataset.lang, false);
    document.querySelectorAll(".lang button").forEach((b) => b.addEventListener("click", () => setLang(b.dataset.lang, true)));
    document.querySelectorAll("[data-copy]").forEach((btn) => btn.addEventListener("click", async () => {
      const code = btn.parentElement.querySelector("code").innerText;
      try {
        await navigator.clipboard.writeText(code);
        btn.classList.add("done");
        const spans = btn.querySelectorAll("span");
        const old = [...spans].map((s) => s.textContent);
        spans[0].textContent = "Copied"; if (spans[1]) spans[1].textContent = "Gekopieerd";
        setTimeout(() => spans.forEach((s, i) => { s.textContent = old[i]; }), 1500);
      } catch (e) { /* no clipboard */ }
    }));
  });
})();
