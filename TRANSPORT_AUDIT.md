# Transport fixes and audit — 2026-09-05

## Changes

- Gate Scan and B-field Gate Scan no longer apply a physical ±20 V spinbox cap to doping/E-field coordinates. The UI shows the allowed interval calculated from the ratio, held coordinate, and configured/applied physical gate limits. Invalid recipes are rejected instead of silently changing entered values. Both ratio targets and negative ratios are supported.
- Frozen batch trajectories are checked before magnet movement. The line-sweep worker also checks configured physical limits and rejects non-finite setpoints.
- Analog lock-in gain is now `10 / sensitivity_in_volts`, shared by connection calibration and signal-chain snapshots. Preamp sensitivity remains A/V, so its gain is `1 / sensitivity`. X/Y current is `DAQ_voltage / (preamp_gain * lockin_gain)`; DC current has only preamp conversion. Gate scans, dual-gate, co-sweep, photocurrent, and B-field transport consume the corrected calibration.
- B-field gate batches retain a timestamp and unique series suffix for CSVs, metadata, logs, and checkpoints. Repeating identical conditions preserves prior output. Duplicate normalized field targets within a single batch remain explicitly rejected by the existing recipe validator.
- Shared run IDs and worker fallback IDs contain a random suffix component as well as a timestamp, avoiding same-second collisions. Existing-file protection remains enabled. Common output previews now show the timestamp-bearing filename.
- B-field transport condition filenames include doping, E-field, ratio, and ratio target, including conditions entered through the raw-gate conversion UI.

## Evidence and verification

The SR830 manual specifies analog X/Y gain as 10 V divided by sensitivity:
https://www.thinksrs.com/downloads/pdfs/manuals/SR830m.pdf
The former `sensitivity * 1000` formula agrees only at 100 mV.

Regression coverage includes both ratio targets, negative ratios, unequal gate limits, derived coordinates above 20, invalid physical endpoints, multiple lock-in/preamp ranges, repeated completed batches, overwrite rejection before movement, condition filename contents, and fixed-clock run uniqueness.

All test modules were run in separate processes. Their assertions passed. The Keithley protection module reports 35 tests OK, then exits abnormally on this Windows environment; this was also reproduced with the original HEAD application modules. A single-process full discovery run therefore does not complete cleanly. Targeted transport regression runs complete normally.

## Practical limits

No live instrument operation or bench calibration was performed. The analog conversion assumes X/Y voltage outputs with zero offset and unity expansion, as documented by the helper; instrument offset/expansion and actual wiring still require bench verification. The preamp gain expression was already correct for A/V sensitivity; the range-dependent error was in the lock-in factor. Existing data files and saved calibration values were not rewritten.
