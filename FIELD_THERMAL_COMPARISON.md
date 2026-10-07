# Live field-dependent thermal protection

The application now enables attoDRY1000 manual page 25 Table 3 protection for
APS100 operations. The former fixed 4.8 K reservoir trip is ignored in this
mode. The legacy thresholds remain in configuration only for explicit legacy
mode and its regression tests. Optional shadow diagnostics are disabled by default.

| Absolute actual magnet field | Strict operating boundary | Software stop threshold |
| --- | --- | --- |
| 0 to 6 T | <5.5 K | 5.4 K |
| >6 to 7 T | <5.0 K | 4.9 K |
| >7 to 8 T | <4.5 K | 4.4 K |
| >8 to 9 T | <4.2 K | 4.1 K |

The 0.1 K margin is an application parameter, not a manufacturer specification
or a validated guarantee against overshoot. The APS worker checks the next
0.05 T toward its target as well as the current field, so it requests a stop
before entering a stricter band. A distant 9 T destination does not impose
4.2 K throughout the low-field portion of a ramp. No extrapolation above 9 T
or at/above 5.5 K is allowed. Field polarity is handled using absolute values.

## Temperature inputs and source limitations

Reservoir is the magnet thermometer; the separate sample channel retains its
own warning/trip settings. The operator confirms there is no available
first-stage input on this installation. It is not required or shown as verified
by software. Missing input does not establish compliance with the physical
first-stage condition in the magnet manual.

The system Table 3 is an operating recommendation, not a quench critical curve.
The C2754M manual also states <=4.2 K before charging and a general <=4.2 K
operating recommendation. The system factory test graph (specification page 21)
shows a low-field temperature excursion above 4.2 K. These source differences
remain documented; software tests cannot certify the physical operating envelope.

## Actions and manual continuation

- MainWindow connects the shared temperature source to the APS worker and feeds
  timestamped field observations to the GUI evaluator.
- The worker evaluates live temperature and actual IMAG field during existing
  adapter safety checks and during continuous-sweep polling. This adds field
  readbacks; it is no longer merely a copy-only diagnostic observer.
- A new charging cycle checks magnet temperature <=4.2 K. Existing batch
  recovery thresholds, stable dwell and heater interval still gate new jobs.
- A thermal violation requests and checks SWEEP PAUSE if sweeping, sets the
  operation stop event and reports a fault. It never turns the heater off or
  zeros leads as an automatic thermal response. A failed pause is reported as
  PAUSE NOT CONFIRMED, not as a successful stop.
- Mandatory heater warm/cool dwell is preserved if the thermal stop occurs
  during it. The stop prevents the subsequent action. Monitoring continues.
- Gate Scan aborts a moving job on a thermal stop. Inspect the equipment and
  use More > Restore from checkpoint to explicitly retry unfinished work.
- For recoverable active protection pauses, the primary button becomes Continue
  scan. It rechecks readiness and pending pause/transition before moving.
- Normal cooling and stability waits proceed automatically once satisfied.
  Temperature warnings and monitoring failures latch manual continuation;
  recovered communications alone cannot resume a protection-interrupted run.
  A manual request rejected before readiness is not queued for later execution.
- Thermal-fault transport cleanup preserves heater state instead of initiating
  an automatic persistent-switch transition. Explicit Stop/shutdown retains
  its separate existing cleanup behavior.

Temperature input freshness remains 3 s; cached field freshness is 5 s to
accommodate the existing 3 s stationary polling. The worker reads field again
for its live checks. Missing/invalid inputs block operation. These are software
controls; device response latency has not been validated on hardware.

## Measurement and logging

Magnet operating permission is separate from new-cycle/recovery permission.
The magnet panel labels the latter New-cycle permission. Existing measurement
recovery thresholds/dwell are retained; this change does not add a new
sample-setpoint control loop or modify Lake Shore heater/PID settings.

Gate Scan JSONL includes live field/time, operating permission and its reason.
After a batch ends, temperature logging continues in that series file until the
next series starts or the app exits, permitting review of post-stop cooling.
Normal metadata/checkpoints retain thermal policy and observed field information.

## Verification

Simulated tests cover signed field bands, strict boundaries, stop margins,
invalid or stale readings, the -0.4 T / 4.833 K event, next-band anticipation,
zero crossing, pre-charge checks without first-stage input, pause failures,
mandatory switch dwell, explicit continuation and existing APS current matching.
No hardware was commanded or application restarted during implementation.

## Simplified continuation UI

The dedicated Resume and Check temperature / Continue buttons have been removed.
Gate Scan checkpoint restoration is under More > Restore from checkpoint.
For a recoverable active protection pause, the existing primary button becomes
Continue scan and remains disabled until temperature and device acknowledgement
checks permit continuation. External sample-stability gates still apply. An
aborted batch is restored explicitly from its checkpoint, not started as a new
batch by a Continue click.

Normal Gate Scan preflight dwell, heater interval and post-move recovery waits
continue automatically once satisfied. Routine persistent-row cooling in B-field
Sweep also proceeds normally. A warning, monitoring failure or protection pause
requires manual confirmation after recovery; it is not treated as routine cooling.

## Persistent-field measurement readiness

After the existing persistent-field verification succeeds, Gate Scan uses a
measurement-specific temperature decision. Live field-dependent magnet limits,
sensor validity, telemetry freshness and sample warning/trip limits still apply.
The next-heater activation interval and magnet recovery temperature (3.9 K by
default) no longer delay measurement at that verified field. They still gate a
subsequent move requiring another heater cycle, including after a measurement ends.

Measurement retains sample recovery and its stability dwell. When the shared
Wait until stable control is selected, its applied sample target, tolerance and
dwell replace the measurement recovery target. A missing target or a target
conflicting with the sample warning limit blocks measurement with an explanation.
Configured warming-rate limits still apply. A changed target or invalid reading
restarts the measurement dwell. This does not relax new-cycle preflight conditions.

The Gate Scan banner identifies whether its permission describes a new magnet
cycle, operating limits, or post-move measurement readiness. Real protection
faults continue to require manual recovery. These changes have been verified
with simulated tests; the running application and hardware were not restarted.
