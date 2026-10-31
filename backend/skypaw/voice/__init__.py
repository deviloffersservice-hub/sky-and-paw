"""Voice and language: Sarvam STT, planning LLM, Bulbul TTS and translation (milestone M5).

Entry points: ``service.SarvamVoice`` / ``service.FakeVoice`` (the ``ports.Voice`` port),
``tts.router`` (``GET /tts/{sha}.wav``), ``client.SarvamClient`` (the only Sarvam client).
"""
