import sys, json, time, base64, httpx
from common import *
def call(name, method, path, **kw):
    t0 = time.perf_counter()
    with httpx.Client(verify=SSL, timeout=60) as c:
        r = c.request(method, BASE + path, headers=HDR, **kw)
    ms = round((time.perf_counter() - t0) * 1000)
    try: body = r.json()
    except Exception: body = r.text[:1000]
    return {"name": name, "status": r.status_code, "latency_ms": ms, "body": body,
            "req_id_hdr": r.headers.get("x-request-id")}
which = sys.argv[1]
results = []
if which == "stt":
    wav = wav_bytes(tone_pcm(2.0))
    results.append(call("stt_rest", "POST", "/speech-to-text",
        files={"file": ("tone.wav", wav, "audio/wav")},
        data={"model": "saaras:v4", "mode": "codemix", "language_code": "hi-IN",
              "keyterms": json.dumps(["V-01", "Sector 7"])}))
elif which == "llm":
    schema = {"type":"object","properties":{"intent":{"type":"string","enum":["aerial_survey","navigate_go2","overwatch","speak","evacuate","broadcast"]},"target":{"type":["string","null"]},"language":{"type":"string"}},"required":["intent","target","language"],"additionalProperties":False}
    body = {"model": "sarvam-105b", "max_tokens": 512, "reasoning_effort": "low",
            "response_format": {"type":"json_schema","json_schema":{"name":"mission_plan","strict":True,"schema":schema}},
            "messages": [{"role":"system","content":"You convert rescue-lead commands into JSON mission plans."},
                         {"role":"user","content":"dog को V-01 के पास भेजो"}]}
    if len(sys.argv) > 2 and sys.argv[2] == "omit":
        body.pop("reasoning_effort"); which = "llm_omit"
    results.append(call(which, "POST", "/v1/chat/completions", json=body))
elif which == "tts":
    for lang, spk, text in [("mr-IN","rohan","मदत येत आहे. शांत राहा."),("hi-IN","priya","मदद आ रही है।"),
                            ("ta-IN","kavitha","உதவி வருகிறது."),("en-IN","shubh","Help is on the way.")]:
        r = call(f"tts_{lang}", "POST", "/text-to-speech", json={"text": text, "language_code": lang, "speaker": spk,
                 "model": "bulbul:v3", "speech_sample_rate": 22050, "output_audio_codec": "wav"})
        r["chars"] = len(text)
        if r["status"] == 200 and isinstance(r["body"], dict) and r["body"].get("audios"):
            raw = base64.b64decode("".join(r["body"]["audios"]))
            open(f"{OUT}/tts_{lang}.wav", "wb").write(raw)
            r["wav_bytes"] = len(raw)
            try:
                with wave.open(io.BytesIO(raw)) as w: r["duration_s"] = round(w.getnframes()/w.getframerate(), 3); r["sr"] = w.getframerate()
            except Exception as e: r["wav_parse_error"] = str(e)
            r["body"] = {k: v for k, v in r["body"].items() if k != "audios"}
        results.append(r)
        if r["status"] == 402: break   # account has no credits; do not hammer
elif which == "tr":
    for tgt in ["mr-IN", "ta-IN"]:
        inp = "Go2 arrived at V-01. Radar 64 bpm."
        r = call(f"translate_{tgt}", "POST", "/translate", json={"input": inp, "source_language_code": "en-IN",
                 "target_language_code": tgt, "model": "sarvam-translate:v1"})
        r["chars"] = len(inp); results.append(r)
        if r["status"] == 402: break
save(which, results)
