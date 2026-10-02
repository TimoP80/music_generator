# TIMBOR — AI Electronic Music Producer (low-power circuit edition)

A rule-based generative music engine that produces **complete, coherent tracks** —
not loops — for hardcore, rave, jungle, trance and related underground electronic
styles. Pure Python + numpy, no DSP dependencies, renders a full track in seconds.

Musicality over randomness: every track is planned (genre → key → progression →
motif → arrangement → energy curve) **before** a single sample is synthesized,
then self-checked by a QC pass that repairs weak components.

## Quick start

```bash
python generate.py "dark gabber anthem, 1993 Rotterdam style" -o gabber.wav
python generate.py "euphoric uk hardcore" --seed 7 -o anthem.wav
python generate.py "old school jungle with amen breaks" -g jungle --bars 48
python generate.py "frenchcore + j-core fusion, playful" -o fusion.wav
python generate.py --list-genres
```

Analyze any output (energy curve, spectral balance, silence/clipping):

```bash
python analyze.py gabber.wav 14
```

## Sample library integration

Your own sample library becomes part of the generator. The engine plans *where
samples belong* in the SongPlan **before** rendering, selects them by musical
fit (explainable scoring), adapts BPM/key, chops them deterministically, and
mixes them on a dedicated bus — while procedural synthesis remains the backbone.

```bash
# one-time (and after adding files): index + analyze the library
python generate.py --index-samples "G:/Samples"

# hybrid generation with your samples
python generate.py "1994 Rotterdam gabber anthem, dark but euphoric" \
    --sample-dir "G:/Samples" --seed 424242 -o timbor_gabber.wav

# explain every selection decision
python generate.py "..." --sample-dir "G:/Samples" --sample-report

# library statistics
python generate.py --sample-report

# how strongly samples participate
--sample-mode off | subtle | balanced | heavy     # default: balanced
```

## Stems and reproducible projects

Every track can be exported as a **structured project** — stems plus a
versioned, machine-readable JSON that documents exactly how the audio was
constructed:

```bash
python generate.py \
    "1994 Rotterdam gabber anthem, dark but euphoric" \
    --sample-dir "data/demo_library" \
    --seed 424242 \
    --stems \
    -o projects/timbor_gabber/audio/master.wav
```

produces:

```
projects/timbor_gabber/
├── project.json
└── audio/
    ├── master.wav      (16-bit PCM, identical to a plain render)
    ├── drums.wav       (32-bit float stems, pre-master)
    ├── bass.wav
    ├── mel.wav
    ├── fx.wav
    └── samples.wav
```

- `--stems` is optional; plain commands still write a single WAV.
- Stems are post-mix-leveling but **pre-mastering**; only `master.wav` has the
  master chain applied (never double-applied). Summing the stems approximates
  the pre-master mix.
- `--stem-format int16` switches stems to 16-bit PCM if a tool prefers it.

### Inspecting a project

```bash
python generate.py --timeline-report projects/timbor_gabber/project.json
```

prints BPM/key/duration, the section map with energies, event counts per bus,
and every sample with its usage stats (uses / chopped / reversed / transposed).

### What project.json contains

`format: timbor-project, version: 1` with:

- **generator** — name, version, python, os, sample rate, seed, generated_at
  (timestamp is metadata only; rendering never depends on it)
- **song** — genre, bpm, key, scale, progression, the motif itself, duration
- **plan** — prompt, authenticity, era, fusion genres, motif variants A/B/C
- **sections** — name, index, start/end in beats, energy, bars
- **timeline** — every rendered event on the 16th-note grid in **beats**
  (canonical time; seconds derive from BPM): procedural notes with
  pitch/velocity/instrument, drum hits, stabs/pads, FX, and sample events with
  `sample_id`, `pitch_semitones`, `stretch_ratio`, `reverse`, `chop_ops`,
  `gain_db`
- **samples** — a reference snapshot of each used sample (id, project-relative
  path, size, mtime, bpm/key with confidences). No audio is embedded.
- **stems** — file list relative to the project root
- **qc** — all QC notes including stem checks

### Reproducibility

Same seed + same generator version + same sample files ⇒ byte-identical stems
and master (verified by test). Per-placement randomness derives from
`(seed, bar, role)` via crc32, so chopping/variation decisions are stable.
`project.json` differs between runs only in the `generated_at` timestamp.
Python/OS floating-point differences could in principle alter DSP output on
other platforms; musical (timeline) equivalence is guaranteed either way.

The `timbor/timeline/validation.py` module checks any timeline for negative
positions, invalid durations, bus names, section overlaps, sample references,
and BPM sanity, returning structured diagnostics.

> **Note:** TIMBOR project JSON describes the musical construction of a track;
> it does not contain copyrighted/source sample audio — only references and
> fingerprints of files in your own library.

### How selection works

Each candidate is scored against the current SongPlan:

```
sample_score = genre_match + category_match + bpm_match + key_match
             + energy_match + spectral_match + duration_fit
             + section_energy_fit + classification_confidence
```

The `--sample-report` output shows every factor for every selected sample.
Selection is deterministic per `--seed`.

### Adaptation

- **BPM**: WSOLA time-stretch with transient protection for small deviations
  (±10%); larger mismatches are bar-aligned instead of smearing the audio.
- **Key**: tonal samples (bass/acid/lead/chord/piano) are transposed to the
  SongPlan key only when key detection confidence ≥ 0.55; drums/FX are never
  pitch-matched.
- **Chopping**: slices on the musical grid with seeded reverse/reorder/repeat/
  mute/stutter operations; repetition detection transforms the loop after 3–6
  consecutive bars (mode-dependent).

### Supported formats and performance

