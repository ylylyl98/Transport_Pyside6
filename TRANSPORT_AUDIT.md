# Transport fixes and audit — 2026-09-05

## Changes

- Gate Scan and B-field Gate Scan no longer apply a physical ±20 V spinbox cap to doping/E-field coordinates. The UI shows the allowed interval calculated from the ratio, held coordinate, and configured/applied physical gate limits. Invalid recipes are rejected instead of silently changing entered values. Both ratio targets and negative ratios are supported.
- Frozen batch trajectories are checked before magnet movement. The line-sweep worker also checks configured physical limits and rejects non-finite setpoints.
- Analog lock-in gain is now `10 / sensitivity_in_volts`, shared by connection calibration and signal-chain snapshots. Preamp sensitivity remains A/V, so its gain is `1 / sensitivity`. X/Y current is `DAQ_voltage / (preamp_gain * lockin_gain)`; DC current has only preamp conversion. Gate scans, dual-gate, co-sweep, photocurrent, and B-field transport consume the corrected calibration.
- B-field gate CSVs use their frozen recipe values, `Kth_G3_fwd` (or DAQ/bidirectional tags), a compact ratio such as `r1.15Vbg`, calibration tags, and a timestamp. Condition numbers and random strings are omitted. Identical recipes within a series use `_02`, `_03`, etc. Duplicate normalized field targets remain explicitly rejected by the existing recipe validator.
- Shared output planning and worker fallback paths use timestamps, adding `_02`, `_03`, etc. only when related output files already exist. Preflight and execution retain the same allocated paths; exclusive CSV creation remains the final overwrite protection. B-field series logs/checkpoints use a shorter series label rather than repeating the whole recipe.
- B-field transport condition filenames include doping, E-field, ratio, and ratio target, including conditions entered through the raw-gate conversion UI.

## Evidence and verification

The SR830 manual specifies analog X/Y gain as 10 V divided by sensitivity:
https://www.thinksrs.com/downloads/pdfs/manuals/SR830m.pdf
The former `sensitivity * 1000` formula agrees only at 100 mV.

Regression coverage includes both ratio targets, negative ratios, unequal gate limits, derived coordinates above 20, invalid physical endpoints, multiple lock-in/preamp ranges, repeated completed batches, overwrite rejection before movement, condition filename contents, and fixed-clock run uniqueness.

All test modules were run in separate processes. Their assertions passed. The Keithley protection module reports 35 tests OK, then exits abnormally on this Windows environment; this was also reproduced with the original HEAD application modules. A single-process full discovery run therefore does not complete cleanly. Targeted transport regression runs complete normally.

## Practical limits

No live instrument operation or bench calibration was performed. The analog conversion assumes X/Y voltage outputs with zero offset and unity expansion, as documented by the helper; instrument offset/expansion and actual wiring still require bench verification. The preamp gain expression was already correct for A/V sensitivity; the range-dependent error was in the lock-in factor. Existing data files and saved calibration values were not rewritten.


## Gate Scan default magnet rate (2026-09-06)

Gate Scan now explicitly programs and verifies its commissioned 0.0343 A/s default before each persistent move, independently of transport sweep RATE settings. The starting/target/output fields must stay within the configured safe field limit and 40 A; range 0 must match the commissioned 40 A boundary. Other current ranges and Fast Mode are unchanged. Rate readback failure blocks movement. The ramp watchdog uses twice the expected ramp duration plus 180 s (minimum 900 s); field stagnation for 120 s requests a pause. Existing instrument fault, communication, and thermal handling remain. The batch log reports rate and estimated ramp duration; checkpoints retain move audits even on failure.

The transport cleanup path already requests captured rate/limit restoration and records restoration failures; historical origin of the residual slow rate is not established. No live magnet operations were performed. Regression coverage includes residual slow settings, 8 T and polarity reversal, range/readback rejection, transport rate isolation, and stalled motion.


## Current-match settling audit (2026-09-06)

The 18:09:38 failure on the 2 T to 4 T transition was caused by IOUT changing from 1.9999 T before pause to 1.9979 T after pause, against IMAG=2.0005 T. The resulting 12.79 mA exceeded the unchanged 10 mA limit. The old heater-enable check used one post-pause reading.

Implemented in the real APS100 adapter: after pausing, require two consecutive current-match readings within the existing limit, spaced by 250 ms, with a 5 s / 20-read bound per verification attempt. If heater-OFF matching remains outside tolerance, allow at most two further FAST lead-matching attempts, each retaining the existing ramp timeout and heater-OFF verification. Never enable the heater after exhausted retries, nonfinite readings, faults, Stop, or a switch-state change. Progress logs show mismatch and rematching attempts. Heater warm/cool dwell durations are unchanged.

