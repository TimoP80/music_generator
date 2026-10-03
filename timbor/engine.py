"""
timbor.engine — the producer: genre packs, arrangement, motif rendering, QC.

Flow:
  1. parse request -> Plan (genre/bpm/key/modes)
  2. build SongPlan: progression, motifs, section list w/ energy
  3. render each section into buses (drums/bass/mel/fx)
  4. QC check -> mix-level repairs (buses make repair cheap)
  5. master -> stereo WAV
"""
from __future__ import annotations

import math
import numpy as np

from . import dsp
from . import voices
from .theory import (hz, note_midi, scale_degree_to_midi, build_chord,
                     chord_from_scale_degree, pick_progression,
                     motif_generate, motif_variate, motif_identity,
                     NOTE_NAMES)
from .timeline import Timeline, SectionSpan, TimelineEvent, SampleUse
from .timeline.events import VALID_BUSES

# ----------------------------------------------------------------------------
# genre packs — one dict per genre drives everything downstream
# ----------------------------------------------------------------------------

GENRES: dict[str, dict] = {
    "gabber": dict(
        bpm=(160, 200), kick="gabber", drums="hardcore", bass="rumble",
        bass_style="roll16", lead="hoover", prog_family=("dark_phryg", "anthem", "epic"),
        scale_pref=("harmonic_minor", "phrygian", "natural_minor"),
        melody_density=0.75, energy_bias=1.0, section_bars=8, swing=0.0,
        era="1990-1995", fx_level=0.5,
    ),
    "frenchcore": dict(
        bpm=(180, 220), kick="gabber", drums="hardcore", bass="rumble",
        bass_style="roll16", lead="supersaw", prog_family=("anthem", "trance_roller", "epic"),
        scale_pref=("natural_minor", "harmonic_minor"),
        melody_density=0.8, energy_bias=1.1, section_bars=8, swing=0.0,
        era="modern", fx_level=0.6,
    ),
    "uk_hardcore": dict(
        bpm=(165, 175), kick="four", drums="hardcore", bass="dist_bass",
        bass_style="off8_roll", lead="supersaw", prog_family=("uplifting", "anthem", "anthem_b"),
        scale_pref=("natural_minor", "dorian"), melody_density=0.85,
        energy_bias=1.0, section_bars=8, swing=0.0, era="modern", fx_level=0.6,
    ),
    "freeform": dict(
        bpm=(170, 180), kick="four", drums="dnb", bass="acid",
        bass_style="acid", lead="supersaw", prog_family=("epic", "dramatic", "trance_epic"),
        scale_pref=("harmonic_minor", "melodic_minor", "lydian"),
        melody_density=0.9, energy_bias=0.95, section_bars=8, swing=0.06,
        era="1995-2005", fx_level=0.65,
    ),
    "jcore": dict(
        bpm=(180, 200), kick="four", drums="hardcore", bass="fm_bass",
        bass_style="roll16", lead="square_lead", prog_family=("jcore_bounce", "jcore_pop", "anthem"),
        scale_pref=("major", "mixolydian", "natural_minor"),
        melody_density=0.9, energy_bias=1.05, section_bars=8, swing=0.0,
        era="modern", fx_level=0.6,
    ),
    "dnb": dict(
        bpm=(160, 180), kick="four", drums="dnb", bass="reese",
        bass_style="jungle", lead="hoover", prog_family=("amen_dark", "anthem", "dramatic"),
        scale_pref=("natural_minor", "dorian", "phrygian"),
        melody_density=0.6, energy_bias=0.9, section_bars=8, swing=0.0,
        era="modern", fx_level=0.55,
    ),
    "jungle": dict(
        bpm=(155, 170), kick="four", drums="jungle", bass="reese",
        bass_style="jungle", lead="stab", prog_family=("amen_dark", "anthem", "sus_engine"),
        scale_pref=("natural_minor", "dorian"),
        melody_density=0.55, energy_bias=0.9, section_bars=8, swing=0.05,
        era="1990-1995", fx_level=0.6,
    ),
    "hard_house": dict(
        bpm=(145, 155), kick="four", drums="hardcore", bass="reverse_bass",
        bass_style="off8", lead="stab", prog_family=("hardhouse", "rave_loop", "anthem_b"),
        scale_pref=("natural_minor", "harmonic_minor"),
        melody_density=0.6, energy_bias=0.95, section_bars=8, swing=0.0,
        era="1990-1995", fx_level=0.5,
    ),
    "trance": dict(
        bpm=(135, 142), kick="four", drums="fourfloor", bass="fm_bass",
        bass_style="off8", lead="supersaw", prog_family=("trance_roller", "trance_epic", "uplifting"),
        scale_pref=("natural_minor", "dorian"), melody_density=0.7,
        energy_bias=0.85, section_bars=16, swing=0.0, era="modern", fx_level=0.6,
    ),
    "acid_trance": dict(
        bpm=(138, 145), kick="four", drums="fourfloor", bass="acid",
        bass_style="acid", lead="supersaw", prog_family=("sus_engine", "trance_roller", "uplifting"),
        scale_pref=("dorian", "natural_minor"), melody_density=0.7,
        energy_bias=0.9, section_bars=16, swing=0.0, era="1990-1995", fx_level=0.55,
    ),
    "big_beat": dict(
        bpm=(120, 130), kick="four", drums="bigbeat", bass="dist_bass",
        bass_style="acid", lead="square_lead", prog_family=("big_beat", "anthem", "amen_dark"),
        scale_pref=("phrygian", "natural_minor", "pentatonic_minor"),
        melody_density=0.55, energy_bias=0.95, section_bars=8, swing=0.04,
        era="1995-2005", fx_level=0.55,
    ),
    "rave": dict(
        bpm=(150, 165), kick="four", drums="breakbeat_rave", bass="reese",
        bass_style="four_off", lead="hoover", prog_family=("rave_loop", "anthem", "epic"),
        scale_pref=("harmonic_minor", "natural_minor"),
        melody_density=0.65, energy_bias=0.95, section_bars=8, swing=0.0,
        era="1990-1995", fx_level=0.55,
    ),
}