- **WAV** (8/16/24/32-bit) and **AIFF**: full analysis + rendering.
- **FLAC / MP3**: indexed and classified (duration, filename tags) but not
  decoded — they are skipped for rendering. Convert to WAV to use them.
- Indexing is incremental (fingerprint = size + mtime); unchanged files are
  never re-analyzed. Generation-time decoding is lazy (only selected samples).
- A 15-file library indexes in <1 s; the analysis cost scales with library
  size but happens once.

### Automatic detection is uncertain — by design

BPM and key estimates carry confidence values and are honestly conservative:

- BPM: onset-envelope autocorrelation; octave errors (half/double-time) can
  occur on unusual material; confidence < 0.4 means the value is only a hint.
- Key: chroma + Krumhansl profiles; low confidence means no auto-transposition.
- Classification blends filename hints with audio features; neither is treated
  as authoritative. Renaming files can improve classification confidence.

### Limitations and troubleshooting

- FLAC/MP3 samples don't render (no bundled decoder) — convert to WAV.
- Very long files (>40 s) are truncated for analysis/rendering.
- Extreme stretch requests (>±10%) fall back to bar alignment (may feel
  loop-y but never smeared).
- If selections look wrong, run `--sample-report` to see the scoring, and
  `--index-samples` again after reorganizing folders with genre/style names.
- The index lives in `data/samples.db`; delete it to force a full rescan.

### Tests

```bash
python tests/test_samples.py
```

covers scanner (recursive scan, unsupported files, duplicates, changed files),
analysis (duration/BPM/key/features), selection determinism, transformations
(stretch/shift/align/chop determinism), hybrid integration renders for gabber,
jungle, trance and hard house, and backward compatibility.

## Genres

| genre        | bpm     | drums            | bass          | lead        |
|--------------|---------|------------------|---------------|-------------|
| gabber       | 160–200 | distorted kick   | rumble 16ths  | hoover      |
| frenchcore   | 180–220 | distorted kick   | rumble 16ths  | supersaw    |
| uk_hardcore  | 165–175 | 4×4 hardcore     | dist off-8th  | supersaw    |
| freeform     | 170–180 | dnb + hardcore   | acid          | supersaw    |
| jcore        | 180–200 | hardcore         | FM 16ths      | square lead |
| dnb          | 160–180 | breakbeat        | reese         | hoover      |
| jungle       | 155–170 | chopped breaks   | reese         | stabs       |
| hard_house   | 145–155 | 4×4 hardcore     | reverse bass  | stabs       |
| trance       | 135–142 | 4×4 clean        | FM off-8th    | supersaw    |
| acid_trance  | 138–145 | 4×4 clean        | acid 303      | supersaw    |
| big_beat     | 120–130 | fat breaks       | dist acid     | square lead |
| rave         | 150–165 | 4×4 + break chop | reese         | hoover      |

Fusion works by keyword detection (`"jungle + hardcore"`, `"hard house with 90s
trance melodies"`): the primary genre drives rhythm/bass, secondaries contribute
melodic vocabulary and atmosphere.

## CLI reference

```
prompt              free-text brief (genre/mood/era/keywords are parsed)
-g, --genre         force one of the 12 genre packs
-b, --bpm           tempo override
-k, --key           e.g. "A minor" or "F#:phrygian"
-m, --mood          dark | euphoric | fun | cinematic
-a, --authenticity  authentic | modern | hybrid | experimental
-s, --seed          reproducible generation
--bars              cap total bars (default 48–64, full arrangement)
--plan-only         print the musical plan without rendering
--index-samples DIR index + analyze a sample library into data/samples.db
--sample-dir DIR    hybrid generation using DIR as the sample library
--sample-mode M     off | subtle | balanced | heavy (default balanced)
--sample-report     explain every sample selection decision
--stems             write per-bus stems next to the master
--stem-format F     float32 (default) | int16
--timeline-report P print musical stats for a project.json

--re-render P       rebuild a project from project.json (no SongPlan needed)
--verify-render     after --re-render: diff rebuilt buses against stored stems
--sample-root PATH  override where re-render/verify resolve library samples
--output DIR        destination for re-rendered audio (default: in place)
--export-midi P     write MIDI (format 1, 480 PPQ) into <root>/midi/
--export-manifest P write the DAW-neutral manifest into <root>/export/
--export-reaper P   write a REAPER .rpp into <root>/reaper/
--export-ableton P  write the Ableton import kit into <root>/ableton/

--generate-album C  generate a full EP/album from an album config JSON
--album-plan-only   with --generate-album: plan documents only, no audio
--out-root DIR      root for generated albums (default: albums/)
--album-report A    print album summary + re-validated continuity; no re-render
--album-manifest A  write the EP-level manifest; no regeneration
--regen-track A     regenerate ONE track (with --track-position N, 0-based)
--album-db PATH     sample index DB override for album mode
```

## Architecture

