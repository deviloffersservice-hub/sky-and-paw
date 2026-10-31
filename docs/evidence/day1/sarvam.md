# Day-one Sarvam live checks (RESEARCH.md §5 item 5)

**Run on:** 2026-10-31 · **By:** voice-engineer agent · **Key:** `SARVAM_API_KEY` from the environment (never printed or stored)
**Toolchain:** Python 3.13, `sarvamai==0.1.41`, `httpx==0.28.1`, `websockets==17.4`, through the environment's HTTPS proxy.
**Raw outputs:** session scratchpad only (`day1sv/out/*.json`); not committed.

## Headline

**The Sarvam account has no credits.** The key is valid: every call got past auth (a bad key returns 403), but every billable call was refused with **402 `insufficient_quota_error` "No credits available."** On the realtime socket the refusal is an `error` event `quota_exceeded` (`status_code: 402`, `is_fatal: true`), then close **1003** "Credits exhausted". No transcript, plan, audio or translation came back, so **none of the functional checks could be completed.**

**Action needed (human):** top up or claim credits for this key at `indus.sarvam.ai`. The RESEARCH.md §2.6 claim of "₹100 free credits per verified signup" does not hold for this key: either the credits were never granted, they have been used up, or the signup is not verified. After the top-up, rerun these checks; the scripts are written and run as they are.

## Results

| # | Check | Status | Latency | Notes |
|---|---|---|---|---|
| 1 | Realtime STT WS handshake | **101** Switching Protocols | 1040 ms to 101; error event at 1130 ms | First server event was **not** `session.begin`. It was `{"event":"error","code":"quota_exceeded","message":"Credits exhausted. ...","is_fatal":true,"status_code":402}`, then close **1003**. The server closed the socket before the first audio chunk, so no `audio_input` or `end` was sent. **No 4000 "account not enabled" was seen**, but the quota check may run before the enablement check, so enablement is still not proven. |
| 2 | REST STT `saaras:v4` codemix + keyterms (2 s WAV) | **402** insufficient_quota_error | 1527 ms | No transcript, so the response JSON shape could not be checked. |
| 3 | Plan LLM `sarvam-105b`, strict json_schema, `reasoning_effort="low"` | **402** insufficient_quota_error | 1027 ms | Got past request validation, so `low`, strict `json_schema`, `max_tokens=512` and the model ID are accepted (see probe). No content or usage came back. No "omit reasoning_effort" retry was needed, because the 402 was not a parameter rejection. |
| 3b | Probe: `reasoning_effort="bogus_value"` (zero-cost) | **400** invalid_request_error | 970 ms | `"body.reasoning_effort : Input should be 'low', 'medium' or 'high'"`. **Validation runs before the quota check**, which is why the 402s above show the request bodies are well formed. |
| 4 | TTS `bulbul:v3` mr-IN / rohan | **402** insufficient_quota_error | 1023 ms | Stopped after the first language to avoid repeated refusals. hi/ta/en were not sent. No WAVs. |
| 4b | SDK `client.text_to_speech.convert(text=, language_code="hi-IN", model="bulbul:v3", speaker="priya")` | **402** (ApiError) | 1008 ms | **`language_code` is accepted by the SDK**: no client-side TypeError, the request was sent, and the server returned only the quota error. |
| 5 | Translate `sarvam-translate:v1` en-IN to mr-IN | **402** insufficient_quota_error | 1038 ms | ta-IN repeat not sent (same refusal expected). |
| 6 | Rupees spent | **₹0.00** | n/a | Every billable call was refused; no audio was streamed, nothing was synthesised or translated, and no tokens were generated. The would-be cost of the full planned set was about ₹0.31 (STT 3 s ≈ ₹0.025; LLM about 120 in + 512 out tokens ≈ ₹0.033; TTS 83 chars ≈ ₹0.25; translate 68 chars ≈ ₹0.14). Check the dashboard to confirm that refused calls are not billed. |

