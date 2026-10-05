# Measurement and analysis PNG export implementation plan

> Execute inline, task by task, with failing regressions and independent review.

**Goal:** Save each available measured current channel separately and export analysis views using the approved PNG layout.

**Architecture:** A shared hardware-free renderer runs in an independent low-priority process. Acquisition completion submits frozen output paths only after thread/controller cleanup. Analysis exports immutable displayed arrays and current axis limits rather than rereading changing CSV files.

**Tech stack:** Python, NumPy, Matplotlib Agg, PySide6 QProcess, unittest and Pillow.

**Approved specification:** User conversation: four separate valid current channels; PNG 150 dpi; single/cut 1200×900, map+cut 2400×900; white background; axis labels 22 pt, ticks 20 pt, header/legend 16 pt; compact margins and light major grid. Titles derive from saved metadata; shared conditions in analysis header and differences in legends. Unknown conditions are never assumed zero. CSV/JSON are untouched. Stopped/error data carry their terminal status.

## Task 1 — Data, labels and rendering
Files: app/plot_export_labels.py, app/png_export.py, tests/test_png_export.py.
- [x] Test finite-channel selection, incomplete-run status, saved metadata titles, varying/common conditions, channel aliases, image sizes, exact direction/group splitting, immutable analysis arrays and sidecar settings.
- [x] Implement automatic output resolution from regular metadata, photocurrent csv_paths and B-field manifests. Skip true 2D grids; use saved resolved X axis for 1D maps.
- [x] Implement adaptive text fitting, engineering current units, fixed canvases, safe unique analysis names and source identities in view JSON.
- [x] Run focused renderer tests and inspect actual output PNGs.

## Task 2 — Process queue and acquisition completion
Files: transport_png_exporter.py, app/ui/png_export_process.py, app/ui/measurement_png_coordinator.py, app/ui/main_window.py, app/ui/history_viewer_launcher.py; tests/test_png_export_process.py.
- [x] Test a distinct hardware-free exporter PID and heartbeat responsiveness, deduplication, delayed cleanup submission, frozen paths and failed-job recovery.
- [x] Add a serial background renderer queue with below-normal process priority, error reporting and nonblocking start/stop. Add persistent default-on automatic channel PNG option.
- [x] Freeze current run paths on start; dispatch only after acquisition thread/controller is done. Never wait on acquisition or cleanup paths.
- [x] Verify startup and measurement responsiveness through the offline wrapper.

## Task 3 — Analysis UI and documentation
Files: app/ui/curve_compare.py, tests/test_curve_compare_ui.py, README.md.
- [x] Test export of displayed arrays/zoom/legend, map+cut and cut-only dimensions, transient selection protection and view restoration.
- [x] Add explicit export actions, background snapshot preparation and restore-view handling. Preserve current folder/type selections during exports.
- [x] Document image folders/names, scientific condition handling, optional automatic saves and process-resource limits.
- [x] Run combined affected tests, inspect normal and compact output images, obtain code review and inspect final diff.

## Verification

- Combined offline verification: 75 tests passed across PNG rendering/process/shutdown, curve comparison, workspace/startup/responsiveness, history/map/cache and viewer process.
- After final caption/grid adjustments: all 9 renderer tests passed; all 5 process tests passed, including the additional actual worker-restart regression (76 distinct tests in total).
- Production-renderer previews verified at 1200×900 and 2400×900, 150 dpi; actual viewer widgets inspected at 100% and 150% display scaling.
- Independent follow-up code review approved; 45 focused offline tests passed in review. No physical instrument run was performed.