```
generate.py        CLI: prompt → Plan → render → stereo WAV
timbor/
  theory.py        scales, chords, genre progressions, motif engine
                   (generate / variate / identity scoring)
  dsp.py           oscillators, FFT filters, delay/reverb/drive/bitcrush,
                   drum synthesis (gabber kick, 909 set, hats, crash…),
                   hoover, reese, acid, reverse bass, pads, stabs, risers
  voices.py        note-event → waveform layer + breakbeat pattern engine
                   (jungle/dnb/bigbeat/hardcore) + break chopping
  engine.py        genre packs, SongPlan (arrangement + energy curve),
                   section renderer into 5 buses, QC + repair, sidechain
  render.py        stereo imaging, master bus, 16-bit WAV export
  samples/
    metadata.py    SampleMetadata model (features + confidence values)
    scanner.py     recursive discovery, cheap headers, lazy decoding
    cache.py       SQLite index (data/samples.db), fingerprint skipping
    analyzer.py    BPM (onset autocorrelation), key (chroma/Krumhansl),
                   spectral + transient features
    classifier.py  filename hints + audio features → category/genre tags
    index.py       scan → diff → analyze → classify → persist
    selector.py    explainable SongPlan-fit scoring, role planning
    transformer.py WSOLA stretch, pitch shift, bar alignment, chop ops
    render.py      sample bus rendering (decide + execute), repetition
                   control, sample QC
  timeline/
    events.py      canonical TimelineEvent (beat-based, JSON round trip)
    timeline.py    Timeline + SectionSpan + SampleUse registry
    serialization.py  versioned project.json (format v1) save/load
    validation.py  structured timeline diagnostics
    project.py     project directory writer + timeline report
  replay.py        project.json → buses (the live render path, re-executed)
  verify.py        re-render vs stored-stem diffing (tiered) + report
  export/
    midi.py        Standard MIDI File format 1, 480 PPQ (drums/bass/mel/chords)
    manifest.py    DAW-neutral manifest.json (construction snapshot)
    reaper.py      REAPER .rpp project with stem items + markers + sample events
    ableton.py     Ableton import kit (manifest + MIDI + stems.md + samples.csv)
  album/
    dna.py         AlbumConfig, SHA-256 seed derivation, AlbumDNA identity
    motif.py       album motif lineage: variants, identity floor, dedup
    palette.py     shared sample palette (strict / balanced / loose modes)
    planning.py    BPM neighborhood (genre-snapped), durations → bars, Plans
    generator.py   orchestration: DNA → lineage + palette → projects
    validation.py  continuity checks (key/BPM/motif/distinctness/palette)
    serialization.py  versioned album.json (format v1) save/load
    manifest.py    EP-level manifest with palette provenance
analyze.py         output sanity checks
tests/             sample, timeline, export/re-render and album suites
```

Render buses: `drums`, `bass`, `mel`, `fx`, `samples` (→ master). The `samples`
bus keeps the architecture ready for future stem export.

### How the spec maps to code

- **Motif identity (§4, §13)** — one motif per track; sections use A / A' / B
  (transposed) / C (inverted or retrograde) variants; QC scores contour+rhythm
  similarity and regenerates variants that drift too far.
- **Arrangement & energy (§8, §9)** — intro → groove → theme → dev → break →
  build → drop → variation → break2 → build2 → drop2 → outro, with per-section
  layer sets and energy values driving levels, filtering and FX.
- **Kick/bass coherence (§6, §14)** — bass patterns are planned against the kick
  grid per style (off-8th, roll16, jungle syncopation…), sidechain ducking under
  the kick envelope, RMS-based genre-aware bus leveling.
- **Break intelligence (§7)** — breaks are re-randomized per bar and re-chopped
  (slice shuffle, occasional reversal) — never one static loop.
- **Era vocabulary (§10, §17)** — `1990-1995` adds drive, HP filtering and vinyl
  crackle; `authentic` forces historical eras; `experimental` injects controlled
  bitcrush/reverse/stutter bars.
- **QC (§19)** — silence check with layer regeneration, motif-identity check,
  kick/bass low-end clash repair (all logged to the console).
- **Production (§14)** — genre-anchored bus leveling, tanh peak control, mono
  bass, widened mids/highs, RMS-targeted master with peak safety, zero clipping.

## Design notes

- All synthesis is deterministic per `--seed`; the same seed + flags always
  produce the same track.
- Filtering is FFT-based (zero-phase), so renders are fast — a 2-minute track
  renders in ~10 s on a laptop.
- The default output is 44.1 kHz 16-bit stereo WAV.

## Phase 4 — Re-render, verification, MIDI and DAW export

The Timeline in `project.json` is the canonical interchange layer. Everything
below is built by **re-executing the same render path** the generator used —
there is no second renderer and no lossy conversion.

### Re-render and verification

```bash
# rebuild every bus from the stored timeline, byte-identical tooling:
python generate.py --re-render projects/timbor_gabber/project.json

# rebuild + diff against the stems shipped in audio/:
python generate.py --re-render projects/timbor_gabber/project.json --verify-render
```

`--re-render` loads the project, replays the timeline through the live engine
path (`timbor/replay.py` — same section offsets, same bus DSP, same sample
transformations), and rewrites `audio/master.wav` and the stems. `--verify-render`
adds a per-bus comparison of the rebuilt audio against the stored stems and
prints a tiered report:

```text
TIMBOR RE-RENDER VERIFICATION
Drums     IDENTICAL          ← bit-exact (int16/float32 WAV round trip)
Bass      FLOAT-EQUIV (…e-16) ← differs only by <1e-9 float round-off
Fx        IDENTICAL
Mel       APPROX (1.3e-01)    ← documented, see limitations
RESULT: PASS | DIFFERENT
```

Tiers: **IDENTICAL** (bit-exact), **FLOAT-EQUIV** (max|diff| ≤ 1e-9, i.e. WAV
storage rounding only), **APPROX** (≤ 0.5, audible-none), **DIFFERENT**
(real musical difference → verify fails). The comparison basis is exact: bus
stems are the pre-stereoize mono bus, so they are diffed directly; `master.wav`
is diffed through the same stereoize + master chain. Buses are quantized to
float32 before diffing so storage rounding (~3e-8) is never misreported.

Verification also resolves every sample reference (`--sample-root` overrides
the library location) and re-runs timeline validation; missing samples or
validation errors fail the report regardless of audio.

Replay fidelity bugs this tooling flushed out (and why it exists): section
offsets now accumulate by truncation like `finalize_mix` (a 1-sample drift per
section previously broke jungle/trance), FX are placed on section offsets with
risers at section *end*, and fractional `dur_steps` (jungle bass 2.5-step
notes) round-trip through event metadata.

