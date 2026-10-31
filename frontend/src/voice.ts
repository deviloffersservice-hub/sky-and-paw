/**
 * Voice strip (bottom-centre): hold-to-talk microphone streamed to the backend, the live
 * transcript from the snapshot, and the typed fallback.
 *
 * Capture: getUserMedia → AudioWorklet (ScriptProcessor fallback) at the device rate →
 * box-filter resample to 16 kHz mono → int16 LE → 1600-sample (3200-byte, 100 ms) binary
 * frames, sent between `{"type":"mic","state":"start"}` and `{"type":"mic","state":"stop"}`.
 */
import { esc, LANGS, LEAD_SAMPLES, logText, MODELS, store } from "./state";
import type { Connection } from "./ws";

export const TARGET_RATE = 16_000;
export const FRAME_SAMPLES = 1600; // 100 ms at 16 kHz = 3200 bytes

const WORKLET = `
class SkypawCapture extends AudioWorkletProcessor {
  constructor() { super(); this.buf = new Float32Array(2048); this.n = 0; }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) {
      for (let i = 0; i < ch.length; i++) {
        this.buf[this.n++] = ch[i];
        if (this.n === this.buf.length) { this.port.postMessage(this.buf.slice(0)); this.n = 0; }
      }
    }
    return true;
  }
}
registerProcessor('skypaw-capture', SkypawCapture);
`;

/** Streaming resampler: averages each output period of the input (anti-alias + decimate). */
export class Resampler {
  private rest = new Float32Array(0);
  private frac = 0;
  constructor(private ratio: number) {}

  push(input: Float32Array): Float32Array {
    const data = new Float32Array(this.rest.length + input.length);
    data.set(this.rest);
    data.set(input, this.rest.length);
    const r = this.ratio;
    const out = new Float32Array(Math.max(0, Math.floor((data.length - this.frac) / r) + 1));
    let n = 0;
    let start = this.frac;
    while (start + r <= data.length) {
      const a = Math.floor(start);
      let b = Math.floor(start + r);
      if (b <= a) b = a + 1;
      let sum = 0;
      for (let i = a; i < b; i++) sum += data[i] ?? 0;
      out[n++] = sum / (b - a);
      start += r;
    }
    const keep = Math.floor(start);
    this.rest = data.slice(keep);
    this.frac = start - keep;
    return out.subarray(0, n);
  }
}

type MicPhase = "idle" | "starting" | "live";