A similar single-reading check before heater OFF now uses bounded read-only settling. No FAST rematching is performed with the heater ON. Existing transition-confirmation and fault handling remain in place.

Other potential transient-sensitive checks reviewed:
- safe_move_to_field field-settling drift: one out-of-tolerance reading fails immediately.
- final target field and final lead-zero readback: single readings can reject a transient.
- zero_output requires automatic Standby following explicit SWEEP ZERO; this is distinct from accepting Pause at the Gate Scan handoff and was retained.
- pause and heater state transitions already use bounded confirmation loops.

The follow-up below replaces transient-sensitive final-reading checks with bounded settling; their physical thresholds remain unchanged. No real hardware was operated. Simulated tests cover the logged mismatch, transient recovery, exhausted retries, two consecutive matches, heater-OFF settling, stop, switch changes, faults, and nonfinite values.


## Stable final readings, verified resume and restoration warnings

Final target-field and lead-zero confirmation now requires two consecutive valid snapshots, with bounded 5 s settling and unchanged tolerances. Target-field settling allows a short recovery window before failing. Heater-state, sweep-active, nonfinite and hardware-fault checks remain blocking. Gate Scan displays matching thresholds, waiting phases and faults in the run status as well as the log.

The Resume button accepts a Gate Scan checkpoint and verifies the current frozen recipes and calibration, a contiguous completion cursor, and completed CSV/metadata presence and successful status in the selected output folder. Older checkpoints use completed signal-chain metadata as calibration evidence. Missing calibration evidence or changed settings block resume. Normal instrument/session/thermal preflight and persistent-field confirmation still run. Resume starts the first unfinished job using a new output series and records the source checkpoint; existing files and checkpoints are not overwritten. It does not resume partway through a gate trajectory.

Transport restoration results include current rate readback. Failed restoration emits a persistent banner and non-blocking warning dialog; later completion messages do not clear the banner. Successful restoration clears it. Tests cover transient final readings, persistent offsets, valid resume, changed recipe/calibration, missing output, inconsistent cursor, legacy calibration evidence, and restoration warning persistence. No live measurements or magnet moves were run.

## PySide6 main migration and local review (2026-10-07)

The original local fixes were migrated onto GitHub main at f04a2b1, preserving PySide6, Drag / Drive acquisition, history analysis and exports. The existing attoDRY1000 sample-temperature controls through Lake Shore 335 remain available: setpoint with readback, live sample temperature, optional stability gating, and confirmed heater-off. Setting a target does not enable an output whose heater range is OFF or alter commissioned PID/ramp settings.

The subsequent review corrected four uncovered cases. Persistent-field Gate Scan now stops acquisition on thermal warnings, sensor/communication faults or disarming, and checks cached telemetry freshness every 250 ms while measuring even if updates cease. Worker cleanup must finish before reservations are released. Transport thermal cleanup retains APS ownership when pause confirmation fails, without requesting an automatic heater transition or lead zeroing. Immediate APS move results are buffered until the request ID is available, retaining duplicate/stale-result rejection. Additional map regions use the main editor's six decimal precision, including 1 microvolt steps.

Validation: the migration passed 829 offline regression tests; the review fixes added 10 regression tests and passed 261 related offline tests covering field batches, transport recovery, UI behavior, map trajectories, output planning and shutdown. Real hardware and user settings were isolated during verification. These results do not establish instrument timing or bench behavior.

## Local attoDRY1000 sample-temperature controls (2026-10-07)

The shared Lake Shore 335 bar now includes explicit heater range and setpoint-ramp controls, confirmed target/range/ramp readback, periodic actual heater-output display, and configurable target/range bounds. Keep range/ramp is the default, preserving existing instrument settings. Inputs, units, PID, heater current/wiring and zone profiles are not written. Heater activation requires a valid sample sensor and confirmed preceding settings; a failed activation verification requests OFF on that same output and distinguishes confirmed from unconfirmed shutdown.

Temperature stability gates use the instrument-confirmed target, block pending/failed commands, reject stale/faulty/nonpositive sample readings, and reset dwell when the hardware target changes. The existing magnet thermal limits remain active; increasing sample temperature does not relax them. Operator instructions and command references are in [SAMPLE_TEMPERATURE_1000.md](SAMPLE_TEMPERATURE_1000.md).

Validation: 31 new tests and 248 distinct related offline tests passed (249 executions because startup was checked twice), including the real controller worker thread with a mock adapter, command/readback failure paths, UI readiness, existing field batches, transport recovery and startup. The bar was rendered and inspected at the normal window width. No real hardware was connected or operated. These changes remain local and uncommitted.

## Local automatic sample heater range (2026-10-07)

