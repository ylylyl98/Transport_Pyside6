# Qt Quick migration

The migration keeps the Python acquisition workers, instrument owners, safety
checks, file formats, and independent analysis/export processes. QML owns the
presentation only. A view must never become the owner of an instrument session.

## Delivery gates

1. Inventory and baseline: all six measurement modes, instrument configuration,
   manual controls, signal chain, thermal/magnet commissioning, history filters,
   plots, exports, settings, and shutdown are part of the compatibility contract.
2. Backend boundaries: immutable run parameters, field-addressed validation,
   explicit lifecycle state, and asynchronous confirmations. Cleanup completion,
   not the last recorded point, releases instrument ownership.
3. Offline Quick shell and plotting: native Quick items, bounded display work,
   full raw data retention, keyboard navigation, scaling, and error reporting.
4. End-to-end Gate Scan: preflight, acquisition, stop, cleanup, data and metadata,
   range policies, and settings round trip with simulated instruments.
5. Feature parity: remaining modes, instrument panels, temperature systems,
   independent history, normalization, maps/cuts, and snapshot export.
6. Release: isolated regression modules, QML load/render checks, reproducible
   dependencies, migration/rollback documentation, then laboratory acceptance.

Offline checks cannot certify laboratory hardware. The Widgets entry remains
available during verification so a failed release can be rolled back without
rewriting user data or settings. No running acquisition is automatically moved
between frontends.

## Baseline findings

The previous main window is `app/ui/main_window.py`; its explicit stop and
multi-stage close contracts must be preserved. `app/ui/tabs/base_tab.py` stores
settings by widget type, which the Quick backend replaces with model values.
Gate Scan, Vds Sweep, CoSweep, and Photocurrent reconstruct Matplotlib plots from
all points on updates. Quick display refresh must be independent of acquisition
and CSV output. History discovery/loading already uses background tasks and a
bounded numeric cache; it stays in its own process.

## Verification protocol

Run each module in its own process using
`.venv\Scripts\python.exe -B verify_offline.py tests.<module>`.
The wrapper isolates settings and blocks hardware/network access. QML preview
must install the same guards before importing instrument controllers. Keep
generated screenshots, reports, and benchmark output under ignored `tmp/`.

Acceptance compares parameter values, trajectory, units, NaN gaps, direction and
condition segmentation, CSV/metadata, manual zoom, full-sweep range, four-channel
layout, stopping/cleanup, late callbacks, and shutdown failures. Display-only
decimation must never modify saved records. Check 100%, 125%, 150%, and 200%
scaling and both themes. Measure event-loop response with slow fake I/O and large
datasets before changing the production launcher.

## Implemented frontends and boundaries

| Stage | Implementation |
| --- | --- |
| 1 | Existing six-mode parameters, output formats, safety/cleanup and history contracts mapped to the existing workers and engines; legacy frontend retained for comparison. |
| 2 | `parameters.py` provides typed, finite-checked fields and old-setting migration; `MeasurementService` reserves devices before asynchronous preflight and releases them after thread cleanup; confirmations are asynchronous and generation-bound. |
| 3 | `application.py` owns persistent Python services; QML provides the main shell, forms and native dialogs. `PlotItem` uses QQuickPaintedItem/QPainter, full numeric buffers, viewport envelopes and coalesced notifications. No QWidget or Matplotlib canvas is created for the Quick views. |
| 4 | Gate Scan uses the existing LineSweepWorker with real parameter objects, metadata/calibration capture, output planning, record streaming, stop and safe cleanup. Fake-device integration exercises actual CSV writes. |
| 5 | Vds Sweep, 2D Map, Photocurrent and both magnetic modes reuse their actual workers/engines. Instrument, lock-in, magnet and temperature panels have QObject presenters. History runs independently with native curves/maps/cuts, ratios, normalization and immutable snapshot export. |
| 6 | The launcher defaults to Quick, checks QML offline after source/environment changes, pins dependencies, preserves Widgets fallback and provides isolated regression/render/benchmark commands. Physical acceptance remains a laboratory task. |

Instrument access stays in existing managers, controllers and task lanes. QML
receives values and issues validated commands. It cannot own a VISA, DAQ or SDK
session. An acquisition remains active through stop, in-flight I/O and cleanup;
the next run cannot steal its devices. Failed shutdown keeps the window and
controller owners alive. STOP also requests LS335 heater OFF for the 1000, voltage
zeroing and magnet pause; it does not mean the magnetic field has reached zero.

