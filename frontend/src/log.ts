/**
 * Mission log overlay ("Log" toolbar button): the last 50 `log[]` entries, newest first,
 * in the selected team language (`log[].text[lang]`, falling back to en-IN).
 * Comms are out of v1.
 */
import { esc, type Lang, LANGS, logText, store } from "./state";

export function initLog(overlay: HTMLElement, list: HTMLElement, langSel: HTMLSelectElement) {
  langSel.innerHTML = LANGS.map((l) => `<option value="${l.code}">${l.native}</option>`).join("");
  langSel.value = store.local.logLang;
  langSel.addEventListener("change", () => {
    store.local.logLang = langSel.value as Lang;
    render(true);
  });
  let last = "";
  function render(force = false) {
    if (!overlay.classList.contains("on") && !force) return;
    const s = store.snap;
    const lang = store.local.logLang;
    const html =
      s && s.log.length
        ? s.log
            .slice(-60)
            .reverse()
            .map(
              (e) =>
                `<li class="log-line src-${esc(e.src)}"><time class="mono">${esc(store.clockFor(`log:${e.t}:${e.key}`, e.t))}</time><span class="src">${esc(e.src)}</span><span class="txt" lang="${lang.slice(0, 2)}">${esc(logText(e, lang))}</span></li>`,
            )
            .join("")
        : '<li class="dim">—</li>';
    if (html !== last) {
      list.innerHTML = html;
      last = html;
    }
  }
  return { render };
}
