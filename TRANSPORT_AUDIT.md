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