Auto by temperature selects a target's commissioned range from a saved application band table or the existing LS335 Zone table. No default temperature thresholds are invented. The new Auto ranges editor persists custom bands or the instrument-table source without issuing commands; Apply performs range selection and verified control writes. Invalid, uncovered, different-input and over-limit tables fail before writes. Manual ranges and Keep remain available.

Closed Loop PID uses the selected target range; an instrument already in Zone mode retains its native profiles and ramp, with no OUTMODE, PID or ZONE writes. Auto can resume a stopped output at a stationary setpoint using its target-zone range. A dedicated Heater Off stops an active Zone ramp before range OFF to prevent a later zone crossing from reactivating the heater; failed ramp-stop confirmation still attempts OFF and reports unconfirmed shutdown. UI previews and polling cannot re-enable heating.

Validation: 32 new regression tests and 175 related offline tests passed, covering table boundaries, invalid source tables, range ceilings, channel mapping, voltage outputs, configuration reload, editor validation, worker-thread Auto/Off ordering, native Zone shutdown, existing thermal protection, field batches and PySide6 startup. The Auto bar and editor were rendered and inspected. All code remains local and uncommitted; no real heater was operated.

## Separate 1000 and 2100 temperature controls (2026-10-07)

Temperature settings now live in Instruments > Magnet for the selected system: a compact attoDRY1000 / LS335 area with its existing Auto/range/ramp/stability controls, and the separate attoDRY2100 / SDK sample/VTI area. The main measurement page shows a read-only selected-system summary and a shortcut to its controls. One existing LS335 widget/controller is reused; targets, drafts, heater settings and wait state persist independently while switching, without issuing temperature commands. The 1000 stability gate remains limited to selected 1000 measurements.

Inactive temperature handlers cannot send Apply/Off/Stop commands to the other system. APS snapshots no longer overwrite 2100 magnet-temperature readback or SDK capabilities. Disconnect clears the 2100 display/cache/capabilities; reconnect requires fresh capability telemetry before enabling temperature control. Main summaries use confirmed targets and cached valid telemetry; stale, faulty and disconnected readings are marked unavailable.

Validation: 12 new regression tests and 177 distinct related offline tests passed (220 executions including repeat UI/command checks), covering system switching, command routing, late results, target isolation, capability refresh, stale summaries, existing start gates, field batches, thermal integration and startup. Both temperature panels were rendered and inspected in the 370 px instrument dock, along with the full main window. Real instrument access and user settings were isolated. No physical heater was operated; all changes remain local and uncommitted.

## Click-to-identify cryostat system (2026-10-07)

The operator-requested Detect & connect system button tries the configured APS100 VISA resource and attoDRY2100 SDK endpoint through the existing single-owner controllers. Identification awaits both connection acknowledgements: one success selects its temperature UI; two successes retain the selection and request a manual choice; no successes show unidentified status with diagnostics. Existing connections are preserved and repeated requests are blocked. The existing commissioning-review gate, connection telemetry and thermal protection remain active. Detection applies no temperature/field setpoints and does not infer a cryostat from LS335 or a port name.

Auto-select connected system also follows ordinary successful connection/disconnection signals. Manual selection disables Auto, including explicitly choosing the current item. Active measurements, magnet reservations/operations and temperature requests hold automatic selection until idle; startup does not initiate connections. Detection failures from an absent alternative backend do not label the successfully connected system as faulted.

The saved local 2100 SDK path pointed at a nonexistent legacy directory. Only attodry2100.sdk_directory was repaired to the existing SDK at D:\Instrument control v3\SpectralSweep-pyside6\CRYO2100. The original configuration is backed up under tmp/auto_system_20261007/user_config.before_sdk_path.json; all other values are preserved. No SDK/hardware was loaded for this path repair.

Validation: 18 new regression tests and 136 distinct related offline tests passed (198 executions including earlier/repeat UI checks), covering connection ordering, missing systems, two online systems, duplicate requests, exceptions, manual override, deferred selection, measurement holds, existing temperature routing, field batches, thermal integration, workspace and startup. Identification states were rendered at the normal 330 px panel content width. Hardware/network access and settings were isolated during tests; no live detection or instrument operation was performed. Application changes remain uncommitted and unpushed.

## Long text inputs and expanded editing (2026-10-07)

Operator, device ID, data root, AC contact, all six measurement filename stems, B-field Gate Scan field lists and condition names, and B-field Sweep quick-add coordinate/Vds lists now use full-width inputs with a selectable, automatically wrapped overflow preview. Preview height is bounded to five lines, with vertical scrolling for longer content. The trailing expand action or Alt+Enter opens a resizable editor with wrapping, Save/Cancel and Ctrl+Enter. Data root also offers a folder chooser.

