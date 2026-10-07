# Recent log review after the workflow updates

Read-only production-code review, 2026-09-07. No hardware commands were issued.
The latest available series log ends at 13:39:50. The Transport pythonw process
still has a start time of 2026-09-06 19:52:59; these logs are not validation of
the new on-disk implementation.

## Actual recent runs

Root: `D:\photocurrent\data\User\YZ289\2026-09-07`.

| Series filename timestamp | Recorded outcome |
| --- | --- |
| Transport `20260906_195304` | Actual start 11:56:24; failed at 12:02:02 during positioning; diagnostic phase `thermal_hold`; CSV has zero data rows. |
| Transport `20260907_120839` | Positioning completed 12:24:25; user Stop at 12:54:14 (29 min 49 s later); CSV has zero data rows. |
| Transport `20260907_130712` | Positioning completed 13:13:30; user Stop at 13:26:54 (13 min 24 s later); cleanup released ownership at 13:39:50; CSV has zero data rows. |
| Gate `20260907_002352` | Lead zeroing from 8 T timed out at 00:47:45 with approximately 1.731 T output remaining. |
| Gate `20260907_014020` | All three conditions at 8 T finished at 02:24:11; all three metadata files report successful output cleanup. |

## Remaining finding 1: new-move thermal permission interrupts ongoing positioning

The 12:02:02 error is `Safe magnet move failed: Magnet operation stopped by user`.
Unlike the subsequent two runs, this series has no corresponding `Stop requested
by user` line. Its diagnostic JSONL explicitly records phase `thermal_hold`.

The last readings include sample 3.842 K and reservoir 3.903 K, then 3.915 K.
The same day's preceding Gate Scan metadata stores reservoir recovery/warning/
trip limits of 3.9/4.2/4.8 K. Replaying the recorded readings using that saved
configuration gives SAFE at 3.862 K, then COOLDOWN_HOLD at 3.903 K, with reason
`Temperatures are below warning but have not reached recovery thresholds`.

The evaluator documents permission for a NEW persistent move. Transport uses
that permission for an ongoing driven positioning operation as well. The
recovery threshold therefore acts as a lower continuous-operation limit even
without a warning-level excursion.

Current-code reproduction: `_request_thermal_pause` during positioning, followed
by the adapter cancellation error, calls `_fail`. `MagnetController.pause` sets
the same stop event used by user Stop. The generic error handler and failed
safe-move handler do not distinguish this intentional thermal interruption.
The previous update corrected the successful-positioning recovery continuation,
but did not cover this cancellation path.

The replay and code path strongly support thermal interruption as the cause of
the recorded failure. The old run did not persist the actual thermal decision
or cancellation origin, so an external/manual pause cannot be excluded solely
from its series log.

Required correction: separate new-heater-cycle permission from permission to
continue an existing operation; preserve real warning/trip handling. Give
intentional controller pauses their own cancellation reason and resume path.
Do not simply increase temperature limits.

## Remaining finding 2: persistent-row transition can deadlock with thermal hold

Simulation-only finding, reproduced against current code. At a completed
condition endpoint, `_persistent_row_transition` is marked before the persistent
operation is submitted. If thermal hold happens before the endpoint pause is
processed, recovery waits for `_persistent_row_entered`, while the endpoint
handler waits for thermal recovery before submitting that operation.

Both sides wait for an event that has not been requested. This affects
`persistent_each_row` multi-condition runs; the latest logs contain one enabled
transport condition and do not demonstrate this particular failure.

Required correction: distinguish a requested row policy from an in-flight
persistent operation; acknowledge the endpoint and initiate the appropriate
transition without a circular recovery dependency.

## Remaining finding 3: discarded sample can strand endpoint acknowledgement

Simulation-only finding, reproduced against current code. An endpoint pause
acknowledgement waits for `_sample_pending` to reach zero. If the final pending
sample belongs to the generation preceding a monitoring hold, `_sample_ready`
decrements the counter but returns immediately after discarding the sample.
It never processes `_endpoint_ack_waiting`.