Main-process plotting and history display use Qt Quick painting. Matplotlib is
retained for detached publication PNG rendering and the optional Widgets
fallback. Display reduction keeps extrema, gaps and segment boundaries; exported
CSV and immutable snapshot arrays retain full samples. No Qt Graphs dependency
is introduced.

## Launch, preview and rollback

From the repository root:

```powershell
# Production: the BAT checks installed dependencies and rechecks changed QML/source.
.\Transport_App.bat
# Same environment, retained original Widgets frontend.
.\Transport_App.bat --widgets
# Independent synthetic preview: hardware/network blocked; isolated settings.
.\tools\Transport_Preview.bat
.\.venv\Scripts\python.exe -B transport_quick_preview.py --check
.\.venv\Scripts\python.exe -B transport_quick_preview.py --mode 1 --panel 2 --dark --screenshot tmp/quick.png
```

Ctrl+Shift+R rebuilds only the isolated Quick preview process from disk. It keeps
its temporary settings across rebuilds, including selected mode/panel. Closing
the supervisor removes that temporary settings directory. The old Widgets
preview remains `transport_ui_preview.py`. Reload analysis in the production
View menu replaces only the history/renderer processes after accepted exports
drain. Main QML, backend, driver and dependency changes require an idle restart.

For frontend rollback, close normally and launch with `--widgets`; do not reset
Git, delete data, or replace configuration. Quick first reads legacy measurement
and instrument keys, then stores its measurement drafts under `quick/measurements`
and window state under `quick/window`. It leaves legacy measurement keys intact.
Instrument/configuration keys remain shared, so review them after switching
frontends. A running measurement is never transferred between them.

Python 3.13.9 and the pinned versions in requirements.txt are the local validation
baseline. The permitted Python range is 3.12–3.14; other versions in this range
still need environment validation on each computer. An existing incompatible
`.venv` is not overwritten. When idle, preserve it under a chosen backup name,
create a fresh `.venv` with the intended interpreter, and install requirements.
Keep the backup until offline checks and laboratory acceptance succeed. Do not
copy virtual environments between computers; install the same pins there.

## Desktop layout update

The four layout batches target 1920 × 1080 displays at 100–125% scaling:

1. Parameter meaning and reachability: B-field Gate Scan has one selected
   execution-condition editor; B-field Sweep uses a shared ratio with calculated,
   read-only Vtg/Vbg. Rejected edits retain valid values, display local errors and
   block Start until corrected. Long safety confirmations scroll above fixed
   Yes/No actions; No keeps initial focus.
2. Measurement workspace: a 400-logical-pixel parameter pane and a flexible plot
   replace three permanent narrow columns. Instruments open below the header
   without blocking Stop. Wide windows can pin them; narrow windows use
   Parameters/Live plot buttons. All six forms put trajectories first, condition
   lists support selection/duplicate/removal, and advanced settings collapse.
   Window size is bounded by available screen space; splitter choices and
   per-page scroll positions are retained. Expand supports dirty and read-only
   text without dropping edits when a model refreshes.
3. Instruments: Connections groups each gate's mode/protection/Apply; Manual
   keeps commanded and measured values beside actions; Magnet separates field
   and temperature actions, commissioning, heater bands and diagnostics. Primary
   and Drive lock-ins have distinct identity and capability-dependent groups.
   Sample, Signal and Runtime use compact labeled sections. Eight native scroll
   views retain each panel's scroll and editor state.
4. History and common controls: compact plot tools keep advanced axes/layout and
   full legends in View; empty channels have explicit messages. History uses one
   export action with map/cut scope, responsive heatmap/cut tabs, visible selection
   counts, explicit empty/updating states, and scrollable/copyable errors. Fixed
   ranges expose units and local validation while retaining the last valid view.
   Main and History share semantic colors and Fusion controls.

Layout verification uses actual QML rendering with isolated settings and hardware
guards, at both target scales and in light/dark themes. It includes the 800 × 600
main shell and 1100 × 700 history minimum, long confirmation text, empty/busy/error
states, and per-section scrolling. Runtime checks exercise Stop with the drawer
open, first-switch scroll restoration, dirty text expansion, pending-invalid-edit
Start rejection, selected condition routing, range validation, and export paths.
Screenshots and per-module logs are local ignored artifacts under
`tmp/ui-layout-implementation`, `tmp/verification/instrument-*` and
`tmp/verification/quick_history_ui_review`; physical instruments were not used.

