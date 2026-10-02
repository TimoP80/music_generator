"""timbor.samples.selector — musical sample selection with explainable scoring.

sample_score = genre_match + category_match + bpm_match + key_match +
               energy_match + spectral_match + arrangement_role +
               section_energy + motif_compatibility (+ user_preference)
"""
from __future__ import annotations

import numpy as np

from .metadata import SampleMetadata
from .cache import SampleIndex

# genre pack -> sample category preferences (spec §12)
GENRE_PREFS: dict[str, dict] = {
    "gabber": dict(categories={"breakbeat": 0.9, "kick": 0.6, "snare": 0.6,
                               "hoover": 1.0, "stab": 0.9, "vocal": 0.7,
                               "fx": 0.7, "impact": 0.8, "drum_loop": 0.7,
                               "texture": 0.5, "perc": 0.5},
                   bpm=(150, 220), energy=(0.6, 1.0),
                   tags={"gabber": 0.9, "hardcore": 0.8, "hoover": 0.6, "dark": 0.5,
                         "vintage": 0.4}),
    "frenchcore": dict(categories={"breakbeat": 0.8, "kick": 0.7, "vocal": 0.7,
                                   "fx": 0.7, "impact": 0.8, "drum_loop": 0.6,
                                   "hoover": 0.9, "stab": 0.8},
                       bpm=(170, 220), energy=(0.7, 1.0),
                       tags={"hardcore": 0.9, "gabber": 0.7, "dark": 0.5}),
    "uk_hardcore": dict(categories={"breakbeat": 0.7, "vocal": 1.0, "stab": 0.9,
                                    "chord": 0.8, "piano": 0.8, "lead": 0.7,
                                    "fx": 0.6, "impact": 0.7},
                        bpm=(160, 180), energy=(0.6, 1.0),
                        tags={"euphoric": 0.8, "hardcore": 0.7}),
    "freeform": dict(categories={"lead": 0.9, "acid": 0.9, "breakbeat": 0.8,
                                 "pad": 0.8, "vocal": 0.7, "fx": 0.8},
                     bpm=(165, 185), energy=(0.5, 1.0),
                     tags={"hardcore": 0.7, "trance": 0.6, "dark": 0.4}),
    "jcore": dict(categories={"vocal": 0.9, "lead": 0.9, "chord": 0.8, "piano": 0.7,
                              "fx": 0.7, "stab": 0.8},
                  bpm=(170, 210), energy=(0.6, 1.0),
                  tags={"euphoric": 0.8, "vintage": 0.3}),
    "dnb": dict(categories={"breakbeat": 1.0, "bass": 0.9, "pad": 0.8, "perc": 0.7,
                            "texture": 0.7, "vocal": 0.7, "fx": 0.6},
                bpm=(160, 180), energy=(0.4, 0.95),
                tags={"dnb": 0.9, "dark": 0.6}),
    "jungle": dict(categories={"breakbeat": 1.0, "perc": 0.8, "bass": 0.8,
                               "texture": 0.8, "vocal": 0.8, "pad": 0.7,
                               "ambience": 0.6, "fx": 0.6},
                   bpm=(150, 175), energy=(0.4, 0.95),
                   tags={"jungle": 1.0, "amen": 0.8, "vintage": 0.6, "dark": 0.5}),
    "hard_house": dict(categories={"kick": 0.8, "vocal": 0.9, "stab": 0.9,
                                   "piano": 0.8, "chord": 0.7, "hoover": 0.8,
                                   "perc": 0.7, "impact": 0.7},
                       bpm=(140, 160), energy=(0.5, 1.0),
                       tags={"house": 0.9, "hoover": 0.5, "vintage": 0.4}),
    "trance": dict(categories={"lead": 1.0, "pad": 0.9, "vocal": 0.9, "chord": 0.8,
                               "piano": 0.7, "riser": 0.9, "impact": 0.8,
                               "fx": 0.7, "texture": 0.6},
                   bpm=(130, 145), energy=(0.3, 0.9),
                   tags={"trance": 1.0, "euphoric": 0.8}),
    "acid_trance": dict(categories={"acid": 1.0, "lead": 0.8, "pad": 0.8,
                                    "riser": 0.9, "fx": 0.7},
                        bpm=(135, 150), energy=(0.4, 0.95),
                        tags={"trance": 0.8, "dark": 0.4}),
    "big_beat": dict(categories={"breakbeat": 1.0, "bass": 0.8, "vocal": 0.7,
                                 "stab": 0.8, "impact": 0.8, "fx": 0.7},
                     bpm=(110, 135), energy=(0.5, 1.0),
                     tags={"dark": 0.4, "vintage": 0.5}),
    "rave": dict(categories={"breakbeat": 0.9, "hoover": 1.0, "stab": 0.9,
                             "piano": 0.9, "vocal": 0.7, "fx": 0.7, "impact": 0.8,
                             "texture": 0.6},
                 bpm=(145, 170), energy=(0.5, 1.0),
                 tags={"hardcore": 0.8, "vintage": 0.7, "euphoric": 0.6, "hoover": 0.6}),
}