### MIDI export

```bash
python generate.py --export-midi projects/timbor_gabber/project.json
```

writes `<root>/midi/<name>.mid` — Standard MIDI File **format 1, 480 PPQ**:

- Track 0 — tempo, time signature, key signature, one marker per section
- Drums — channel 10 (GM percussion map)
- Bass / Melody / Chords — channels 1 / 2 / 3 at written pitch
- FX and sample events — text-meta events (place, reverse, gain) on a notes track

MIDI is a *replayable sketch*, not a mix: instrument sounds, gains and bus
processing stay in TIMBOR. Byte-identical for the same project; `--midi-format`
accepts format 1 only.

### DAW-neutral manifest

```bash
python generate.py --export-manifest projects/timbor_gabber/project.json
```

writes `<root>/export/<name>_export.json` — a construction snapshot: song
summary, section map, per-bus event counts, stem file list, full sample usage
(id, path, adaptation values, per-use placements) and generator provenance.
Deterministic bytes (`sort_keys`, no timestamps) so it diffs cleanly in git.

### REAPER export

```bash
python generate.py --export-reaper projects/timbor_gabber/project.json
```

writes `<root>/reaper/<name>.rpp`: one track per bus with the rendered stem as
a media item, section **markers at exact seconds**, correct sample rate/tempo/
length, and a **TIMBOR Sample Events** track listing every library sample use
(position, PLAYRATE, PITCH, reverse notes). Paths are relative
(`../audio/drums.wav`), forward-slashed, so the project relocates with its
folder. Open in REAPER and you get the arrangement instantly.

### Ableton Live export

```bash
python generate.py --export-ableton projects/timbor_gabber/project.json
```

writes `<root>/ableton/` — an *import kit*, not a faked session file:

| file            | purpose                                             |
|-----------------|-----------------------------------------------------|
| `README.md`     | step-by-step import instructions for Live           |
| `manifest.json` | the DAW-neutral manifest (same deterministic bytes)  |
| `midi/<name>.mid`| the MIDI export (byte-identical to `midi/`)        |
| `stems.md`      | stem → track reference table with durations         |
| `samples.csv`   | every library-sample placement as importable rows   |

`.als` is **intentionally not synthesized**: it is an undocumented,
GZip-wrapped Live-set format that changes between versions — a fragile fake
would waste your time in a DAW. The kit plus Live's *drag-and-drop MIDI import*
gets you to a working session in minutes.

### Phase-4 regression record

Recorded in `regression_before.json` (phase start) and `regression_after.json`
(final code, seed 424242, md5):

| project    | master before   | master after    |
|------------|-----------------|-----------------|
| gabber     | `5930c5ea…` (1503 events) | `4a86cc80…` (1574 events) |
| jungle     | `ccb37f2c…` (1060 events) | `a74d0dbe…` (1074 events) |
| trance     | `c38d9630…`     | `db7f1278…`     |
| hard_house | `22cd4ea4…`     | `35cc8969…`     |

**Honest reading:** masters changed versus phase start — and were expected to.
The phase fixed real timeline-builder/replay fidelity bugs (section-offset
cumulation, FX end placement, `dur_steps` round trip), so event counts moved
(gabber 1503 → 1574) and the shipped projects were regenerated with the final
code. The meaningful regression proof is *intra-phase*: with final code,
repeated renders are byte-identical, `--verify-render` returns drums/bass/fx/
samples IDENTICAL and mel/bass FLOAT-EQUIV-or-APPROX on all four genres, and
all six test suites pass. No musical decision (genre packs, motifs, QC, master
chain) changed in this phase.

### Phase-4 limitations

- **APPROX tier on `mel` (gabber ≈ 0.126)** — the drop-section melody doubling
  (`content*0.8 + HP-filtered(content)*0.4` applied to the whole bus) is a mix
  transform not fully recoverable from stored note events; replay reconstructs
  it from the notes, leaving a small residual. Musical timing and content are
  identical. Jungle/hard_house residuals are ≈ 3e-4.
- **APPROX on `bass` (jungle/trance ≈ 0.05)** — interplay of fractional
  `dur_steps` rounding and WSOLA sample adaptation.
- **RESULT: DIFFERENT exit code** — by design, verification PASSES only on
  exact/float-equiv buses; approx is reported, not forgiven.
- **No `.als`** — deliberate (see above).
- **MIDI/manifest are snapshots** — they do not re-synthesize audio; the
  re-render path is the only audio regenerator.
- **Windows**: rewriting a WAV that was just read can transiently raise
  `PermissionError`; re-running the command succeeds.

## Phase 5 — EP / album generation with shared musical DNA

An orchestration layer **above** the single-song pipeline. The single-song flow
remains authoritative and byte-for-byte unchanged (phase-4 test hashes are
identical); the album layer drives it with shared constraints.

```text
album config + master seed
    ↓ SHA-256-derived child seeds (stable per track label)
AlbumDNA  (key/mode, BPM neighborhood, progression family, Motif A,
           rhythm fingerprint, era/mood, signature instruments)
    ↓                         ↘
motif lineage (A, A', A''…)    shared sample palette (built once)
    ↓                         ↘
per-track engine.Plan → render_track → independent projects + exports
    ↓
continuity validation + album.json + EP manifest
```

### Album configuration

```json
{
  "name": "TIMBOR — Machine Rave EP",
  "seed": 424242,
  "tracks": 4,
  "genres": ["gabber", "jungle", "trance", "hard_house"],
  "bpm_center": 170, "bpm_range": 12,
  "key": "D", "mode": "minor", "mood": "dark",
  "duration_range": [45, 75],
  "motif_strategy": "family",
  "divergence": "medium",
  "shared_palette": true,
  "palette_mode": "balanced",
  "sample_dir": "data/demo_library"
}
```

