# attoDRY1000 sample-temperature control

Open **Instruments → Magnet**, select **attoDRY1000 (APS100)**, and connect
**LS335** in **attoDRY1000 temperature (LS335)**. Its controls operate the commissioned
Lake Shore 335 sample output independently of the APS100 persistent-switch heater.
The default channel mapping is sample B and magnet/reservoir A.

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
