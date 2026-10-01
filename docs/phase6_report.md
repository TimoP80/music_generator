# TIMBOR — Phase 6 Final Report
## Album Sequencing, Album Mastering & Release Assembly

Invariant verified by test: **Phase 6 assembles and masters the album without
changing the identity or reproducibility of any individual TIMBOR track.**
Every operation in this layer is assembly-only; no code path calls
`generate_song()` or regenerates motifs, plans, samples, or masters.

---

## 1. Files created

| File | Purpose |
|---|---|
| `timbor/album/sequencing.py` | `SequenceConfig`, `build_sequence`, `validate_sequence`, explicit ordering, `wav_info` header probe, `save/load_sequence` |
| `timbor/album/transitions.py` | `Transition` dataclass (JSON round-trip), `quantize_duration`, `crossfade_curves`, `plan_boundary`, `apply_boundary`, `measure_boundary`, `click_status`, `boundary_diagnostics` |
| `timbor/album/loudness.py` | K-weighted gated loudness, short-term series, LRA, true-peak approx, WAV decode (8/16/24/32-float, mono/stereo), `analyze_file/analyze_album`, deterministic report |
| `timbor/album/mastering.py` | `policy_from` (preserve/match/target), `compute_gains`, `apply_gain`, block-lookahead `limit()`, `peak_report` |
| `timbor/album/release.py` | `render_album`, album QC, `album_qc`, chunked WAV writers, `build_cue`, `build_tracklist`, `build_release_manifest`, `build_release_readme`, `verify_album`, `verify_master_immutability`, `package_release`, `assemble_release` |
| `tests/test_album_sequencing.py` | ordering/timing/gaps/modes, determinism, validation |
| `tests/test_album_transitions.py` | quantize grid, curves, overlap guard, click diagnostics |
| `tests/test_album_loudness.py` | determinism, gates, RMS≠LUFS, policies, clamps, limiter |
| `tests/test_album_render.py` | byte-determinism, immutability, limiter, match gains, verify tiers |
| `tests/test_album_release.py` | 9 artifacts, package structure, reproducibility, 3-mode sweep |
| `studio/server.py` | Album Studio backend (stdlib `http.server`) |
| `studio/www/index.html`, `studio/www/app.css`, `studio/www/app.js` | Album Studio frontend (9 screens) |
| `docs/phase6_report.md` | this report |

## 2. Files modified

* `generate.py` — phase-6 CLI: `--sequence-album`, `--render-album`,
  `--verify-album`, `--album-loudness`, `--package-album`
  (`+ --include-track-masters`) and `_album_release_ops()`;
  `_load_album_arg` accepts an album directory or `album.json`.
* `README.md` — Phase 6 documentation section.
* `tests/test_album_*.py` — fixture corrections during verification
  (quantization expectations, blend-zone assertions, gc pressure relief).
  No engine file outside `timbor/album/` was modified; phase-1..5 behavior
  is untouched (all 13 prior suites re-run green).

## 3. Sequencing architecture

`SequenceConfig.from_album(album)` reads the optional `sequencing` +
`transition` sections of album.json (conservative defaults: mode `gap`,
gap 2.0 s, quantize `none`, equal-power curve). Track order is **explicit**
(`sequencing.order`, track numbers; filesystem listings are never used).
`build_sequence()` computes, per track: `start/end_sample`,
`gap_before_samples`, `overlap_samples`, plus second-based mirrors for
display — all boundaries are integer samples, never accumulated
floating-point seconds. `validate_sequence()` rejects negative positions,
unintentional truncation (`end−start ≠ source_samples`), plan/total
mismatches, stale transition positions after reordering, and overlaps
exceeding half of the shorter master. Modes: `gap` (silence), `crossfade`
(overlap + curve), `continuous` (hard cut, DJ/rave back-to-back).
Reordering changes only the sequence — seeds, motifs, plans and renders
are untouched (test §30).

## 4. Transition implementation

Four types: `hard_cut`, `gap`, `linear_crossfade`,
`equal_power_crossfade` (default type `gap`; mode+curve select the
crossfade automatically). Equal-power curves are amplitude-domain
cos/sin so summed power is constant (test: fo²+fi² = 1.0). Beat-aware:
`quantize none|beat|bar|phrase` snaps transition durations **up** to the
next grid multiple derived from the source BPM (3 s @ 170 → 3 bars; 0.4 s
→ 2 beats; on-grid values stay). Both source and destination BPMs are
stored per transition; mismatches are flagged, never silently rounded.
Safety: `plan_boundary` **raises** `ValueError` when a requested crossfade
exceeds half of the shorter master (no silent clamp); `apply_boundary`
never writes to source masters (replace-and-mix in the blend zone only);
`measure_boundary` click diagnostics compare the seam step against the
local context (±2 ms seam / ±500 ms context, context excludes the seam)
with absolute thresholds in silence and ratio thresholds in loud context
→ PASS/WARN/ERROR per boundary.

## 5. Loudness measurement method

