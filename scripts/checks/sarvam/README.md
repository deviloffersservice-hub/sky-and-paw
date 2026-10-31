# Sarvam day-one live checks

Runs the six checks from docs/RESEARCH.md §5 item 5 against the real API. Total cost ≈ ₹0.31.
Requires `SARVAM_API_KEY` in the environment (never in a file).

```bash
python -m venv .venv && . .venv/bin/activate
pip install sarvamai==0.1.41 websockets httpx
cd scripts/checks/sarvam
python rest.py tr        # translate probe (cheapest) — stop here if it returns 402
python rest.py stt       # REST STT saaras:v4 codemix + keyterms
python rest.py llm       # sarvam-105b strict json_schema, reasoning_effort=low
python rest.py llm omit  # same, parameter omitted (does omission disable reasoning?)
python rest.py tts       # bulbul:v3 mr/hi/ta/en
python probe.py          # SDK TTS call (language_code)
python c1_ws.py          # realtime STT socket handshake + 1 s tone
```
Outputs land in `out/`. Copy the summary into docs/evidence/day1/sarvam.md.