All latencies are wall-clock round trips from this cloud environment through the proxy. They are error-path timings (about 1.0 s, about 1.5 s with a 64 KB upload), not service latencies. **None of the latencies the brief asks for (STT partial/final, plan, TTS first byte, translate) could be measured.**

## Discrepancies with docs/RESEARCH.md

1. **§2.6 credits:** "₹100 free credits per verified signup" did not apply to this key: the balance is zero. This needs to be fixed before M1. The free credits may need dashboard verification, or have already been used.
2. **§2.2 realtime enablement:** the handshake **does return 101** with this key, and no 4000 close was seen. That rules out "account not enabled" being *the first* gate. Whether streaming is enabled after the quota check is still UNVERIFIED. New fact: **out-of-credits on the socket is an `error` event (`code: quota_exceeded`, `status_code: 402`, `is_fatal: true`) followed by close 1003**, arriving about 90 ms after the 101 and *before* `session.begin`. The client must handle `error` as a possible first event.
3. **§2.3 `reasoning_effort`:** the server accepts only **`low`, `medium`, `high`** (plus omitted or null, presumably meaning default). The string `"none"` is **not** in the server's allowed set, and neither is `"max"`. SDK 0.1.41's `ReasoningEffort` literal is `"none" | "low" | "high" | "max"`: it **lacks `medium` and includes two values the server rejects**. RESEARCH.md's correction ("use `low`/`medium`/`high` or `null`") matches the server. **SPEC.md / the voice-engineer brief's `reasoning_effort=none` is wrong and will 400.** Whether `null` actually *disables* reasoning, rather than falling back to the default (on), is UNVERIFIED until credits are available.
4. **§2.4 TTS parameter name:** the SDK `text_to_speech.convert` signature takes **`language_code`** (required), and the call was accepted client-side. This matches RESEARCH.md. The REST body with `language_code`, `speaker`, `model`, `speech_sample_rate`, `output_audio_codec` passed server validation (402, not 422).
5. **§2.1 error codes:** confirmed `402 insufficient_quota_error` on REST; message "No credits available.". The `x-request-id` header matches `error.request_id` (format `YYYYMMDD_<uuid>`).

## Not verified (blocked by credits)
- `session.begin` payload, partial/final timing, `session.end` billed duration on the realtime socket.
- REST STT response shape and behaviour of `keyterms` on v4.
- Strict json_schema output from `sarvam-105b`, `usage` token counts, reasoning-token overhead at `low`.
- TTS WAV length and duration for mr/hi/ta/en with rohan/priya/kavitha/shubh; speaker/language compatibility.
- Translate output quality and the handling of "V-01", "Go2" and "64 bpm" tokens.

## Rerun after reported top-up (2026-10-31, later the same day)
The coordinator reported that credits had been added. I reran check 1 (realtime socket) once with no changes: **still refused.** Handshake **101** at 1011 ms, then the same `error` / `quota_exceeded` / `status_code: 402` event at 1031 ms, then close **1003** "Credits exhausted". As instructed, I stopped after this first refusal; checks 2–6 were not rerun. Spend: **₹0.00**. Possible causes: the credits went to a different key or organisation than the `SARVAM_API_KEY` this session holds, the top-up hasn't taken effect yet, or this session's environment still has an older key.

## Second rerun after reported key replacement (2026-10-31)
The coordinator reported that `SARVAM_API_KEY` had been replaced with a key from another account. I probed with check 5 (translate en-IN to mr-IN) in a fresh process: **402 `insufficient_quota_error` "No credits available."**, 1011 ms (request_id `20261031_98895df5-…`). I stopped there as instructed, so checks 1–4 and 6 were not rerun. Spend: **₹0.00**.

Key identity: no hash was saved in the earlier runs, so **whether the key value changed is unknown**. From now on: SHA-256 prefix of the current value is `b209d61be475`. The variable is not set in any shell profile file (`~/.bashrc`, `~/.profile`, `/etc/environment`, `/etc/profile*`). It is inherited from the session's process environment, which is fixed when the container starts. A key replaced in the environment's secrets after this session started most likely does **not** reach this session; a new session is needed to pick it up.