Existing QLineEdit identities, signals, recipe getters and setting keys are preserved. Visual wrapping does not add characters to names or paths; expanded editing rejects actual line breaks in single-value fields. B-field and condition-list editors preserve supported multiline values and the existing parser order, duplicate checks, broadcast rules and limits. Expanded Save refuses oversize drafts instead of truncating, respects field validators and locks, and cannot overwrite a value reloaded while the editor was open. Settings restore refreshes previews even when input signals are blocked.

Validation: 17 new regression tests and 62 existing related offline tests passed, covering exact Unicode/path preservation, cancel/apply, keyboard editing, field locks, stale drafts, maximum length, signal-chain persistence, all filename/recipe interfaces, multiline list parsing, settings reload, output planning, workspace and startup. Controls were rendered and inspected at the actual 316–386 px measurement input widths, along with Sample / Files and expanded editors. Hardware access and user settings were isolated; all changes remain local, uncommitted and unpushed.

## Inclusive ranges and parenthesized point counts (2026-10-07)

At the operator's request, B-field Gate Scan and B-field Sweep coordinate/Vds editors share one bounded numeric parser. `(start, stop, count)` generates equally spaced values including both endpoints, with `linspace(...)` and `np.linspace(...)` as aliases. An unparenthesized comma list continues to represent explicit values. Groups may be mixed with lists or placed on separate lines; separators inside parentheses stay within the group. Only the numeric grammar is parsed, without evaluating Python code.

Colon ranges now include the requested stop. Decimal step arithmetic avoids accidental duplicate endpoints from binary rounding. A non-divisible span appends the exact stop with a shorter final interval; neither ascending nor descending ranges overshoot. Point counts must be positive integers, with two or more points for distinct endpoints. Expansion limits are checked before allocation and count both endpoints. Gate Scan retains normalized duplicate rejection and configured field limits; conditions retain existing scalar broadcasting and voltage protection. Saved condition-table precision remains six decimal places. Resume checkpoints continue using their stored explicit fields and existing recipe/calibration/cursor validation.

UI hints and README examples now describe the inclusive syntax. Older stop-extension workarounds must use the desired actual endpoint (for example, `-1:1.5:0.5` becomes `-1:1:0.5` for a scan ending at +1). User settings, stored recipes, measurements and checkpoints were not rewritten.

Validation: 14 new regression tests and 198 distinct related offline tests passed, covering point/list distinctions, named aliases, mixed/multiline groups, ascending/descending ranges, exact and shortened final intervals, decimal endpoints, invalid/nonfinite counts, malformed expressions, preallocation caps, duplicate/shared endpoints, magnetic-field and voltage limits, preview/start/save consistency, existing batch resume and thermal protection, filenames, output planning and startup. Both input forms were rendered and inspected at the normal panel widths. No real instruments were connected or operated; changes remain local and uncommitted.

## Main publication review (2026-10-07)

The operator requested review and publication of the accumulated temperature, system-identification and text-input changes. The review started from main at `6cdfeb413da083d7b4390f2b60c9b5a72d040cc5`, matching the freshly fetched GitHub main.

Ten added LS335 regression tests cover already active PID outputs with Keep range, failed setpoint/ramp confirmation, late sensor faults, shutdown confirmation failure, configured range ceilings, the compatibility setpoint API, and native Zone profiles. Unconfirmed writes on an already heating output now request and verify OFF on that same output. An existing range above the configured ceiling must be turned off before applying a new target. Keep in native Zone mode validates the complete commissioned table before a target change; manual range/ramp overrides are rejected in that mode. No PID, output-mode, sensor-setup or Zone-table writes were added.

Four added UI regressions cover pending SDK requests and overlapping 2100 temperature Apply/Stop results. Apply remains blocked until both requests finish, duplicate Stop requests are suppressed, unrelated telemetry errors cannot release the mutation guard, and synchronous request failures release only their own guard. Automatic system selection also waits for SDK requests to drain.

Final verification covered all 74 test modules, including the SDK controller suite: 948 distinct tests passed with zero assertion failures, test errors or skips. Hardware/network access was blocked and settings/output roots were temporary. The default single-process run exceeded its 150-second watchdog. Two combined UI batches exited with Windows native exceptions; one identical failure was reproduced against the unchanged main snapshot. All 12 affected modules were then rerun in separate processes and passed with clean exits. The complete accounting and logs are retained locally under `tmp/final_review_20261007`; this does not establish that combined-process UI testing is reliable on this Windows environment.

All 34 publication files passed Python syntax and whitespace checks. The local shortcut and temporary test/backup artifacts are excluded. The original `before-github-main-sync-20261007-003836` stash remains untouched. No live instrument connection, heating, magnet operation or bench verification was performed.