# which roles exist in the arrangement and what they want
ROLE_SPECS: dict[str, dict] = {
    "main_break": dict(categories={"breakbeat", "drum_loop"},
                       dur=(1.5, 16), loop=True, weight=1.3),
    "alt_break": dict(categories={"breakbeat", "drum_loop"},
                      dur=(1.5, 16), loop=True, weight=1.1),
    "perc_loop": dict(categories={"perc", "drum_loop"}, dur=(1.0, 8),
                      loop=True, weight=0.8),
    "vocal_hit": dict(categories={"vocal"}, dur=(0.15, 2.0), loop=False, weight=0.9),
    "vocal_texture": dict(categories={"vocal"}, dur=(2.0, 16), loop=True, weight=0.8),
    "melodic_loop": dict(categories={"lead", "chord", "piano", "acid"},
                         dur=(2.0, 16), loop=True, weight=0.8),
    "stab_hit": dict(categories={"stab", "chord", "hoover"}, dur=(0.1, 1.2),
                     loop=False, weight=0.8),
    "bass_sample": dict(categories={"bass", "acid"}, dur=(0.5, 8), loop=False,
                        weight=0.7),
    "riser": dict(categories={"riser", "fx"}, dur=(1.0, 8), loop=False, weight=0.9),
    "impact": dict(categories={"impact", "fx"}, dur=(0.3, 4), loop=False, weight=0.8),
    "texture": dict(categories={"texture", "ambience", "pad"}, dur=(2.0, 20),
                    loop=True, weight=0.6),
    "pad": dict(categories={"pad", "texture", "chord"}, dur=(2.0, 20), loop=True,
                weight=0.7),
}


def _clamp01(x: float) -> float:
    return float(np.clip(x, 0.0, 1.0))


