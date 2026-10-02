# AGENTS.md — TIMBOR

TIMBOR is a rule-based generative electronic-music engine: it plans a track
(genre → key → progression → motif → arrangement → energy curve) before
synthesizing a single sample, then renders a full track in seconds. It also
generates multi-track EPs/albums with shared musical DNA, exports to DAWs
(MIDI / REAPER / Ableton), and ships a local web UI ("Album Studio").

The root `README.md` is a long, authoritative spec-style document (phases 1–6,
CLI reference, architecture, determinism guarantees, limitations). Read it for
design intent; this file covers what an agent needs to *operate* in the repo.

## Environment

- Pure Python + **numpy**. No `requirements.txt` / `pyproject.toml` / build step.
- `numpy` is required. `pytest` is used for tests. `modal` is optional (only the
  opt-in Stable Audio cloud path).
- Run everything from the repository root. Tests and modules manually
  `sys.path.insert(0, <repo root>)`, so invocation from elsewhere breaks.
- Python 3.14 / numpy 2.x is the current dev environment.

## Essential commands

```bash
# Generate a single track
python generate.py "1994 Rotterdam gabber anthem, dark but euphoric" --seed 424242 -o out.wav
python generate.py --list-genres
python generate.py "..." --plan-only          # plan only, no render
python generate.py "..." --stems              # writes project.json + per-bus stems

# Sample library
python generate.py --index-samples "G:/Samples"   # builds/updates data/samples.db
python generate.py "..." --sample-dir data/demo_library --sample-report

# Inspect / reproduce a project
python generate.py --timeline-report projects/timbor_gabber/project.json
python generate.py --re-render projects/timbor_gabber/project.json --verify-render

# DAW exports (never modify the source project)
python generate.py --export-midi    <project.json>
python generate.py --export-manifest <project.json>
python generate.py --export-reaper   <project.json>
python generate.py --export-ableton  <project.json>

# Album generation (phase 5)
python generate.py --generate-album albums/machine-rave-ep.json --out-root albums
python generate.py --album-report   albums/timbor-machine-rave-ep/album.json
python generate.py --regen-track    albums/timbor-machine-rave-ep/album.json --track-position 1

# Album release assembly (phase 6, assembly-only — never regenerates music)
python generate.py --sequence-album albums/timbor-machine-rave-ep
python generate.py --render-album   albums/timbor-machine-rave-ep
python generate.py --verify-album   albums/timbor-machine-rave-ep
python generate.py --album-loudness albums/timbor-machine-rave-ep
python generate.py --package-album  albums/timbor-machine-rave-ep --include-track-masters

# Local web UI (Album Studio) — serve from repo root
python studio/server.py --port 8765
python studio/server.py --list-albums

# Analyze a rendered WAV (energy curve, spectral balance, silence/clipping)
python analyze.py out.wav 14

# Tests
python -m pytest tests/test_samples.py          # preferred
python tests/test_samples.py                     # also works (script mode)
python -m pytest tests/                          # full suite (see memory note below)
```

## Tests: conventions and gotchas

- Every `tests/test_*.py` is **dual-mode**: pytest-collectable functions plus a
  `if __name__ == "__main__": sys.exit(main())` script runner. Both work.
- Many suites define a module-level `FAILED = []` and a `main()` that catches
  `AssertionError`/`Exception`, prints per-test status, and returns exit code 1
  on failure. Run the file directly to get that readable report.
- Test names are long and descriptive; assertions are plain `assert` with an
  explanatory message. Keep that style.
- **Memory-constrained host.** `tools_replay_check.py` stages bus arrays to disk
  as `.f64` files because "the machine runs close to the RAM ceiling", and
  `studio/server.py` pins `OPENBLAS_NUM_THREADS`/`OMP_NUM_THREADS`/
  `MKL_NUM_THREADS` to `1`. Prefer running individual test files over the whole
  suite in one process when memory is tight.
- **Fixture dependency:** `tests/test_album_render.py` (and other album suites)
  need the generated Machine Rave EP under `albums/timbor-machine-rave-ep/`;
  `_require_ep()` raises `RuntimeError("Machine Rave EP not found — generate it
  first")`. Audio WAVs are gitignored, so on a fresh clone regenerate the EP
  before album tests:
  `python generate.py --generate-album albums/machine-rave-ep.json --out-root albums`
- `tests/test_stable_audio.py` must never make cloud calls — it uses fakes.

## Architecture and data flow