The layout update passed **178 tests in 12 isolated modules**: 108 in the eight
Quick modules and 70 in the affected magnetic sweep, derived map, region and
runtime modules. The final report with source hashes is
`tmp/ui-layout-implementation/final-validation.json`. The guarded preview entry
also passed `transport_quick_preview.py --check`. Native file-picker interaction
and physical instrument acceptance remain separate from these offline checks.

## Responsiveness update

The responsiveness investigation used isolated settings, synthetic records and
hardware access guards throughout. No real instruments were connected or used.
The measured causes and changes are:

- QVariantList replacements rebuilt whole parameter forms and discarded focus
  and unsaved text. Forms now retain indexed editor instances, update values
  without replacing the editor tree, and distinguish a new field/recipe identity.
  Collapsed trajectory review does not build a full map on each edit.
- Generic instrument notifications refreshed configuration forms and unrelated
  main-window properties. Configuration, sample-folder and telemetry signals
  are now separate; safety/command/status notifications remain immediate.
  Instrument pages load on first visit and then retain their controls/scroll.
- Panning scanned all samples and rebuilt completed curve paths on every frame.
  Finite segments and monotonic direction are cached by data revision; monotonic
  viewports use binary search, and unchanged paths remain cached. Cache entries
  are bounded per displayed trace/channel. Full numeric data and exports remain
  unchanged; non-monotonic paths retain the general visibility algorithm.
- History search repeatedly formatted metadata, and dragging rewrote all view
  settings. Index workers now prepare display/search metadata. Panning notifies
  range controls only and coalesces settings writes while export/reload still
  capture the current view immediately.
- Normal startup imported the unused LightField/.NET runtime and repeated pip
  plus full preview checks. LightField loads only when explicitly requested.
  The launcher reads installed dependency metadata first, including active
  transitive dependencies. Missing/mismatched dependencies still invoke pip.
  Only successful preflights with matching source hashes, Python/dependency
  versions and startup-related environment variables are reused. Source changes,
  corrupt cache or a failed check cannot produce a successful cached check.

Same-workload local median measurements (Windows 11, Python 3.13.9, Qt 6.11.2):

| Synthetic scenario | Before | After |
| --- | ---: | ---: |
| 10,000-point map, isolated parameter form edit + event drain | 115.56 ms | 3.10 ms |
| Instrument readback notification burst | 26.66 ms | 6.08 ms |
| Four channels × one million samples, pan a 1% viewport | 42.04 ms | 5.41 ms |
| Same plot, full-width viewport pan | 52.10 ms | 18.07 ms |
| Filter 5,000 history entries | 108.41 ms | 5.12 ms |
| Repeated unchanged startup preflight step | 6.28 s | about 0.43 s |

These are workload measurements, not frame-rate or physical-I/O guarantees.
The separate full-window benchmark at 125% (1536 × 864 logical / 1920 × 1080
rendered pixels) observed a 10.38 ms median for parameter edit/event drain and
3.98 ms for synthetic telemetry; all six mode switches reached a rendered frame
in 73–94 ms. This full-window workload is different from the isolated-form
comparison above. First-time cache construction and growing/non-monotonic
curves still require work; those costs have not been hidden from cold metrics.

Reproduce the current state without instruments:

```powershell
.\.venv\Scripts\python.exe -B tools/benchmark_quick_ui.py --scale 1.25 --width 1536 --height 864
.\.venv\Scripts\python.exe -B tools/benchmark_quick_plot.py
# Force a new guarded preflight instead of using its cache:
.\.venv\Scripts\python.exe -B tools/check_transport_startup.py --force
```

`TRANSPORT_VALIDATE_STARTUP=1` also forces a new preflight from the BAT.
Preflight caches live inside ignored `.venv`; deleting them only makes the next
launch recheck. `transport_quick_preview.py --check` always performs a fresh
check. The responsiveness changes passed 172 tests in 15 isolated modules;
reports are under `tmp/responsiveness/`, with full-window benchmark reports
under `tmp/verification/quick_ui*`. Tests cover persistent input/focus, rejected
input and recipe routing, telemetry refreshes, plot cache invalidation, range
state, exports, fake-device stop/response, and startup cache invalidation.

## Review corrections

The follow-up review identified five regressions beyond the initial responsiveness
tests. The corrections cover these contracts:

- A connected gate requested at 0 V is still validated, reserved and passed to
  the acquisition worker so it is ramped to zero before sampling and during
  cleanup. A disconnected gate remains optional where a zero fixed bias permits
  it. Photocurrent applies this behavior through the validated Quick path;
  the retained Widgets worker contract stays unchanged.
- B-field Sweep consumes the calibration captured by asynchronous preflight;
  starting its controller does not repeat a lock-in read on the GUI thread.
- Four-channel exports retain real signal identifiers and enough view metadata
  to restore their channel layout and limits in History.
- A newly selected history type starts with its own automatic bounds when it
  has no saved viewport. Switching back and refreshing retain that type's view.
- Sample/file editors retain their objects and keyboard focus when a field is
  committed, so Tab continues to the next input.

These paths are checked with synthetic CSVs, QML keyboard events and fake
instruments under the offline guards. They do not constitute hardware acceptance.
The final review-fix validation passed **351 tests in 25 separately isolated
modules**, plus a forced guarded startup preflight. The restored four-channel
History window was rendered and inspected with zero QML warnings. Evidence is
recorded in ignored `tmp/review-fixes/final-validation.json` and the accompanying
module logs and preview.

## Layout and workflow refinement

The main window now exposes Connections and Sample & files directly, and shows
the intended save destination even before the first run creates that folder.
The phase label distinguishes idle from error. Long application messages occupy
one status line; Status details provides selectable, scrollable text without
blocking the stop controls. Escape closes the details popup.

Instrument pages are ordered around setup, while retaining their stable section
IDs and persistent editors. Manual targets and ramp/read actions have explicit
labels. Lock-in automatic actions are collapsed below settings, and protection
and commissioning information remain visible. Measurement forms identify the
condition being edited, reveal the selected recipe row, and use consistent
Advanced labels without rebuilding field editors or eagerly generating previews.

History filters sit above the file list, while restore/export actions sit above
the comparison. Axis and color ranges open in a dedicated dialog with the same
validation and view state as before. Ctrl+F focuses search; F5 refreshes the list.
Plot toolbars use Reset view and Plot options to clarify their scope.

This refinement passed **144 tests in 12 isolated offline modules**. Main-window
and history keyboard workflows were also checked at 125% scale. Guarded renders
cover all six measurement modes, eight instrument pages, and history in light
and dark themes at 100% and 125%; the main window was rendered at 1920×1080
physical pixels. Small-window checks retain reachable start/stop controls at
800×600, including after long multiline errors. No real instruments were used.
Evidence is under ignored `tmp/workflow-final`, `tmp/workflow-final-125`,
`tmp/workflow_main`, `tmp/workflow_instrument`, and
`tmp/verification/{measurement_audit,history_workflow}`.

## Workspace partition review (2026-10-27)

The workspace uses fixed region titles for measurement setup, live data, and
instruments. Instruments open on the right; Pin keeps them beside the plot at
1920×1080/125%, with an automatic drawer fallback on narrower windows. Resizing
and switching between drawer and dock keep the same editors. Setup can be hidden
and restored, and the measurement log has its own bounded panel and Close action.
Opening the log in a narrow window reveals the live-data page.

Recipe editors have a separator and inset distinct from their list and actions.
The final optional section group is labeled Additional settings, preserving
native Tab order and the existing section instances. Each source address now
sits beside its protection settings and Apply action. Other instrument pages
use clearer connection, readback, target/action, and diagnostic groups. History
uses matching pane insets and visible draggable dividers; heatmap/cut captions
stay with their plot, and settings wrap as label/control pairs.

Final evidence combines **151 tests across 12 isolated offline modules**, plus
36 application/form/workflow checks at 125%. Light/dark renders cover six modes,
eight instrument pages, history, dock/drawer transitions, and the small-window
fallback, without accessing real instruments. A pending recipe-list reveal now
ignores its destroyed list after a rapid mode change. One initial 125% test read
the scroll position before its queued restoration under concurrent rendering;
it now waits for the actual position, with a bounded deadline. Both scales then
passed independently and concurrently; production scroll timing was unchanged.
The initial failure and final evidence are retained under ignored
`tmp/partition_review/final-validation.json` and its referenced logs.

## History responsiveness review (2026-10-27)

The history viewer remains an independent process. Its previous unchanged-file
cache already avoided CSV parsing and plotting, but each four-second check still
reformatted all metadata and notified the entire UI. A scan longer than the
interval accumulated another automatic request, so scans could run continuously.
Changes to any file also rebuilt the selected curves, including when the changed
file was not selected.

