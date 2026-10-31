import time, httpx
from common import *
from sarvamai import SarvamAI
res = {}
# SDK TTS (httpx client with the proxy CA)
client = SarvamAI(api_subscription_key=KEY, httpx_client=httpx.Client(verify=SSL, timeout=60))
t0 = time.perf_counter()
try:
    r = client.text_to_speech.convert(text="मदद आ रही है।", language_code="hi-IN", model="bulbul:v3", speaker="priya")
    res["sdk_tts"] = {"ok": True, "audios": len(r.audios)}
except TypeError as e:
    res["sdk_tts"] = {"client_side_TypeError": str(e)}
except Exception as e:
    res["sdk_tts"] = {"exc": type(e).__name__, "status": getattr(e, "status_code", None), "body": getattr(e, "body", None)}
res["sdk_tts"]["latency_ms"] = round((time.perf_counter() - t0) * 1000)
# LLM probe: invalid reasoning_effort -> does 422 validation precede 402 quota?
t0 = time.perf_counter()
with httpx.Client(verify=SSL, timeout=60) as c:
    r = c.post(BASE + "/v1/chat/completions", headers=HDR, json={"model": "sarvam-105b", "max_tokens": 16,
        "reasoning_effort": "bogus_value", "messages": [{"role": "user", "content": "hi"}]})
try: b = r.json()
except Exception: b = r.text[:800]
res["llm_invalid_effort_probe"] = {"status": r.status_code, "latency_ms": round((time.perf_counter() - t0) * 1000), "body": b}
save("probe", res)