FUSION_KEYWORDS = {
    "gabber": ("gabber", "gabba", "hardcore kick"),
    "frenchcore": ("frenchcore", "french"),
    "uk_hardcore": ("uk hardcore", "ukhc", "happy hardcore", "hardcore"),
    "freeform": ("freeform",),
    "jcore": ("j-core", "jcore", "j core", "anime", "denpa"),
    "dnb": ("dnb", "d&b", "drum and bass", "drum n bass", "drum'n'bass"),
    "jungle": ("jungle", "amen"),
    "hard_house": ("hard house", "hardhouse", "tanny"),
    "trance": ("trance", "uplifting"),
    "acid_trance": ("acid trance", "acid"),
    "big_beat": ("big beat", "bigbeat", "chemical"),
    "rave": ("rave", "1990s", "90s", "oldschool", "old school", "early hardcore"),
}


# ----------------------------------------------------------------------------
# Plan / SongPlan
# ----------------------------------------------------------------------------

class Plan:
    def __init__(self, prompt: str, genre: str | None = None, bpm: float | None = None,
                 key: str | None = None, mood: str | None = None,
                 authenticity: str = "hybrid", seed: int | None = None,
                 bars_limit: int | None = None, sample_mode: str = "balanced",
                 prog_family: list[str] | None = None,
                 motif_override: list[int | None] | None = None):
        self.prompt = (prompt or "").lower()
        self.rng = np.random.default_rng(seed if seed is not None else
                                         int(np.random.default_rng().integers(0, 2**31)))
        self.genre, self.secondary = self._detect_genre(genre)
        self.pack = GENRES[self.genre]
        self.bpm = float(bpm) if bpm else self._pick_bpm()
        self.mood = mood or self._pick_mood()
        self.key, self.scale = self._pick_key(key)
        self.authenticity = authenticity
        self.seed = seed
        self.bars_limit = bars_limit
        self.era = self._pick_era()
        # album layer (EP generation): explicit progression family + motif
        # injection; None for normal generation (behavior unchanged)
        self.prog_family_override = list(prog_family) if prog_family else None
        self.motif_override = list(motif_override) if motif_override else None
        self.album_palette: dict | None = None  # set by the album generator
        self.sample_mode = sample_mode
        self.sample_dir: str | None = None  # set by CLI when provided

    def _detect_genre(self, explicit: str | None) -> tuple[str, list[str]]:
        hits: list[tuple[int, str]] = []
        for g, kws in FUSION_KEYWORDS.items():
            for kw in kws:
                if kw in self.prompt:
                    hits.append((self.prompt.index(kw), g))
                    break
        if explicit:
            g = explicit.lower().replace("-", "_").replace(" ", "_")
            g = g if g in GENRES else "rave"
            return g, [h[1] for h in sorted(hits) if h[1] != g][:2]
        if hits:
            primary = sorted(hits)[0][1]
            return primary, [h[1] for h in sorted(hits) if h[1] != primary][:2]
        return "rave", []

    def _pick_bpm(self) -> float:
        lo, hi = self.pack["bpm"]
        if self.genre == "trance" and "hard" in self.prompt:
            return 145.0
        return float(self.rng.integers(lo, hi + 1))

    def _pick_key(self, explicit: str | None) -> tuple[str, str]:
        if explicit:
            parts = explicit.split(":")
            root = parts[0].strip().capitalize()
            if root not in NOTE_NAMES:
                root = "A"
            mode = parts[1].strip().lower() if len(parts) > 1 else None
            if mode in ("min", "minor"):
                mode = "natural_minor"
            elif mode in ("maj", "major"):
                mode = "major"
            return root, mode or self._pick_scale()
        return NOTE_NAMES[int(self.rng.integers(0, 12))], self._pick_scale()

    def _pick_scale(self) -> str:
        prefs = self.pack["scale_pref"]
        w = np.ones(len(prefs))
        if self.mood == "dark":
            for i, s in enumerate(prefs):
                if s in ("phrygian", "harmonic_minor"):
                    w[i] *= 2.5
        if self.mood == "euphoric":
            for i, s in enumerate(prefs):
                if s in ("dorian", "major", "lydian", "melodic_minor"):
                    w[i] *= 2.5
        w = w / w.sum()
        return prefs[int(self.rng.choice(len(prefs), p=w))]

    def _pick_mood(self) -> str:
        # explicit mood words first; note genre names like "hard house" must not
        # shadow them, so dark is matched last
        for m, kws in (("euphoric", ("euphoric", "uplifting", "happy", "emotional", "melodic")),
                       ("fun", ("fun", "playful", "silly", "anime")),
                       ("cinematic", ("cinematic", "epic", "atmospheric")),
                       ("dark", ("dark", "aggressive", "brutal", "angry", "hard"))):
            if any(k in self.prompt for k in kws):
                return m
        return {"gabber": "dark", "frenchcore": "dark", "uk_hardcore": "euphoric",
                "freeform": "cinematic", "jcore": "fun", "dnb": "dark",
                "jungle": "dark", "hard_house": "dark", "trance": "euphoric",
                "acid_trance": "dark", "big_beat": "fun", "rave": "euphoric"}[self.genre]

    def _pick_era(self) -> str:
        p = self.prompt
        if any(k in p for k in ("1990", "1991", "1992", "1993", "1994", "1995", "90s", "oldskool")):
            return "1990-1995"
        if "2000" in p or "y2k" in p:
            return "1995-2005"
        if "modern" in p:
            return "modern"
        # AUTHENTIC mode (spec §17): force the historical era of the genre
        if self.authenticity == "authentic":
            return {"gabber": "1990-1995", "jungle": "1990-1995",
                    "hard_house": "1990-1995", "rave": "1990-1995",
                    "acid_trance": "1990-1995", "freeform": "1995-2005",
                    "big_beat": "1995-2005"}.get(self.genre, self.pack["era"])
        return self.pack["era"]


