# Analysis module reload implementation plan

> Execute inline in the existing codex/curve-history checkout, task by task with TDD and review.

**Goal:** Reload analysis/viewer and PNG-renderer code while Transport and measurements stay running.

**Approved design:** The user approved the previous chat proposal: a Reload analysis button preserves folder, selection, cuts and zoom; accepted exports finish before the processes restart. This is an extension of existing process lifecycles, not a new subsystem.

**Architecture:** The launcher asks the viewer to freeze a JSON session and finish accepted analysis exports before closing. It also requests a graceful measurement-PNG worker restart. New run jobs are durably staged during that restart and promoted after the old worker exits. The launcher reopens the viewer with its saved session only after both old processes finish. No instrument objects or acquisition callbacks are recreated.

**Tech stack:** Python, PySide6 signals/QProcess/QTimer, existing disk-spool worker, unittest offline wrapper.

## Task 1 — Restart PNG worker without losing exports
Files: app/ui/png_export_process.py; tests/test_png_export_process.py.
- [x] Add a real-process regression: finish old accepted jobs, change PID, retain new jobs submitted during reload, maintain GUI heartbeat.
- [x] Implement request_reload(), reload_finished signal, busy state, durable deferred job promotion and shutdown during reload.
- [x] Run focused process tests, including normal shutdown and worker-error recovery.

## Task 2 — Capture and restore analysis session
Files: app/ui/analysis_png_export.py, app/ui/curve_compare.py; tests/test_curve_compare_ui.py.
- [x] Test custom-folder/selection/filter/zoom restoration and map primary/cut/color-scale/layout restoration in a fresh widget.
- [x] Expose view capture without copying arrays; add JSON session capture/restore using existing controls and pending-limit application.
- [x] Run comparison/export tests and inspect controls at 100%/150% scaling.

## Task 3 — Reload controls, launcher and lifecycle
Files: app/ui/history_viewer_launcher.py, transport_history_viewer.py, app/ui/main_window.py, README.md; tests/test_history_viewer_process.py, tests/test_png_viewer_shutdown.py, tests/test_workspace_layout.py.
- [x] Test a real viewer PID change and state handoff, duplicate-click prevention, reload while export is pending, and reload with an active measurement panel.
- [x] Add Reload analysis controls using existing Qt styling; wait asynchronously for exports and graceful viewer closure, reopen from saved session, report errors separately from measurement state.
- [x] Cancel reopen on main shutdown; preserve accepted PNG jobs even during reload.
- [x] Document scope and verify affected tests, rendering and an independent code review. Leave changes available for user review without committing.

## Final verification

- 44 renderer/process/core tests and 45 GUI/workspace/responsiveness tests passed (89 distinct tests), with instrument access blocked and temporary settings.
- After the final session/layout repair, all 16 curve-comparison tests passed again. The corrected asynchronous date-fixture precondition and all viewer/shutdown tests also passed.
- Actual viewer controls inspected at 100% and 150% scale. Independent review verified shutdown successor handoff, startup failure/retry and final splitter/expanded-layout repair; all findings resolved.
- No physical instrument run performed. The first launch of this main-window change loads the new controls; subsequent analysis/renderer-only code updates use Reload without restarting Transport.
