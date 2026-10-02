"""
TIMBOR — AI electronic music producer (low-power circuit edition).

Generates complete, coherent tracks (not loops) for hardcore / rave / DnB /
trance / hard house / big beat styles using rule-based musical intelligence:
genre packs, motif-based melodies, kick/bass coordination, arrangement and
energy curves, self-QC, and a mastering chain. Pure numpy.
"""
from .engine import Plan, render_track, GENRES
from .render import stereoize, master, write_wav
from .yue_engine import YueEngineProvider, EngineConfig as YueEngineConfig, GenerationRequest as YueEngineRequest
from .ace_step_engine import AceStepEngineProvider, EngineConfig as AceStepEngineConfig, GenerationRequest as AceStepEngineRequest

__all__ = ["Plan", "render_track", "GENRES", "stereoize", "master", "write_wav",
           "YueEngineProvider", "YueEngineConfig", "YueEngineRequest",
           "AceStepEngineProvider", "AceStepEngineConfig", "AceStepEngineRequest"]