class SongPlan:
    """Complete musical blueprint (spec §12) — built before any audio."""

    def __init__(self, plan: Plan):
        self.plan = plan
        self.rng = np.random.default_rng(plan.seed if plan.seed is not None
                                         else plan.rng.integers(0, 2**31))
        pack = plan.pack
        self.bpm = plan.bpm
        self.beat = 60.0 / self.bpm
        self.bar_dur = self.beat * 4
        self.step = self.beat / 4  # 16th
        self.key = plan.key
        self.scale = plan.scale
        fam = list(getattr(plan, "prog_family_override", None) or
                   pack["prog_family"])
        self.prog_name, self.prog = pick_progression(
            self.rng, str(self.rng.choice(fam)))
        # main motif — the track's identity. Album tracks receive the shared
        # lineage variant here (normalized to the genre's phrase length).
        steps = 16 if pack["melody_density"] >= 0.8 else 8
        contour = self.rng.choice(["arch", "wave", "rise", "dawn"])
        self.motif = motif_generate(self.rng, self.scale, self.key, steps,
                                    contour=str(contour), rest_p=0.10)
        if getattr(plan, "motif_override", None):
            mo = list(plan.motif_override)
            self.motif = (mo + [None] * steps)[:steps]
        self.variants = {
            "A": self.motif,
            "B": motif_variate(self.rng, self.motif, style="transpose",
                               amount=int(self.rng.integers(1, 3))),
            "C": motif_variate(self.rng, self.motif, style=str(self.rng.choice(
                ["invert", "retrograde", "embellish"]))),
        }
        self.bass_inst = pack["bass"]
        self.bass_style = pack["bass_style"]
        self.lead_inst = pack["lead"]
        self.sections: list[Section] = []
        self.kit_seed: int | None = None
        self.event_log: list[dict] = []  # records rendered events for the timeline
        self._build_arrangement()
        self._assign_abs_bars()

    # -- arrangement (spec §8/§9) -------------------------------------------
    def _build_arrangement(self) -> None:
        rng = self.rng
        pack = self.plan.pack
        sb = pack["section_bars"]
        if self.plan.bars_limit:
            total = self.plan.bars_limit
        else:
            total = int(rng.integers(0, 3)) + (64 if self.plan.genre in
                                               ("trance", "acid_trance") else 48)
        # scale section length with track length so short renders stay coherent
        sb = max(4, min(sb, total // 10))
        # energy curve: intro(LOW) groove theme dev break build drop var brk2 drop outro
        curve = [
            ("intro",   sb,     0.30), ("groove",  sb,     0.55),
            ("theme",   sb,     0.75), ("dev",     sb,     0.85),
            ("break",   sb // 2, 0.25), ("build",   sb // 2, 0.60),
            ("drop",    sb,     1.00), ("variation", sb,   0.90),
            ("break2",  sb // 2, 0.30), ("build2",  sb // 2, 0.65),
            ("drop2",   sb,     1.00), ("outro",   sb,     0.40),
        ]
        acc = 0
        for name, bars, energy in curve:
            bars = max(4, bars)
            if acc + bars > total:
                rest = total - acc
                if rest < 8:
                    if rest > 0:
                        self.sections.append(Section(name, rest, energy, self))
                    break
                bars = rest
            self.sections.append(Section(name, bars, energy, self))
            acc += bars
            if acc >= total:
                break

    def total_bars(self) -> int:
        return sum(s.bars for s in self.sections)

    def _assign_abs_bars(self) -> None:
        """Give every Section its absolute start bar (for event logging)."""
        acc = 0
        for s in self.sections:
            s.abs_start_bar = acc
            acc += s.bars

    def duration(self) -> float:
        return self.total_bars() * self.bar_dur

    def describe(self) -> str:
        lines = [
            f"genre={self.plan.genre}" + (f" (fusion: {','.join(self.plan.secondary)})"
                                          if self.plan.secondary else ""),
            f"bpm={self.bpm:.0f}  key={self.key} {self.scale}  "
            f"progression={self.prog_name} {self.plan.mood}  era={self.plan.era}",
            f"motif={' '.join(str(x) if x is not None else '.' for x in self.motif)}",
            f"length={self.total_bars()} bars ≈ {self.duration() / 60:.1f} min",
            "arrangement: " + " -> ".join(f"{s.name}({s.bars})" for s in self.sections),
        ]
        return "\n".join(lines)


class Section:
    def __init__(self, name: str, bars: int, energy: float, song: "SongPlan"):
        self.name = name
        self.bars = bars
        self.energy = energy
        self.song = song
        self.variant = {"intro": "A", "theme": "A", "dev": "B", "drop": "A",
                        "variation": "C", "drop2": "B"}.get(name, "A")
        self.abs_start_bar = 0  # set after arrangement is built
        self.layers: set[str] = self._layers()

    def _layers(self) -> set[str]:
        s = set()
        e = self.energy
        if self.name == "intro":
            s |= {"drums_light", "fx"}
            if self.song.rng.random() < 0.7:
                s.add("pad")
        elif self.name in ("groove", "outro"):
            s |= {"drums", "bass", "fx"}
            if self.song.rng.random() < 0.5:
                s.add("stab")
        elif self.name in ("theme", "dev"):
            s |= {"drums", "bass", "melody", "pad", "fx"}
            if self.song.rng.random() < 0.7:
                s.add("stab")
        elif self.name == "break":
            s |= {"drums_light", "pad", "melody_soft", "fx"}
        elif self.name in ("build", "build2"):
            s |= {"drums_build", "bass", "melody_soft", "fx", "riser"}
        elif self.name in ("drop", "drop2", "variation"):
            s |= {"drums", "bass", "melody", "stab", "fx", "pad"}
        else:
            s |= {"drums", "bass", "fx"}
        if e >= 0.95:
            s.add("fx_full")
        return s


# ----------------------------------------------------------------------------
# rendering — per-section buses
# ----------------------------------------------------------------------------

def _build_kit(pack: dict, bpm: float, seed: int) -> dict:
    """Deterministic kit from an explicit seed (shared by render and replay)."""
    k = {
        "hat": dsp.hat(seed=seed), "hat_open": dsp.hat(seed=seed + 1, open_hat=True),
        "clap": dsp.clap_909(seed=seed + 2), "snare": dsp.snare_909(seed=seed + 3),
        "tom": dsp.tom(seed=seed + 4), "crash": dsp.crash(seed=seed + 5),
        "ride": dsp.ride(seed=seed + 6),
    }
    if pack["kick"] == "gabber":
        k["kick"] = dsp.kick_gabber(bpm, seed=seed + 7)
    else:
        k["kick"] = dsp.kick_four(bpm, seed=seed + 7, punchy=pack["kick"] == "four")
    if pack["drums"] in ("dnb", "jungle"):
        k["snare"] = dsp.snare_909(seed=seed + 3, bright=False)
    return k


def _kit(song: SongPlan) -> dict:
    """Drum kit for the genre, cached per song by section render loop."""
    seed = int(song.rng.integers(0, 2**31))
    song.kit_seed = seed  # recorded for exact re-render replay
    return _build_kit(song.plan.pack, song.bpm, seed)


def _drum_events(song: SongPlan, sec: Section) -> list[tuple[int, str, float]]:
    rng = song.rng
    bar_steps = 16
    pack = song.plan.pack
    style = pack["drums"]
    ev: list[tuple[int, str, float]] = []
    if sec.name in ("break",):
        for bar in range(sec.bars):
            pat = voices.break_pattern(rng, "jungle" if style == "jungle" else "dnb",
                                       bar_steps, intensity=0.5)
            for st, v, vel in pat:
                if v == "kick":
                    continue  # breaks: no kick, keep space
                ev.append((bar * bar_steps + st, v, vel * 0.8))
        return ev
    if sec.name in ("build", "build2"):
        # accelerating kick: quarters -> 8ths in last half
        for bar in range(sec.bars):
            div = 4 if bar < sec.bars // 2 else 2
            for st in range(0, bar_steps, div):
                ev.append((bar * bar_steps + st, "kick", 0.95))
            if bar >= sec.bars // 2:
                for st in range(2, bar_steps, 4):
                    ev.append((bar * bar_steps + st, "snare", 0.5))
        return ev
    if sec.name == "intro":
        for bar in range(sec.bars):
            for st in (0,):
                ev.append((bar * bar_steps + st, "kick", 0.8))
            for st in (4, 12):
                ev.append((bar * bar_steps + st, "hat", 0.5))
        return ev
    # main grooves
    if style == "fourfloor":
        for bar in range(sec.bars):
            for st in range(0, bar_steps, 4):
                ev.append((bar * bar_steps + st, "kick", 1.0))
            for st in (4, 12):
                ev.append((bar * bar_steps + st, "clap", 0.55))
            for st in range(2, bar_steps, 4):
                ev.append((bar * bar_steps + st, "hat", 0.4))
            ev.append((bar * bar_steps + 14, "hat_open" if bar % 2 else "hat", 0.35))
        return ev
    for bar in range(sec.bars):
        # re-chop the break every bar (spec §7: never one static loop)
        pat = voices.break_pattern(rng, style, bar_steps,
                                   intensity=0.6 + sec.energy * 0.4)
        for st, v, vel in pat:
            ev.append((bar * bar_steps + st, v, vel))
        # fill on last bar of section
        if bar == sec.bars - 1 and rng.random() < 0.8:
            for st, v in ((12, "tom"), (13, "snare"), (14, "snare"), (15, "crash")):
                if st == 15 and sec.name not in ("drop", "drop2", "variation"):
                    v = "snare"
                ev.append((bar * bar_steps + st, v, 0.8))
    return ev


def _bass_events(song: SongPlan, sec: Section) -> list[tuple[int, float, float, float, dict]]:
    """(step, midi, dur_steps, vel, extra) events."""
    rng = song.rng
    pack = song.plan.pack
    hits = voices.kick_bass_plan(rng, 16, song.bass_style)
    ev = []
    prog = song.prog
    if sec.name in ("break",):
        return ev  # bass drops out in breakdown
    root_pc = note_midi(song.key)
    for bar in range(sec.bars):
        chord = prog[bar % len(prog)]
        deg, shape = chord
        # octave=3 → root + 36 semitones → fundamentals at ~65 Hz (C2)
        octave = 3
        if song.bass_style == "roll16":
            midi = scale_degree_to_midi(song.key, song.scale, deg, octave=3)
            for st in range(16):
                if hits[st]:
                    ev.append((bar * 16 + st, midi, 1.0, 0.85, {}))
        elif song.bass_style in ("off8", "off8_roll"):
            midi = scale_degree_to_midi(song.key, song.scale, deg, octave=octave)
            dur = 2.0 if song.bass_style == "off8" else 1.5
            for st in range(16):
                if hits[st]:
                    ev.append((bar * 16 + st, midi, dur, 0.9, {}))
        elif song.bass_style == "acid":
            scale = song.scale
            for st in range(16):
                if hits[st]:
                    d = deg if st == 0 else deg + int(rng.choice([-1, 0, 0, 2, 4]))
                    midi = scale_degree_to_midi(song.key, scale, d, octave=3)
                    accent = rng.random() < 0.25
                    slide = st > 0 and rng.random() < 0.3
                    ev.append((bar * 16 + st, midi, 1.0, 0.75 if not accent else 1.0,
                               {"accent": accent, "slide": slide}))
        elif song.bass_style == "jungle":
            scale = song.scale
            for st in range(16):
                if hits[st]:
                    midi = scale_degree_to_midi(song.key, scale, deg, octave=3)
                    if rng.random() < 0.3:
                        midi = scale_degree_to_midi(song.key, scale, deg + 2, octave=3)
                    ev.append((bar * 16 + st, midi, 2.5, 0.9, {}))
        else:  # four
            midi = scale_degree_to_midi(song.key, song.scale, deg, octave=octave)
            for st in range(0, 16, 4):
                ev.append((bar * 16 + st, midi, 3.0, 0.85, {}))
    return ev


def _melody_events(song: SongPlan, sec: Section) -> tuple[list[tuple[int, float, float, float, dict]], str]:
    """Motif-based melody for the section. Returns (events, variant_used)."""
    rng = song.rng
    variant = sec.variant
    motif = song.variants.get(variant, song.motif)
    steps_per_note = 1 if song.plan.pack["melody_density"] >= 0.8 else 2
    ev = []
    octave = 4 if song.lead_inst not in ("stab",) else 5
    dense = song.plan.pack["melody_density"]
    for bar in range(sec.bars):
        prog = song.prog
        deg, shape = prog[bar % len(prog)]
        for i, d in enumerate(motif):
            if d is None:
                continue
            st = bar * 16 + i * steps_per_note
            if st % 2 == 1 and rng.random() > dense:
                continue
            # snap motif degree into the current chord context
            mm = scale_degree_to_midi(song.key, song.scale, d + (deg if bar % 4 == 3 else 0),
                                      octave=octave)
            dur_steps = steps_per_note * (2 if (i == len(motif) - 1 and rng.random() < 0.4) else 1)
            ev.append((st, mm, dur_steps, 0.8 + 0.2 * rng.random(), {}))
        # call & response: half-bar answer in last bar (spec §4)
        if bar == sec.bars - 1 and sec.name in ("drop", "drop2", "theme"):
            for i, d in enumerate(motif[:4]):
                if d is not None:
                    mm = scale_degree_to_midi(song.key, song.scale, d + 7, octave=octave)
                    ev.append((bar * 16 + 8 + i * 2, mm, 1.0, 0.7, {}))
    return ev, variant


def _chord_events(song: SongPlan, sec: Section) -> list[tuple[int, float, float, str, dict]]:
    ev = []
    prog = song.prog
    for bar in range(sec.bars):
        deg, _shape = prog[bar % len(prog)]
        chord = chord_from_scale_degree(song.key, song.scale, deg,
                                        octave=3, seven=song.plan.mood == "cinematic")
        st = bar * 16
        if "stab" in sec.layers:
            ev.append((st + (0 if sec.name != "variation" else 8), 0, 2.0, "stab",
                       {"notes": chord}))
        if "pad" in sec.layers:
            ev.append((st, 0, 16.0, "pad", {"notes": chord}))
    return ev


def _log_event(song: SongPlan, sec: "Section", **kw) -> None:
    """Record one rendered event so the timeline mirrors the audio exactly.

    Positions are converted from section-local steps to absolute steps here.
    """
    kw["start_step"] = sec.abs_start_bar * 16 + kw["start_step"]
    song.event_log.append(kw)


def render_section(song: SongPlan, sec: Section, kit: dict) -> dict[str, np.ndarray]:
    """Render one section into buses. Returns dict of bus -> mono audio."""
    n = int(sec.bars * 16 * song.step * dsp.SR)
    buses = {"drums": np.zeros(n), "bass": np.zeros(n), "mel": np.zeros(n),
             "fx": np.zeros(n)}
    rng = song.rng
    pack = song.plan.pack

    # --- drums ---
    if "drums" in sec.layers or "drums_light" in sec.layers or "drums_build" in sec.layers:
        for step, voice, vel in _drum_events(song, sec):
            if voice not in kit:
                continue
            i0 = int(step * song.step * dsp.SR)
            if i0 >= n:
                continue
            w = kit[voice]
            e = min(n, i0 + len(w))
            buses["drums"][i0:e] += w[: e - i0] * vel
            _log_event(song, sec, bus="drums", type=_VOICE_TO_TYPE.get(voice, "perc"),
                       role=_VOICE_TO_TYPE.get(voice, "perc"), section=sec.name,
                       instrument=voice, start_step=step, dur_steps=1,
                       velocity=float(vel))
        # swing (spec §3): nudge odd 16ths
        # break chopping for jungle/dnb/big beat: re-chop rendered bar
        if pack["drums"] in ("jungle", "dnb", "bigbeat") and sec.name not in ("break",):
            bar_n = int(16 * song.step * dsp.SR)
            for b in range(sec.bars):
                a, bnd = b * bar_n, (b + 1) * bar_n
                if bnd <= n:
                    chopped, chop_dec = voices.chop_with_decisions(
                        rng, buses["drums"][a:bnd], song.step * 2, stutters=2)
                    buses["drums"][a:bnd] = chopped
                    _log_event(song, sec, bus="drums", type="break_chop", role="drums",
                               section=sec.name, start_step=b * 16, dur_steps=16,
                               extra={"decisions": chop_dec})

    # --- bass ---
    for step, midi, dur_s, vel, extra in _bass_events(song, sec):
        i0 = int(step * song.step * dsp.SR)
        if i0 >= n:
            continue
        dur = dur_s * song.step
        voice_seed = int(rng.integers(0, 2**31))
        w = voices.synth_voice(song.bass_inst, midi, dur, vel,
                               seed=voice_seed,
                               last_midi=None, **extra)
        e = min(n, i0 + len(w))
        buses["bass"][i0:e] += w[: e - i0] * vel
        _log_event(song, sec, bus="bass", type="bass", role="bass", section=sec.name,
                   instrument=song.bass_inst, start_step=step, dur_steps=dur_s,
                   pitch=float(midi), velocity=float(vel), extra=extra,
                   voice_seed=voice_seed)

    # --- melody / lead ---
    if "melody" in sec.layers or "melody_soft" in sec.layers:
        soft = "melody_soft" in sec.layers and "melody" not in sec.layers
        evs, evs_variant = _melody_events(song, sec)
        inst = song.lead_inst
        for step, midi, dur_s, vel, extra in evs:
            i0 = int(step * song.step * dsp.SR)
            if i0 >= n:
                continue
            dur = dur_s * song.step
            voice_seed = int(rng.integers(0, 2**31))
            w = voices.synth_voice(inst, midi, dur, vel,
                                   seed=voice_seed)
            if soft:
                w = dsp.lp(w, 2200) * 0.6
            e = min(n, i0 + len(w))
            buses["mel"][i0:e] += w[: e - i0] * vel
            _log_event(song, sec, bus="mel", type="melody", role="melody",
                       section=sec.name, instrument=inst, start_step=step,
                       dur_steps=dur_s, pitch=float(midi), velocity=float(vel),
                       extra={"variant": evs_variant, "soft": bool(soft)},
                       voice_seed=voice_seed)
        # octave doubling on drops (variation without new material)
        if sec.name in ("drop", "drop2") and inst in ("supersaw", "hoover"):
            dbl = buses["mel"].copy()
            buses["mel"] = buses["mel"] * 0.8 + dsp.hp(dbl, 800) * 0.4

    # --- chords: stabs & pads ---
    for step, _midi, dur_s, kind, extra in _chord_events(song, sec):
        i0 = int(step * song.step * dsp.SR)
        if i0 >= n:
            continue
        notes = extra["notes"]
        chord_seed = int(rng.integers(0, 2**31))
        if kind == "stab":
            w = sum(voices.synth_voice("stab", nn, dur_s * song.step, 0.8,
                                       seed=chord_seed)
                    for nn in notes[:3])
            w = dsp.normalize(w, 0.7)
        else:
            w = dsp.pad_chord([hz(nn) for nn in notes],
                              dur_s * song.step, seed=chord_seed)
        e = min(n, i0 + len(w))
        buses["mel"][i0:e] += w[: e - i0] * 0.7
        _log_event(song, sec, bus="mel", type=kind, role="chord", section=sec.name,
                   instrument=kind, start_step=step, dur_steps=dur_s,
                   extra={"notes": [int(x) for x in notes]},
                   voice_seed=chord_seed)

    # --- fx bus ---
    fx = buses["fx"]
    if "riser" in sec.layers:
        rd = min(n / dsp.SR, 4 * song.bar_dur)
        riser_seed = int(rng.integers(0, 2**31))
        r = dsp.riser(rd, seed=riser_seed)
        i0 = max(0, n - len(r))
        fx[i0:] += r[: n - i0] * 0.5
        _log_event(song, sec, bus="fx", type="fx", role="riser", section=sec.name,
                   instrument="riser", start_step=0,
                   dur_steps=sec.bars * 16, velocity=0.5, voice_seed=riser_seed)
    if sec.name in ("drop", "drop2"):
        impact_seed = int(rng.integers(0, 2**31))
        im = dsp.impact(seed=impact_seed)
        fx[: min(n, len(im))] += im[: min(n, len(im))] * 0.4
        cr = kit.get("crash")
        if cr is not None:
            fx[: min(n, len(cr))] += cr[: min(n, len(cr))] * 0.5
        _log_event(song, sec, bus="fx", type="fx", role="impact", section=sec.name,
                   instrument="impact", start_step=0, dur_steps=1, velocity=0.9,
                   voice_seed=impact_seed)
    if sec.name == "break" or sec.name == "outro":
        cr_seed = int(rng.integers(0, 2**31))
        cr = dsp.vinyl_crackle(n / dsp.SR, seed=cr_seed, level=0.05)
        cr = np.pad(cr, (0, max(0, n - len(cr))))[:n]
        fx += cr
        _log_event(song, sec, bus="fx", type="fx", role="fx", section=sec.name,
                   instrument="crackle", start_step=0, dur_steps=sec.bars * 16,
                   velocity=0.05, voice_seed=cr_seed)
    if sec.name == "break2":
        dl_seed = int(rng.integers(0, 2**31))
        dl = dsp.downlifter(min(n / dsp.SR, 2 * song.bar_dur), seed=dl_seed)
        fx[: len(dl)] += dl[: min(n, len(dl))] * 0.4
        _log_event(song, sec, bus="fx", type="fx", role="fx", section=sec.name,
                   instrument="downlifter", start_step=0, dur_steps=8,
                   velocity=0.4, voice_seed=dl_seed)
    # era texture
    if song.plan.era == "1990-1995" and rng.random() < 0.6:
        cr_seed = int(rng.integers(0, 2**31))
        cr = dsp.vinyl_crackle(n / dsp.SR, seed=cr_seed, level=0.025)
        cr = np.pad(cr, (0, max(0, n - len(cr))))[:n]
        fx += cr
        _log_event(song, sec, bus="fx", type="fx", role="fx", section=sec.name,
                   instrument="crackle", start_step=0, dur_steps=sec.bars * 16,
                   velocity=0.025, voice_seed=cr_seed)
    # vocal chops for uk hardcore / jcore flavor
    if song.plan.genre in ("uk_hardcore", "jcore") and sec.name in ("drop", "drop2", "variation"):
        for k in range(sec.bars):
            if rng.random() < 0.5:
                st = int(rng.integers(0, 8))
                midi = scale_degree_to_midi(song.key, song.scale,
                                            int(rng.integers(0, 7)), octave=5)
                vox_seed = int(rng.integers(0, 2**31))
                w = voices.synth_voice("vox", midi, song.step * 2, 0.5,
                                       seed=vox_seed)
                i0 = int((k * 16 + st) * song.step * dsp.SR)
                e = min(n, i0 + len(w))
                if i0 < n:
                    fx[i0:e] += w[: e - i0] * 0.4
                    _log_event(song, sec, bus="fx", type="fx", role="vox",
                               section=sec.name, instrument="vox",
                               start_step=k * 16 + st, dur_steps=2,
                               pitch=float(midi), velocity=0.4,
                               voice_seed=vox_seed)

    # EXPERIMENTAL mode (spec §17): controlled audio mischief, identity intact
    if song.plan.authenticity == "experimental" and rng.random() < 0.6:
        kind = str(rng.choice(["crush", "reverse", "stutter"]))
        bar_n = int(16 * song.step * dsp.SR)
        b0 = int(rng.integers(0, max(1, sec.bars - 1))) * bar_n
        _log_event(song, sec, bus="mel", type="melody", role="chord", section=sec.name,
                   instrument=f"experimental_{kind}", start_step=b0 // int(song.step * dsp.SR) // 16 * 16,
                   dur_steps=16, extra={"mode": kind})
        if kind == "crush":
            buses["mel"] = dsp.bitcrush(buses["mel"], bits=10)
        elif kind == "reverse":
            seg = buses["mel"][b0:b0 + bar_n][::-1].copy()
            buses["mel"][b0:b0 + len(seg)] = seg
        else:
            eighth = max(64, bar_n // 8)
            seg = np.tile(buses["mel"][b0:b0 + eighth].copy(), 4)
            buses["mel"][b0:b0 + min(bar_n, len(seg))] = seg[:bar_n]

    # per-bus section gain by energy + layer subtraction (spec §9)
    # NOTE: applied centrally in finalize_mix (per-section, pre-assembly) so
    # live rendering and project replay share one implementation.
    return buses


# ----------------------------------------------------------------------------
# QC (spec §19)
# ----------------------------------------------------------------------------

def qc_check(song: SongPlan, sections: list[tuple[Section, dict]]):
    """Returns list of (issue, fix) applied to the bus dict or mix params."""
    notes = []
    # 1) silence check
    for sec, buses in sections:
        tot = sum(float(np.max(np.abs(b))) if len(b) else 0.0 for b in buses.values())
        if tot < 1e-4:
            notes.append(f"section {sec.name}: near-silent -> regenerating drums layer")
            kit = _kit(song)
            new = render_section(song, sec, kit)
            buses.update(new)
    # 2) motif identity: A-variants must stay recognizable
    ident = motif_identity(song.motif, song.variants["B"])
    if ident < 0.35:
        notes.append(f"variant B too far from motif (identity={ident:.2f}) -> regenerated")
        song.variants["B"] = motif_variate(song.rng, song.motif, style="transpose",
                                           amount=1)
    # 3) low-end clash: check kick vs bass energy ratio in drops
    for sec, buses in sections:
        if sec.name not in ("drop", "drop2"):
            continue
        d = buses["drums"][: dsp.SR * 4]
        b = buses["bass"][: dsp.SR * 4]
        d_lo = float(np.sqrt(np.mean(dsp.lp(d, 120) ** 2))) if len(d) else 0
        b_lo = float(np.sqrt(np.mean(dsp.lp(b, 120) ** 2))) if len(b) else 0
        if b_lo > d_lo * 2.0 and b_lo > 0:
            buses["bass"] = dsp.hp(buses["bass"], 60)
            notes.append(f"section {sec.name}: bass dominating low end -> HP + rebalance")
    return notes


# ----------------------------------------------------------------------------
# track assembly / mixdown
# ----------------------------------------------------------------------------

def _sidechain(bass: np.ndarray, kick: np.ndarray) -> np.ndarray:
    """Duck bass under kick transient (kick/bass separation, spec §14).

    Frame-rate control signal with fast attack and exponential-ish release.
    """
    if len(kick) == 0 or len(bass) == 0:
        return bass
    n = min(len(bass), len(kick))
    hop = 64
    env = np.abs(dsp.lp(kick[:n], 150))
    frames = env[::hop]
    peak = float(np.max(frames)) or 1.0
    frames = np.minimum(frames / peak, 1.0)
    duck = np.empty(len(frames))
    d = 1.0
    thr = 0.35
    for i, v in enumerate(frames):
        if v > thr:
            d = min(d, 1.0 - 0.6 * ((v - thr) / (1.0 - thr)))
        else:
            d = min(1.0, d + 0.012)  # ~150 ms full release at hop=64
        duck[i] = d
    duck = np.interp(np.arange(n), np.arange(0, n, hop), duck)
    return bass[:n] * duck


# genre-aware mix targets (RMS ratios relative to the drum bus)
BASS_LEVEL = {"rumble": 0.62, "reese": 0.8, "reverse_bass": 0.85, "acid": 0.7,
              "dist_bass": 0.75, "fm_bass": 0.65, "sub": 0.7}
MEL_LEVEL = {"gabber": 0.6, "frenchcore": 0.6, "uk_hardcore": 0.9, "freeform": 0.9,
             "jcore": 0.85, "dnb": 0.7, "jungle": 0.65, "hard_house": 0.7,
             "trance": 0.85, "acid_trance": 0.8, "big_beat": 0.8, "rave": 0.75}
# sample bus target by --sample-mode (spec §19)
SAMPLE_LEVEL = {"subtle": 0.3, "balanced": 0.55, "heavy": 0.8}


# ----------------------------------------------------------------------------
# timeline construction — representation of exactly what was rendered
# ----------------------------------------------------------------------------

_VOICE_TO_TYPE = {"hat": "hat", "hat_open": "hat", "clap": "clap",
                  "snare": "snare", "tom": "tom", "crash": "crash",
                  "ride": "ride", "kick": "kick"}


def _project_path(p: str) -> str:
    """Prefer project-relative paths for portability (spec §7)."""
    import os
    try:
        rel = os.path.relpath(p)
        return rel if not rel.startswith("..") else p
    except ValueError:
        return p


def _build_timeline(song: SongPlan, placements: list[dict]) -> Timeline:
    """Build the Timeline from the recorded event log (exact rendered truth)
    plus the sample placement decisions."""
    t = Timeline(song.bpm)
    beat_of_step = 0.25  # 16th-note grid: step = 0.25 beats

    abs_bar = 0
    for i, sec in enumerate(song.sections):
        t.add_section(SectionSpan(name=sec.name, index=i,
                                  start_beat=abs_bar * 4.0,
                                  end_beat=(abs_bar + sec.bars) * 4.0,
                                  energy=sec.energy, bars=sec.bars))
        abs_bar += sec.bars

    for ev in song.event_log:
        md = {"extra": ev.get("extra", {})}
        if ev.get("voice_seed") is not None:
            md["voice_seed"] = int(ev["voice_seed"])
        if ev.get("dur_steps") is not None:
            # keep the exact step count (may be fractional, e.g. jungle bass
            # = 2.5 steps) so replay reconstructs the identical voice length
            md["dur_steps"] = ev["dur_steps"]
        t.add(TimelineEvent(
            start_beat=ev["start_step"] * beat_of_step,
            duration_beats=round(ev.get("dur_steps", 1) * beat_of_step, 4),
            bus=ev["bus"], type=ev["type"], role=ev["role"], section=ev["section"],
            instrument=ev.get("instrument", ""),
            pitch=ev.get("pitch"),
            velocity=float(ev.get("velocity", 1.0)),
            metadata=md,
        ))

    import zlib
    import math
    for p in placements:
        m: SampleMetadata = p["sample"]
        sid = f"smp_{zlib.crc32(m.path.encode()) & 0xFFFFFFFF:08x}"
        if sid not in t.samples:
            t.register_sample(SampleUse(
                id=sid, path=_project_path(m.path), filename=m.filename,
                file_size=m.size, mtime=m.mtime, bpm=m.bpm,
                bpm_confidence=m.bpm_confidence, key=m.key,
                key_confidence=m.key_confidence, category=m.category))
        start_beat = p["abs_bar"] * 4.0
        if p.get("start_offset_eighths"):
            start_beat += p["start_offset_eighths"] * 0.5
        if p["kind"] == "oneshot":
            dur_beats = min(16.0, max(0.25, (m.duration or 1.0) * song.bpm / 60.0))
        else:
            dur_beats = 4.0
        gain = (0.5 * (0.6 + 0.4 * p.get("energy", 0.8))) if p["kind"] == "oneshot" \
            else (0.45 + 0.35 * p.get("energy", 0.8)) * \
                 (0.6 + 0.4 * p.get("plan_intensity", 0.8))
        t.add(TimelineEvent(
            start_beat=start_beat, duration_beats=dur_beats, bus="samples",
            type=p["event_type"], role=p["role"], section=p["section"],
            sample_id=sid, source_path=_project_path(m.path),
            pitch_semitones=p.get("pitch_semitones", 0.0),
            stretch_ratio=p.get("stretch_ratio", 1.0),
            reverse=bool(p.get("reverse", False)),
            chop_ops=p.get("ops", {}),
            gain_db=round(20 * math.log10(max(gain, 1e-6)), 2),
            metadata={"kind": p["kind"], "variant": p.get("variant", "drop"),
                      "dropout": p.get("dropout", ""),
                      "loop_bars": p.get("loop_bars"),
                      "gain_linear": float(gain),
                      "energy": round(p.get("energy", 0.8), 3),
                      "plan_intensity": round(p.get("plan_intensity", 0.8), 3)}))
    return t


# ----------------------------------------------------------------------------
# deterministic mixdown tail — shared by live rendering and project re-render
# ----------------------------------------------------------------------------

def finalize_mix(bus_arrays: dict, sample_bus, *, bass_inst: str, genre: str,
                 era: str, sample_mode: str, total_bars: int, step: float,
                 duration: float, mel_inst: str = "supersaw") -> dict:
    """Assemble section buses, apply bus shaping/sidechain/leveling.

    Deterministic given the inputs — no RNG. Used by render_track and by
    project re-render so both paths byte-match.
    """
    total_n = int(duration * dsp.SR) + dsp.SR
    drums = np.zeros(total_n); bass = np.zeros(total_n)
    mel = np.zeros(total_n); fxb = np.zeros(total_n)
    smp = np.zeros(total_n)
    pos = 0
    for arrs in bus_arrays:
        n = int(arrs["bars"] * 16 * step * dsp.SR)
        # per-section gains (spec §9) — identical math to the old in-section path
        name = arrs.get("name", "")
        energy = arrs.get("energy", 0.8)
        gains = {"drums": 1.0, "bass": 1.0, "mel": 0.45 + 0.55 * energy, "fx": 1.0}
        if name in ("intro", "outro"):
            gains["drums"] *= 0.5
            gains["bass"] *= 0.6
        if name == "break":
            gains["drums"] *= 0.7
        for bus, arr in arrs["buses"].items():
            a = arr[:n] * gains.get(bus, 1.0)
            if len(a) < n:
                a = np.pad(a, (0, n - len(a)))
            dst = {"drums": drums, "bass": bass, "mel": mel, "fx": fxb}[bus]
            dst[pos:pos + n] += a
        pos += n
    if sample_bus is not None and len(sample_bus.get("samples", [])):
        s = sample_bus["samples"]
        s = s[:total_n]
        smp[:len(s)] += s
    else:
        smp = None
    drums, bass, mel, fxb = drums[:pos], bass[:pos], mel[:pos], fxb[:pos]

    # bus-level shaping before the mix stage
    bass = _sidechain(bass, drums)
    mel = dsp.simple_reverb(mel, size=0.7, wet=0.13, damp=5200)
    fxb = dsp.simple_reverb(fxb, size=0.85, wet=0.22, damp=4500)
    if era == "1990-1995":
        mel = dsp.hp(mel, 200)
        drums = dsp.dist_drive(drums, 1.25)
    if era == "modern":
        bass = dsp.hp(bass, 30)

    # RMS-based genre-aware bus leveling (auto-mix): kick anchors the balance.
    dpeak = float(np.max(np.abs(drums))) or 1.0
    drums = drums * (0.92 / dpeak)
    d_rms = float(np.sqrt(np.mean(drums ** 2))) or 1e-9
    targets = {"bass": BASS_LEVEL.get(bass_inst, 0.7),
               "mel": MEL_LEVEL.get(genre, 0.8),
               "fx": 0.4,
               "samples": SAMPLE_LEVEL.get(sample_mode, 0.55)}
    leveled = {"drums": drums}
    for name, b in (("bass", bass), ("mel", mel), ("fx", fxb)):
        rms = float(np.sqrt(np.mean(b ** 2)))
        if rms > 1e-9:
            b = b * (targets[name] * d_rms / rms)
        b = np.tanh(b * 1.1)  # gentle peak control that keeps density
        leveled[name] = b
    if smp is not None and len(smp) > 0 and float(np.max(np.abs(smp))) > 1e-6:
        # sample-level: relative to drum RMS, with mode-dependent target
        s_rms = float(np.sqrt(np.mean(smp ** 2)))
        if s_rms > 1e-9:
            # scale sample bus so its peak relative to drums matches the mode target
            smp = smp * (targets["samples"] * d_rms / s_rms)
        # light compression + tanh peak control
        smp = np.tanh(smp * 1.05)
        leveled["samples"] = smp
    elif smp is not None:
        leveled["samples"] = smp
    else:
        leveled["samples"] = np.zeros(pos)
    return leveled


def _apply_palette(assignments: list[dict], palette: dict,
                   index) -> list[dict]:
    """Swap per-track picks for palette entries on gated roles (EP §10).

    A palette entry is used whenever the track's own selection chose a role
    the palette gates; roles the track never selected stay unforced (no track
    must reuse every palette sample). Selection rationale is annotated.
    """
    entries = list(palette.get("roles", {}).values())
    out = []
    for a in assignments:
        entry = next((e for e in entries if a["role"] in e.get("roles", [])),
                     None)
        if entry is not None:
            m = index.get(entry["path"])
            if m is not None and a["sample"].path != m.path:
                a = dict(a)
                a["sample"] = m
                a["why"] = dict(a.get("why", {}))
                a["why"]["palette"] = f"album palette ({palette.get('mode')})"
        out.append(a)
    return out


def render_track(plan: Plan, sample_index=None) -> tuple[SongPlan, dict, list[str]]:
    """Render a track. sample_index: optional samples.cache.SampleIndex for
    hybrid rendering (procedural backbone + sample library character)."""
    from .samples.render import render_sample_bus, qc_samples, clear_audio_cache
    song = SongPlan(plan)
    kit = _kit(song)
    rendered: list[tuple[Section, dict]] = []
    for sec in song.sections:
        rendered.append((sec, render_section(song, sec, kit)))
    qc_notes = qc_check(song, rendered)

    # -- sample assignments (planned, not random) ---------------------------
    assignments: list[dict] = []
    placements: list[dict] = []
    sample_bus = None
    if sample_index is not None and plan.sample_mode != "off":
        from .samples.selector import select_for_song
        from .samples.render import decide_placements
        assignments = select_for_song(song, sample_index, seed=plan.seed or 0,
                                      mode=plan.sample_mode)
        # album palette: shared sonic anchors override per-track picks on
        # gated roles (EP generation, spec §10); adaptation downstream still
        # renders each anchor in the track's own BPM/key context
        palette = getattr(plan, "album_palette", None)
        if palette:
            assignments = _apply_palette(assignments, palette, sample_index)
        if assignments:
            placements = decide_placements(song, assignments)
            sample_bus = render_sample_bus(song, assignments, placements)
            qc_notes.extend(qc_samples(song, assignments))
            clear_audio_cache()
    song.sample_assignments = assignments

    # timeline records everything that was decided (procedural + samples)
    song.timeline = _build_timeline(song, placements)

    # assemble + level via the shared deterministic tail
    bus_arrays = [{"bars": sec.bars, "buses": buses, "name": sec.name,
                   "energy": sec.energy} for sec, buses in rendered]
    leveled = finalize_mix(bus_arrays, sample_bus, bass_inst=song.bass_inst,
                           genre=plan.genre, era=plan.era,
                           sample_mode=plan.sample_mode,
                           total_bars=song.total_bars(), step=song.step,
                           duration=song.duration(), mel_inst=song.lead_inst)
    return song, leveled, qc_notes