A shipped example lives at [albums/machine-rave-ep.json](albums/machine-rave-ep.json).
All fields are optional except name/seed/tracks; per-track overrides, custom
titles and explicit prompts are supported (see `AlbumConfig` in
`timbor/album/dna.py`).

### Usage

```bash
# full EP: 4 projects + all phase-4 exports + album.json + manifest
python generate.py --generate-album albums/machine-rave-ep.json --out-root albums

# plan documents only (no audio, spec §25-style inspection)
python generate.py --generate-album albums/machine-rave-ep.json --album-plan-only

# print summary + re-validated continuity (never regenerates)
python generate.py --album-report albums/timbor-machine-rave-ep/album.json

# write the EP-level manifest (never regenerates)
python generate.py --album-manifest albums/timbor-machine-rave-ep/album.json

# rebuild ONE track byte-identically; siblings untouched
python generate.py --regen-track albums/timbor-machine-rave-ep/album.json --track-position 1

# phase-4 verification works on album children like any project
python generate.py --re-render albums/timbor-machine-rave-ep/01/project.json --verify-render
```

Resulting layout: `albums/<slug>/album.json` plus per-track directories with
`project.json`, `audio/` stems, and the full `midi/ export/ reaper/ ableton/`
export suite; `<slug>_manifest.json` ties them together.

### Determinism model

* Seeds derive by SHA-256 from `(album_seed, label)` — track N's seed depends
  only on its own label, so adding or changing a later track never reshuffles
  earlier ones (no sequential RNG state).
* The DNA, Motif A, every lineage variant, the palette and every per-track
  BPM/duration are pure functions of (config, seed, library).
* Full-tree test: two independent generations of the same album are
  **byte-identical** across all audio and export files (JSON documents equal
  modulo `generated_at`, which is metadata only — the phase-4 convention).
* `--regen-track` reproduces the original track's master exactly because
  every input is recorded in album.json.

### Motif lineage (spec §6–§8)

Track 1 states Motif A untouched. Later tracks derive labeled variants
(A'_1, A'_2 …) through documented transformation chains: transpose, invert,
retrograde, octave shift, rhythmic augment, note omission/insertion,
fragmentation, call/response, arpeggiation, and genre rhythmic adaptation
(e.g. gabber rest-fill, jungle syncopation). Guarantees, enforced at
derivation time:

* **identity floor** — `motif_identity(base, variant) ≥ 0.35` (a light
  transformation fallback, then a guaranteed-safe transpose, keep the family
  recognizable); track 0 always scores 1.0;
* **uniqueness** — fingerprint collisions resolve with deterministic
  re-transposition, so no two tracks share a byte-identical melody;
* every track records `family / parent / variant / identity_score /
  fingerprint / transformations` in album.json, its own project.json
  (`metadata.album.lineage`) and the manifest.

Identity scoring reuses `theory.motif_identity` (interval contour + rhythm),
compared on the same grid length the engine will render.

### Shared sample palette (spec §9–§11)

Built **once** from the existing sample index + `score_sample` scoring against
album-level pseudo-songs (one per distinct album genre — a sample qualifies if
it fits the EP somewhere). Modes:

* `strict` — 7 gated roles; every major sample slot comes from the palette;
* `balanced` (default) — 4 signature roles (main break, impact, vocal hit,
  melodic loop) are shared anchors, everything else stays per-track;
* `loose` — only the album break is shared.

