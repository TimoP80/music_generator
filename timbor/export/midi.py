"""timbor.export.midi — Standard MIDI File (format 1) export from a project.

Beats convert directly to ticks (PPQ=480) — never through seconds. Drums use
a GM-style map on channel 10. Sample events become track text metadata, not
invented pitches. Output bytes are deterministic for a given project.
"""
from __future__ import annotations

import os

PPQ = 480  # pulses per quarter note

# GM drum map (channel 10). Export interpretation only — the timeline is
# never modified.
DRUM_MAP = {
    "kick": 36,     # Bass Drum 1
    "snare": 38,    # Acoustic Snare
    "clap": 39,     # Hand Clap
    "hat": 42,      # Closed Hi-Hat
    "hat_open": 46, # Open Hi-Hat
    "tom": 45,      # Low Tom
    "crash": 49,    # Crash Cymbal 1
    "ride": 51,     # Ride Cymbal 1
    "perc": 63,     # generic percussion fallback
}

MAX_VELOCITY = 127


def _vlq(n: int) -> bytes:
    """MIDI variable-length quantity."""
    out = bytearray([n & 0x7F])
    n >>= 7
    while n:
        out.insert(0, (n & 0x7F) | 0x80)
        n >>= 7
    return bytes(out)


def _chunk(tag: bytes, data: bytes) -> bytes:
    return tag + len(data).to_bytes(4, "big") + data


def _meta(type_id: int, payload: bytes) -> bytes:
    return bytes([0xFF, type_id]) + _vlq(len(payload)) + payload


def _text_event(type_id: int, text: str) -> bytes:
    return _meta(type_id, text.encode("utf-8", errors="replace"))


def _tempo_event(bpm: float) -> bytes:
    us_per_qn = int(round(60_000_000 / bpm))
    return _meta(0x51, us_per_qn.to_bytes(3, "big"))


def _time_sig_event(num: int = 4, den: int = 4) -> bytes:
    d = {1: 0, 2: 1, 4: 2, 8: 3, 16: 4}[den]
    return _meta(0x58, bytes([num, d, 24, 8]))


def _key_sig_event(root_pc: int, minor: bool) -> bytes:
    sharps = {0: 0, 7: 7, 2: 2, 9: 4, 4: -5, 11: 3, 6: -2, 1: 5, 8: -3, 3: -1,
              10: 6, 5: -4}.get(root_pc, 0)
    return _meta(0x59, bytes([sharps & 0xFF, 1 if minor else 0]))


def _note_events(track: list[tuple[int, int, int, int]],
                 channel: int = 0) -> list[tuple[int, bytes]]:
    """(start_tick, dur_tick, note, vel) -> sorted (tick, message) stream.
    channel: 0-based MIDI channel (drums use 9 = GM channel 10)."""
    on, off = 0x90 | channel, 0x80 | channel
    evs: list[tuple[int, int, bytes]] = []
    for start, dur, note, vel in track:
        note = max(0, min(127, note))
        vel = max(1, min(MAX_VELOCITY, vel))
        evs.append((start, 1, bytes([on, note, vel])))
        evs.append((start + max(1, dur), 0, bytes([off, note, 0])))
    evs.sort(key=lambda t: (t[0], t[1]))
    return [(t, data) for t, _, data in evs]


