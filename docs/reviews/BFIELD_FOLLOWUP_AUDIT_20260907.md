# B-field follow-up audit — 2026-09-07

## Implementation update

The findings below describe the pre-fix audit. Production fixes have now been
implemented in this working tree:

- Persistent lead zeroing explicitly starts with ZERO SLOW, checks heater OFF
  and the commissioned VMAG limit, then selects ZERO FAST using the existing
  RATE 5. Actual magnet discharge stays SLOW. Matching already uses FAST.
  Transition logs identify the selected mode and FAST rate.
- APS movement and zeroing can outlive their initial timeout while making
  forward progress; lack of progress eventually pauses the supply. Initial
  transport positioning also derives its budget from distance and recipe rate.
- Fresh runs reset thermal/transition state and old restoration records.
  Configuration, bias, sweep-acceptance and endpoint-acknowledgement waits have
  a generous no-progress watchdog. Bias ramps and settling report progress.
- One-way batches reposition before the next condition. Round-trip batches
  complete the reverse leg before a per-row persistent transition.
- Thermal recovery preserves positioning/bias/endpoint continuations and cannot
  start a sweep while asynchronous bias work is unfinished. The requested bias
  settling time is executed and remains cancelable.
- Gate Scan cooldown Stop completes on pause acknowledgement. Same-field
  conditions revalidate the snapshot and thermal permission without another
  heater cycle. Fault/disconnect handling requests worker cleanup before release.
- Gate Scan publishes its terminal result after output cleanup and metadata
  writing. Failed zeroing is an error and blocks batch progression; resume
  rejects explicit failed safe-state evidence. Checkpoint errors are reported
  and block continuation, rather than silently losing resume information.

Validation: 20 new regression tests in `tests/test_bfield_workflow_recovery.py`,
plus the existing targeted transport, gate-batch, UI, thermal, and APS adapter
tests. Testing used simulated devices only. No live instrument command was
issued and no running measurement application was restarted.

Scope: additional findings after the previously reported timeout, restart-state,
Stop-during-cooldown, condition-order, and unused bias-settle problems.
Application code and instrument settings were not changed. Reproductions used
in-process fake instruments, the existing Qt test fixtures, and temporary files.
These findings establish software behavior; they do not establish that every
trigger has occurred in the recorded bench runs.

## 1. Same-field Gate Scan shortcut bypasses verification (high priority)

`app/engine/gate_scan_field_batch.py`, `_request_next_move`, takes the same-field
shortcut using the previous verified target and the current heater-OFF flag.
It calls `_start_measurement_after_move` before evaluating thermal permission,
and never calls `_verify_persistent_snapshot` on this path. Freshness, actual
field, faults, output current, and sweep state are not rechecked.

Reproduction: previous target 1 T, stale current snapshot reporting 2 T and a
quench, heater OFF, and thermal permission denied. The next measurement was
dispatched with requested field 1 T and snapshot field 2 T.

Required correction: revalidate fresh persistent and thermal state before every
condition, including conditions that do not need a new magnet move.

## 2. Gate Scan fault/disconnect does not terminate acquisition (high priority)

`_on_magnet_fault` fails the batch only in `verifying`; `_on_disconnected` only
clears the snapshot and persistent confirmation. Neither stops an active
measurement. Lake Shore updates likewise evaluate state without enforcing a
measurement-phase transition in this orchestrator.

Reproduction: deliver APS fault and disconnect while phase is `measuring`.
The phase stays `measuring`, the batch remains active, and no worker Stop is
requested. Combined with finding 1, invalid state can also reach later jobs.

Required correction: define explicit fault/disconnect handling in every phase,
preserving partial data and waiting for electrical-output cleanup acknowledgement.

## 3. Thermal recovery loses the original transport phase (high priority)

`app/engine/bfield_transport_controller.py`, `_request_thermal_pause` and
`_resume_after_thermal_recovery`, do not consistently preserve a continuation
for positioning or unfinished bias work. The default recovery branch calls
`_begin_leg` directly.

Two reproductions:

- Positioning has `_target=None`; recovery reaches `float(None)` and raises
  `TypeError`. This is an uncaught exception in this recovery path; no live
  application crash was induced.
- A real TaskLane bias job was held by a threading Event. Thermal pause was
  acknowledged and recovery invoked before releasing that Event. A sweep was
  dispatched while the bias task was still pending.

Required correction: preserve the interrupted phase, require the corresponding
operation completion, and resume positioning/bias preparation/sweeping through
separate continuations. Do not relax thermal thresholds.

## 4. Gate output-zero failure still reports success (high priority)

`app/workers/line_sweep.py`, `run`, emits `finished` before the `finally` cleanup.
Cleanup failures are logged and written into `safe_state`, but do not change
the terminal result. `GateScanFieldBatch._on_measurement_terminal` advances on
`finished` without requiring a successful safe-state result. Resume validation
checks metadata status but not `safe_state.ok`.

Reproduction: run the actual worker with simulated GPIB failures only during
final gate zeroing. It emitted `finished`, emitted no error, and wrote metadata
with `status="finished"` and `safe_state.ok=false`.

Required correction: produce the terminal result after cleanup; stop batch
advancement on failed zeroing and include safe-state evidence in resume checks.

## Verification boundaries

All reproductions above completed without hardware access. The asynchronous
bias case used the actual TaskLane and processed the Qt event loop. The cleanup
case used the actual LineSweepWorker and metadata writer, with acquisition and
instrument I/O replaced by fakes. No production fix is included in this audit.
