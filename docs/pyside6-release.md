# PySide6 official local version

## GitHub automation

Software tests, portable Windows EXE builds and tag-based GitHub Releases are
configured in [GitHub release instructions](github-release.md). The source
launcher remains available. Cloud software verification does not replace
physical instrument acceptance.

## Qt Quick update

The current root launcher opens the Qt Quick/QML frontend. Its migration,
dependencies, validation commands and fallback are documented in
[Qt Quick migration](qt-quick-migration.md). Close normally and run
`Transport_App.bat --widgets` to use the retained Widgets frontend with the same
environment. No restoration of the historical backup below is required for this
frontend fallback. The earlier promotion record below is historical.

The project root now contains the PySide6 implementation, including workspace
layout, manual response improvements, sweep precision validation, completed-history
ETA calibration, and Lock-in refresh recovery. Start it with the root
`Transport_App.bat`; it uses the root `.venv` and `requirements.txt`.

The previous working tree (including uncommitted changes) was preserved at
`tmp/pre_pyside6_20260929_125757`. Its manifest lists replaced and added files.
To roll back when the app is closed, restore replaced files from that backup
and remove only the added files listed in its manifest. The backup also preserves
the previous `.gitignore` and local shortcut. No Git reset is needed.

The migration folder is retained for reference, not the official launch location.
Do not use its BAT for subsequent releases. Running processes do not reload this
release automatically; close normally when idle and relaunch the root BAT.
No running application was stopped and no hardware was accessed for promotion.
Real hardware acceptance is separate from offline verification.

At the initial local promotion, no GitHub upload, commit or push was performed. Local environments, migration
copies, backups and generated verification artifacts are excluded from Git.

## Included behavior

- CoSweep axis values support six decimal places. Cached MODEL 2400 and 2401
  source ranges determine programming resolution; the 2401 supports the
  0.2/2/20 V ranges only. Start checks connections, source mode and busy hardware
  before resolution. Missing ranges on supported models are refreshed in the
  background; review the result and press Start again to begin. Messages identify
  the source, model, cached range and readback failure. Unknown resolution and
  invalid physical targets still block acquisition without silent rounding.
- ETA uses completed Ave=1 (1111 points / 1430 s) and Ave=3
  (20541 points / 47944 s) references for matching YZ324 hardware/trajectory.
  Ave=2 is interpolated; larger averages are extrapolated. This supersedes
  the provisional Ave=3 estimate in the September 28 calibration note.
- Quiet Keithley readback completion restores Lock-in panel availability,
  while active sweep ownership continues to lock its commands.

Run an isolated offline test with `.venv\Scripts\python.exe -B verify_offline.py
tests.test_pyside6_startup`. Run each module in a separate process. The wrapper
blocks hardware/network access and isolates user settings.

Promotion validation: 413 tests across 36 separately isolated modules passed.
One legacy argument-routing fixture needed cached model/range data for the new
precision preflight; its rerun passed. Full local results are recorded in
`tmp/formal_validation/summary.json`. `git diff --check` also passed.
