# Dual lock-in drag / drive implementation plan

**Goal:** Acquire both currents at each existing scan point with independently verified lock-in calibration.

**Architecture:** Keep `lockin` as Drag for compatibility; add `lockin_drive` as Drive. Reuse the lock-in panel with an explicit session key. A frozen `drag_drive` calibration in the signal-chain snapshot feeds one shared DAQ sampling/conversion function. Existing scan workers append optional columns to their records/CSV. Single-instrument mode remains the default for existing configurations.

**Tech stack:** Python, PySide6, NI-DAQmx, PyVISA, unittest with `verify_offline.py` (hardware/network blocked).

**Spec:** Conversation-confirmed requirements: two SR850s, Drag GPIB 8, Drive GPIB 9 on the original interface; AI0/1 Drag, AI2 DC, AI3/4 Drive; SR570 on Drag, SR551 fixed gain 10 on Drive; Rs initially 100000 ohm and editable; existing measurement tabs; two Lock-in control subpages. SR551 presence is user-configured, not detected. Both sets of settings and the per-run Rs must be preserved in metadata.

## Constraints

- Do not operate connected hardware or restart the running app.
- Preserve unrelated local history/PNG work and the previous lock-in-control fix.
- Read all five channels in one DAQ task; do not claim simultaneous-sampling hardware.
- Require both control sessions, correct X/Y routing, zero offset, unity expansion and Drive A-B/external reference before a dual run. Reject duplicate addresses.
- Hold both lock-in resources during a dual run. Saved drafts never count as instrument verification.
- No automatic reference/phase/offset changes. SR551 gain 10 is an explicit configured constant.

## Tasks

1. [x] Add `app/drag_drive.py` and `tests/test_drag_drive.py`: test actual unequal sensitivities, changeable Rs, missing AI4 and single-mode compatibility before implementing common conversion. For DAQ drive=2 V, Sdrive=.1 V, G=10 and Rs=100000 ohm, expect 20 nA.
2. [x] Extend `Connections`, `DeviceManager`, parameterized `LockinPanel`, and `ConnDock`; test address/session isolation and DAQ AI4 registration. Add a compact signal-chain dual-mode configuration widget and Drag/Drive control subpages.
3. [x] Extend `SRSLockin` analog-output readback and `capture_run_signal_chain`: test atomic resource claims, unsupported outputs/offsets, independent sensitivities and immutable calibration. Feed the frozen metadata through existing run paths.
4. [x] Integrate common sampling into existing workers, append optional raw/derived columns with correct units, and expose result channels in plots/history. Test CSV row/header alignment and values from real offline worker runs.
5. [x] Run targeted offline regressions, inspect rendered UI, and request independent read-only code review. Resolve findings and document usage/limits. Leave changes uncommitted in the shared checkout.

Verification command starts with:

```powershell
.\.venv\Scripts\python.exe -B verify_offline.py test_drag_drive test_lockin_control test_srs_lockin test_lockin_metadata test_signal_chain_ui
```

## Delivery verification

- Final core/control/DAQ/Keithley regression: 115 tests passed.
- Final dual/B-field/2D regression: 88 tests passed.
- Additional history, PNG, workspace, timing and signal-chain suites passed in focused runs (counts overlap).
- 21 dedicated Drag/Drive tests cover unequal sensitivities, Rs changes, CSV/metadata, plot selection, exports, resource isolation and ordinary mode.
- Signal-chain dialog and main workspace rendered and visually checked offline.
- Independent read-only review rechecked fixes for address-sync ownership, B-field live channels and history units; no unresolved material findings.
- Usage and limits documented in docs/drag-drive.md. Hardware acceptance remains unverified; no instrument or running app was operated.
- Changes remain uncommitted; pre-existing history/PNG and lock-in-control work preserved.
