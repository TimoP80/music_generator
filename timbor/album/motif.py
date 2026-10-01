"""timbor.album.motif — album motif lineage (spec §6–§8).

One base motif per album ("Motif A"); every track derives a recognizable
variant through deterministic transformation chains. Identity is measured,
stored and enforced with a floor: a track never drifts into an unrelated
melody, and no two tracks ever receive identical variants.

Operators build on theory.motif_variate (transpose / invert / retrograde /
octave / augment / thin / embellish) plus lineage-specific moves
(fragmentation, call/response, arpeggiation, genre rhythmic adaptation).
"""
from __future__ import annotations

import zlib

import numpy as np

from ..theory import motif_generate, motif_variate, motif_identity
from .dna import derive_seed

# base operator pool; order is stable, choice is seeded
OPERATORS = ("transpose", "thin", "augment", "embellish", "retrograde",
             "invert", "octave")


def rhythm_fingerprint(motif: list[int | None], steps: int = 16) -> list[int]:
    """Where the motif breathes — a 0/1 grid scaled to `steps` positions."""
    n = max(1, len(motif))
    idx = [round(i * (n - 1) / max(1, steps - 1)) for i in range(steps)]
    return [1 if motif[i] is not None else 0 for i in idx]


def motif_fingerprint(motif: list[int | None]) -> str:
    """Stable short identity tag of a motif (for dedup + provenance)."""
    blob = ",".join("." if x is None else str(x) for x in motif)
    return f"{zlib.crc32(blob.encode()):08x}"


def base_motif(seed: int, scale: str, root: str, steps: int = 16) -> tuple[
        list[int | None], str]:
    """Album-level Motif A, deterministic in the seed."""
    from ..theory import SCALES
    if scale not in SCALES:                      # human alias → theory name
        scale = {"minor": "natural_minor", "aeolian": "natural_minor"}.get(
            scale, "natural_minor")
    rng = np.random.default_rng(derive_seed(seed, "album-motif"))
    contour = str(rng.choice(["arch", "wave", "rise", "dawn"]))
    m = motif_generate(rng, scale, root, steps, contour=contour, rest_p=0.10)
    return m, contour


# ----------------------------------------------------------- lineage moves

