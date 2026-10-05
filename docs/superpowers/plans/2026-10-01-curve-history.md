# Curve History Implementation Plan

**Goal:** Browse current-device history and compare saved curves/maps without sharing the measurement GUI or instrument ownership.

**Architecture:** A Curve Compare tab is a lightweight QProcess launcher. The independent viewer has Qt model/view history pages, asynchronous file indexing/loading, Matplotlib plots, a bounded numeric cache and display-only QSettings persistence. IPC carries only folder, refresh, focus and close notifications.

**Tech Stack:** Python 3.10+, PySide6, Matplotlib, NumPy, unittest. No new dependencies.

**Spec:** ../specs/2026-10-01-curve-history-design.md

## Constraints

- User refinements: all dates newest first; separate measurement pages; 2D map then measured-coordinate cuts; isolated viewing process.
- Measurement/acquisition code and saved formats stay unchanged.
- Viewer imports no hardware/control modules, and runs with reduced CPU priority.
- Header-only indexing; full CSVs only for selected records; 64 MiB numeric cache.
- Native map PassIndex identifies slow rows. Default to all rows, normalize reverse/backward and never average duplicates.
- Preserve acquisition order and conditions; check fixed/varying/signal units against primary map.
- Only genuinely changed inputs invalidate background results. Persist choices per folder/type.

## Task 1: Read-only history and data models

Files: app/curve_history.py, app/curve_map.py, app/curve_cache.py and corresponding tests.

- [x] Write failing tests for nested records, statuses, legacy files, malformed/partial CSVs, directions and unit mismatch.
- [x] Implement cached indexing and selected-file snapshots.
- [x] Add real snake-map fixtures, exact cuts, row selection, duplicate rejection and primary-unit validation.
- [x] Add condition grouping, E-field/Keithley aliases and bounded-cache invalidation/eviction tests.
- [x] Run focused unittest tests through the offline wrapper.

## Task 2: Independent viewer workspace

Files: app/ui/curve_history_model.py, app/ui/curve_compare.py, transport_history_viewer.py, tests/test_curve_compare_ui.py.

- [x] Write failing Qt tests for all-date/type filtering, chronological sort, overlays, persistence and asynchronous changes.
- [x] Implement native history pages, zoom/save plot toolbar, map/fixed-coordinate controls and selected-file cache.
- [x] Apply user layout refinement: full-height file sidebar, draggable width and reversible expanded-list mode.
- [x] Verify unchanged polling cannot starve slow reads and axes/coordinate replacements recalculate cuts.
- [x] Render synthetic-data widgets at normal/small sizes and 100%/150% display scale; inspect screenshots.

## Task 3: Process and measurement integration

Files: app/ui/history_viewer_launcher.py, app/ui/main_window.py, README.md, tests/test_history_viewer_process.py and tests/test_workspace_layout.py.

- [x] Write failing tests for a distinct viewer PID, no hardware imports, folder notification, process reuse and separate measurement controls.
- [x] Wire the lightweight tab, source-folder edits and completion refresh; keep start/stop/shutdown ownership intact.
- [x] Verify process behavior and main-window startup/acquisition responsiveness with hardware blocked and temporary settings.
- [x] Request independent code review and repair important findings with regression tests.
- [x] Document current behavior and its shared-resource limits.
- [x] Run final combined checks and inspect final diff.
