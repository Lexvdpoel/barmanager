// Small progressive enhancements shared by all pages.
(function () {
  "use strict";
  const csrf = document.body.dataset.csrf || "";
  // Translations: Dutch is the source text, window.BM_I18N holds the English ones when English is on.
  const I18N = window.BM_I18N || {};
  function t(text, vars) {
    let s = I18N[text] || text;
    if (vars) Object.keys(vars).forEach((k) => { s = s.split("{" + k + "}").join(String(vars[k])); });
    return s;
  }
  window.BM = { csrf, t, lang: window.BM_LANG || "nl" };

  window.BM.api = async function (url, body) {
    const r = await fetch(url, {
      method: body === undefined ? "GET" : "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf, Accept: "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
    });
    let data = {};
    try { data = await r.json(); } catch (e) { /* not json */ }
    if (!r.ok) {
      const err = new Error(data.detail || t("Er ging iets mis ({status})", { status: r.status }));
      err.status = r.status;
      throw err;
    }
    return data;
  };

  // Selects that submit their form right away.
  document.querySelectorAll("select[data-autosubmit]").forEach((s) =>
    s.addEventListener("change", () => s.form.submit()));

  // Forms that need confirmation.
  document.querySelectorAll("form[data-confirm]").forEach((f) =>
    f.addEventListener("submit", (e) => { if (!window.confirm(f.dataset.confirm)) e.preventDefault(); }));

  // Local time for timestamps.
  document.querySelectorAll("[data-ts]").forEach((el) => {
    const d = new Date(parseFloat(el.dataset.ts) * 1000);
    el.textContent = d.toLocaleString(window.BM.lang === "en" ? "en-GB" : "nl-NL",
      { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  });

  // Pages waiting for a background lookup refresh themselves.
  const auto = document.querySelector("[data-autorefresh]");
  if (auto) setTimeout(() => location.reload(), parseFloat(auto.dataset.autorefresh) * 1000);

  // +/- buttons on inventory cards without a full page reload.
  document.querySelectorAll("form.adj").forEach((f) => {
    f.addEventListener("submit", async (e) => {
      e.preventDefault();
      const card = f.closest(".card");
      const delta = parseInt(f.querySelector("[name=delta]").value, 10);
      const m = f.action.match(/\/locaties\/(\d+)\/items\/(\d+)\//);
      if (!m || !card) return f.submit();
      try {
        const p = await window.BM.api(`/api/locaties/${m[1]}/items/${m[2]}/aanpassen`, { delta });
        // A product with two categories is shown twice; update every copy.
        document.querySelectorAll(`.card[data-pid="${m[2]}"]`).forEach((el) => {
          el.querySelector(".qty-n").textContent = p.quantity;
          el.querySelector("button[data-step='-1']").disabled = p.quantity <= 0;
          const min = parseInt(el.querySelector(".qty-n").dataset.min || "0", 10) || 0;
          el.classList.remove("st-op", "st-bijna", "st-ok");
          el.classList.add(p.quantity <= 0 ? "st-op" : (min && p.quantity <= min ? "st-bijna" : "st-ok"));
        });
      } catch (err) {
        f.submit();
      }
    });
  });
})();