def _fragment(rng, motif):
    """Keep the first half of the phrase, stated twice (question)."""
    n = max(2, len(motif) // 2)
    return motif[:n] + motif[:n]


def _call_response(rng, motif):
    """Statement + answer: statement, short rest, retrograde tail."""
    n = len(motif)
    tail = [x for x in motif[::-1] if x is not None][: max(1, n // 4)]
    rest = [None] * max(1, n // 8)
    return list(motif) + rest + (tail if rng.random() < 0.5 else tail[::-1])


def _arpeggiate(rng, motif):
    """Fill leaps with an ordered neighbor step."""
    out: list[int | None] = []
    prev: int | None = None
    for x in motif:
        if x is None:
            out.append(None)
            continue
        if prev is not None and abs(x - prev) > 1 and rng.random() < 0.6:
            out.append(prev + (1 if x > prev else -1))
        out.append(x)
        prev = x
    return out


def _genre_rhythm(rng, motif, genre: str):
    """Adapt rest/density to the genre's feel without changing pitches."""
    m = list(motif)
    if genre in ("gabber", "frenchcore"):
        # dense 16th drive: fill SOME inner rests with the previous pitch —
        # aggressive rest removal would erase the shared rhythm identity
        out: list[int | None] = []
        for i, x in enumerate(m):
            if x is None and 0 < i and out[i - 1] is not None \
                    and rng.random() < 0.5:
                out.append(out[i - 1])
            else:
                out.append(x)
        m = out
    elif genre in ("jungle", "dnb", "big_beat"):
        # syncopation: thin out some odd positions
        m = [None if (x is not None and i % 2 == 1 and rng.random() < 0.35) else x
             for i, x in enumerate(m)]
    elif genre in ("trance", "acid_trance"):
        # off-8th push: repeat some notes onto the following 8th
        out: list[int | None] = []
        for x in m:
            out.append(x)
            if x is not None and rng.random() < 0.3:
                out.append(x)
        m = out
    return m


def genre_phrase_steps(genre: str) -> int:
    """Phrase length the engine will use for this genre (16th grid)."""
    from ..engine import GENRES
    pack = GENRES.get(genre, GENRES["rave"])
    return 16 if pack["melody_density"] >= 0.8 else 8


def derive_track_motif(seed: int, index: int, base: list[int | None],
                       genre: str, strategy: str = "family",
                       max_ops: int | None = None,
                       steps: int | None = None) -> tuple[list[int | None], dict]:
    """Derive track `index`'s variant from the album base motif.

    Returns (variant, lineage_meta). Deterministic per (seed, index, base):
    deriving a later track never changes an earlier track's variant. The
    result is normalized to the genre's phrase length so the stored lineage
    matches exactly what the engine renders.
    """
    rng = np.random.default_rng(derive_seed(seed, f"motif-{index}"))

    if max_ops is None:
        # lineage depth grows with distance from the source; the 'anchored'
        # strategy keeps every track at most one light transformation away
        depth = {0: 0, 1: 1, 2: 1, 3: 2, 4: 2}.get(index, 2 + (index % 2))
        max_ops = min(depth, 1) if strategy == "anchored" else depth

    v = list(base)
    applied: list[str] = []
    for _ in range(max_ops):
        op = str(rng.choice(OPERATORS))
        if op == "transpose":
            amount = int(rng.integers(1, 3)) * (1 if rng.random() < 0.5 else -1)
            v = motif_variate(rng, v, style="transpose", amount=amount)
            applied.append(f"transpose({amount:+d})")
        elif op == "octave":
            v = motif_variate(rng, v, style="octave")
            applied.append("octave_shift")
        elif op == "invert":
            v = motif_variate(rng, v, style="invert")
            applied.append("invert")
        elif op == "retrograde":
            v = motif_variate(rng, v, style="retrograde")
            applied.append("retrograde")
        elif op == "augment":
            v = motif_variate(rng, v, style="augment")
            applied.append("rhythmic_augment")
        elif op == "thin":
            v = motif_variate(rng, v, style="thin")
            applied.append("note_omission")
        elif op == "embellish":
            v = motif_variate(rng, v, style="embellish")
            applied.append("note_insertion")
    if max_ops == 0 or index == 0:
        # a pure statement: track 0 IS Motif A; max_ops=0 callers asked for
        # the untouched phrase. Normalized to the target grid — the engine
        # renders exactly this length for the genre.
        if steps:
            v = (list(v) + [None] * steps)[:steps]
        return v, {"transformations": ["base_statement"]}
    # shaping moves apply only to genuine variants
    if rng.random() < 0.4:
        v = _arpeggiate(rng, v)
        applied.append("arpeggiation")
    if rng.random() < 0.3:
        v = _call_response(rng, v)
        applied.append("call_response")
    if rng.random() < 0.35:
        v = _fragment(rng, v)
        applied.append("fragmentation")
    v = _genre_rhythm(rng, v, genre)
    applied.append(f"genre_rhythm({genre})")
    if steps:
        v = (list(v) + [None] * steps)[:steps]
    return v, {"transformations": applied}


def build_lineage(seed: int, base: list[int | None], genres: list[str],
                  strategy: str = "family") -> dict:
    """Full lineage for the album: {track_index: (variant, lineage_meta)}.

    Track 0 IS Motif A; later tracks are labeled A', A'', ... with their
    index. Guarantees per track:

    * identity floor — a variant scoring below IDENTITY_FLOOR is re-derived
      with a single light transformation (deterministic), then, if still
      under, lifted with a safe transpose; track 0 is exempt (it IS A);
    * uniqueness — a variant fingerprinting identically to an earlier
      track's receives one transpose (+1), so no two tracks share a
      byte-identical melody.
    """
    from .validation import IDENTITY_FLOOR
    out: dict[int, tuple[list, dict]] = {}
    seen: dict[str, int] = {}
    for i, g in enumerate(genres):
        steps = genre_phrase_steps(g)
        v, meta = derive_track_motif(seed, i, base, g, strategy, steps=steps)
        ref = base[:len(v)]          # same-grid reference for fair scoring
        if i > 0 and motif_identity(ref, v) < IDENTITY_FLOOR:
            # drifted too far: fall back to one light transformation
            v, meta = derive_track_motif(seed, i, base, g, strategy,
                                         max_ops=1, steps=steps)
        if i > 0 and motif_identity(ref, v) < IDENTITY_FLOOR:
            # guaranteed-safe net: a plain transpose of the base statement
            # (transposition cannot lower interval/rhythm identity)
            v = [None if x is None else x + 1 for x in ref]
            meta = {"transformations": ["transpose(+1)"]}
        fp = motif_fingerprint(v)
        tries = 0
        while fp in seen and tries < 12:
            # resolve collisions deterministically; +tries keeps distinct
            # offsets for every re-collision (3rd, 4th… occurrence)
            v = motif_variate(np.random.default_rng(
                derive_seed(seed, f"motif-dedup-{i}-{tries}")), v,
                style="transpose", amount=1)
            meta = dict(meta)
            meta["transformations"] = list(meta["transformations"]) + \
                [f"dedup_transpose({tries + 1})"]
            fp = motif_fingerprint(v)
            tries += 1
        primes = "'" * min(2, 1 + i // 4)
        meta.update({
            "family": "main",
            "parent": "A",
            "variant": "A" if i == 0 else f"A{primes}_{i}",
            "motif": list(v),
            "identity_score": round(motif_identity(base[:len(v)], v), 4),
            "fingerprint": fp,
        })
        seen[fp] = i
        out[i] = (v, meta)
    return out