Reproduction ends with pending=0, endpoint_ack_waiting=True, and no next-leg
command. The newly added long phase watchdog eventually reports an error, but
normal execution does not resume.

Required correction: always complete the pending-I/O bookkeeping and endpoint
continuation, independently of whether that sample is retained as valid data.

## Evidence limitations and next validation

All three recent transport CSVs have headers but no measurements. They support
investigation of preparation, interruption, and cleanup; they cannot validate
continuous-sweep data quality or the reverse-leg implementation. A newly started
process and a complete simulated start/position/bias/forward/reverse/cleanup run
with injected thermal and monitoring interruptions are the next meaningful
checks. These three issues were subsequently fixed as recorded below.

## Implementation update

### Follow-up: why the second and third runs can remain idle after positioning

The logs explicitly say positioning completed at 12:24:25 and 13:13:30;
describing these runs as stuck in positioning was inaccurate. They then remain
silent for 29m49s and 13m24s respectively, with zero CSV rows. The second run's
final checkpoint has target_field_t=0.5 and leg_index=0, consistent with having
entered the condition/bias path after positioning.

Offline replay (`tmp/replay_transport_restart.py`) compares selected methods from
Git HEAD with the current implementation, using Qt queued bias completion and a
mock magnet. Git HEAD is an available old source baseline, not a verified dump of
the still-running process. The reproduced chain is:

1. Thermal pause sets both `_thermal_hold` and `_thermal_pause_pending`.
2. Cancellation is treated as failure, starting cleanup before the pause ACK.
3. Cleanup consumes the ACK; the thermal flags remain set.
4. Old `_enable_background_work` does not reset these flags on the next run.
5. Positioning completes, bias completion chooses thermal hold, and recovery
   returns immediately because `_thermal_pause_pending` is still true. There is
   no new outstanding pause whose ACK could release that wait.

Observed replay output: HEAD remains `biasing`, hold=true, awaiting_pause=true,
resume_action=bias, and sends zero sweep commands despite SAFE thermal permission.
Current code resets both flags and reaches `starting_sweep`, sending the forward
sweep command with no failures. A permanent queued-completion regression test
now covers this failed-run-to-next-run sequence. This strongly supports retained
thermal state as the explanation for the subsequent two runs; their historical
logs do not persist the flags and cannot prove that exact in-memory sequence.

- Added a separate continuation evaluation: fresh valid temperatures below warning
  allow an existing operation to continue. New starts and recovery still use the
  configured recovery thresholds and dwell. Warning/trip and sensor validity checks
  remain active. The logged reservoir 3.903 K case has a regression test.
- Added typed APS100 cancellation with thermal/monitor/user origins, propagated
  through safe-move results and audit records. Intentional pauses no longer emit
  the generic hardware-failure signal. Thermal positioning resumes only after the
  pause acknowledgement and recovery permission.
- Endpoint acknowledgement now submits the persistent-row operation before waiting
  for recovery. A separate in-flight flag prevents duplicate commands.
- Discarding a pending read now still processes an acknowledged endpoint once its
  pending count reaches zero. Reads spanning thermal pauses are also discarded.
- Thermal logs now include origin, interrupted phase, temperatures, warning limits,
  reason and recovery destination; checkpoints include thermal interruption context.

Validation: 243 tests passed across workflow recovery, thermal evaluation,
transport responsiveness/sweep, gate batch, magnet control, current matching,
heater transitions, both B-field tabs, and APS100 adapter suites. New regressions
exercise thermal cancellation through acknowledgement and positioning recovery,
persistent-row acknowledgement/recovery, discarded endpoint reads, and worker
cancellation origins. One existing cancellation-message assertion was updated.
`git diff --check` passed. This is simulated regression coverage, not a complete
hardware lifecycle validation. The running application was not restarted and no
live instrument commands were sent; it must be restarted to load these changes.
