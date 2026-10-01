"""Determinism check: live render vs project replay (two-process, low memory).

Usage:
  python tools_replay_check.py save <tmpdir> [genre]    # live render -> buses + project
  python tools_replay_check.py compare <tmpdir> [genre] # replay project, diff vs buses

Stages the live buses as .f64 files so only one render lives in memory at a
time (the machine runs close to the RAM ceiling).
"""
import gc
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MODE, TMP = sys.argv[1], sys.argv[2]
GENRE = sys.argv[3] if len(sys.argv) > 3 else "gabber"
os.makedirs(TMP, exist_ok=True)

from timbor import Plan, render_track
from timbor.timeline.serialization import save_project, load_project
from timbor.replay import render_project, compare_buses
from timbor.samples.cache import SampleIndex
from timbor.samples.render import clear_audio_cache

PROMPTS = {
    "gabber": "1994 Rotterdam gabber anthem, dark but euphoric",
    "jungle": "90s jungle with reese bass",
    "trance": "euphoric trance anthem",
    "hard_house": "hard house banger",
}
GENRES_CFG = {"gabber": None, "jungle": "jungle", "trance": "trance",
              "hard_house": "hard_house"}

if MODE == "save":
    idx = SampleIndex()
    plan = Plan(PROMPTS[GENRE], genre=GENRES_CFG[GENRE], seed=424242)
    plan.sample_dir = "data/demo_library"
    song, buses, qc = render_track(plan, sample_index=idx)
    idx.close()
    save_project(os.path.join(TMP, "project.json"), song, song.timeline, qc)
    for k, v in buses.items():
        v.tofile(os.path.join(TMP, f"live_{k}.f64"))
    print(f"saved {GENRE}", {k: len(v) for k, v in buses.items()})
else:
    project = load_project(os.path.join(TMP, "project.json"))
    clear_audio_cache()
    rebuilt, diags = render_project(project)
    ok = True
    cmp = compare_buses({k: np.fromfile(os.path.join(TMP, f"live_{k}.f64"),
                                        dtype=np.float64) for k in rebuilt},
                        rebuilt)
    for k, r in cmp.items():
        print(f"  {k:8s} {r['status']:12s} max|diff|={r['max_diff']:.3e}")
        if r["status"] in ("different",):
            ok = False
    print("RESULT:", "PASS (within documented approximation tiers)" if ok else "DIFFER")
