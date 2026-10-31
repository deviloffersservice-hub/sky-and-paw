/**
 * TTS badge and playback. When `tts.playing.url` changes, the console plays the
 * backend-served Bulbul WAV in an <audio> element. Browsers allow that only after a user
 * gesture, so playback is armed by the first pointer or key press on the page; a clip
 * that started in the last 10 s before then is played on arming.
 */
import { type Backend, httpUrl } from "./backend";
import { esc, langName, store, type TtsPlaying } from "./state";

export function initAudio(badge: HTMLElement, audio: HTMLAudioElement, backend: Backend) {
  let armed = false;
  let lastKey = "";
  let pending: TtsPlaying | null = null;

  const play = (p: TtsPlaying) => {
    audio.src = httpUrl(backend, p.url);
    audio.dataset.started = String(p.started_t);
    audio.play().catch(() => {
      // Autoplay refused (no gesture yet) or the file is missing: the badge still shows the text.
    });
  };
  const arm = () => {
    if (armed) return;
    armed = true;
    document.body.dataset.audio = "armed";
    if (pending && store.missionT() - pending.started_t < 10) play(pending);
    pending = null;
  };
  window.addEventListener("pointerdown", arm, { capture: true });
  window.addEventListener("keydown", arm, { capture: true });

  let lastHtml = "";
  return {
    armed: () => armed,
    render() {
      const p = store.snap?.tts?.playing ?? null;
      const html = p ? `<b>Go2 speaking · ${esc(langName(p.lang))} · ${esc(p.voice)}</b>${esc(p.text)}${armed ? "" : '<small class="tts-hint">click anywhere to hear it</small>'}` : "";
      badge.classList.toggle("on", !!p);
      if (html !== lastHtml) {
        badge.innerHTML = html;
        lastHtml = html;
      }
      if (!p) return;
      const key = `${p.url}|${p.started_t}`;
      if (key === lastKey) return;
      lastKey = key;
      if (armed) play(p);
      else pending = p;
    },
  };
}
