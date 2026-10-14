"""tests.test_rerender — --re-render architecture: replay from project.json,
tiered bus comparison, deterministic replay, verify_render integration.

Run:  python tests/test_rerender.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from timbor import Plan, render_track
from timbor.render import stereoize, master
from timbor.timeline import load_project
from timbor.timeline.project import write_project_directory
from timbor.replay import render_project, compare_buses
from timbor.verify import verify_render, format_verify_report

TMP = None
FAILED = []


# ---------------------------------------------------------------- helpers

def _get_tmp():
    global TMP
    if TMP is None:
        TMP = tempfile.mkdtemp(prefix="timbor_rerender_")
    return TMP


def _make_project(tag: str):
    """Render a small deterministic project; return (project_dict, root,
    live_buses, live_master_mono)."""
    ws_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    lib_src = os.path.join(ws_root, "data", "demo_library")
    from timbor.samples.cache import SampleIndex
    from timbor.samples.index import index_library
    tmp_dir = _get_tmp()
    lib = os.path.join(tmp_dir, f"lib_{tag}")
    db = os.path.join(tmp_dir, f"samples_{tag}.db")
    if not os.path.isdir(lib):
        shutil.copytree(lib_src, lib)
        index_library(lib, db_path=db, verbose=False)
    idx = SampleIndex(db)
    try:
        plan = Plan("dark gabber at 180 bpm", seed=424242, bars_limit=16,
                    sample_mode="balanced")
        plan.sample_dir = lib
        song, buses, qc = render_track(plan, sample_index=idx)
        l, r = stereoize(buses)
        l, r = master(l, r)
        root = os.path.join(tmp_dir, f"proj_{tag}")
        pj = write_project_directory(os.path.join(root, "audio", "master.wav"),
                                     song, qc, buses, l, r)
        return load_project(pj), root, buses, (l + r) * 0.5
    finally:
        idx.close()


# ---------------------------------------------------------------- tests

def test_replay_tiered_comparison():
    project, root, live, live_master = _make_project("tiers")
    replayed, diags = render_project(project, sample_root=root)
    tiers = compare_buses(live, replayed)
    # hard requirement: no bus may be musically DIFFERENT
    for bus, t in sorted(tiers.items()):
        assert t["status"] != "different", \
            f"{bus}: DIFFERENT (max|diff|={t['max_diff']})"
    # the deterministic paths must hold their documented tiers
    assert tiers["drums"]["status"] in ("exact", "float_equiv"), tiers["drums"]
    assert tiers["bass"]["status"] in ("exact", "float_equiv"), tiers["bass"]
    assert tiers["samples"]["status"] == "exact", tiers["samples"]
    # approx buses (if any) stay inside the honest tolerance
    for bus in ("mel", "fx"):
        if tiers.get(bus, {}).get("status") == "approx":
            assert tiers[bus]["max_diff"] <= 0.5, tiers[bus]
    # replayed master equals the live master within the same envelope
    rl, rr = stereoize(replayed)
    rl, rr = master(rl, rr)
    rm = (rl + rr) * 0.5
    n = min(len(rm), len(live_master))
    d = float(np.max(np.abs(rm[:n] - live_master[:n])))
    assert d <= 0.5, f"master diff {d}"
    print(f"  tiered comparison: OK ({', '.join(f'{b}={t['status']}({t['max_diff']:.1e})' for b, t in sorted(tiers.items()))})")


def test_replay_deterministic():
    project, root, _live, _m = _make_project("det")
    a, _ = render_project(project, sample_root=root)
    b, _ = render_project(project, sample_root=root)
    for k in sorted(set(a) | set(b)):
        assert np.array_equal(a[k], b[k]), f"replay not deterministic: {k}"
    print(f"  replay determinism: OK ({len(a)} buses bit-identical across runs)")


def test_verify_render_report():
    project, root, _live, _m = _make_project("verify")
    result = verify_render(project, root)
    assert "stems" in result and "pass" in result
    assert set(result["stems"]) >= {"drums", "bass", "mel", "fx", "samples",
                                    "master"}
    # shipped stems are float32 WAVs; drums/bass/samples must round-trip exactly
    for bus in ("drums", "bass", "samples"):
        st = result["stems"][bus]["status"]
        assert st in ("exact", "float_equiv"), f"{bus}: {st}"
    for bus in ("mel", "fx"):
        st = result["stems"][bus]["status"]
        assert st in ("exact", "float_equiv", "approx"), f"{bus}: {st}"
    report = format_verify_report(project, result, root)
    assert "TIMBOR RE-RENDER VERIFICATION" in report
    assert "RESULT:" in report
    assert "IDENTICAL" in report  # at least drums/bass/samples reach this tier
    print(f"  verify_render: OK (pass={result['pass']}, "
          f"samples {result['samples_resolved']})")


def main():
    global TMP
    TMP = tempfile.mkdtemp(prefix="timbor_rerender_")
    try:
        for t in (test_replay_tiered_comparison, test_replay_deterministic,
                  test_verify_render_report):
            name = t.__name__
            try:
                t()
            except AssertionError as e:
                FAILED.append(name)
                print(f"  {name}: FAIL — {e}")
            except Exception as e:
                FAILED.append(name)
                import traceback
                print(f"  {name}: ERROR — {type(e).__name__}: {e}")
                traceback.print_exc()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    if FAILED:
        print(f"\nFAILED: {', '.join(FAILED)}")
        return 1
    print("\nAll re-render tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
