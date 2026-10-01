"""timbor.samples.classifier — filename hints + features -> category/genre tags.

Filename information is useful but never authoritative: audio features can
override or confirm the hints, and confidence reflects agreement.
"""
from __future__ import annotations

import os
import re

from .metadata import SampleMetadata

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "kick": ["kick", "bd", "bassdrum", "bass drum", "bd_"],
    "snare": ["snare", "snr", "sd_"],
    "clap": ["clap", "handclap"],
    "hat": ["hat", "hihat", "hi-hat", "hh_", "open hat", "closed hat"],
    "ride": ["ride", "crash", "cymbal"],
    "perc": ["perc", "shaker", "conga", "bongo", "rimshot", "rim", "triangle",
             "cowbell", "clave", "tambourine"],
    "tom": ["tom"],
    "breakbeat": ["break", "amen", "think", "apache", "funky drummer", "hot pants",
                  "breakbeat", "cut", "chop"],
    "drum_loop": ["drumloop", "drum loop", "beat", "groove", "loop", "rhythm"],
    "bass": ["bass", "basses", "sub", "808 bass", "reese", "rumble"],
    "acid": ["acid", "303", "tb", "squelch"],
    "hoover": ["hoover", "mentasm", "hoover"], 
    "lead": ["lead", "saw", "supersaw", "melody", "arp", "arpeggio", "synth"],
    "pad": ["pad", "atmosphere", "atmos", "soundscape", "drone"],
    "stab": ["stab", "hit", "chord stab", "mentasm"],
    "chord": ["chord", "chords", "progression"],
    "piano": ["piano", "ep", "keys", "rhodes", "e-piano"],
    "vocal": ["vocal", "vox", "voice", "chant", "shout", "acapella", "acapella", "rap",
              "sing"],
    "fx": ["fx", "effect", "sweep", "noise", "glitch", "zip"],
    "riser": ["riser", "uplifter", "uplift", "raise"],
    "impact": ["impact", "boom", "hit", "crash", "explosion", "smash"],
    "texture": ["texture", "grain", "granular", "field", "vinyl", "crackle"],
    "ambience": ["ambience", "ambient", "atmos", "rain", "crowd", "wind"],
}


def _filename_tags(path: str) -> list[str]:
    rel = path.replace("\\", "/").lower()
    stem = os.path.splitext(os.path.basename(rel))[0]
    words = set(re.split(r"[^a-z0-9]+", rel))
    tags: list[str] = set()
    for cat, kws in CATEGORY_KEYWORDS.items():
        for kw in kws:
            for w in words:
                if w == kw or (len(kw) > 3 and kw in w):
                    tags.add(cat)
    # tempo/mood/free tags
    extra_kw = {
        "gabber": ["gabber", "gabba", "rotterdam"], "jungle": ["jungle", "amen", "ragga"],
        "hardcore": ["hardcore", "hard", "gabber"], "dnb": ["dnb", "drum", "bass",
                                                            "neuro"],
        "trance": ["trance", "uplifting", "epic"], "house": ["house", "4x4", "tech"],
        "hoover": ["hoover", "mentasm"], "amen": ["amen", "amen break"],
        "dark": ["dark", "evil", "dystopian"], "euphoric": ["euphoric", "uplifting",
                                                            "bright"],
        "vintage": ["909", "808", "707", "vintage", "analog", "lofi", "lo-fi"],
    }
    for t, kws in extra_kw.items():
        if any(k in words or any(k in w for w in words) for k in kws):
            tags.add(t)
    # bpm in filename like "174bpm" or "174 bpm"
    m = re.search(r"(\d{2,3})\s*(?:bpm)?", stem)
    if m and 60 <= int(m.group(1)) <= 220:
        tags.add(f"bpm{m.group(1)}")
    return sorted(tags)


def classify(m: SampleMetadata) -> None:
    """Fill category/subcategory/genre_tags/classification_confidence in place."""
    tags = _filename_tags(m.path)
    m.file_tags = [t for t in tags if not t.startswith("bpm")]
    # explicit bpm hint from filename wins over detection if detection is weak
    for t in tags:
        if t.startswith("bpm") and (m.bpm is None or m.bpm_confidence < 0.3):
            try:
                m.bpm = float(t[3:])
                m.bpm_confidence = 0.45  # filename hint: medium-low confidence
            except ValueError:
                pass

    tags = set(m.file_tags)
    dur = m.duration
    # feature-driven priors
    percussive = m.tonalness < 0.3 and m.transient_density > 3
    tonal_loop = m.tonalness >= 0.4 and dur > 2.0
    noisy = m.zero_crossing_rate > 3.5 and m.tonalness < 0.35

    audio_guess = "unknown"
    if dur < 0.9 and percussive:
        audio_guess = "kick" if (m.sub_energy > 0.4 and m.spectral_centroid < 800) else \
            ("hat" if m.spectral_centroid > 5000 else
             ("snare" if 1200 < m.spectral_centroid < 4000 else "perc"))
    elif m.is_loop and percussive and dur <= 16:
        audio_guess = "breakbeat" if (tags & {"breakbeat"}) or \
            m.transient_density > 5 else "drum_loop"
    elif noisy and dur < 4:
        audio_guess = "fx" if m.spectral_centroid > 2500 else "texture"
    elif m.spectral_centroid > 4000 and m.transient_density > 6 and dur > 4:
        audio_guess = "riser" if tags & {"riser"} else "fx"
    elif tonal_loop:
        if m.sub_energy > 0.35 and m.spectral_centroid < 400:
            audio_guess = "bass"
        elif tags & {"vocal"}:
            audio_guess = "vocal"
        elif m.mid_energy > 0.4 and m.high_energy < 0.1:
            audio_guess = "chord" if dur > 4 else "stab"
        else:
            audio_guess = "lead"
    elif percussive and 0.9 <= dur < 1.5:
        audio_guess = "perc"
    elif dur >= 8 and m.tonalness >= 0.35:
        audio_guess = "ambience" if m.high_energy < 0.05 else "pad"

    # agreement between filename hints and audio -> confidence
    if audio_guess in m.file_tags:
        m.category = audio_guess
        m.classification_confidence = 0.9
    elif m.file_tags:
        # filename category wins over audio guess but confidence reduced
        fn_cat = next((t for t in m.file_tags if t in CATEGORY_KEYWORDS), None)
        m.category = fn_cat or audio_guess
        m.classification_confidence = 0.65 if fn_cat == audio_guess else 0.5
    else:
        m.category = audio_guess
        m.classification_confidence = 0.55 if audio_guess != "unknown" else 0.2

    m.subcategory = ""
    for t in ("amen", "hoover", "vintage", "dark", "euphoric", "gabber", "jungle",
              "hardcore", "dnb", "trance", "house"):
        if t in m.file_tags:
            m.subcategory = t
            break
    m.genre_tags = sorted((set(m.file_tags) &
                           {"gabber", "jungle", "hardcore", "dnb", "trance", "house",
                            "dark", "euphoric", "vintage"}) |
                          ({m.subcategory} if m.subcategory else set()))
