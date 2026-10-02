#!/usr/bin/env python3
"""
generate.py — TIMBOR track generator CLI.

Examples:
  python generate.py "dark gabber at 180 bpm in F# minor"
  python generate.py "euphoric uk hardcore anthem" --seed 7 -o anthem.wav
  python generate.py "90s jungle with reese bass" --genre jungle --bars 48
  python generate.py "frenchcore + j-core fusion, playful" -o fusion.wav
  python generate.py --index-samples "G:/Samples"
  python generate.py "1994 Rotterdam gabber anthem" --sample-dir "G:/Samples" --seed 424242
  python generate.py --sample-report
  python generate.py --list-genres
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from timbor import GENRES, Plan, render_track, write_wav


def _project_ops(args) -> int:
    """--re-render / --export-* operations on a saved project (spec §3, §10, §17)."""
    from timbor.timeline.serialization import load_project
    import os

    proj_path = args.re_render or args.export_midi or args.export_manifest or \
        args.export_reaper or args.export_ableton
    project = load_project(proj_path)
    root = os.path.dirname(os.path.abspath(proj_path))
    name = os.path.basename(root) or "track"

    # --- exports (never modify the source project, spec §25) ----------------
    if args.export_midi:
        from timbor.export import export_midi
        p = export_midi(project, os.path.join(root, "midi"), name=name,
                        midi_format=args.midi_format)
        print(f"wrote {p}")
    if args.export_manifest:
        from timbor.export import export_manifest
        p = export_manifest(project, os.path.join(root, "export"), name=name)
        print(f"wrote {p}")
    if args.export_reaper:
        from timbor.export import export_reaper
        p = export_reaper(project, root)
        print(f"wrote {p}")
    if args.export_ableton:
        from timbor.export.ableton import export_ableton
        p = export_ableton(project, root)
        print(f"wrote {p}")
    if not args.re_render:
        return 0

    # --- re-render (executes stored events; no re-planning, spec §2) ---------
    from timbor.replay import render_project, resolve_samples, compare_buses
    from timbor.render import stereoize, master, write_stems, write_wav
    out_dir = args.output or os.path.join(root, "rerendered")

    if args.verify_render:
        from timbor.verify import verify_render, format_verify_report
        result = verify_render(project, root, sample_root=args.sample_root)
        print(format_verify_report(project, result, root))
        return 0 if result["pass"] else 2

    print("Rendering...")
    buses, diags = render_project(project, sample_root=args.sample_root)
    l, r = stereoize(buses)
    l, r = master(l, r)
    audio_dir = os.path.join(out_dir, "audio")
    write_stems(audio_dir, buses, l, r)
    print("Writing stems...")
    for b in ("drums", "bass", "mel", "fx", "samples", "master"):
        print(f"  {b}")
    print(f"wrote {os.path.join(audio_dir, 'master.wav')}")
    statuses = [d["status"] for d in diags["sample_status"]]
    print(f"sample references: "
          f"{statuses.count('ok')}/{len(statuses)} resolved")
    return 0


def _load_album_arg(path: str) -> tuple[dict, str]:
    """Accept an album.json path OR an album directory; return (doc, root)."""
    import os
    import json
    p = path
    if os.path.isdir(p):
        p = os.path.join(p, "album.json")
    with open(p, "r", encoding="utf-8") as f:
        doc = json.load(f)
    if doc.get("format") != "timbor-album":
        raise ValueError(f"not a TIMBOR album: {p}")
    return doc, os.path.dirname(os.path.abspath(p))


def _album_release_ops(args) -> int:
    """Phase-6 operations: sequence / render / verify / loudness / package.

    All of these are assembly-level: they never regenerate music and never
    modify track masters.
    """
    import os
    from timbor.album.sequencing import (SequenceConfig, build_sequence,
                                         validate_sequence, save_sequence)
    from timbor.album.release import (assemble_release, verify_album,
                                      package_release,
                                      verify_master_immutability)
    from timbor.album.mastering import policy_from
    from timbor.album.release import _md5
    from timbor.album import loudness as loudness_mod

    album, root = _load_album_arg(args.sequence_album or args.render_album
                                  or args.verify_album or args.album_loudness
                                  or args.package_album)
    cfg = SequenceConfig.from_album(album)

    if args.sequence_album:
        seq = build_sequence(album, root, cfg)
        problems = validate_sequence(seq, cfg)
        out_dir = os.path.join(root, "album")
        os.makedirs(out_dir, exist_ok=True)
        save_sequence(os.path.join(out_dir, "sequence.json"), seq)
        print(f"TIMBOR SEQUENCE — mode={seq['mode']} quantize={seq['quantize']}")
        for e in seq["entries"]:
            print(f"  {e['position']:02d} {e.get('title') or e['track_id']:20s} "
                  f"start {e['start_seconds']:8.2f}s  "
                  f"gap {e['gap_before']:.2f}s  overlap {e['overlap_samples']} smp")
        print(f"total: {seq['duration_seconds']:.2f}s "
              f"({seq['total_samples']} samples @ {seq['sample_rate']} Hz)")
        if problems:
            print("PROBLEMS:", *problems, sep="\n  ")
            return 1
        print(f"wrote {os.path.join(root, 'album', 'sequence.json')}")
        return 0

    if args.render_album:
        before = {t["number"]: _md5(os.path.join(
            root, t["directory"], "audio", "master.wav"))
            for t in album["tracks"]}
        out = assemble_release(album, root, cfg)
        imm = verify_master_immutability(album, root, before)
        print(f"album master: {out['outputs']['album_wav']}")
        print(f"hash: {out['album_master_hash']}")
        print(f"QC: {'OK' if out['qc_ok'] else 'FAILED'}")
        print(f"track masters unchanged: {imm['unchanged']}" +
              (f" ({imm['changed']})" if imm["changed"] else ""))
        v = verify_album(album, root, cfg)
        print(f"self-verify: {v['status']}")
        return 0 if out["qc_ok"] and imm["unchanged"] else 1

    if args.verify_album:
        v = verify_album(album, root, cfg)
        print("TIMBOR ALBUM VERIFICATION")
        print(f"  stored hash: {v['stored_hash']}")
        print(f"  fresh  hash: {v['fresh_hash']}")
        print(f"  status:      {v['status']} "
              f"(max|diff|={v['max_abs_diff']:.2e}, "
              f"{v['samples_compared']} samples)")
        return 0 if v["status"] in ("IDENTICAL", "FLOAT-EQUIVALENT") else 2

    if args.album_loudness:
        loud = loudness_mod.analyze_album(album, root)
        wav = os.path.join(root, "album", "album.wav")
        if os.path.isfile(wav):
            loud["album"] = loudness_mod.analyze_file(wav)
        p = loudness_mod.save_loudness(os.path.join(root, "album",
                                                    "loudness.json"), loud)
        print(loudness_mod.format_loudness_report(loud))
        print(f"wrote {p}")
        return 0

    if args.package_album:
        res = package_release(album, root,
                              include_track_masters=args.include_track_masters)
        print(f"release package: {res['release_dir']}")
        for f in res["files"]:
            print(f"  {f}")
        if res["track_masters"]:
            print("  tracks/:", *res["track_masters"], sep="\n    ")
        print(f"album master hash: {res['album_master_hash']}")
        return 0
    return 1


def _album_ops(args) -> int:
    """--generate-album / --album-report / --album-manifest / --regen-track.

    Inspection operations never regenerate audio (spec §25); only
    --generate-album and --regen-track render.
    """
    import json
    import os
    from timbor.album import (AlbumConfig, generate_album, load_album,
                              format_album_report, export_manifest,
                              regen_track)

    if args.generate_album:
        with open(args.generate_album, "r", encoding="utf-8") as f:
            cfg = AlbumConfig.from_json(json.load(f))
        album = generate_album(cfg, args.out_root,
                               render=not args.album_plan_only,
                               db_path=args.album_db)
        print(format_album_report(album))
        if args.album_plan_only:
            print("\n(plan only — no audio rendered)")
            return 0
        print(f"\nalbum root: {album['root']}")
        mpath = export_manifest(album, album["root"])
        print(f"wrote {mpath}")
        n_err = sum(1 for i in album.get("validation", [])
                    if i["severity"] == "error")
        return 2 if n_err else 0

    album_path = args.album_report or args.album_manifest or args.regen_track
    album = load_album(album_path)
    if args.album_report:
        import os
        from timbor.album import revalidate_album
        album["validation"] = revalidate_album(
            album, os.path.dirname(os.path.abspath(album_path)))
        print(format_album_report(album))
        return 0
    if args.album_manifest:
        out_root = os.path.dirname(os.path.abspath(album_path))
        p = export_manifest(album, out_root)
        print(f"wrote {p}")
        return 0
    # regen-track
    if args.track_position is None:
        print("--regen-track requires --track-position N (0-based)")
        return 1
    res = regen_track(album_path, position=args.track_position,
                      db_path=args.album_db)
    print(f"regenerated track {args.track_position}:")
    print(f"  project: {res['project']}")
    for k, v in res["exports"].items():
        print(f"  {k}: {v}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TIMBOR — AI electronic music producer")
    ap.add_argument("prompt", nargs="?", help="free-text musical brief")
    ap.add_argument("-g", "--genre", help=f"one of: {', '.join(GENRES)}")
    ap.add_argument("-b", "--bpm", type=float, help="override tempo")
    ap.add_argument("-k", "--key", help="key like 'A minor' or 'F#:phrygian'")
    ap.add_argument("-m", "--mood", choices=["dark", "euphoric", "fun", "cinematic"])
    ap.add_argument("-a", "--authenticity", default="hybrid",
                    choices=["authentic", "modern", "hybrid", "experimental"])
    ap.add_argument("--era", help="explicit musical era for remote music-model prompt context")
    ap.add_argument("-s", "--seed", type=int, default=None)
    ap.add_argument("-o", "--out", default=None, help="output wav path")
    ap.add_argument("--bars", type=int, default=None, help="cap total bars")
    ap.add_argument("--plan-only", action="store_true", help="print plan, no render")
    ap.add_argument("--list-genres", action="store_true")
    # opt-in remote Stable Audio 3 provider (independent of the procedural engine)
    ap.add_argument("--stable-audio", action="store_true",
                    help="generate WAV through the configured Stable Audio 3 Modal service")
    ap.add_argument("--stable-audio-mode", default="text-to-audio",
                    choices=["text-to-audio", "audio-to-audio", "inpaint"],
                    help="official Stable Audio inference mode")
    # additional opt-in music engines
    ap.add_argument("--engine", choices=["timbor", "yue2", "acestep"],
                    default="timbor",
                    help="music engine to use (default: timbor)")
    ap.add_argument("--stable-audio-model", default=None,
                    help="small-music only; must match the deployed Modal model")
    ap.add_argument("--lyrics", default=None,
                    help="lyrics for Stable Audio, YuE2, or ACE-Step")
    ap.add_argument("--duration", type=float, default=None,
                    help="remote model output duration in seconds")
    ap.add_argument("--audio-input", default=None,
                    help="WAV input for audio-to-audio, inpaint, or continuation")
    ap.add_argument("--audio-sample-id", default=None,
                    help="registered TIMBOR sample-library ID to condition on")
    ap.add_argument("--audio-strength", type=float, default=None,
                    help="audio-to-audio noise level in [0, 1]")
    ap.add_argument("--inpaint-start", type=float, action="append", default=None,
                    help="inpaint/continuation mask start (repeat for multiple regions)")
    ap.add_argument("--inpaint-end", type=float, action="append", default=None,
                    help="inpaint/continuation mask end (repeat for multiple regions)")
    # sample subsystem
    ap.add_argument("--index-samples", metavar="DIR",
                    help="scan + analyze a sample library, then exit")
    ap.add_argument("--sample-dir", metavar="DIR",
                    help="use this indexed sample library for hybrid rendering")
    ap.add_argument("--sample-mode", default="balanced",
                    choices=["off", "subtle", "balanced", "heavy"],
                    help="how strongly samples participate (default: balanced)")
    ap.add_argument("--sample-report", action="store_true",
                    help="explain sample selections; with --index-samples prints "
                         "library statistics instead")
    # stem / project export
    ap.add_argument("--stems", action="store_true",
                    help="export per-bus stems + project.json alongside the master")
    ap.add_argument("--stem-format", default="float32", choices=["float32", "int16"],
                    help="stem file format (default: 32-bit float WAV)")
    ap.add_argument("--timeline-report", metavar="PROJECT_JSON",
                    help="print the timeline of an exported project and exit")
    # project operations
    ap.add_argument("--re-render", metavar="PROJECT",
                    help="re-render audio from a saved project.json")
    ap.add_argument("--verify-render", action="store_true",
                    help="with --re-render: compare against the project's stems")
    ap.add_argument("--sample-root", metavar="PATH",
                    help="alternate root for resolving sample references")
    ap.add_argument("--output", metavar="DIR",
                    help="output directory for --re-render (default: <project>/rerendered)")
    # exports
    ap.add_argument("--export-midi", metavar="PROJECT",
                    help="export a Standard MIDI File from a project")
    ap.add_argument("--midi-format", type=int, default=1, choices=[1],
                    help="SMF format (1 only)")
    ap.add_argument("--export-manifest", metavar="PROJECT",
                    help="export the DAW-neutral manifest")
    ap.add_argument("--export-reaper", metavar="PROJECT",
                    help="export a REAPER project (.rpp)")
    ap.add_argument("--export-ableton", metavar="PROJECT",
                    help="export an Ableton-compatible interchange package")
    # album / EP generation (phase 5)
    ap.add_argument("--generate-album", metavar="CONFIG_JSON",
                    help="generate a full EP/album from an album config JSON")
    ap.add_argument("--album-plan-only", action="store_true",
                    help="with --generate-album: write plan documents, no audio")
    ap.add_argument("--out-root", metavar="DIR", default="albums",
                    help="root directory for generated albums (default: albums/)")
    ap.add_argument("--album-report", metavar="ALBUM_JSON",
                    help="print the album summary + validation; no regeneration")
    ap.add_argument("--album-manifest", metavar="ALBUM_JSON",
                    help="write the EP-level manifest; no regeneration")
    ap.add_argument("--regen-track", metavar="ALBUM_JSON",
                    help="regenerate ONE track of a saved album (with "
                         "--track-position); siblings untouched")
    ap.add_argument("--track-position", type=int, metavar="N",
                    help="track position for --regen-track (0-based)")
    ap.add_argument("--album-db", metavar="PATH",
                    help="sample index database override for album mode")
    # album sequencing / mastering / release (phase 6)
    ap.add_argument("--sequence-album", metavar="ALBUM",
                    help="plan + validate the album sequence (writes "
                         "album/sequence.json; no audio)")
    ap.add_argument("--render-album", metavar="ALBUM",
                    help="assemble album.wav + all release artifacts from "
                         "existing track masters")
    ap.add_argument("--verify-album", metavar="ALBUM",
                    help="re-assemble and compare against the stored "
                         "album.wav (IDENTICAL/FLOAT-EQUIVALENT/DIFFERENT)")
    ap.add_argument("--album-loudness", metavar="ALBUM",
                    help="measure track masters + assembled album "
                         "(loudness.json + report)")
    ap.add_argument("--package-album", metavar="ALBUM",
                    help="build the release/ package from album/ artifacts")
    ap.add_argument("--include-track-masters", action="store_true",
                    help="with --package-album: also copy track masters "
                         "into release/tracks/")
    args = ap.parse_args(argv)

    if args.list_genres:
        for g, pack in GENRES.items():
            lo, hi = pack["bpm"]
            print(f"{g:12s} {lo}-{hi} bpm  drums={pack['drums']:16s} bass={pack['bass']}")
        return 0

    # -- library report mode ----------------------------------------------
    if args.sample_report and not args.prompt:
        from timbor.samples.cache import SampleIndex
        idx = SampleIndex()
        st = idx.stats()
        idx.close()
        print("TIMBOR SAMPLE LIBRARY")
        print(f"Total files: {st['total']:,}")
        print(f"Analyzed: {st['analyzed']:,}   errors: {st['errors']:,}")
        print("\nCategories:")
        for cat, c in sorted(st["categories"].items(), key=lambda kv: -kv[1]):
            print(f"  {cat:14s} {c:>7,}")
        k = st["keys"]
        print(f"\nKey detection: high confidence {k['high']:,} / "
              f"medium {k['medium']:,} / none {k['none']:,}")
        if st.get("bpm_bands"):
            print("\nBPM distribution:")
            for band, c in st["bpm_bands"].items():
                bar = "#" * max(1, int(c / max(st['analyzed'], 1) * 40))
                print(f"  {band:8s} {c:>7,}  {bar}")
        return 0

    # -- indexing mode ------------------------------------------------------
    if args.index_samples:
        from timbor.samples.index import index_library, format_stats
        print(f"Indexing {args.index_samples} ...")
        stats = index_library(args.index_samples, verbose=True)
        print(format_stats(stats))
        return 0

    # -- timeline report mode ------------------------------------------------
    if args.timeline_report:
        from timbor.timeline.serialization import load_project
        from timbor.timeline.project import timeline_report
        proj = load_project(args.timeline_report)
        print(timeline_report(proj))
        return 0

    # -- project operations ---------------------------------------------------
    if args.re_render or args.export_midi or args.export_manifest or \
            args.export_reaper or args.export_ableton:
        return _project_ops(args)

    # -- album operations (phase 5) --------------------------------------------
    if args.generate_album or args.album_report or args.album_manifest or \
            args.regen_track:
        return _album_ops(args)

    # -- album sequencing / mastering / release (phase 6) -----------------------
    if args.sequence_album or args.render_album or args.verify_album or \
            args.album_loudness or args.package_album:
        return _album_release_ops(args)

    if args.stable_audio and args.engine != "timbor":
        ap.error("--stable-audio cannot be combined with --engine")

    if args.stable_audio or args.engine in {"yue2", "acestep"}:
        import os
        provider_name = "Stable Audio 3" if args.stable_audio else ("YuE2" if args.engine == "yue2" else "ACE-Step")

        try:
            if args.stable_audio:
                from timbor.stable_audio import (GenerationRequest, StableAudioConfig,
                                                 StableAudioError, get_provider)
                config = StableAudioConfig.from_env()
                request_type = GenerationRequest
                provider = get_provider(config)
                error_type = StableAudioError
            elif args.engine == "yue2":
                from timbor.yue_engine import (EngineConfig, GenerationRequest,
                                               YueEngineError, YueEngineProvider)
                config = EngineConfig.from_env()
                request_type = GenerationRequest
                provider = YueEngineProvider(config)
                error_type = YueEngineError
            else:
                from timbor.ace_step_engine import (EngineConfig, GenerationRequest,
                                                    AceStepEngineError, AceStepEngineProvider)
                config = EngineConfig.from_env()
                request_type = GenerationRequest
                provider = AceStepEngineProvider(config)
                error_type = AceStepEngineError
            if not args.prompt and not args.genre:
                raise error_type("provide a prompt or --genre")
            duration = args.duration if args.duration is not None else config.default_duration
            if args.stable_audio and (args.inpaint_start is None) != (args.inpaint_end is None):
                raise StableAudioError("--inpaint-start and --inpaint-end must be supplied together")
            if args.stable_audio and args.inpaint_start and len(args.inpaint_start) != len(args.inpaint_end):
                raise error_type("supply the same number of --inpaint-start and --inpaint-end values")
            if args.stable_audio and args.stable_audio_mode == "inpaint" and not args.inpaint_start:
                raise error_type("inpaint mode requires --inpaint-start and --inpaint-end")
            if args.stable_audio and args.stable_audio_mode == "audio-to-audio" and args.inpaint_start:
                raise error_type("inpaint ranges require --stable-audio-mode inpaint")
            if not args.stable_audio and (args.audio_input or args.audio_sample_id):
                raise error_type("conditioning audio is not exposed by these text-to-music services")
            if not args.stable_audio and args.stable_audio_mode != "text-to-audio":
                raise error_type("--stable-audio-mode requires --stable-audio")
            if not args.stable_audio and (args.inpaint_start or args.inpaint_end or args.audio_strength is not None):
                raise error_type("audio conditioning controls require --stable-audio")
            if not args.stable_audio and args.stable_audio_model:
                raise error_type("--stable-audio-model requires --stable-audio")
            request_prompt = args.prompt or args.genre or ""
            if args.engine == "acestep" and len(request_prompt) > 512:
                raise error_type("ACE-Step caption must be at most 512 characters")
            request = request_type(
                prompt=request_prompt, duration=duration,
                seed=args.seed if args.seed is not None else -1,
                model=args.stable_audio_model if args.stable_audio else None,
                negative_prompt=None, genre=args.genre, era=args.era,
                bpm=args.bpm, key=args.key, mood=args.mood,
                mode=args.stable_audio_mode if args.stable_audio else "text-to-audio",
                audio_path=args.audio_input if args.stable_audio else None,
                sample_id=args.audio_sample_id if args.stable_audio else None,
                strength=args.audio_strength if args.stable_audio else None,
                inpaint_starts=args.inpaint_start if args.stable_audio else None,
                inpaint_ends=args.inpaint_end if args.stable_audio else None,
                lyrics=args.lyrics)
            if args.engine == "acestep":
                request.validate(config)
            audio, metadata = provider.generate(request)
            out = args.out or ("stable_audio.wav" if args.stable_audio else f"{args.engine}.wav")

            out_abs = os.path.abspath(out)
            os.makedirs(os.path.dirname(out_abs) or ".", exist_ok=True)
            temp_path = out_abs + ".partial"
            try:
                with open(temp_path, "wb") as f:
                    f.write(audio)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_path, out_abs)
            finally:
                try:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                except OSError:
                    pass
            metadata_path = out_abs + ".json"
            metadata_temp = metadata_path + ".partial"
            try:
                with open(metadata_temp, "w", encoding="utf-8") as f:
                    json.dump(metadata, f, indent=2, sort_keys=True)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(metadata_temp, metadata_path)
            finally:
                try:
                    if os.path.exists(metadata_temp):
                        os.remove(metadata_temp)
                except OSError:
                    pass
            print(f"== TIMBOR {provider_name} ==")
            print(f"model={metadata['model']} seed={metadata['seed']} "
                  f"duration={metadata['duration']:.2f}s "
                  f"sample_rate={metadata['sample_rate']} Hz")
            print(f"validation=PASS RMS={metadata['diagnostics']['rms']:.5f} "
                  f"peak={metadata['diagnostics']['peak']:.5f} "
                  f"silent={metadata['diagnostics']['silent_fraction']:.1%}")
            print(f"generation={metadata.get('generation_seconds') or 'n/a'}s "
                  f"total={metadata['total_seconds']:.3f}s "
                  f"size={metadata['audio_bytes']} bytes")
            print(f"wrote {out_abs}")
            print(f"wrote {metadata_path}")
            return 0
        except error_type as exc:
            print(f"{provider_name} failed: {exc}", file=sys.stderr)
            return 2
        except OSError as exc:
            print(f"{provider_name} output error: {exc}", file=sys.stderr)
            return 2

    if args.audio_input or args.audio_sample_id or args.stable_audio_mode != "text-to-audio":
        ap.error("audio input and Stable Audio mode controls require --stable-audio")
    if args.duration is not None or args.lyrics:
        ap.error("--duration and --lyrics require --stable-audio or --engine yue2/acestep")
    if args.engine != "timbor":
        ap.error("--engine must be timbor, yue2, or acestep")

    if not args.prompt and not (args.genre or args.bpm):
        ap.print_help()
        return 1

    t0 = time.time()
    plan = Plan(prompt=args.prompt or "", genre=args.genre, bpm=args.bpm, key=args.key,
                mood=args.mood, authenticity=args.authenticity, seed=args.seed,
                bars_limit=args.bars, sample_mode=args.sample_mode)
    if args.sample_dir:
        plan.sample_dir = args.sample_dir

    # The local procedural engine remains the default generation path.
    sample_index = None
    if args.sample_dir and args.sample_mode != "off":
        from timbor.samples.cache import SampleIndex
        sample_index = SampleIndex()

    try:
        song, buses, qc = render_track(plan, sample_index=sample_index)
    finally:
        if sample_index is not None:
            sample_index.close()
    dt = time.time() - t0

    print("== TIMBOR song plan ==")
    print(song.describe())
    if args.sample_dir and sample_index is not None:
        n_smp = len(buses.get("samples", []))
        print(f"samples: mode={args.sample_mode} "
              f"({'active bus rendered' if n_smp else 'no suitable samples found'})")
    if qc:
        print("QC repairs:")
        for n in qc:
            print(f"  - {n}")
    else:
        print("QC: clean (no repairs needed)")

    if args.sample_report and sample_index is not None:
        from timbor.samples.selector import explain_selection
        print()
        print(explain_selection(getattr(song, "sample_assignments", []), song=song))

    if args.plan_only:
        return 0

    from timbor import stereoize, master
    l, r = stereoize(buses)
    l, r = master(l, r)
    out = args.out or "track.wav"
    if args.stems:
        from timbor.timeline.project import write_project_directory
        pj = write_project_directory(out, song, qc, buses, l, r,
                                     stem_fmt=args.stem_format)
        print("Writing stems...")
        for name in ("drums", "bass", "mel", "fx", "samples", "master"):
            print(f"  {name}")
        print(f"Writing project JSON...  {pj}")
    else:
        write_wav(out, l, r)
    dur = len(l) / 44100.0
    print(f"wrote {out}  ({dur / 60:.1f} min, rendered in {dt:.1f}s)")
    print("Complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
