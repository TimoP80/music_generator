# Ableton Live import kit — TIMBOR 162.0 BPM F natural_minor

TIMBOR does not write `.als` files (undocumented format; a synthesized one
could corrupt a Live session). This kit instead contains everything needed
to rebuild the track in Ableton Live in a few minutes using only documented
Live interfaces.

## Project facts

* Tempo: **162.0 BPM**, time signature **4/4**
* Key: **F natural_minor**
* Length: 192 beats (12 sections:
  intro → groove → theme → dev → break → build → drop → variation → break2 → build2 → drop2 → outro)
* Generator: TIMBOR v0.3.0,
  seed **424242**

## Import steps

1. **Set the session tempo** to 162.0 BPM (and enable the global
   quantization you prefer).
2. **Stems:** import the six bus stems listed in `stems.md` from the
   project's `audio/` directory onto six audio
   tracks named Drums / Bass / Melody / FX / Samples / Master. Drop them at
   bar 1 with warping **off** — they are already correctly timed and mixed.
3. **MIDI (optional, for editing):** drag `midi/timbor_jungle.mid` onto a MIDI
   track; Live splits it into the named tracks (Drums on channel 10,
   Bass, Melody, Chords; FX/Samples tracks carry text meta-events only).
   Set Live's MIDI import to *merge into one track* if you prefer manual
   routing.
4. **Samples:** every sample placement is documented in `samples.csv`
   (source path, role, bar position, pitch/stretch/reverse transform) so
   you can re-create the sample bus with your own Simpler/Sampler racks
   and the exact source files.
5. **Cue points:** section positions (INTRO, DROP, …) are in the manifest
   under `sections`; place locators at `start_beat / 4 + 1` bars.
6. **Verification:** the manifest in this kit is byte-identical to the
   DAW-neutral `export/timbor_jungle_export.json`.

## What is not included

* `.als` — intentionally not synthesized (see above).
* Instrument sounds — MIDI notes reference GM mappings (channel 10 drum
  map, melodic notes at written pitch); pick your own instruments.
* Mix/finalization — the exported stems are the mix; Live's master chain
  stays empty.