def build_midi(project: dict) -> bytes:
    """Serialize the project timeline to SMF format 1 bytes (deterministic)."""
    from timbor.theory import NOTE_INDEX

    bpm = float(project["song"]["bpm"])

    def ticks(beats: float) -> int:
        return int(round(beats * PPQ))

    # --- collect events per track -------------------------------------------
    drums: list = []
    bass: list = []
    melody: list = []
    chords: list = []
    sample_texts: list[str] = []
    sample_by_id = {s["id"]: s for s in project.get("samples", [])}

    for ev in sorted(project["timeline"], key=lambda e: (e["start_beat"], e["id"])):
        bus, typ = ev["bus"], ev["type"]
        if bus == "drums":
            if typ == "break_chop":
                continue  # arrangement texture, not a MIDI note
            drums.append((ticks(ev["start_beat"]), ticks(ev["duration_beats"]),
                          DRUM_MAP.get(typ, DRUM_MAP["perc"]),
                          int(round(ev["velocity"] * MAX_VELOCITY))))
        elif bus == "bass":
            if ev.get("pitch") is None:
                continue
            bass.append((ticks(ev["start_beat"]), ticks(ev["duration_beats"]),
                         int(round(ev["pitch"])),
                         int(round(ev["velocity"] * MAX_VELOCITY))))
        elif bus == "mel":
            if typ in ("melody", "lead") and ev.get("pitch") is not None:
                melody.append((ticks(ev["start_beat"]), ticks(ev["duration_beats"]),
                               int(round(ev["pitch"])),
                               int(round(ev["velocity"] * MAX_VELOCITY))))
            elif typ in ("stab", "pad", "chord"):
                notes = ev.get("metadata", {}).get("extra", {}).get("notes", [])
                for i, nn in enumerate(notes[:4]):
                    chords.append((ticks(ev["start_beat"]),
                                   ticks(ev["duration_beats"]),
                                   int(nn), max(40, 100 - i * 10)))
        elif ev.get("sample_id"):
            s = sample_by_id.get(ev["sample_id"], {})
            sample_texts.append(
                f"[SAMPLE] {s.get('filename', ev['sample_id'])} "
                f"ROLE={ev['role']} START={ev['start_beat']:g} "
                f"DURATION={ev['duration_beats']:g} "
                f"PITCH={ev.get('pitch_semitones', 0):+g} "
                f"STRETCH={ev.get('stretch_ratio', 1.0):g} "
                f"REVERSE={1 if ev.get('reverse') else 0}")

    # --- render tracks -------------------------------------------------------
    def render(name: str, events: list[tuple[int, bytes]],
               extra_meta: bytes = b"") -> bytes:
        out = bytearray()
        out += _vlq(0) + _text_event(0x03, name)
        if extra_meta:
            out += extra_meta
        last = 0
        for t, data in events:
            out += _vlq(max(0, t - last))
            out += data
            last = t
        out += _vlq(0) + _meta(0x2F, b"")
        return bytes(out)

    root = NOTE_INDEX.get(project["song"]["key"].capitalize(), 9)
    minor = project["song"]["scale"] not in ("major", "lydian", "mixolydian")
    conductor_events = [(0, _tempo_event(bpm)),
                        (0, _time_sig_event(4, 4)),
                        (0, _key_sig_event(root, minor))]
    conductor_events += [(ticks(s["start_beat"]),
                          _text_event(0x06, s["name"].upper()))
                         for s in project.get("sections", [])]
    conductor_events.sort(key=lambda t: t[0])

    conductor = render("TIMBOR", conductor_events)
    trk_drums = render("Drums", _note_events(drums, channel=9))   # GM ch 10
    trk_bass = render("Bass", _note_events(bass, channel=0))
    trk_melody = render("Melody", _note_events(melody, channel=1))
    trk_chords = render("Chords", _note_events(chords, channel=2))
    fx_texts = [f"[FX] role={ev['role']} sec={ev['section']}"
                for ev in sorted(project["timeline"],
                                 key=lambda e: (e["start_beat"], e["id"]))
                if ev["bus"] == "fx"]
    trk_fx = render("FX", [], extra_meta=b"".join(
        _vlq(0) + _text_event(0x04, t) for t in fx_texts))
    trk_samples = render("Samples", [],
                         extra_meta=b"".join(_vlq(0) + _text_event(0x04, t)
                                             for t in sample_texts))

    tracks = [conductor, trk_drums, trk_bass, trk_melody, trk_chords,
              trk_fx, trk_samples]
    header_data = ((1).to_bytes(2, "big") +          # format 1
                   len(tracks).to_bytes(2, "big") +
                   PPQ.to_bytes(2, "big"))
    return _chunk(b"MThd", header_data) + b"".join(
        _chunk(b"MTrk", t) for t in tracks)


def export_midi(project: dict, out_dir: str, name: str = "track",
                midi_format: int = 1) -> str:
    """Write <out_dir>/<name>.mid. Returns the path."""
    if midi_format != 1:
        raise ValueError("only SMF format 1 is supported")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{name}.mid")
    with open(path, "wb") as f:
        f.write(build_midi(project))
    return path