Automatic checks now wait four seconds after scan completion, reuse formatted
rows, and remain quiet when the index and warnings are unchanged. Selected CSV
and sidecar signatures determine whether plots need rebuilding; deletion and
measurement-type changes still invalidate the affected view. Explicit Refresh
and folder changes retain their pending requests. A background check allows
exporting the immutable displayed snapshot; actual plot updates still gate
export readiness.

Search updates only its own property while typing, then applies one filter after
150 ms of inactivity. Clear actions, folder changes and session capture handle
pending input. QML uses the existing list count rather than converting the whole
file list again for labels, and caches the map source model while locating the
selected item. Closed Plot options no longer creates all legend labels; opening
it still exposes every trace.

Run the hardware-blocked synthetic benchmark with:

```powershell
.\.venv\Scripts\python.exe -B tools/benchmark_quick_history.py --scale 1.25
```

The local 1,000-file / 100,000-point check at 1920×1080 physical pixels measured
search key-event processing at **76.22 → 20.53 ms median**, **113.58 → 31.65 ms
maximum**. Warm indexing measured about **553 → 226 ms** on the worker. Unselected
file growth changed from one curve read/publication to zero, while selected
growth retained all 100,001 points and immutable export arrays. These are
offscreen observations, not latency guarantees; the report separates worker
time, GUI processing, and intentional settling waits. First or changed large
CSVs still need parsing, and drawing very many separate curves remains costly.
The baseline and final reports are under ignored `tmp/history_perf/`.
Final verification passed **97 tests in seven separately isolated modules**,
plus **27 history-workflow and plot tests at 125% scale**, with no hardware
access. The benchmark passed its selection, search, scrolling, panning, snapshot
integrity and clean-shutdown checks with zero QML warnings. The combined test
evidence is `tmp/history_perf/final-validation.json`.

## Repeatable release evidence

```powershell
.\.venv\Scripts\python.exe -B tools/verify_quick_release.py --jobs 1
.\.venv\Scripts\python.exe -B tools/benchmark_quick_plot.py
```

The release runner invokes `verify_offline.py` once per test module in separate
processes, with a log per module and summary under ignored `tmp/quick-release`.
Use sequential execution for the release sweep: legacy GUI tests include short
renderer deadlines and can fail under competing rendering workloads.
The benchmark is synthetic and reports the environment and measured timings;
its results are observations, not frame-rate guarantees. Four million retained
samples (one million per channel) reduced to about 1,403 displayed vertices per
channel on the final local run; cold four-channel painting measured 56.7 ms and
cached painting 9.7 ms (medians, offscreen on this machine).

The migration baseline on 2026-10-26 was **1,099 tests across 87 modules**, with
zero failures/errors/skips in their latest individual reports. The eight new
Quick modules account for 90 tests. Evidence is combined in
`tmp/qt_quick_migration/final-validation.json`; final Quick logs are in
`tmp/quick-final/`. All six modes and eight instrument pages load without QML
warnings or property exceptions. Previews at 100%, 125%, 150%, and 200% scale in
light/dark themes were rendered and visually inspected. A separate guarded
process confirmed the main backend imports neither QtWidgets nor Matplotlib.

The first parallel legacy sweep had a Windows access violation in the old
Widgets history test process, followed by an export timeout on one rerun under
load. The final complete 43-test isolated rerun passed; the transient crash's
cause was not established. A separate pre-existing policy failure in the
Widgets runtime settings was fixed by using the project's safe spinboxes; its
tests and runtime-settings tests passed afterward. The reports retain these
first-attempt results rather than treating the release as having passed first try.

Each measurement mode keeps its own plot data and manual view while switching
modes. Range/layout/toolbar preferences use `quick/plots`, separated for ordinary
and Drag/Drive use, and read old `plot_mode` preferences on first use. A new run
uses its recipe's channel/X selection and clears old zoom, retaining the selected
range policy and layout. Only scalar view preferences enter QSettings; numeric
samples stay in Python buffers.

Laboratory acceptance must still verify physical voltage ramps/readback,
compliance, AO isolation, stop/zero acknowledgement, APS100 persistent transitions,
LS335 heater and sensor failures, attoDRY2100 SDK stop acknowledgement, cable and
channel routing, and recovery after interrupted communications. Offline tests
exercise those contracts with simulated devices, not the real equipment.