```
generate.py                 CLI: prompt -> Plan -> render -> stereo WAV
timbor/
  __init__.py               public API: Plan, render_track, GENRES, stereoize, master, write_wav
  theory.py                 scales, chords, progressions, motif engine (generate/variate/identity)
  dsp.py                    oscillators, FFT filters, drum synthesis, hoover/reese/acid, pads, risers
  voices.py                 note-event -> waveform layers; breakbeat pattern + chopping
  engine.py                 GENRES packs, Plan/SongPlan/Section, section renderer -> 5 buses, QC+repair
  render.py                 stereoize, master chain, WAV export (16-bit and float32)
  replay.py                 project.json -> buses (the live render path, re-executed)
  verify.py                 re-render vs stored-stem diffing (tiered) + report
  timeline/                 canonical beat-based event layer + versioned project.json (format v1)
  samples/                  scan/analyze/classify/select/transform; SQLite index
  export/                   midi.py, manifest.py, reaper.py, ableton.py
  album/                    phase-5 DNA/orchestration + phase-6 sequencing/mastering/release
analyze.py                  output sanity checks
studio/                     stdlib http.server UI; server.py shells out to generate.py
```

Render buses: `drums`, `bass`, `mel`, `fx`, `samples` → master. Stems are
**pre-mastering**; only `master.wav` has the master chain applied, and it is
never double-applied.

The `timbor/album/__init__.py` public API covers phase-5 symbols only; phase-6
modules (`sequencing`, `transitions`, `loudness`, `mastering`, `release`) are
imported by full submodule path.

## Non-obvious invariants (do not break these)

- **Determinism is sacred.** Same seed + generator version + sample files ⇒
  byte-identical stems and master. Never let wall-clock time, dict/set
  iteration order, or unseeded RNG affect rendered audio or deterministic
  documents. `generated_at` timestamps are metadata only and are explicitly
  excluded from comparison (JSON docs are equal modulo timestamps).
- **Per-placement randomness** derives from `(seed, bar, role)` via `crc32`, not
  from a sequential RNG stream.
- **Beat-based time is canonical** in the timeline; seconds derive from BPM.
  Album assembly positions everything in **integer samples**, never by
  accumulating floating-point seconds.
- **Manifests are byte-deterministic:** `sort_keys=True`, no timestamps, so they
  diff cleanly in git.
- **Phase-6 release code must not regenerate music.** `test_album_render.py::
  test_no_regeneration_dependency` scans `release.py` source for banned calls
  (`render_track(`, `generate_song(`, `SongPlan(`, `render_section(`) and fails
  if any appear. Keep the release layer assembly-only.
- **Exports are snapshots** (MIDI/manifest) and never re-synthesize audio; the
  re-render path (`timbor/replay.py`) is the only audio regenerator.
- **`--verify-render` exit code is `2` on a DIFFERENT result** by design (approx
  tiers are reported, not forgiven). See README "Phase-4 limitations" for the
  documented `mel`/`bass` APPROX residuals.
- **Opt-in cloud path.** Stable Audio 3 via Modal is disabled unless
  `STABLE_AUDIO_ENABLED=true` and URL/key are set. Never make it required and
  never make tests depend on it. Local sample libraries are never uploaded.

## Files, artifacts, and git hygiene

- `.gitignore` excludes all audio (`*.wav`/`*.aif`/`*.flac`/`*.mp3`), `*.db`,
  `*.log`, and `.env`. **Commit JSON documents, not rendered artifacts.**
- `.env` holds `STABLE_AUDIO_*` secrets (including an API key). It is
  gitignored — never commit it, never echo its contents, and never hardcode
  secrets. `.env.example` is the committed template.
- Runtime state (not source): `data/samples.db` (sample index),
  `data/sample_libraries.json` (registered roots), `data/studio_creations.db`
  (Studio creations). Delete `data/samples.db` to force a full library rescan.
- `projects/` and `albums/` contain committed project/album JSON plus local
  (gitignored) audio. Shipped examples: `projects/timbor_gabber`,
  `projects/timbor_jungle`, `albums/timbor-machine-rave-ep`.
- `regression_before.json` / `regression_after.json` are the phase-4 regression
  record (seed 424242, md5 hashes) — informational, not a test fixture.

## Platform and format gotchas

- **Windows:** rewriting a WAV that was just read can transiently raise
  `PermissionError`; re-running the command succeeds.
- **FLAC / MP3** are indexed and classified (duration/filename tags) but **not
  decoded** — no bundled decoder. They are skipped for rendering and cannot be
  previewed; convert to WAV to use them.
- Very long samples (>40 s) are truncated for analysis/rendering.
- Files are processed with forward-slash paths in exports so projects relocate.
- The Studio frontend is plain `studio/www/index.html` + `app.css` + `app.js`
  (no bundler/framework). The Python engine is authoritative; the UI reads/writes
  JSON and shells out to `generate.py` for long jobs.

## Style conventions

- `from __future__ import annotations` at the top of every module.
- Module docstrings state purpose and, for engines/CLIs, the flow or usage.
- Type hints on public APIs; `dict[str, np.ndarray]`-style generics.
- Section banners like `# ---- ... ----` in large modules.
- Public packages expose a curated `__all__` in their `__init__.py`.
- Never use em dashes in source code; use commas, periods, parentheses, or
  semicolons.