def score_sample(m: SampleMetadata, song, role: str, section_energy: float,
                 rng: np.random.Generator) -> tuple[float, dict]:
    """Return (score, explain-dict). Higher is better."""
    pack_pref = GENRE_PREFS[song.plan.genre]
    spec = ROLE_SPECS[role]
    why: dict = {}

    # 1) genre match: tag overlap + genre bpm window
    gtags = set(m.genre_tags)
    wanted = set(pack_pref.get("tags", {}))
    tag_overlap = len(gtags & wanted) / max(1, len(wanted))
    gscore = tag_overlap
    if m.subcategory and m.subcategory in pack_pref.get("tags", {}):
        gscore += 0.3
    bpm = m.bpm or 0
    in_window = pack_pref["bpm"][0] <= bpm <= pack_pref["bpm"][1] if bpm else False
    gscore += 0.2 if in_window else 0.0
    why["genre"] = round(_clamp01(gscore), 2)

    # 2) category match
    cats = spec["categories"]
    cmax = max((pack_pref["categories"].get(c, 0.2) for c in cats), default=0.2)
    cat_exact = m.category in cats
    cscore = cmax if cat_exact else 0.15 * cmax
    why["category"] = round(_clamp01(cscore), 2)

    # 3) bpm match: prefer samples near the song bpm (including half/double)
    bscore = 0.4  # unknown bpm: neutral
    if bpm:
        ratios = []
        for r in (1.0, 0.5, 2.0):
            if r * bpm > 0:
                # use ERB-scale distance for more natural BPM matching
                errb = 24.7 * (1.0 + 0.00437 * bpm)
                ratios.append(abs(np.log10(bpm * r / song.bpm) * errb / 1000.0))
        dist = min(ratios)
        bscore = _clamp01(1.0 - dist)
    why["bpm"] = round(bscore, 2)

    # 4) key match: only meaningful for tonal material (spec: respect confidence)
    kscore = 0.5
    if m.key and m.key_confidence > 0.4:
        from timbor.theory import NOTE_INDEX
        song_root = NOTE_INDEX[song.key]
        sam_root = m.key_root
        if sam_root is not None:
            iv = (song_root - sam_root) % 12
            # root match = 1.0, fifth = 0.8, third = 0.7, tritone = 0.1
            kscore = {0: 1.0, 7: 0.8, 5: 0.75, 4: 0.7, 3: 0.7, 9: 0.6, 2: 0.5,
                      10: 0.5, 1: 0.3, 11: 0.3, 6: 0.15, 8: 0.4}.get(iv, 0.4)
            if m.is_minor and song.scale in ("major", "major-ish"):
                kscore -= 0.1
    why["key"] = round(_clamp01(kscore), 2)

    # 5) energy match
    target = (pack_pref["energy"][0] + (pack_pref["energy"][1] - pack_pref["energy"][0])
              * section_energy)
    escore = _clamp01(1.0 - abs(np.log10(max(m.energy, 1e-6) / max(target, 1e-6))) * 0.5)
    why["energy"] = round(escore, 2)

    # 6) spectral match: bright material for euphoric moods, dark for dark
    dark = song.plan.mood == "dark"
    cent_norm = _clamp01(m.spectral_centroid / 6000.0)
    sscore = _clamp01(1.0 - abs(cent_norm - (0.25 if dark else 0.55)) * 2.0)
    why["spectral"] = round(sscore, 2)

    # 7) arrangement role: duration fit
    dlo, dhi = spec["dur"]
    dscore = _clamp01(1.0 - abs(np.log2(max(m.duration, 0.05) / ((dlo + dhi) / 2))) * 0.5)
    why["duration"] = round(dscore, 2)

    # 8) section energy fit: loops in low-energy sections want lower energy
    fit = 1.0 - abs(np.log10(max(m.energy, 1e-6) / max(section_energy, 1e-6))) * 0.5
    why["section_fit"] = round(_clamp01(fit), 2)

    # 9) classification quality
    why["confidence"] = round(_clamp01(m.classification_confidence), 2)

    total = (1.0 * why["genre"] + 1.2 * why["category"] + 1.0 * why["bpm"] +
             0.8 * why["key"] + 0.9 * why["energy"] + 0.6 * why["spectral"] +
             0.7 * why["duration"] + 0.6 * why["section_fit"] +
             0.5 * why["confidence"]) * spec.get("weight", 1.0)
    return float(total), why


