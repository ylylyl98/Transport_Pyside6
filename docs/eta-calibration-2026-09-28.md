# ETA recalibration, 2026-09-28

## Evidence and limits

The only completed YZ324 derived Vds/Doping map remains run `919061ec6b1b_20260928_210934`: Ave=1, delay=0.4 s, 1111 points, 1430 s including cleanup. Its calibration remains unchanged.

New run `20260928_221602` is still running. Two other new runs stopped after only 10 and 20 points; their whole-run elapsed times were excluded because startup/stop cleanup would dominate.

The new provisional reference uses read-only snapshots of completed CSV rows during steady acquisition. The measurement was not interrupted. No experiment files were edited. Snapshot time is a monotonic clock; boundary uncertainty is approximately one point. Both windows below are in the first slow-axis pass, not independent runs or whole-map validation.

| Window | Completed rows | Elapsed seconds | Seconds/point |
|---|---:|---:|---:|
| Calibration, 22:17:52.618 to 22:19:42.628 | 40 to 87 (47 new) | 110.010 | 2.3406 |
| Subsequent check, through 2026-09-28T22:21:56.294771 | 87 to 145 (58 new) | 133.666 | 2.3046 |

Configuration: YZ324, Derived coordinates, fast Vds / slow Doping, Keithley bias, Ave=3, delay=0.4 s, Vds 0.8 to 1.3 V in 0.001 V steps; Doping -1.5 to 1.5 in 0.075 steps; fixed E=0, ratio Vtg=1. Total planned points: 20541. G1/G2/G3: GPIB0::2/3/1::INSTR, DAQ Dev1, lock-in GPIB0::8::INSTR. SR850 recorded time constant 100 ms, slope 12 dB/oct. This observation does not isolate DAQ read latency or establish settling adequacy.

## Implementation

Use rounded measured throughput 2.34 s/point for the Ave=3 reference. Subtract the model's ordinary-point cost (0.68 s) to get 1.66 s/point additional overhead. Add that residual to the existing trajectory model, retaining explicit delay, ramp, point count and final zero-return contributions.

Select the new provisional reference only for the existing matching sample/hardware/axis route with Ave>=3. Ave>3 still assumes an unverified 10 ms per extra read. Ave=1 retains the completed-run reference; Ave=2 uses its existing extrapolation. These separate observations must not be interpreted as a measured time per average. No extrapolation can establish that instrument settings/software were identical across the two runs.

The current configuration now estimates 13 h 21 min 11.28 s before starting. Live ETA still adapts from actual point durations and is unchanged. No extra runtime file reads, instrument queries, or per-point saves were added. Restart is needed for new code; the currently running acquisition was not restarted.

## Sources

- Metadata: [YZ324_1.67K0T_ACDCMoTe2E2_DCMoTe2E1_AmpMoS2E1_Vds0.8to1.3V_Doping-1.5to1.5_E0_rVtg1_17Hz_LIA200mV_Pre10nA_20260928_221602_metadata.json](D:/photocurrent/data/User/YZ324/2026-09-28/map_2d/YZ324_1.67K0T_ACDCMoTe2E2_DCMoTe2E1_AmpMoS2E1_Vds0.8to1.3V_Doping-1.5to1.5_E0_rVtg1_17Hz_LIA200mV_Pre10nA_20260928_221602_metadata.json)
- CSV: [YZ324_1.67K0T_ACDCMoTe2E2_DCMoTe2E1_AmpMoS2E1_Vds0.8to1.3V_Doping-1.5to1.5_E0_rVtg1_17Hz_LIA200mV_Pre10nA_20260928_221602.csv](D:/photocurrent/data/User/YZ324/2026-09-28/map_2d/YZ324_1.67K0T_ACDCMoTe2E2_DCMoTe2E1_AmpMoS2E1_Vds0.8to1.3V_Doping-1.5to1.5_E0_rVtg1_17Hz_LIA200mV_Pre10nA_20260928_221602.csv)

The source CSV is growing, so its present row count will exceed the captured snapshot counts. The calibration above is pinned to the stated observation windows.
