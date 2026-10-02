# Changelog

## Unreleased (2026-10-03)

- Expanded Studio into a local-first music creation workspace with durable creation and take history, presets, batch generation, search, favorites, notes, retry controls, and sample-library scan cancellation.
- Added opt-in Stable Audio 3, YuE2, and ACE-Step generation clients and Modal deployment/configuration support. Remote providers remain disabled by default, use separate credentials, validate returned WAV files, and avoid retries that could duplicate paid inference.
- Extended the CLI and Studio API for remote music engines while keeping the deterministic procedural renderer as the default; improved sample rendering, scanning, selection, and sample-index lifecycle handling.
- Updated setup guidance, environment templates, and tests for the new Studio and optional generation paths.
- Refreshed the Machine Rave EP release metadata for equal-power crossfades and loudness matching.