def select_for_song(song, index: SampleIndex, seed: int, mode: str = "balanced",
                    role_weights: dict | None = None) -> list[dict]:
    """Assign samples to SongPlan roles. Deterministic per seed.

    Returns a list of dicts: {role, section, sample, why, score}
    """
    rng = np.random.default_rng(seed)
    plan_roles = _plan_sample_roles(song, mode)
    assignments: list[dict] = []
    used: set[str] = set()
    mode_mult = {"off": 0.0, "subtle": 0.6, "balanced": 1.0, "heavy": 1.4}.get(mode, 1.0)

    for role, section_names, prob in plan_roles:
        if rng.random() > prob * mode_mult:
            continue
        spec = ROLE_SPECS[role]
        cands: list[tuple[float, SampleMetadata, dict]] = []
        for cat in spec["categories"]:
            for m in index.by_category(cat, analyzed_only=True, limit=120):
                if m.path in used:
                    continue
                sec_energy = 0.7
                if section_names:
                    secs = [s for s in song.sections if s.name in section_names]
                    sec_energy = float(np.mean([s.energy for s in secs])) if secs else 0.7
                sc, why = score_sample(m, song, role, sec_energy, rng)
                cands.append((sc, m, why))
        if not cands:
            continue
        cands.sort(key=lambda t: (-t[0], t[1].path))
        # pick from the top slice (deterministic jitter keeps variety)
        top = cands[: max(3, len(cands) // 8)]
        pick_i = int(rng.integers(0, len(top)))
        score, m, why = top[pick_i]
        used.add(m.path)
        assignments.append(dict(role=role, section=section_names or ["all"],
                                sample=m, why=why, score=round(score, 2)))
    return assignments


def _plan_sample_roles(song, mode: str) -> list[tuple[str, list[str] | None, float]]:
    """Which roles the SongPlan wants, for which sections, with what probability."""
    g = song.plan.genre
    roles: list[tuple[str, list[str] | None, float]] = []
    # every genre wants a main break if it's break-driven; 4x4 genres still use
    # breaks as top layers in drops
    break_driven = song.plan.pack["drums"] in ("jungle", "dnb", "bigbeat")
    if break_driven:
        roles.append(("main_break", ["drop", "drop2", "groove", "dev"], 0.95))
        roles.append(("alt_break", ["variation", "theme"], 0.8))
    else:
        roles.append(("main_break", ["drop", "drop2"], 0.7 if mode != "subtle" else 0.4))
    roles.append(("perc_loop", ["groove", "theme", "dev"], 0.7))
    roles.append(("vocal_hit", ["drop", "drop2", "variation"], 0.75))
    roles.append(("vocal_texture", ["break", "break2", "intro"], 0.8))
    roles.append(("melodic_loop", ["theme", "dev", "drop"], 0.75))
    roles.append(("stab_hit", ["drop", "drop2", "variation", "groove"], 0.7))
    roles.append(("riser", ["build", "build2"], 0.85))
    roles.append(("impact", ["drop", "drop2"], 0.8))
    roles.append(("texture", ["intro", "break", "break2", "outro"], 0.8))
    roles.append(("pad", ["break", "break2", "theme"], 0.7))
    if mode == "heavy":
        roles.append(("bass_sample", ["drop", "drop2"], 0.6))
    return roles


def explain_selection(assignments: list[dict], song=None) -> str:
    out = ["SAMPLE SELECTION REPORT", ""]
    if not assignments:
        out.append("(no samples selected)")
        return "\n".join(out)
    for a in assignments:
        m = a["sample"]
        out.append(f"role={a['role']}  sections={','.join(a['section'])}  "
                   f"score={a['score']}")
        out.append(f"  selected: {m.filename}")
        out.append("  why:")
        out.append(f"    category = {m.category}  "
                   f"(confidence {m.classification_confidence:.2f})")
        out.append(f"    genre match = {a['why'].get('genre', 0):.2f}")
        out.append(f"    bpm = {m.bpm if m.bpm is not None else '?'} "
                   f"(conf {m.bpm_confidence:.2f})  song bpm = "
                   f"{song.bpm if song else '?'}")
        out.append(f"    key = {m.key or '?'} (conf {m.key_confidence:.2f})  "
                   f"song key = {song.key + ' ' + song.scale if song else '?'}")
        out.append(f"    energy = {m.energy:.2f}  spectral match = "
                   f"{a['why'].get('spectral', 0):.2f}")
        out.append(f"    duration = {m.duration:.2f}s  loop = {m.is_loop}")
        out.append("")
    return "\n".join(out)