export function initVoice(els: {
  ptt: HTMLButtonElement;
  line: HTMLElement;
  sub: HTMLElement;
  toggle: HTMLButtonElement;
  more: HTMLElement;
}, conn: Connection) {
  const { ptt, line, sub, toggle, more } = els;
  let phase: MicPhase = "idle";
  let press = 0;
  let error = "";
  let framesSent = 0;
  let ctx: AudioContext | null = null;
  let workletReady: Promise<void> | null = null;
  let teardown: (() => void) | null = null;
  let pcm = new Int16Array(FRAME_SAMPLES);
  let fill = 0;
  let resampler: Resampler | null = null;

  more.innerHTML =
    `<div class="row"><label class="dim" for="micLang" style="align-self:center;font-size:12px">Mic language</label><select class="sel" id="micLang" aria-label="Microphone language"><option value="auto">auto-detect</option>${LANGS.map((l) => `<option value="${l.code}">${l.native}</option>`).join("")}</select></div>` +
    LEAD_SAMPLES.map((s, i) => `<button class="btn" data-s="${i}">${esc(s.t)}<small>${s.l}</small></button>`).join("") +
    `<div class="row"><input id="cmdText" placeholder="Type a command…" aria-label="Typed command"><select class="sel" id="cmdLang" aria-label="Command language">${LANGS.map((l) => `<option value="${l.code}">${l.native}</option>`).join("")}</select><button class="btn" id="cmdSend">Send</button></div>`;
  const micLang = more.querySelector("#micLang") as HTMLSelectElement;
  const cmdText = more.querySelector("#cmdText") as HTMLInputElement;
  const cmdLang = more.querySelector("#cmdLang") as HTMLSelectElement;
  cmdLang.value = "hi-IN";

  const sendText = (text: string, lang: string) => {
    if (!text.trim()) return;
    if (conn.send({ type: "text_command", text: text.trim(), lang })) {
      more.classList.remove("on");
      toggle.setAttribute("aria-expanded", "false");
    } else {
      error = "Not connected: command not sent";
      render();
    }
  };
  toggle.setAttribute("aria-expanded", "false");
  toggle.setAttribute("aria-label", "Typed command and samples");
  toggle.addEventListener("click", () => {
    const on = more.classList.toggle("on");
    toggle.setAttribute("aria-expanded", String(on));
    if (on) cmdText.focus({ preventScroll: true });
  });
  more.addEventListener("click", (e) => {
    const t = e.target as HTMLElement;
    const b = t.closest<HTMLElement>("[data-s]");
    if (b) {
      const s = LEAD_SAMPLES[Number(b.dataset.s)];
      if (s) sendText(s.t, s.l);
    }
    if (t.id === "cmdSend") {
      sendText(cmdText.value, cmdLang.value);
      cmdText.value = "";
    }
  });
  cmdText.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      sendText(cmdText.value, cmdLang.value);
      cmdText.value = "";
    }
  });

  const flush = (final: boolean) => {
    if (fill === FRAME_SAMPLES || (final && fill > 0)) {
      const out = pcm.slice(0, fill);
      if (conn.sendBinary(out.buffer)) framesSent++;
      pcm = new Int16Array(FRAME_SAMPLES);
      fill = 0;
    }
  };
  const feed = (chunk: Float32Array) => {
    if (phase !== "live" || !resampler) return;
    const s16k = resampler.push(chunk);
    for (let i = 0; i < s16k.length; i++) {
      const v = Math.max(-1, Math.min(1, s16k[i] ?? 0));
      pcm[fill++] = v < 0 ? v * 0x8000 : v * 0x7fff;
      if (fill === FRAME_SAMPLES) flush(false);
    }
  };

  const start = async () => {
    if (phase !== "idle") return;
    const id = ++press;
    phase = "starting";
    error = "";
    framesSent = 0;
    render();
    if (!conn.open) {
      phase = "idle";
      error = "Not connected: microphone not sent";
      render();
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      phase = "idle";
      error = "Microphone needs HTTPS or localhost";
      render();
      return;
    }
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    } catch (err) {
      if (id === press) {
        phase = "idle";
        error = `Microphone unavailable: ${(err as Error).name || err}`;
        render();
      }
      return;
    }
    if (id !== press || phase !== "starting") {
      stream.getTracks().forEach((t) => t.stop());
      return;
    }
    try {
      ctx ??= new AudioContext();
      await ctx.resume();
      const source = ctx.createMediaStreamSource(stream);
      const sink = ctx.createGain();
      sink.gain.value = 0;
      sink.connect(ctx.destination);
      let node: AudioNode;
      if (ctx.audioWorklet && typeof AudioWorkletNode !== "undefined") {
        workletReady ??= ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([WORKLET], { type: "text/javascript" })));
        await workletReady;
        const w = new AudioWorkletNode(ctx, "skypaw-capture", { numberOfInputs: 1, numberOfOutputs: 1, channelCount: 1, channelCountMode: "explicit" });
        w.port.onmessage = (e: MessageEvent<Float32Array>) => feed(e.data);
        node = w;
      } else {
        const sp = ctx.createScriptProcessor(4096, 1, 1);
        sp.onaudioprocess = (e) => feed(new Float32Array(e.inputBuffer.getChannelData(0)));
        node = sp;
      }
      source.connect(node);
      node.connect(sink);
      teardown = () => {
        source.disconnect();
        node.disconnect();
        sink.disconnect();
        if (node instanceof AudioWorkletNode) node.port.onmessage = null;
        stream.getTracks().forEach((t) => t.stop());
      };
      if (id !== press || phase !== "starting") {
        teardown();
        teardown = null;
        return;
      }
      resampler = new Resampler(ctx.sampleRate / TARGET_RATE);
      pcm = new Int16Array(FRAME_SAMPLES);
      fill = 0;
      phase = "live";
      conn.send({ type: "mic", state: "start", lang: micLang.value });
    } catch (err) {
      stream.getTracks().forEach((t) => t.stop());
      phase = "idle";
      error = `Audio capture failed: ${(err as Error).message || err}`;
    }
    render();
  };

  const stop = () => {
    if (phase === "idle") return;
    const wasLive = phase === "live";
    phase = "idle";
    press++;
    if (wasLive) {
      flush(true);
      conn.send({ type: "mic", state: "stop", lang: micLang.value });
    }
    teardown?.();
    teardown = null;
    resampler = null;
    render();
  };

  ptt.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    ptt.setPointerCapture(e.pointerId);
    void start();
  });
  ptt.addEventListener("pointerup", stop);
  ptt.addEventListener("pointercancel", stop);
  ptt.addEventListener("lostpointercapture", stop);
  ptt.addEventListener("keydown", (e) => {
    if ((e.key === " " || e.key === "Enter") && !e.repeat) {
      e.preventDefault();
      void start();
    }
  });
  ptt.addEventListener("keyup", (e) => {
    if (e.key === " " || e.key === "Enter") {
      e.preventDefault();
      stop();
    }
  });
  window.addEventListener("blur", stop);

  let lastLine = "";
  let lastSub = "";
  function render() {
    const tr = store.snap?.transcript;
    const live = phase !== "idle";
    ptt.classList.toggle("live", live);
    ptt.setAttribute("aria-pressed", String(live));
    let l: string;
    if (tr?.active || live) l = `<span class="partial">${esc(tr?.active && tr.partial ? tr.partial : "…")}</span>`;
    else if (tr?.final) l = `${esc(tr.final)} <span class="dim mono" style="font-size:11px">${esc(tr.lang ?? "")}</span>`;
    else l = `<span class="dim">Hold to talk · ${MODELS.sttRealtime} · ${micLang.value === "auto" ? "auto-detect" : esc(micLang.value)}</span>`;
    let s: string;
    if (error) s = `<b class="bad">${esc(error)}</b>`;
    else if (phase === "live") s = `<b>Mic</b> streaming 16 kHz · ${framesSent} × 100 ms`;
    else if (phase === "starting") s = "<b>Mic</b> opening…";
    else {
      const llm = store.snap?.log.filter((e) => e.src === "LLM").slice(-1)[0];
      s = llm ? `<b>${MODELS.llm}</b> ${esc(logText(llm, "en-IN"))}` : `Planner: ${MODELS.llm} · —`;
    }
    if (l !== lastLine) {
      line.innerHTML = l;
      lastLine = l;
    }
    if (s !== lastSub) {
      sub.innerHTML = s;
      lastSub = s;
    }
  }
  const ticker = window.setInterval(() => {
    if (phase === "live") render();
  }, 250);

  /** Shows a short message (e.g. a backend error event) on the strip's second line for 6 s. */
  let noticeTimer = 0;
  const notice = (msg: string) => {
    error = msg;
    render();
    window.clearTimeout(noticeTimer);
    noticeTimer = window.setTimeout(() => {
      if (error === msg) error = "";
      render();
    }, 6000);
  };

  return { render, stop, notice, close: () => more.classList.remove("on"), dispose: () => window.clearInterval(ticker), stats: () => ({ phase, framesSent }) };
}
