import asyncio, json, time, base64, websockets
from common import *
URL = ("wss://api.sarvam.ai/speech-to-text-realtime/ws?language_code=hi-IN&model=saaras:v3-realtime"
       "&stream_type=fast&mode=codemix&encoding=linear16&sample_rate=16000")
async def main():
    res = {"url": URL, "events": []}
    t0 = time.perf_counter()
    try:
        async with websockets.connect(URL, additional_headers=HDR, ssl=SSL, open_timeout=15, max_size=None) as ws:
            res["handshake_status"] = ws.response.status_code
            res["handshake_ms"] = round((time.perf_counter() - t0) * 1000)
            res["resp_headers_subset"] = {k: v for k, v in ws.response.headers.raw_items() if k.lower().startswith(("x-", "sec-websocket-protocol"))}
            async def reader():
                try:
                    async for m in ws:
                        ev = {"t_ms": round((time.perf_counter() - t0) * 1000)}
                        try: ev["msg"] = json.loads(m)
                        except Exception: ev["raw"] = str(m)[:500]
                        res["events"].append(ev)
                except websockets.ConnectionClosed as e:
                    pass
            rt = asyncio.create_task(reader())
            await asyncio.sleep(1.0)  # let session.begin arrive
            pcm = tone_pcm(1.0)
            res["send_start_ms"] = round((time.perf_counter() - t0) * 1000)
            for i in range(0, len(pcm), 3200):
                chunk = pcm[i:i+3200]
                await ws.send(json.dumps({"event": "audio_input", "audio": base64.b64encode(chunk).decode()}))
                await asyncio.sleep(0.1)
            res["chunks_sent"] = len(pcm) // 3200
            res["end_sent_ms"] = round((time.perf_counter() - t0) * 1000)
            await ws.send(json.dumps({"event": "end"}))
            try: await asyncio.wait_for(rt, timeout=15)
            except asyncio.TimeoutError: res["reader_timeout"] = True
            res["close_code"] = ws.close_code; res["close_reason"] = ws.close_reason
    except websockets.InvalidStatus as e:
        r = e.response
        res["handshake_status"] = r.status_code
        res["handshake_body"] = r.body.decode(errors="replace")[:1000] if r.body else None
    except websockets.ConnectionClosed as e:
        res["closed"] = {"code": e.rcvd.code if e.rcvd else None, "reason": e.rcvd.reason if e.rcvd else None}
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
    res["total_ms"] = round((time.perf_counter() - t0) * 1000)
    save("c1_ws", res)
asyncio.run(main())
