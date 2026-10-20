# September 8 Gate Scan failure

Latest failure: 12:44:36, Gate Scan job 4/21, moving from -0.4 T to
-0.35 T. The first three field jobs completed. Checkpoint and command audit:
`D:\photocurrent\data\User\YZ289\2026-09-08\gate_scan\YZ289_3.6K_e1_-0.1V0.9mV433Hz_series_20260908_122249_bfield_batch_checkpoint.json`.

The audit records stored magnet field -0.3997 T, supply output 0 T,
heater OFF, previous lower limit -0.4509 T, upper limit -0.4000 T.
Lead matching calls `_start_output_sweep_to(-0.3997)`, which calls
`_set_directional_target`. Since the output is above the target, that function
directly writes `LLIM -3.997000` in kG without adjusting the opposing limit.
The existing ULIM is -4.000 kG, so the proposed lower limit exceeds the upper
limit by 0.003 kG (0.0003 T). ESR was zero immediately before this command and
16 immediately afterwards; subsequent readback shows the old limits unchanged.
This identifies an invalid limit update during lead matching, not a timeout or
thermal pause. The actual ramp toward the fourth target had not yet started.

`tmp/replay_aps_limit_conflict.py` reproduces the crossed-limit command using
the current adapter and a fake register writer enforcing LLIM <= ULIM.
Required fix: read both existing limits and update the opposing limit first
when necessary before setting the directional target. Cover both negative-field
downward matching and its positive-field upward mirror with enforcing fakes.
The existing `set_limits_t` already orders paired updates; the directional
target helper bypasses it. Do not ignore ESR or retry the same invalid command.

The latest Transport sweep is different: it started 01:50:26, completed forward
and backward legs, and released ownership 05:20:25. Its CSV contains 5360 data
rows. This is real-run evidence that the earlier zero-data startup blockage did
not recur in that sweep, but does not validate all other paths.

The workspace pythonw process started September 7 at 14:41. Command-line
inspection through CIM was unavailable; process start time alone does not
prove every latest disk edit was loaded. No live hardware commands, restart,
or production-code edits were performed during this diagnosis.

## Similar-path audit

All production LLIM/ULIM writes occur in `set_limits_t` or
`_set_directional_target`. The latter serves both driven `start_sweep_to` and
heater-OFF `_start_output_sweep_to` (lead matching). Thus the issue is shared
across consumers; it is not confined to the Gate Scan tab or negative fields.

`tmp/audit_aps_limit_paths.py` uses the serial fake plus register-order
enforcement (reject LLIM > ULIM with ESR 16 and preserve previous registers).
Four failures reproduced with the current production adapter:

| Operation | Existing limits T | Current T | Target T | Rejected command |
| --- | --- | --- | --- | --- |
| Recorded negative lead match | -0.4509, -0.4 | 0 | -0.3997 | LLIM -3.997000 |
| Positive lead match mirror | 0.4, 0.4509 | 0 | 0.3997 | ULIM 3.997000 |
| Driven positive reposition | 0.4, 0.45 | 0 | 0.2 | ULIM 2.000000 |
| Driven negative reposition | -0.45, -0.4 | 0 | -0.2 | LLIM -2.000000 |

The driven examples require current field outside retained limits (for example
after zeroing); ordinary Transport setup first sets broad limits and therefore
does not necessarily encounter this state. Four controls passed: forward and
reverse crossing zero with broad limits, plus positive/negative matching whose
targets remain on the valid side of the opposing limit. This explains why a
small difference in stored magnet readback can expose the failure intermittently.

Six paired-limit update cases passed: moving the whole interval upward/downward,
shrinking/expanding it, and touching either old endpoint. Configuration, rollback
and restoration call this ordered paired setter. These results verify only the
crossed-limit property, not all possible instrument failures or firmware rules
for equal limits.

Existing `_FakeAPS100Resource` and high-level mock accept crossed limits without
raising, which explains why earlier tests did not catch this. A fix should use
an enforcing fake in permanent regression coverage, not merely assert command
strings. No production patch was applied during this additional audit.

## Fix implemented after the audit

`_set_directional_target` now reads both limits. When an upward target is below
the old lower limit, or a downward target is above the old upper limit, it uses
the ordered paired setter to bound the current-to-target interval before issuing
the sweep. No-motion targets below the old interval use the lower endpoint,
avoiding an invalid zero-width paired update. Ordinary targets retain the existing
opposite limit. All writes and readbacks retain their error checks.

The recorded case now writes `ULIM 0.000000` followed by `LLIM -3.997000` and
then permits FAST matching. Positive-field matching is handled symmetrically.
The shared serial test fake now rejects crossed limits with ESR=16. Permanent
tests cover ten directional cases and prohibit sweep commands after a failed
opposing-limit write. The replay scripts now assert successful repair.

Validation: 156 tests passed (adapter 29, current matching 11, Gate Scan batch
49, Transport sweep 35, Gate Scan default rate 5, workflow recovery 27), plus
all 14 offline audit cases. No live hardware commands or restart were performed.
Restart the application after the current measurement has stopped to load this
patch; the actual instrument response to the repaired sequence remains to be
verified in a new run.