`loudness.py` implements an in-process **K-weighted, gated loudness
measure** labelled `itu_bs1770_style_k_weighted_gated`: the BS.1770
K-weighting biquad cascade converted to an exact 8192-tap FIR applied by
bounded-memory overlap-add, 400 ms blocks / 75 % overlap, absolute
(−70 LUFS) + relative (−10 LU) gating. Reported per track and album:
peak dBFS, **true-peak approximation** (4× linear interpolation — always
labelled approximate, never presented as a certified oversampled meter),
RMS (reported separately, never labelled LUFS), integrated loudness,
short-term loudness (3 s window / 1 s hop), loudness range, duration,
leading/trailing silence. Deterministic; long inputs chunked
(`_OLA_BLOCK = 1<<20`) to bound memory. Machine Rave EP: album
−13.77 integrated, −3.31 dBFS peak, 6.49 LU LRA; tracks −13.63 / −13.46 /
−14.27 / −14.33 LUFS.

## 6. Album-level gain / limiter behavior

`policy_from(album)` defaults to **preserve** (masters unchanged).
`match` anchors on the loudest track (0 dB), leaves tracks within
`match_window_db` (3 LU default) untouched, and **lifts** quieter tracks
toward the anchor (boost-only, capped at 3.0 dB; warned above 6 LU).
`target` applies a fixed offset toward `target_lufs` (−14 default), same
cap. Gains are recorded per track (`album_gain_db`) and applied in the
assembly chain `source master → album gain → sequencing`; source WAVs are
never modified. If assembly would clip, QC reports it; the deterministic
block-lookahead limiter runs only when `limiter: true` is explicitly set
and holds the configured ceiling (`limiter_ceiling_db`, −1.0 dBTP default;
test: ceiling held deterministically). The limiter is never applied to
individual track masters. EP validation: `match` yields gains
{01–04: 0.0} — correct, the EP sits inside the 3 LU window.

## 7. Deterministic rendering architecture

All timing is integer samples end-to-end. The assembly writes float32
samples in fixed chunks (`_write_wav_float32`, step 1<<18 — byte-identical
to the monolithic writer, verified). Same masters + same configuration →
byte-identical `album.wav`: the EP's gap-mode master hashes to
`aef39072b6381d306c63e0563d0e2b9c` (asserted in the render suite).
`verify_album` re-assembles and compares in 1 M-sample chunks at three
tiers: `IDENTICAL` (bitwise), `FLOAT-EQUIVALENT` (≤1e-9), `DIFFERENT`.

## 8. Release package structure

```text
albums/<album>/album/
    album.wav  album_int16.wav  sequence.json  loudness.json  qc.json
    album.cue (MM:SS:FF @ 75 fps)  tracklist.json
    release_manifest.json  README.md (deterministic, timestamp-free)
albums/<album>/release/     packaged copy via --package-album
                            (+ tracks/ masters with --include-track-masters)
```

The release manifest records album master hash, per-track source-master
hashes, gains, sequence mode/hash, and generator provenance. Track
projects are referenced, never duplicated into `album/`.

## 9. Machine Rave EP validation

All three modes built from the same four child projects:

| Mode | Duration | Master hash |
|---|---|---|
| gap (2.0 s) | 252.83 s | `aef39072b6…` (deterministic) |
| continuous | 246.83 s | — |
| crossfade (3.0 s) | 234.83 s | — |

Sequence metadata describes each version exactly (positions, gaps,
overlaps, transitions with both BPMs); album QC passes for all
(0 errors); loudness, cue, tracklist and manifest regenerate correctly
per mode. Masters identical across all three builds.

## 10. Source-master immutability results

`verify_master_immutability` hashes every `tracks[n]/audio/master.wav`
before and after sequencing + gain application + render + packaging:
**unchanged** in all tests (release suite §29, render suite gap/
crossfade/match/limiter tests). QC additionally records per-track master
md5s (`master_unchanged_01..04`).

## 11. Album replay / verification results

* `verify_album` on the intact EP: **IDENTICAL**, max|diff| = 0,
  10,488,225 samples compared.
* Tampered stored master → **DIFFERENT** (test).
* Tiered comparison reuses the phase-4 semantics (identical /
  float-equivalent / different).

## 12. Test results

| Suite | Result |
|---|---|
| test_album_sequencing | PASS |
| test_album_transitions | PASS |
| test_album_loudness | PASS |
| test_album_render (7 checks) | PASS |
| test_album_release (7 checks) | PASS |
| 13 phase-1..5 suites (samples, timeline, rerender, midi, manifest, reaper, album_dna/motifs/palette/planning/generation/manifest/validation) | PASS |

18/18 suites green; gap hash intact after all fixes. One transient
MemoryError on the memory-constrained machine was eliminated by chunked
WAV writers, chunked verify, mono (l+r)/2 boundary diagnostics, and
bounded-memory OLA/chunked loudness scanning — with values proven
unchanged.

## 13. Known limitations

* The loudness measure is BS.1770-*style*, not a certified meter; true
  peak is a 4× linear-interpolation approximation.
* Match/target gains are boost-only (cap 3.0 dB) by design.
* Transitions quantize **duration** to the beat grid; playback is not
  beat-matched between differing BPMs.
* Very short album tracks still render full arrangements (single-song
  pipeline floor), so durations can exceed the configured window.
* The album layer inherits phase-4 verify tiers (mel doubling residual,
  jungle/trance bass ≈0.05–0.08).
* Studio jobs are session-local (in-memory); the CLI remains the
  scriptable interface.

## 14. Deferred features (spec §37)

Streaming-service loudness presets · MP3/AAC encoding · vinyl mastering ·
CD PQ beyond basic cue · Dolby Atmos / surround · automatic cover
artwork · automatic metadata submission · generative transition music ·
album-wide musical composition changes.
