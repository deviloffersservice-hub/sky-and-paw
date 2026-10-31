"""Scripted phrases and sample commands, copied from docs/mockups/ops-v2.html.

Sources: ``SP.PHRASES`` (Go2 speaker, SPEC §6.10), ``SP.EVAC`` (EVERYONE OUT broadcast),
``SP.LEAD_SAMPLES`` and ``SP.TEAM_SAMPLES`` (demo transcripts with their translations).
These translations are hand-checked, so ``lookup()`` serves them without a Sarvam call.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The four team languages (SPEC §6.9, §6.10), in display order.
TEAM_LANGS: tuple[str, ...] = ("en-IN", "hi-IN", "mr-IN", "ta-IN")


def _l4(en: str, hi: str, mr: str, ta: str) -> dict[str, str]:
    return {"en-IN": en, "hi-IN": hi, "mr-IN": mr, "ta-IN": ta}


@dataclass(frozen=True)
class Phrase:
    id: str
    label: str
    text: dict[str, str]


PHRASES: tuple[Phrase, ...] = (
    Phrase(
        "p_help",
        "Help is coming",
        _l4(
            "Help is on the way. Stay calm.",
            "मदद आ रही है। शांत रहें।",
            "मदत येत आहे. शांत राहा.",
            "உதவி வருகிறது. அமைதியாக இருங்கள்.",
        ),
    ),
    Phrase(
        "p_hear",
        "Can you hear me?",
        _l4(
            "Can you hear me? Tap twice on something.",
            "क्या आप मुझे सुन सकते हैं? दो बार थपथपाएँ।",
            "तुम्हाला ऐकू येतंय का? दोनदा टकटक करा.",
            "என் குரல் கேட்கிறதா? இரண்டு முறை தட்டுங்கள்.",
        ),
    ),
    Phrase(
        "p_still",
        "Don't move",
        _l4(
            "Don't move. Cover your mouth and nose.",
            "हिलें नहीं। मुँह और नाक ढक लें।",
            "हलू नका. तोंड आणि नाक झाका.",
            "அசையாதீர்கள். வாய், மூக்கை மூடுங்கள்.",
        ),
    ),
    Phrase(
        "p_medic",
        "Medic is close",
        _l4(
            "A medic is two minutes away. Keep breathing slowly.",
            "मेडिक दो मिनट दूर है। धीरे-धीरे साँस लेते रहें।",
            "मेडिक दोन मिनिटांवर आहे. हळू श्वास घ्या.",
            "மருத்துவர் இரண்டு நிமிடத்தில். மெதுவாக சுவாசியுங்கள்.",
        ),
    ),
)

EVAC: dict[str, str] = _l4(
    "Evacuate now. Follow the route on your phone to west staging.",
    "अभी बाहर निकलें। फ़ोन पर दिखाए रास्ते से पश्चिमी स्टेजिंग जाएँ।",
    "आत्ताच बाहेर पडा. फोनवरील मार्गाने पश्चिम स्टेजिंगला जा.",
    "இப்போதே வெளியேறுங்கள். ஃபோனில் உள்ள வழியில் மேற்கு முகாமுக்கு.",
)


@dataclass(frozen=True)
class Sample:
    text: str
    lang: str
    hint: str  # mockup intent hint: survey | nav | broadcast | speak
    tr: dict[str, str]


LEAD_SAMPLES: tuple[Sample, ...] = (
    Sample(
        "ड्रोन भेजो, पूरी इमारत मैप करो और लोगों को ढूंढो",
        "hi-IN",
        "survey",
        _l4(
            "Send the drone, map the whole building and find people.",
            "ड्रोन भेजो, पूरी इमारत मैप करो और लोगों को ढूंढो",
            "ड्रोन पाठवा, संपूर्ण इमारत मॅप करा आणि लोकांना शोधा.",
            "ட்ரோனை அனுப்பு, முழு கட்டிடத்தையும் வரைபடமாக்கி மக்களைத் தேடு.",
        ),
    ),
    Sample(
        "dog को V-01 के पास भेजो",
        "hi-IN",
        "nav",
        _l4(
            "Send the dog to V-01.",
            "dog को V-01 के पास भेजो",
            "कुत्र्याला V-01 कडे पाठवा.",
            "நாயை V-01 க்கு அனுப்பு.",
        ),
    ),
    Sample(
        "Everyone, V-01 is the priority. Priya goes first.",
        "en-IN",
        "broadcast",
        _l4(
            "Everyone, V-01 is the priority. Priya goes first.",
            "सब लोग, V-01 प्राथमिकता है। प्रिया पहले जाएगी।",
            "सगळ्यांनो, V-01 प्राधान्य आहे. प्रिया आधी जाईल.",
            "எல்லோரும், V-01 முன்னுரிமை. பிரியா முதலில்.",
        ),
    ),
    Sample(
        "Tell them a medic is close, in Marathi",
        "en-IN",
        "speak",
        _l4(
            "Tell them a medic is close, in Marathi.",
            "उन्हें मराठी में बताओ कि मेडिक पास है।",
            "त्यांना मराठीत सांगा की मेडिक जवळ आहे.",
            "மருத்துவர் அருகில் என்று மராத்தியில் சொல்.",
        ),
    ),
)

TEAM_SAMPLES: dict[str, Sample] = {
    "t2": Sample(
        "V-01 जवळ पोहोचले, श्वास आहे पण पाय अडकला आहे",
        "mr-IN",
        "broadcast",
        _l4(
            "Reached V-01, breathing but leg is trapped.",
            "V-01 के पास पहुँची, साँस है पर पैर फँसा है।",
            "V-01 जवळ पोहोचले, श्वास आहे पण पाय अडकला आहे",
            "V-01 ஐ அடைந்தேன், சுவாசம் உள்ளது ஆனால் கால் சிக்கியுள்ளது.",
        ),
    ),
    "t3": Sample(
        "இங்கே இருட்டாக இருக்கிறது, ட்ரோன் லைட் வேண்டும்",
        "ta-IN",
        "broadcast",
        _l4(
            "It's dark here, I need the drone light.",
            "यहाँ अंधेरा है, ड्रोन की लाइट चाहिए।",
            "इथे अंधार आहे, ड्रोनचा लाइट हवा आहे.",
            "இங்கே இருட்டாக இருக்கிறது, ட்ரோன் லைட் வேண்டும்",
        ),
    ),
    "t4": Sample(
        "Slab above V-02 is moving, need a structural check before we cut",
        "en-IN",
        "broadcast",
        _l4(
            "Slab above V-02 is moving, need a structural check before we cut.",
            "V-02 के ऊपर की स्लैब हिल रही है, काटने से पहले जाँच चाहिए।",
            "V-02 वरची स्लॅब हलते आहे, कापण्याआधी तपासणी हवी.",
            "V-02 மேலே உள்ள ஸ்லாப் நகர்கிறது, வெட்டும் முன் சோதனை தேவை.",
        ),
    ),
}


def phrase(phrase_id: str) -> Phrase:
    for p in PHRASES:
        if p.id == phrase_id:
            return p
    raise KeyError(phrase_id)


def _tables() -> list[dict[str, str]]:
    tables = [p.text for p in PHRASES] + [EVAC]
    for s in (*LEAD_SAMPLES, *TEAM_SAMPLES.values()):
        tables.append({**s.tr, s.lang: s.text})
    return tables


def _norm(text: str) -> str:
    return " ".join(text.split()).rstrip(".।").casefold()


_INDEX: dict[tuple[str, str], dict[str, str]] = {
    (lang, _norm(text)): table for table in _tables() for lang, text in table.items()
}


def lookup(text: str, src: str, dst: str) -> str | None:
    """Known (hand-checked) translation of ``text`` from ``src`` to ``dst``, or None."""
    table = _INDEX.get((src, _norm(text)))
    return table.get(dst) if table else None


def all_speakable() -> list[tuple[str, str]]:
    """(text, lang) of every scripted Go2 phrase plus the evacuation line, for TTS prewarm."""
    out: list[tuple[str, str]] = []
    for table in [p.text for p in PHRASES] + [EVAC]:
        out.extend((text, lang) for lang, text in table.items())
    return out