Gated roles are enforced by substitution in `engine._apply_palette` after each
track's own selection (no track is forced to reuse every sample — spec §10).
Because placement/adaptation runs inside each track's own BPM/key context, the
same break lands differently in a 170 gabber track and a 142 trance track.
The manifest records full provenance per entry: id, path, category, BPM, key,
selection score, roles, **tracks using it and placement counts** (e.g. the
demo EP's amen break: 4/4 tracks, 112 placements).

### Continuity validation (spec §15)

`validate_album` returns structured issues, never subjective scores:

| check | severity |
|---|---|
| duplicate sibling seeds | error |
| key/mode drift from the DNA center | error |
| motif identity below 0.35 / duplicate variants | error |
| rendered project bpm/motif/key vs album records | error |
| palette entry used by no track | warning |
| palette entry below majority reach | warning |
| BPM outside the neighborhood (genre-snapped) | warning |
| identical genre+BPM track signatures | warning |
| duration outside configured window (±40% tolerance) | warning |

`--album-report` re-runs the checks against the projects currently on disk —
inspection never regenerates audio.

### BPM neighborhood

Per-track BPM = center ± seeded jitter, **snapped to the genre pack's
authentic range** when the two conflict (a trance track stays at 135–142 even
if the album center is 170 — musical reasonableness wins over geometry, spec
§12). Out-of-window snaps are reported as warnings, not hidden.

### Tests

```bash
python tests/test_album_dna.py        # config, seed derivation, DNA
python tests/test_album_motifs.py     # lineage, identity floor, uniqueness
python tests/test_album_palette.py    # modes, provenance, path matching
python tests/test_album_planning.py   # BPM neighborhood, durations, plans
python tests/test_album_generation.py # end-to-end render + determinism + regen
python tests/test_album_manifest.py   # EP manifest + provenance
python tests/test_album_validation.py # continuity checks + album.json gate
```

### Phase-5 limitations

* Album tracks with very short durations (≈30 s) still render full
  arrangements (the single-song pipeline's floor), so durations can exceed the
  configured window at small ranges.
* Palette scoring compares candidates against the album's *distinct* genres;
  a palette mode `strict` on a wide-genre album can produce unused entries
  (reported as warnings per spec §10's no-forcing rule).
* `regen_track` replays from the stored lineage variant in album.json; if you
  hand-edit that JSON, the rebuild follows the edit (the record is the
  contract).
* The album layer inherits the phase-4 verify tiers as-is (mel doubling
  residual, jungle/trance bass ≈0.05–0.08).

## Phase 6 — Album sequencing, mastering & release assembly

Phase 6 turns a set of independently rendered track masters into a
coherent, reproducible album release. It is **assembly-only**: no module
in this layer regenerates music, calls the song engine, or writes to a
track project. The individual masters remain the canonical artifacts; the
album is a deterministic derived artifact.

### Modules

```text
timbor/album/
    sequencing.py    SequenceConfig, build_sequence, validate_sequence
    transitions.py   Transition dataclass, quantize_duration, crossfade
                     curves, boundary planning + click diagnostics
    loudness.py      K-weighted gated loudness (ITU BS.1770-style),
                     short-term series, LRA, true-peak approx, WAV decode
    mastering.py     preserve/match/target gain policy, block-lookahead
                     album limiter (explicit, default OFF)
    release.py       render_album, album QC, verify_album, cue sheet,
                     tracklist, release manifest/README, packaging
```

### Sequencing

An optional `sequencing` section in album.json controls assembly:

```json
{
  "sequencing": {
    "enabled": true,
    "transition_mode": "gap",
    "gap_seconds": 2.0,
    "fade_in_seconds": 0.0,
    "fade_out_seconds": 0.0,
    "preserve_track_boundaries": true
  },
  "transition": {
    "quantize": "bar",
    "crossfade_seconds": 4.0,
    "curve": "equal_power"
  }
}
```

* **Modes**: `gap` (configurable silence between tracks, the default),
  `crossfade` (boundaries overlap by the configured duration), and
  `continuous` (hard cut, DJ/rave-style back-to-back).
* **Ordering** is explicit (`sequencing.order`, list of track numbers).
  Filesystem listings are never used to infer order. Reordering changes
  only the sequence — seeds, motifs, plans and renders are untouched.
* **Integer-sample positioning**: all boundaries are computed in samples
  (start/end/overlap/gap), never by accumulating floating-point seconds.
  `sequence.json` stores both samples and seconds for display.
* **Quantization**: `none | beat | bar | phrase` — transition durations
  snap **up** to the next grid multiple derived from the source track's
  BPM. Both source and destination BPMs are recorded per transition;
  mismatches are flagged, never silently rounded.
* **Validation** (`validate_sequence`) rejects negative positions,
  unintentional truncation (end−start ≠ source length), totals that don't
  match the plan, stale transition positions after reordering, and
  overlaps exceeding half of the shorter track.

### Transitions

`Transition` types: `hard_cut`, `gap`, `linear_crossfade`,
`equal_power_crossfade` (default type: `gap`; mode+curve select the
crossfade type automatically). Crossfades use amplitude-domain
cos/sin curves so summed power is constant (equal-power) or a linear
ramp. `plan_boundary` **raises** `ValueError` when a requested crossfade
exceeds half of the shorter master instead of clamping silently.
`measure_boundary` performs click diagnostics around each seam: the
sample-step discontinuity at the boundary is compared against the local
context (±500 ms, excluding the seam itself) with absolute thresholds for
silent context and ratio thresholds for loud context — producing
PASS / WARN / ERROR per boundary.

### Loudness analysis

`loudness.py` implements an in-process **K-weighted, gated loudness
measure** (`itu_bs1770_style_k_weighted_gated`): the BS.1770 K-weighting
biquad cascade converted to an exact 8192-tap FIR applied via bounded-
memory overlap-add, 400 ms blocks with 75 % overlap and the standard
absolute (−70 LUFS) + relative (−10 LU) gating. Also reported: peak dBFS,
true-peak **approximation** (4× linear interpolation — clearly labelled
as approximate, not a certified oversampled meter), RMS (never labelled
LUFS), short-term loudness series (3 s window / 1 s hop), loudness range,
duration, and leading/trailing silence. Decode supports 8/16/24/32-float
WAV, mono or stereo. All analysis is deterministic; long inputs are
processed in chunks to bound memory.

### Album loudness policy

```json
{ "loudness": { "mode": "preserve", "limiter": false } }
```

* **preserve** (default): masters are used unchanged.
* **match**: anchors on the loudest track (0 dB), leaves tracks within
  `match_window_db` (default 3 LU) untouched, lifts quieter tracks toward
  the anchor (boost only, capped at 3.0 dB, warned above 6 LU).
* **target**: applies a fixed offset toward `target_lufs` (default −14),
  same boost cap.

Gains are recorded per track (`album_gain_db`) and applied in the
assembly chain — the source WAVs are never modified. If assembly would
clip, QC reports it; a deterministic block-lookahead limiter is applied
only when `limiter: true` is explicitly set, and it holds the configured
celing (`limiter_ceiling_db`, default −1.0 dBTP).

### Album render + release artifacts

`--render-album` reads every `tracks[n]/audio/master.wav` and writes:

```text
albums/<album>/album/
    album.wav               32-bit float album master
    album_int16.wav         16-bit delivery copy
    sequence.json           canonical sequence (samples + seconds)
    loudness.json           per-track + album measurements
    qc.json                 structure/audio/boundary/QC checks
    album.cue               MM:SS:FF (75 fps) cue sheet
    tracklist.json          machine-readable tracklist
    release_manifest.json   hashes + provenance
    README.md               deterministic, timestamp-free release notes
albums/<album>/release/     packaged copy (manifest.json + artifacts,
                            optionally tracks/ with --include-track-masters)
```

The render is **byte-deterministic**: same masters + same configuration →
byte-identical `album.wav` (verified by md5 in the test suite; the EP's
gap-mode master is `aef39072b6…`). `verify_album` re-assembles in chunks
and compares at three tiers: `IDENTICAL` (bitwise), `FLOAT-EQUIVALENT`
(≤1e-9), `DIFFERENT`. `verify_master_immutability` hashes every source
master before and after assembly.

### CLI

```bash
python generate.py --sequence-album albums/timbor-machine-rave-ep
python generate.py --render-album   albums/timbor-machine-rave-ep
python generate.py --verify-album   albums/timbor-machine-rave-ep
python generate.py --album-loudness albums/timbor-machine-rave-ep
python generate.py --package-album  albums/timbor-machine-rave-ep \
       --include-track-masters
```

### Album Studio (web interface)

`studio/server.py` (stdlib-only `http.server`) serves the TIMBOR Album
Studio — a 10-screen production workstation (Overview, DNA, Tracks,
Motifs, Sample Library, Sample Palette, Sequence, Loudness, QC, Release)
over real album and sample-index data. It exposes read APIs for
album/sequence/loudness/QC/manifest documents, WAV streaming with HTTP
Range support, waveform peak extraction, and job-based long operations
(`render`, `verify`, `loudness`, `package`, `resequence`) that shell out to
`generate.py`. Sequence and loudness edits write real configuration back
into album.json; the Python engine remains authoritative.

#### Sample Library setup and workflow

Start Studio from the project root:

```bash
python studio/server.py --port 8765
```

Open **Sample Library**, enter an existing directory path, choose **+ Add
Library**, then press **Scan / Rescan**. Roots are global to the local Studio
instance; sample files stay at their original locations. Scans run in a
background worker, compare existing size/mtime fingerprints, re-analyze only
new or modified files, delete stale index rows for files missing from that
root, and calculate stable exact-content duplicate groups. The UI reports
scan progress and current file; cancellation is not supported.

### Music creation

The **NEW MUSIC** workspace creates a standalone TIMBOR track without changing
the active album. Describe the sound, then optionally set genre, mood, BPM,
key, bar count, sample blend/library, and a reproducible seed. Generation runs
in the background; finished takes can be auditioned in the shared transport or
downloaded as WAV. Each take is saved beneath `projects/created/` with project
and stem artifacts for procedural renders.

TIMBOR's local procedural engine is the default and has no cloud inference
cost. Stable Audio 3 appears only when its opt-in Modal URL/key are configured
and `STABLE_AUDIO_ENABLED=true`; remote inference can incur Modal charges and
requires authorized Hugging Face model access. It is intentionally unavailable
until those checks pass. Local sample libraries are used only by the procedural
renderer; they are never uploaded to Modal.

#### Stable Audio 3 — serverless Modal generation (opt-in)

The optional remote mode uses Stability AI's official Stable Audio 3 inference
implementation through an on-demand Modal service. It is CPU-only Small Music
(`stabilityai/stable-audio-3-small-music`, up to 120 seconds) and pins official
source commit `3a82c807b69cf4b7c5c05270011a5d5e47abac18`. The checkpoint is
gated: accept applicable Stability AI and text-encoder licenses and confirm
Hugging Face account access before deployment. The Modal `huggingface-token`
secret must provide `HF_TOKEN`.

```bash
modal deploy modal/stable_audio.py
```

The deploy code creates a random bearer credential in ignored local `.env` and
passes it to Modal as an in-memory Secret without printing it. Configure the
returned endpoint as `STABLE_AUDIO_MODAL_URL`; set `STABLE_AUDIO_ENABLED=true`
to opt in. Use the `*.modal.run` invocation URL printed by `modal deploy`
(for this app, `https://<workspace>--timbor-stable-audio-3-stableaudioservice-web.modal.run`),
not the `modal.com/apps/...` dashboard page, which answers POST with HTTP 405.
A trailing `/generate` or slash is trimmed automatically. Never commit or share
`.env`. Remote generation may incur Modal charges; no inference occurs on the
default procedural path.

The CLI supports text-to-audio, audio-to-audio, and inpainting/continuation.
Only Small Music and CPU are enabled. Conditioning uploads one WAV, not a sample
library. Generated WAVs and JSON metadata are written only after local format,
duration, and signal validation. The client avoids automatic retries after
timeouts to prevent duplicate paid inference.

```powershell
python generate.py "dark Rotterdam rave" --stable-audio --genre gabber `
  --bpm 190 --duration 30 --seed 12345 -o stable_audio.wav
```

Cloud generation remains blocked until gated Hugging Face access has been
granted; no successful cloud output or latency is claimed until a real model
WAV is received and validated locally.

#### YuE2 and ACE-Step — separate opt-in Modal GPU endpoints

YuE2 and ACE-Step are independent full-track generation services. They do not
replace or alter TIMBOR's deterministic local renderer, do not use local sample
libraries, and are never selected unless explicitly enabled. Each service uses
its own bearer key and endpoint; configure one provider without setting the
other. Modal GPU time, cold starts, storage, and model downloads may incur
charges. Studio limits remote requests to one take at a time to avoid accidental
batch billing. Generated duration is validated from the actual WAV. For YuE2,
the requested duration is a target/metadata only because its pipeline does not
accept an exact output-duration parameter.

YuE2 is deployed from the official `multimodal-art-projection/YuE` source at
`yue2-v0.1.6`, using `m-a-p/YuE2-3B` and the YuE2 VAE on an L40S by default.
The model weights are CC BY-NC 4.0 with additional creator terms. This TIMBOR
configuration is labeled for personal/noncommercial use only unless you obtain
a separate license; review and follow the model's current license and creator
terms before using it. ACE-Step is deployed from the official
`ace-step/ACE-Step-1.5` source at `v0.1.8`, with the 0.6B language-model
backend selected by default. Both services currently expose text-to-music
only and return validated 48 kHz stereo WAV.

From the repository root, run `python modal/configure_music_models.py`. It
creates missing separate API keys in the ignored `.env`, deploys both apps, and
stores each printed Modal invocation URL without exposing the keys. Modal CLI
and account setup must already be available (`modal setup`). To only create
keys/configuration without deploying, use:

```bash
python modal/configure_music_models.py --configure-only
```

Deployments remain opt-in; pass `--enable` only if you want
the script to set both `*_ENABLED=true` after both deployments succeed.

Alternatively, deploy each endpoint independently with `modal deploy
modal/yue_engine.py` and `modal deploy modal/ace_step_engine.py`, then place each
printed `*.modal.run` invocation URL in its matching `YUE2_MODAL_URL` or
`ACESTEP_MODAL_URL`. Do not use a Modal dashboard URL, and never commit or share
`.env`. GPU, CPU, memory, idle timeout, duration limits, and (for ACE-Step) the
LM model can be adjusted with matching `.env` variables; deployment settings
are read at deploy time, so redeploy after changing them. Check Modal logs and
billing before increasing resource limits or duration.

The YuE2 weights have noncommercial license restrictions unless separately
licensed. Review all model terms before use.

```bash
python generate.py "1994 Rotterdam gabber anthem, dark but euphoric" \\
  --engine yue2 --duration 180 --lyrics "[Chorus] Raise the signal" \\
  --seed 424242 -o yue2.wav
python generate.py "dark warehouse rave" --engine acestep \\
  --duration 30 --seed 424242 -o acestep.wav
```

Each remote CLI request writes a WAV and `.wav.json` metadata sidecar only
after format, stereo/sample-rate, duration-limit, and signal checks pass.
Timeouts are not automatically retried, to avoid duplicate paid inference.
No actual YuE2 or ACE-Step deployment or cloud generation is claimed until a
real endpoint responds and its WAV passes local validation.

If a cloud take hangs and then fails, the container is usually crash-looping
rather than merely slow: `modal app logs` shows the reason, most often a 403
`GatedRepoError` because the account whose token is stored in the
`huggingface-token` secret has not been granted access to the gated checkpoint.
The deployed service reports this directly: `GET /health` returns 503 with the
load error, and `GET /diagnostic` includes `model_loaded` and
`model_load_error`, so the failure is visible instead of surfacing as a bare
timeout.

### Sample Library setup and workflow

Start Studio from the project root:

```bash
python studio/server.py --port 8765
```

Open the browser at `http://127.0.0.1:8765`. The **NEW MUSIC** section is the
landing screen; compose a prompt, adjust style and arrangement controls, then
press **CREATE TRACK**. Completed takes can be previewed or downloaded, and
remain independent of the selected album.

The Studio uses TIMBOR's `SampleIndex`, scanner, analyzer, and classifier.
Search, filters, sorting, and pagination execute against SQLite; only the
requested page is sent to the browser. The inspector reports stored sample
metadata and analysis features. WAV waveforms are extracted from actual PCM
samples and cached by file mtime; WAV preview streams through the shared
transport with HTTP Range support. AIFF analysis/playback is available only
when the current Python runtime supplies the `aifc` decoder. FLAC and MP3
are indexed with available header/filename metadata but need a decoder for
analysis/rendering and are not browser-previewable in the current backend.
Studio does not claim these formats are decoded.

**Add to Palette** changes the active album's explicit palette only; it does
not mutate the shared library, generation seed, project audio, or rendered
masters. Role/category compatibility is validated against TIMBOR's palette
and selector role definitions. Palette edits are future generation inputs and
do not silently regenerate tracks. The Palette screen shows actual project
placements, roles, and transformations from project provenance. Selector
component scores and per-placement reasons are shown only if stored by project
provenance; the UI does not invent them.

Sample IDs are opaque root-relative tokens. Media resolution accepts only
registered-library IDs for previews and verifies canonical paths remain under
the registered root; symlinks and Windows junctions are not traversed by the
scanner. Existing `/media/` track playback remains constrained to the active
album. Removing a root unregisters it from Studio without deleting source
files or indexed rows; those rows are hidden from current library search until
the root is registered again. No arbitrary filesystem browsing endpoint is
exposed.

Troubleshooting: check **Index Statistics** and each format's decoder status;
verify the selected root exists and is readable; press **Scan / Rescan** after
file edits; convert FLAC/MP3 to WAV where audio analysis or preview is needed.
The default database remains `data/samples.db`; root configuration is stored
in `data/sample_libraries.json` (both are local runtime state, not source files).

### Tests

```bash
python tests/test_album_sequencing.py  # ordering, timing, gaps, modes
python tests/test_album_transitions.py # quantize, curves, overlap guard
python tests/test_album_loudness.py    # determinism, gates, policies
python tests/test_album_render.py      # determinism, immutability, verify
python tests/test_album_release.py     # artifacts, package, reproducibility
```

### Phase-6 limitations

* The loudness measure is an in-process BS.1770-style implementation, not
  a certified meter; true peak is a 4× linear-interpolation approximation.
* Match/target gains are boost-only (cap 3.0 dB) by design — no
  attenuation of louder masters, no streaming-service presets.
* Transitions are the four basic types; beat-grid alignment quantizes
  duration but does not beat-match playback between differing BPMs.
* Deferred per spec §37: MP3/AAC encode, vinyl/CD-PQ mastering beyond the
  cue sheet, Atmos/surround, artwork, metadata submission, generative
  transition music.
