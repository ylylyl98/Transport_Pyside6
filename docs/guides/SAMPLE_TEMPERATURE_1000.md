# attoDRY1000 sample-temperature control

Open **Instruments → Magnet**, select **attoDRY1000 (APS100)**, and connect
**LS335** in **attoDRY1000 temperature (LS335)**. Its controls operate the commissioned
Lake Shore 335 sample output independently of the APS100 persistent-switch heater.
The default channel mapping is sample B and magnet/reservoir A.

## Sensor current reversal (AC excitation)

In **Instruments → Magnet → Sample temperature**, the **LS335 sensor current
reversal** controls show independent A/B readbacks and explicit ON/OFF buttons.
Only PTC/NTC RTD inputs support this control. OFF selects DC excitation: temperature
readout continues, but thermal EMF compensation is lost. It does not turn off the
sensor, temperature polling, or heater output. The [official Model 335 manual](https://www.lakeshore.com/docs/default-source/product-downloads/335_manual.pdf),
section 4.4.5 and the `INTYPE` command reference, describes the alternating
excitation and its possible pickup in sensitive measurements.

The application reads the existing input settings, changes only compensation,
and verifies sensor type, autorange, units and reversal. A fixed range must also
match; an autoranging input may select a new range. A/B settings are refreshed
with control telemetry and stored with run-start context. Unknown, stale or
unsupported settings are displayed explicitly; connecting or loading a recipe
never applies a default reversal setting.

Changes require idle measurement workflows and, when APS100 is connected, fresh
stationary fault-free telemetry. A stationary nonzero field is allowed. A shared
command reservation prevents a conflicting magnet operation. New measurements
are blocked until the setting transaction completes; old temperature samples and
stability windows are invalidated and new readings qualify again. B-field Sweep
shows the status. Its default **Unchanged** mode never automatically toggles B;
the APS100 Stepped **Per point** and **Whole sweep** modes can do so explicitly.
A must already be ON and is never changed automatically. Confirm temperature
offset and measurement noise on the actual setup before choosing OFF for a run.
See [Sample B current reversal during APS100 stepped sweeps](../measurement-guide.md#sample-b-current-reversal-during-aps100-stepped-sweeps)
for the ON qualification windows, OFF acquisition, restoration and interruption rules.

## APS100 / sample-heater interlock

LS335 sample-heating commands require a fresh APS100 observation confirming idle,
fault-free standby, persistent-switch heater OFF, and zero magnet field and power
supply output. This includes target-only and Keep-range requests: their effect
depends on the instrument's existing control configuration. Driven operation,
nonzero persistent fields, sweeps, active magnet commands/reservations, unknown
switch states, missing readings and expired readings inhibit these commands.
Both the Quick and Widgets interfaces use the same controller-level interlock;
commands are checked again in the worker when they execute.

Zero uses the commissioned `magnet.field_tolerance_t` for both field readings
(default 0.002 T), and `magnet.current_match_tolerance_a` for output current
(default 0.01 A). These are software readback tolerances, not newly established
physical heating limits. The magnetic readback age limit is
`lakeshore335.maximum_field_reading_age_s` (default 5 seconds).

Before entering driven mode, moving field or starting a transport sweep, the
worker requires fresh LS335 readback with sample RANGE=0 and heater output=0;
Zone mode additionally requires the ramp stopped. An enabled range with zero
instantaneous output is insufficient. Use **Heater OFF**, then wait for its
readback and the existing thermal protection to permit the magnet operation.
Control readback freshness uses `maximum_reading_age_s` (default 3 seconds).
An atomic reservation prevents the two workers from starting conflicting commands.

During LS335 control polling (default every 2 seconds), an unsafe or unknown
magnet state with sample heating enabled or unconfirmed requests sample Heater OFF
and reports a fault. Communication failures leave shutdown unconfirmed and block
new magnet starts. Heating never restarts automatically after recovery. The
dedicated **Heater OFF**, magnet **Pause**, and persistent-mode cleanup remain
available. This protection never changes the APS100 persistent-switch heater as
part of shutting off sample heating, and preserves mandatory switch dwell timing.

This is a software interlock with polling and communication latency. It cannot
prevent front-panel changes before their next readback, or guarantee shutdown
after communications/power loss or application exit. Existing magnet temperature
and fault protection remains necessary. Offline mock verification does not replace
controlled hardware acceptance.

## Temperature controls

The main measurement page shows a read-only temperature summary for the selected
system. **Temperature controls…** opens and scrolls to that system's controls.
Selecting **attoDRY2100 (SDK)** shows its separate SDK sample/VTI temperature area.
The two systems retain their own targets, drafts and readbacks while switching;
switching does not send temperature commands or change the other system's settings.
1000 stability waiting applies only when 1000 is selected. Disconnected, faulty or
stale readings are not presented as current temperatures in the main summary.

**Detect & connect system** tries the configured APS100 VISA resource and 2100 SDK
endpoint using the existing connection controllers. Successful connections stay
open. Complete the existing configuration-review checkbox before connecting.
After both attempts finish, **Auto-select connected system** selects the only
connected system. Both online requires a manual choice; neither online remains
unidentified, with connection errors in the identification label's tooltip/log.
Selecting a system manually turns Auto off; recheck Auto to enable it again.
Identification does not apply temperature or field setpoints. The ordinary
connection telemetry and thermal protection remain active. LS335 alone is not
evidence of a connected 1000 magnet system. Detection uses configured addresses
and requires a valid SDK folder and reachable endpoint for 2100. Active measurement,
magnet and temperature requests hold automatic selection until the operation is
idle. The application does not initiate these connections at startup.

1. Enter the sample target in kelvin.
2. Choose **Auto by temperature**, **Keep heater range**, **Heater OFF**, **Low**, **Medium** or **High**.
   In PID mode, Keep preserves the current setting, so an output that is OFF stays OFF. Output 2
   in voltage mode instead supports OFF or ON; Medium/High are disabled.
3. Choose **Keep ramp**, **Ramp OFF** or **Ramp ON**. An explicit ramp uses the rate
   entered in K/min (0.1–100). This changes the setpoint ramp; the cryostat determines
   the actual cooling rate.
4. Click **Apply temperature**. The confirmed target, heater range, output percentage
   and ramp configuration appear below the controls. Sample readings update separately.
5. Optionally select **Wait until stable** to block measurement starts until fresh,
   valid sample readings stay within the configured tolerance for the required dwell.
   The defaults are ±0.05 K for 5 seconds. Pending or failed control commands cannot
   satisfy this gate. A failed apply requires a successful new apply.

**Heater Off** requests and verifies RANGE=0 on the mapped sample output. It remains
available during an apply request; the worker processes requests in order. A failed
or disconnected shutdown is displayed as unconfirmed. If activation reaches the
instrument but its verification fails, the adapter requests OFF on that same output
and reports whether OFF was confirmed. This also applies when the output was
already heating with Keep range: unconfirmed target/ramp writes or a sensor fault
after the target write request shutdown. If its existing range exceeds the
configured ceiling, use Heater Off before applying a new target.
In native LS335 Zone mode, Heater Off also stops and verifies the setpoint ramp
before requesting range OFF, preventing a later zone crossing from restarting
heating. A failed ramp-stop confirmation still attempts OFF and reports the
shutdown as unconfirmed. Use the dedicated Heater Off button to stop Zone control;
applying a new Zone setpoint together with a manual OFF range is rejected.

## Automatic heater range

Select **Auto by temperature**, enter the target, and click **Apply temperature**.
The application selects the first band whose upper temperature covers the target;
an exact upper-bound temperature stays in that band. The preview shows the selected
range for custom bands. Instrument bands are resolved and displayed after readback.
Changing a target or selecting Auto does not send commands until Apply is clicked.

**Auto ranges…** opens the temperature-band editor. By default it reads the
commissioned LS335 Zone table; the application does not supply unverified temperature
thresholds. Uncheck the instrument-table option to enter your own ascending upper
temperatures and OFF/Low/Medium/High ranges. Save persists the application settings
without commanding the instrument. Invalid, missing, uncovered, ambiguous-channel
or over-limit tables fail before control writes. There is no fallback to High.

When the instrument is in Closed Loop PID mode, Auto applies the target's selected
range after setpoint/ramp confirmation. When the instrument is already in Zone mode,
Auto retains its commissioned range/ramp profiles and requires **Keep ramp**. Custom
application bands are rejected in that mode to avoid competing with native Zone
control. All active zones must use the configured sample sensor or default input
and obey the range ceiling. A stopped output at a stationary setpoint can be resumed
with its commissioned target-zone range; during a native ramp the instrument retains
control of zone transitions. The output mode, PID and Zone tables are never written.
Keep in native Zone mode also validates the instrument table before changing a
target. Only Keep range/ramp or Auto with Keep ramp is accepted in that mode;
manual overrides are rejected. A new target can advance the instrument's Zone
profile, including its heater range. Use Heater Off to stop native Zone control.

Heater Off remains effective with Auto selected. Configuration polling and preview
updates are read-only; another Apply is required to request heating again.

The application checks the unique closed-loop/zone output mapping, enabled sample
input and kelvin setpoint units before writing. It verifies every setpoint, range
and ramp write, and checks sample sensor validity before enabling heating. Sensor
configuration, heater wiring/current limits, PID, zone tables and power-up behavior
remain commissioned instrument settings. The control display uses HTR? for actual
output percentage, rather than MOUT? for the manual-output offset.

Temperature controls do not change magnet thermal protection. Under the default
sample limits, a target at or above 4.6 K conflicts with magnet measurement readiness;
the field-dependent magnet/reservoir limits also remain active. Heating a sample to
a higher temperature does not grant permission for magnet moves or field workflows.

Application bounds can be set in the `lakeshore335` configuration:

| Setting | Default | Purpose |
| --- | --- | --- |
| `sample_minimum_temperature_k` | 0.001 | Lowest permitted positive target |
| `sample_maximum_temperature_k` | 300 | Highest permitted target |
| `sample_maximum_heater_range` | 3 | Maximum selectable heater range, 0–3 |
| `sample_auto_heater_ranges` | `[]` | Read LS335 Zone table; otherwise use configured temperature/range bands |
| `control_polling_interval_s` | 2 | Control configuration/display refresh interval |
| `sample_stability_tolerance_k` | 0.05 | Measurement readiness tolerance |
| `sample_stability_dwell_s` | 5 | Continuous valid stability dwell |

These application bounds are not a claim about the cryostat's achievable range.
Configure them for the commissioned sample, sensor and heater installation.
Temperature telemetry keeps its existing polling interval and thermal freshness
checks. Control-configuration errors remain separate from valid thermal telemetry.

Command definitions were checked against the
[Lake Shore Model 335 manual](https://www.lakeshore.com/docs/default-source/product-downloads/335_manual.pdf),
sections 4.5.1.7.7, 5.3 and 6.4.1 (INTYPE?, HTRSET?, SETP, RAMP, RANGE, HTR? and ZONE?).
Verification uses fake VISA resources and mock adapters only; real instrument
timing and heating have not been tested.

## Stepped B-field Sweep

APS100 Stepped mode uses **directional approach → confirmed pause → independent
field and temperature qualification → extra delay → measure → post-read verification**.
The nominal targets schedule sampling; accepted samples use the instrument field
readback as their x coordinate. A stable offset does not require manual Continue.
These are current-derived readbacks, not independent measurements of the sample's
true field. Remanence, calibration error and unobserved transients remain possible.

The software uses up to two stages: approximately 80% of the remaining distance,
then the final target at 20% of the per-run band rates (limited by APS100 resolution).
It does not reproduce PPMS firmware or use oscillating approach. A small overshoot
within the acceptance envelope is paused and qualified without reversing. A stalled
move can also qualify if the confirmed stationary readback is within acceptance.

The separately editable commissioning defaults are:

| Setting | Default | Meaning |
| --- | --- | --- |
| Approach stopping tolerance | ±0.2 mT (±2 Oe) | Fine motion stopping threshold; capped by endpoint acceptance |
| Point acceptance | ±0.5 mT (±5 Oe) | Allowed nominal-target offset at interior samples |
| Endpoint acceptance | ±0.2 mT (±2 Oe) | Start, turn and final endpoint acceptance; also bounds the scan envelope |
| Maximum history reversal | 0.2 mT (2 Oe) | Largest detected backtracking from the directional extreme |
| Maximum field window span | 0.2 mT (2 Oe) | Independent field-stability peak-to-peak limit |
| Maximum field drift | 0.1 mT/s (1 Oe/s) | Absolute fitted drift during the field window |
| Maximum actual point gap | 0 (automatic) | Zero uses twice the largest planned interval; an explicit gap must cover that interval |
| Field stability window / extra delay | 2 s / 1 s | At least three fresh readings; delay starts after both field and temperature qualify |
| Field qualification / no-progress timeout | 60 s / 15 s | Separate from the temperature wait; no-progress time is extended for rates too slow to move one readback quantum |

Set these values for the smallest feature that must be resolved. They are not field
accuracy specifications. The first point is qualified in the preparation direction;
later points follow the measured branch. Passed interior nominal targets are logged
and skipped, never revisited or filled with duplicate readings. The turning point is
never silently skipped. A measured gap over the configured limit terminates the run
with a coverage-failure record instead of silently interpolating missing features.

History includes acquired observations before, during and after motion, after pause,
and through qualification/acquisition. Min/max field and maximum reverse excursion
are retained across retries and protection interruptions. Detected history reversal,
endpoint-envelope violation, unacceptable stationary offset, or qualification timeout
ends the run through normal protective cleanup. Existing data remain saved and the
event log/checkpoint records the reason; invalid acquisitions are retained as labeled
unaccepted events rather than inserted into the valid measurement CSV. Protection
holds still use their existing manual recovery. A known APS100 telemetry gap leaves
the magnetic history unverified and prevents resuming the same measured branch.

Motion timeouts are based on distance and verified rates, including the slow tail,
with separate preparation overhead; there is no one-hour minimum. Field and
temperature readiness have independent budgets and a total point deadline prevents
alternating readiness from keeping a point alive indefinitely. Automatic
same-direction correction uses the remaining field budget, not a fresh timeout.

APS100 persistent-switch current matching and supply-lead zero completion also
default to 10 mT, configured separately by `magnet.current_match_tolerance_t`
and `magnet.lead_zero_tolerance_t`. Current thresholds are calculated as
`0.010 / coil_constant_t_per_a` A (about 0.04919 A for 0.20328 T/A).
Lead zero uses its own threshold, independently of field-target accuracy.
Heater-state confirmation, dwell, voltage monitoring and repeated verification
remain required. The LS335 sample-heater zero-field interlock retains its
separate stricter thresholds. Reconnect the magnet when idle after changing
these adapter settings; an existing connection keeps its constructed settings.
Choose **B-field Sweep → Measurement mode → Stepped**, enter Start/Stop and
Step size in **mT**, then set the fixed Sample target and stability
criteria. Point counts are calculated and read-only. Targets must align with the
APS100 0.1 mT resolution; incompatible spacing is rejected rather than silently rounded. A non-dividing
step appends the exact Stop. A round trip visits the turning point once.
The preview shows points per pass and per condition, including any shorter final interval.
Saved step values retain their internal tesla units; remembered point-count recipes
are converted to the equivalent step when opened.

For each APS100 run, set **Maximum sweep rate (T/min)**. Above this input, the UI
shows the configured ceiling and current request in **Oe/s** (0.12 T/min = 20 Oe/s).
Range and voltage limits may further reduce the allowed rate. The application reads and
validates the existing **RANGE/RATE 0–4** table, rounds the requested maximum down to
the instrument's 0.1 mA/s resolution, and uses the lower rate in each range. It verifies
the resulting table and calculates motion timeouts across all crossed ranges, including
zero crossings. On completion, cancellation or error it attempts to restore and verify
the original table; a restoration failure is reported. Fast Mode RATE 5 is unchanged.
Requested and effective rates are saved in metadata; the CSV filename omits rate and
point-count tags and abbreviates Doping as D.
Acquisition delay controls continuous sweep sampling; stepped sampling is paced
by qualification at each point and still uses the selected averaging count.

Every point requires an acknowledged pause, at least three fresh stationary field
readings spanning the field window, and a fresh temperature window after the pause.
The additional settling delay starts only after both windows qualify and restarts
if qualification is lost. The default LS335 telemetry poll
interval is 0.5 s; the 10 s stability window is an observation duration, not a
single reading or a fixed delay that automatically permits measurement. Shorter
windows are less sensitive to slow changes and require experimental validation.
Stale telemetry, field excursions or invalid temperatures reset qualification.
Each acquisition is checked against subsequent fresh field and temperature readings;
an interrupted or unstable acquisition is discarded and requalified. Requested field
and actual field readbacks are stored separately; a nearby measured field is never
relabeled as the nominal target. CSV columns include before/after readbacks,
observed history min/max, reverse excursion, monotonic observation times, actual
point gap, quality status, crossed nominal point indices and stability metrics.
History failure cannot be cleared by Continue. A permitted protection recovery
requalifies the current point while preserving its prior magnetic history. It never accepts a
different temperature. Existing protection pauses still require manual
continuation, and Stop retains the normal persistent-mode cleanup.

The target is a measurement criterion: the application does not send a temperature
setpoint or turn on the sample heater. Existing magnet and sample-heater
interlocks remain in force. The APS100 persistent-switch heater is a separate device
and may remain on in driven mode. Passive stabilization while driven is possible
but depends on the actual thermal load and refrigerator; it is not guaranteed.

The default 4 K target, 0.05 K tolerance, 10 s window, 0.02 K span, 0.01 K/min
drift and 600 s timeout are starting values requiring validation on the actual
instrument and sample; sensor lag and measurement self-heating also matter.
Offline tests do not replace hardware acceptance.
