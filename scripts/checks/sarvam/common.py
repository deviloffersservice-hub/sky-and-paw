import os, ssl, json, math, struct, io, wave, base64, time
KEY = os.environ["SARVAM_API_KEY"]
CA = "/root/.ccr/ca-bundle.crt"
SSL = ssl.create_default_context(cafile=CA) if os.path.exists(CA) else ssl.create_default_context()
BASE = "https://api.sarvam.ai"
HDR = {"api-subscription-key": KEY}
OUT = os.path.join(os.path.dirname(__file__), "out")
def red(s):
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False, default=str)
    return s.replace(KEY, "***REDACTED***")
def save(name, obj):
    with open(os.path.join(OUT, name + ".json"), "w") as f:
        f.write(red(json.dumps(obj, ensure_ascii=False, indent=2, default=str)))
    print(red(json.dumps(obj, ensure_ascii=False, indent=2, default=str))[:4000])
def tone_pcm(seconds, sr=16000, f=440.0, amp=0.3):
    n = int(seconds * sr)
    return b"".join(struct.pack("<h", int(amp * 32767 * math.sin(2 * math.pi * f * i / sr))) for i in range(n))
def wav_bytes(pcm, sr=16000):
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(pcm)
    return b.getvalue()
