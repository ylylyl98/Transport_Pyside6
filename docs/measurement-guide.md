# Measurement guide

This guide describes the default Qt Quick interface. For installation, EXE downloads and launch options, see [Setup and launch](user-setup.md) ([中文](user-setup.zh-CN.md)). The retained Widgets frontend is selected with `--widgets`; its labels can differ.

The application coordinates Keithley 2400 source meters, an NI DAQ device and an SP-2300 monochromator for electrical transport and photocurrent measurements. It plots acquired data while saving each measurement to CSV.

> **Laboratory software:** This program can change instrument outputs. Verify cable routing, instrument limits, compliance settings, and the selected hardware addresses before every run. Software safeguards are helpful, but they are not a replacement for laboratory safety procedures or hardware interlocks.

## Measurement modes and live plots

Quick numeric fields use compact display text while idle. Focus a field to edit
its full value, or hover to inspect it. Display rounding does not change recipe
values, instrument commands or saved measurement precision. Small nonzero values
remain nonzero; manual readouts use engineering units such as nA, µA and mV.
The retained Widgets frontend trims decimal padding while preserving each
control's configured precision.

File output, acquisition options and other low-frequency settings start
collapsed with a short summary. Expand their headings to edit them. Scan ranges,
bias modes and the stepped scan's target temperature remain directly accessible.
Errors retain their section indicator and can be revealed from validation.
Long explanations have a **Show explanation** action. Stop controls and
protection/connection status remain visible independently of these groups.

| Measurement page | Purpose |
| --- | --- |
| **Vds Sweep** | Sweep drain-source bias while holding top- and back-gate biases. Vds can come from Keithley G3 or an NI-DAQ analog-output channel. |
| **Gate Scan** | Run a one-dimensional raw-voltage trajectory or a derived doping/electric-field trajectory, with an optional reverse pass. |
| **B-field Sweep** | Sweep APS100 continuously, or measure prescribed field points on APS100 / attoDRY2100 with a fixed sample-temperature target; hold one or more fixed Doping/E-field/Vds rows, with optional round trip. |
| **2D Map** | Acquire a 1D or 2D grid across `Vtg`, `Vbg`, and/or `Vds`; select fast and slow axes and preview the planned sweep. |
| **Photocurrent** | Sweep monochromator wavelength for one or more enabled Vtg/Vbg recipe conditions, with optional per-condition Vds. |
| **B-field Gate Scan** | Execute gate-scan conditions at selected magnetic-field targets. |

Across the modes, the application averages DAQ readings, plots the selected current channel live, and writes the acquired points to CSV as the run proceeds. The plot can switch between a single selected channel and a four-channel comparison view.

In Quick Gate Scan, **Trajectory** selects **Raw** voltages or **Derived** Doping/E-field coordinates. The Widgets selector uses **Raw Voltages** / **Doping / E-field**. For derived values, the coupling ratio can multiply either `Vbg` (the backward-compatible default) or `Vtg`. The displayed equations, computed voltage preview, generated sweep trajectory, plot axes, CSV columns, and run metadata all use the selected definition.

Quick coordinate inputs use **(V)** with the selected equation shown alongside the range. Doping and E-field are weighted gate voltages, not calibrated carrier density or physical electric field. With `r multiplies Vbg`, `Doping = Vtg + r*Vbg` and `E-field = Vtg - r*Vbg`; selecting Vtg instead applies r to Vtg in both equations. Gate Scan names the held coordinate explicitly and previews the requested physical gate voltages at both endpoints after edits are committed. The preview applies no output; protection remains checked before acquisition. Coordinate labels consistently use purple for Doping, teal for E-field, blue for Vtg, bronze for Vbg and slate for Vds; text and equations remain the primary identifiers.

Sampling controls show **Points / pass** alongside **Step**. Gate Scan keeps one editable point count shared by all active axes and shows each axis's calculated step. Vds Sweep, Map and Photocurrent keep their editable steps and show the resulting point counts. A one-point Gate Scan visits Start only. Summaries report a shorter final interval or an actual endpoint different from Stop when required by the existing worker's endpoint policy. Large summaries calculate in the background. Continuous B-field Sweep uses acquisition delay rather than a prescribed point count.

Use **Preview sweep range** above the parameters, or **Sweep preview** beside **Live data**, to inspect a sweep without applying outputs. The preview commits the focused field and pending condition edits, plans in the background, and uses a separate plot. Return to Live data to see the retained acquired traces. While visible, the preview automatically follows the current page, selected condition and committed edits; rapid changes are combined before rebuilding, and obsolete background results are discarded. **Refresh** remains available for an immediate rebuild. Map previews use Fast axis horizontally and Slow axis vertically; a 1D map keeps the swept coordinate horizontally and shows point order vertically. Vds and wavelength previews likewise use their physical sweep coordinate horizontally. Gate Scan follows its swept coordinate; when several raw axes sweep together, it explicitly uses their shared Step Index. A Start circle and an End square have separate labels, including when both occupy the same position; a forward Stop / turn diamond is shown for return scans. Map previews also show physical gate bounds and rectangle outlines for merged regions. Very large paths explicitly report any display truncation; acquisition still uses the complete plan. B-field Gate Scan previews the selected condition, and continuous B-field Sweep shows only its field range, not a predicted sample count.

For an L-shaped or stepped map, use **2D Map → Additional gate regions → Use Vtg / Vbg regions** if needed. Set the primary Vtg/Vbg ranges for Region 1, then **Add region** for each extra rectangle and edit its start, stop and step inline. This section is beside the map ranges; expand its heading to edit additional regions. Preview to confirm the combined region and scan order. Overlapping coordinates are measured once; disconnected rectangles are rejected by the existing planner. This supports unions of axis-aligned rectangles, rather than arbitrary polygon vertices.

Plot ticks use readable intervals such as 0.5 V rather than equal subdivisions of the padded view bounds. Point order and Step Index ticks are nonnegative integers. This affects only tick labels and grid lines, not sweep values or zoom limits. Start, End and Stop / turn are labeled at their actual positions and are omitted from the trace legend to avoid duplicate labels.

Preview paths and sample markers are drawn separately: the line may omit redundant straight-line vertices, while the markers use the original planned samples without compression. Sweeps of up to 2,000 points show every sample position per curve; coincident positions overlap. Dense sweeps retain their path and report that sample markers are hidden, rather than displaying an unexplained subset. The summary gives the planned point count and marker policy. Refresh recomputes the current settings; source-code changes to the Python renderer require restarting the application.

Condition On/Off switches sit beside the condition title. Each raw Gate Scan axis has its Sweep switch, Start and Stop on one row. In B-field Sweep, **Fixed gate inputs** selects **Vtg / Vbg** or **Doping / E-field** for all rows. Editing either physical gate keeps the other fixed; switching views changes no setpoints. In the Vtg/Vbg view, changing the shared ratio also preserves the physical gates. Both views save through the existing Doping/E-field model and use the same execution plan and protections. The input-view choice lasts for the current application session.

Live plots default to **Follow Sweep** for the x-axis: Gate Scan follows its selected Doping/E-field or single raw-voltage sweep, while 2D Map follows its fast axis. A manual x-axis override can display Step Index, `Vtg`, `Vbg`, `Vds`, Doping, or E-field; changing this display setting replots already collected points without changing the hardware trajectory.

**X range: Full sweep** is the default live view: the complete execution range is shown before the first point, with a small margin. Gate Scan and 2D Map use the generated trajectory, including derived coordinates, additional regions and round-trip step indices. **Acquired data** fits the X range to recorded points; plots without a known plan also follow recorded points. Y limits follow measured values in either mode. Four-channel views share X limits and scale Y independently. The toolbar provides pan, rectangle zoom and figure saving; incoming points preserve manual X/Y limits. **Reset view** (**Reset** in a compact Quick plot, **Home** in Widgets) restores the selected range policy and automatic Y scaling. Changing the X coordinate resets X zoom, and changing the current channel resets Y zoom. Starting a new run clears the previous plan and zoom while retaining the selected range mode. Readback beyond the planned view is included automatically.

Use **Analysis** to open the separate read-only viewer for saved scans, maps and measured line cuts. This is the **Curve Compare** page in the Widgets frontend. See [Analysis and exports](history-and-export.md).

## Typical measurement workflow

1. Start the application and open **Instruments** in the top command area. Select **Setup → Connections** to open **Connections & protection**.
2. Select the GPIB/serial/DAQ addresses and the operating mode for each Keithley. Use **Scan resources** to list detected resources. Clear the address fields for unused devices before connecting: **Connect all** attempts configured, nonempty addresses.
3. Set the save location, operator name, device ID, amplifier gain, and lock-in gain as appropriate for the experiment.
4. Click **Connect all** and confirm that the required instruments report an OK status.
5. Use **Manual controls** in Instruments when needed: type and ramp a gate target, use the ±0.1 V ramp buttons, read one gate on demand, safely ramp a gate or DAQ AO back to 0 V, or move the monochromator. Gate controls are available only in 2-wire voltage-source mode and while no measurement is active.
6. Select a measurement mode above the parameters, set its sweep bounds, timing, averaging, source, and file name, then review the trajectory summary. Photocurrent supports an editable bias recipe: unchecked rows are retained but skipped, every enabled condition creates its own CSV file, and Vds values require a compatible connected and explicitly enabled source.
7. Start the measurement and monitor the live plot and status messages. Use **Stop measurement** for the active run or the fixed top **STOP ALL / ZERO VOLTAGES** button when needed.
8. Review the resulting CSV in the selected save directory.

The application owns the instrument connections: address or Keithley-mode changes require reconnection before they apply to a measurement.

The **Keithley 2400** source option supports both 2400 and 2401 instruments.
Numeric input precision does not imply that the instrument can program every
entered value. The source's voltage range determines its programming resolution:

| Source range | Voltage programming increment |
| --- | --- |
| 200 mV | 5 µV (`0.000005 V`) |
| 2 V | 50 µV (`0.00005 V`) |
| 20 V | 500 µV (`0.0005 V`) |
| 200 V (2400 only) | 5 mV (`0.005 V`) |

These are **source programming** resolutions, not voltage measurement resolutions
or output accuracy. See the [Keithley 2400/2401 specifications](https://download.tek.com/document/SPEC-2400LV_C_L_May2013.pdf).
The application's existing voltage limits still apply; this table does not extend them.

All measurement modes check the physical Keithley targets, including fixed gates,
derived-coordinate conversions and Bias 2 offset targets. The scan start and each
generated point must lie on the source's programming grid; choosing a step larger
than the minimum is insufficient if it is not a compatible multiple. For example,
on the 20 V source range a `0.0001 V` scan step is too small, and `0.0007 V` is not
a multiple of the `0.0005 V` increment. Requested sample targets are never silently
rounded and the app does not automatically switch source range to fit a recipe.

Quick previews use cached model/range information. Missing range information may
require refreshing readback and pressing Start again. Execution checks refresh the
source range after reserving the device and before applying outputs. Unknown
models, unavailable ranges and incompatible targets stop the run with an explanation.
Ramp steps are maximum increments: intermediate ramp points are aligned to the
source grid without exceeding that increment; a ramp smaller than one programming
increment is rejected. The final target remains exact. DAQ outputs retain their
own limits rather than using the Keithley resolution table.
Manual targets and relative steps follow the same source-grid rules. An invalid
manual request is reported without sending that unsupported target.

**Instruments → Magnet & temperature** contains separate controls for **attoDRY1000 / LS335** and **attoDRY2100 / SDK**. **Detect & connect** tries the configured connections and selects the only connected system; if both are online, choose one manually. The header shows the selected system's read-only summary. The 1000 supports verified sample targets, heater ranges, ramps, optional stability waiting and automatic ranges from commissioned temperature bands. See [sample-temperature instructions](guides/SAMPLE_TEMPERATURE_1000.md).

## Workspace and editing

The Quick workspace separates **Measurement setup** on the left, **Live data** in the center, and **Instruments** on the right. Open Instruments, then use **Pin** (also available in View) to keep the panel beside the plot; this works at 1920×1080 with 125% scaling. Narrower windows show instruments in a right-hand drawer while keeping the global Stop and measurement actions reachable. Drag the section dividers to resize. **Hide** in the setup header expands the plot area; **View → Show parameters** restores the setup panel. The measurement log has its own panel and Close control; opening it in a narrow window selects Live plot automatically.

Start, Stop, progress and measurement status sit below the plot column, allowing the
setup and docked instrument panels to use the full workspace height. In narrow windows,
the same action panel remains below the workspace on both Parameters and Live plot
pages. Actions wrap when needed; the global **STOP ALL / ZERO VOLTAGES** stays at the top.

In **Live data**, switch directly between **1 plot / 2 plots / 4 plots** above the plot.
Click the **axis title on the graph itself** (for example **Vds (V)** or **Ids_DC (A)**)
to open its choices beside the title. Click a choice to apply it immediately. X titles
select the shared coordinate and X range policy. Each Y title selects that plot's
signal, keeping the other plots and their ranges. In one plot, the Y-title menu also
provides **Second Y signal**: choosing a signal adds an overlaid curve; **None** returns
to a single signal. The same menu chooses **Shared Y axis / Independent left / right
Y axes**. With independent axes, clicking the right title changes the right signal.
Titles show a small arrow, support Tab and Space/Enter, and retain a separate keyboard
focus outline. Escape or a click outside closes the menu. A compact toolbar combines
the **1 / 2 / 4 plot** shortcuts, view actions, export and status; groups wrap only when
the available width is insufficient. Two plots share X and keep their Y
ranges independent; they appear side by side when the plot is wide enough, otherwise
one above the other. The selectors exclude duplicate signals.

Selections update immediately, including points already acquired, and can be changed
during a measurement. Overlaid values keep their original scale; independent left/right
axes are useful when signal magnitudes differ. Pan, rectangle zoom and wheel zoom
act on both overlaid axes. **Reset view** restores automatic ranges for the current
layout. Switching back to two or four plots restores that layout's previous zoom;
returning to one plot restores its single-signal or overlay arrangement. Each measurement
page remembers its layout and signal pair; existing single/four-plot defaults remain
compatible. An enabled channel with no finite samples is shown as waiting or empty,
without zeros or a substituted signal.

Live **Export** preserves both selected signals, axis settings and full-resolution data.
The PNG shows the selected one/two/four-plot arrangement; its accompanying view can be restored in Analysis.
Two-plot PNGs stack the panels and label their shared X axis once.
Changing the display does not alter acquisition CSVs or the automatic acquisition
export policy. Sweep preview continues to show the planned trajectory independently.

Each measurement page starts with the collapsible File output card, followed by its trajectory; advanced ramps and plot settings stay folded. Field errors must be corrected before Start. Photocurrent, B-field Gate Scan, B-field Sweep and additional map regions share an inline condition editor. Change On/Off, values and sources directly; fields wrap into multiple rows to fit the sidebar, with vertical scrolling and no horizontal scrolling. **Enlarge** provides a larger editor. The setup sidebar defaults to 520 logical pixels and can be dragged up to 720; condition pages use a 420 minimum beside the live plot, and saved widths are retained within these bounds. Narrow windows still show parameters and the plot on separate pages.

Collapsed section headings show their error count. **Locate error** reopens the setup,
selects the affected condition when needed, and focuses the invalid field. If a map
axis change hides an invalid parameter, **Other invalid parameters** keeps it editable
and shows its last accepted value; the rejected input was not applied. Helper text
and plot instructions follow the font size in **Runtime settings**.

**Add condition / Add region** appends a default row; **Duplicate** copies the selected row immediately after it, and **Remove** deletes the selected row. The new or copied row becomes selected. In B-field Gate Scan, the first Add captures the current complete setup; after removing the last condition the global setup below becomes the active implicit recipe again. **Advanced settings** opens the selected scan's remaining ramp, acquisition and output fields. B-field Sweep keeps its shared coupling ratio and calculated, read-only Vtg/Vbg; map regions have no enable switch. Editing and row actions are locked during acquisition and cleanup.

Long names, paths and numeric lists offer an inline **Expand** action or **Alt+Enter** to view/edit the full text, then Save or **Ctrl+Enter** to apply. Read-only values can also be expanded and copied. Data root has an inline folder chooser (**Alt+B**); visual wrapping preserves the stored value.

The measurement, instrument and history pages share control colors in both themes:
primary actions and selected tools are blue, stop commands are red, and disabled
controls are gray. Keyboard focus has a separate outline. The progress bar shows
a percentage: blue during normal work, green on completion, amber while stopping
or cleaning up, and red on failure; the phase and status text remain visible.

Buttons, tool buttons, selectors, single-line inputs and navigation tabs share a
consistent height and vertical alignment, growing with the selected runtime font size.
Multiline filename editors keep the height needed for their text, and graph-axis title
buttons retain their compact size beside the graph.

## Quick-add and field-target syntax

Quick-add and B-field Gate Scan accept single numbers, comma/newline-separated lists (`-1,1,5` means three values), point-count ranges (`(-1,1,5)` means five equally spaced points including -1 and +1), and `start:stop:step` ranges including both endpoints. `linspace(-1,1,5)` and `np.linspace(-1,1,5)` are aliases of the parenthesized point-count form. For five doping values from -1 through +1 use `(-1,1,5)` or `-1:1:0.5`. If a step does not divide the span, the exact stop is appended: `0:1:0.3` gives `0,0.3,0.6,0.9,1`; the final interval is shorter. A point count must be a positive integer, with at least two points for distinct endpoints. Ranges can be mixed with explicit values; commas inside parentheses belong to the range.

Doping/E-field (or Vtg/Vbg) and Vds share this syntax; scalar values repeat and lists pair row by row, with at most 100 conditions per addition. B-field Gate Scan retains its 10,000-target limit, duplicate-field rejection and configured field bounds. These limits include range endpoints. The live preview shows the expanded order before running or adding conditions. Older expressions that extended stop to compensate for its previous exclusion must now use the desired actual endpoint (for example, replace `-1:1.5:0.5` with `-1:1:0.5`). Existing settings and saved measurements are not rewritten.

Quick-added Vds uses Keithley 2400; the source remains editable in the conditions table. Connected voltage protections are checked during preview, and full instrument validation still runs before starting a sweep.

## Data output

### Optional second bias source

The original Vds source is **Bias 1**. Configure an additional Keithley 2400/2401
as **G4 / Bias 2** in Connections & protection, with its own address, voltage
limit and current compliance. Its address is empty by default. Existing saved
recipes load with Bias 2 **Disabled** and need no additional instrument.

Each measurement offers **Disabled**, **Fixed**, and **Follow**. Fixed holds the
entered voltage independently of Bias 1. Follow uses the commanded trajectory:
`Bias 2 = Bias 1 + offset`, including reverse passes and map row changes. Follow
requires an enabled Bias 1; Fixed can be used with Bias 1 disabled. Vds Sweep
itself still requires a Bias 1 source. Photocurrent and magnetic workflows save
the Bias 2 settings per condition. In Widgets, select a Photocurrent or B-field
condition to edit its Bias 2 settings below the table.

An enabled Bias 2 must be connected with verified protection and representable
targets before acquisition starts. Paired moves interleave bounded voltage steps,
then apply the measurement's settling delay before sampling. Two independent
instruments are not hardware-synchronous: the offset is a target-point relation,
not a guarantee during each individual write or startup transition. Stop and
cleanup return participating sources to zero independently, rather than retaining
the offset. Disabled Bias 2 is not written or read by the measurement; disabling
it is not a manual zero command. Use its explicit Zero control when needed.

The extra CSV fields are `Bias2` (commanded volts) and `Ibias2_Keithley` (measured
amps). Original `Vds` and `Ids_Keithley` retain their Bias 1 meaning. Unused
channels are blank, not a measured zero. Keithley currents are read sequentially,
so they are associated with one scan point but are not simultaneous measurements.
The second source is currently Keithley-only; Bias 1 retains its DAQ AO option.
Dual-bias map ETA uses a model allowance and live timing; older single-bias
historical calibration is not applied to it.

For one Lock-in serving either Drag or Drive, select its role in the global
Signal chain settings before starting; see [single Lock-in roles](drag-drive.md).
This AC role is independent of the two Keithley bias-current measurements.

Files are written under:

```text
<save base>/<user>/<device ID>/
```

The default base directory is `D:\photocurrent\data`; it is configurable under **Instruments → Sample & files**. CSV files include the requested biases, raw DAQ channels, converted current channels, and—when Vds is driven by Keithley—the measured Keithley current. Gate Scan and 2D Map also save `Vds_measured` when an NI DAQ AO channel supplies Vds, so the requested trajectory can be compared with the physical AO monitor. Gate scans additionally include derived `Doping`, `Efield`, and sweep `Direction` columns; photocurrent scans include `Wavelength`.

CSV writes are flushed during acquisition, which helps preserve data already collected if a run is interrupted.

In Qt Quick, the **File output** card at the top of the setup keeps **File base name**
beside the final CSV filename preview. Typing updates a read-only draft preview;
press Enter or leave the field to apply the name. **Expand / Alt+Enter** opens a
larger editor with the preview in the same dialog. Long names wrap, and **Copy**
copies complete preview filenames. Collapse the card to leave more room for the
measurement parameters. Plot controls remain under **Plot settings**.

The card shows the CSV count and destination folder; **Details** reveals full
filename examples, metadata and the run log. In B-field Gate Scan, the card edits
the selected condition and previews that condition at the first field target,
while the count includes all enabled jobs. `{timestamp}` and collision suffixes
are assigned on Start. Previewing does not change accepted parameters, reserve
filenames or create files or directories.

See [History and exports](history-and-export.md) for comparison, PNG snapshots, accompanying CSV exports and view restoration. Exported analysis CSVs have their own three-row label format; they do not replace the acquisition files.

## Safety behavior

- Quick Vds Sweep, Gate Scan (including B-field Gate Scan), 2D Map and Photocurrent validate requested `Vtg`, `Vbg` and `Vds` against ±20 V and any lower verified per-gate or DAQ-channel limit. B-field Sweep follows its separate controller path and checks fixed-bias conditions against the verified per-gate and DAQ-output limits.
- Quick manual absolute gate targets are limited to ±20 V. This is not a universal limit on every control: the per-gate protection editor accepts values up to 200 V, and relative manual step buttons validate against the applied per-gate protection limit. Check the verified instrument limits before manual operation.
- On connection, a Keithley already configured as a voltage source has its existing voltage setpoint read first. Any nonzero setpoint is safely ramped to 0 V without toggling the output, then the 1 µA current-compliance / 20 V source-range baseline is configured. The saved per-gate maximum source-voltage and current-compliance profile is then applied and verified automatically. Those limits remain editable with **Apply G1/G2/G3 protection** after connection (the Widgets frontend uses **Apply**). Nonzero moves remain blocked if protection is unverified or the current-compliance state is tripped; Read and Zero remain available.
- Manual gate moves read the Keithley's present programmed source level, then ramp rather than stepping abruptly. The per-gate **Ramp to 0 V** controls (**Zero** in Widgets) use the stricter safe-ramp step to return that source to 0 V.
- Connecting a DAQ reads each AO's existing voltage and adopts it as the ramp starting point without issuing an AO write. A nonzero output is highlighted in Instrument Setup.
- DAQ AO writes are isolated by channel: changing AO0 does not rewrite AO1. Measured AO readback is kept separate from the held command so readback noise cannot become a new output command.
- Every DAQ AO write is limited to a maximum 50 mV change. Larger moves must use the per-channel ramp-to-target or ramp-to-zero controls, which advance in controlled steps; unused AO channels remain untouched.
- Normal DAQ disconnect does not zero or rewrite its AO channels. Normal sweep cleanup ramps only the DAQ channel selected by that sweep. Normal Keithley disconnect and application shutdown ramp voltage-source channels to 0 V, verify the ramp, leave their outputs ON to clamp the sample, and release only the VISA session; they never issue `OUTP OFF` automatically.
- **STOP ALL / ZERO VOLTAGES** requests all workers to stop and safely ramps connected Keithley and requested DAQ AO outputs to 0 V.

Always confirm actual instrument state independently after an error, interrupted connection, or emergency stop.

### Magnetic-field completion and stop behavior

APS100 B-field Sweep Start/Stop now share the commissioned field envelope with
planning and execution: at most ±8 T, or the smaller configured control, absolute
field, or current × coil-constant limit. The form displays that effective limit.
The attoDRY2100 limit remains SDK-specific. Adding or duplicating conditions
chooses an unused name such as Con2/Con3; existing recipe names are preserved.

The sweep page displays LS335 A/B sensor current-reversal readbacks. Change these
under **Instruments → Sample temperature** while idle; OFF keeps DC temperature
readout but removes thermal EMF compensation. See [LS335 sensor current reversal](guides/SAMPLE_TEMPERATURE_1000.md#sensor-current-reversal-ac-excitation).

For hysteresis sampling, choose **B-field Sweep → Measurement mode → Stepped**.
Set Start/Stop in T, nominal Step size in mT, and one sample-temperature target.
APS100 approaches in the branch direction, confirms pause, qualifies field and
temperature independently, waits the extra delay, measures, then verifies fresh
post-readings. Stable offsets within the separate point/endpoint acceptance limits
are sampled at their field readback, without manual Continue. Passed interior
nominal targets are logged and skipped without reversing. History reversals, endpoint
violations, excessive actual sample gaps and quality timeouts end the run and preserve
its data and diagnostic records. Thermal protection retains its existing recovery
rules in Unchanged mode; automatic sample reversal ends the run on a monitoring or
thermal interruption. These software limits require commissioning; current-derived field readback
is not calibrated sample-field accuracy. See the [full settings and defaults](guides/SAMPLE_TEMPERATURE_1000.md#stepped-b-field-sweep).

For APS100 continuous and stepped sweeps, set **Maximum sweep rate (T/min)** per run. The configured maximum and current request are displayed in **Oe/s** above the input; 0.12 T/min equals 20 Oe/s. Start reads the instrument's **RANGE/RATE 0–4** table, and uses the lower of the requested maximum and each stored range rate; actual rates can therefore be slower. The requested maximum is rounded down to the instrument's 0.1 mA/s resolution. The program verifies the written rates, records requested and effective tables in metadata, and uses the effective rates for motion timeouts. On completion, cancellation or error it attempts to restore and verify the original rate table; a restoration failure is reported. Fast mode RATE 5 is not changed. The attoDRY2100 ramp remains SDK-managed. Filenames omit sweep-rate, stepped-point-count and condition-count tags, and use **D** for Doping and **E** for E-field; the complete recipe remains in metadata. Previously saved files are not renamed.

APS100 stepped moves use intermediate targets and reduced rates within the verified per-run limits. A fresh worker readback guards each commanded direction. An undershot stationary point can receive a same-direction correction within the original deadline; an overshot point is paused without reversing. Continue rechecks the point without authorizing a backwards move. Only planned round-trip turns and preparation between separate conditions change direction; preparation is not acquired as measurement data. Temporary approach rates are restored and verified before measurement. Field/temperature qualification and the extra delay restart after excursions or stale readings; post-acquisition checks discard invalid samples. Requested and measured fields remain separate in CSVs. Continuous sweeps retain their existing acquisition-during-motion behavior, and attoDRY2100 retains its SDK-managed path.

For **attoDRY2100**, select that system under Instruments, review its commissioned connection and limits, and use **Stepped** mode. The workflow actively sets and verifies the sample-temperature target through the SDK; sample temperature control stays enabled while the field is on. The 1000 heater-OFF restriction does not apply to this backend. Field ramping uses the cryostat's SDK control, without APS100 ramp tables, persistent-switch commands or lead-zeroing rules. Start requires Driven mode and no ongoing field motion.

At each 2100 point, the field setpoint must match, SDK field control must report IDLE, and three fresh field readings must be within ±10 mT. Then fresh sample-temperature readings must qualify over the selected window: by default 10 s, within ±0.05 K of the same target, with peak-to-peak variation ≤0.02 K and absolute fitted drift ≤0.01 K/min. These limits are editable. The 10 s is a temperature observation window, not a fixed field delay or a guarantee of thermal equilibrium. Both field and temperature are checked again after acquisition; a failed check discards that sample and requalifies the point.

If temperature cannot qualify before the timeout, 2100 field control receives Stop, temperature control stays enabled, and safety monitoring continues. **Continue** retries the same point and requires fresh qualification; it does not override the limits. Normal completion and user Stop also stop field control and retain sample-temperature control. Manual field/temperature changes and backend switching are locked during measurement and cleanup. Electrical outputs are zeroed and files flushed before releasing the devices; a failed SDK Stop retains the reservation and requires retrying Stop. CSV and metadata record the backend, requested/measured field, sample temperature and stability metrics, with magnet temperature stored separately. Verify this workflow on the commissioned hardware before production acquisition; offline tests do not establish physical settling performance.

Successful **APS100** B-field Transport sweeps always finish in **Driven (leave heater ON)**, holding the final field for the next sweep without persistent-mode cooldown or lead zeroing. Here, heater means the APS100 persistent-switch heater, not the sample heater. A round trip finishes at its starting field. Older recipes and saved Persistent selections are overridden, and the successful final-mode display is fixed to Driven. Stop, error, interlock, and application-shutdown cleanup still use Persistent mode.

B-field Transport runs voltage ramps and sample acquisition on one serialized worker, and CSV/checkpoint writes on another. STOP cancels ramps between steps, pauses APS100 or requests 2100 SDK field Stop, then waits for in-flight instrument work, bias zeroing, and output flushing before releasing devices. An unresponsive driver can still delay cleanup, but it no longer blocks Qt telemetry processing.

With **Keithley 2400** selected as the Vds source, B-field Transport publishes the measured G3 current in amperes as **Ids_Keithley** for live plots and CSV output. The historical **Keithley_current** column remains an identical alias from the same acquisition. A failed read is recorded in **Keithley_read_error** and is not replaced by a zero current. Existing CSV files with only the historical column remain readable in History.

Monitoring-loss recovery, endpoint windows and optional settling have specific acknowledgement and timeout rules. See [Developer notes: transport timing and recovery](developer-notes.md#transport-timing-and-recovery), [sample-temperature controls](guides/SAMPLE_TEMPERATURE_1000.md) and [thermal protection](guides/FIELD_THERMAL_COMPARISON.md).

[Documentation index](README.md) · [Project overview](../README.md)

### Sample B current reversal during APS100 stepped sweeps

The default **Unchanged** leaves the LS335 sample B current reversal as set.
**Per point** first qualifies a full temperature window with B ON. It confirms B
OFF, waits the configured settling time, acquires, then confirms B ON and waits
again. A fresh ON temperature window must qualify, and the pre- and post-acquisition
ON temperatures must differ by no more than the configured maximum temperature
span, before the point is committed. OFF readings remain in use for thermal safety,
but are not mixed into either ON stability window.

**Whole sweep** keeps B OFF during the sweep and retains the existing sample target,
span and drift stability requirements; no tolerance is automatically widened.
The settling time is a configured wait, not proof that a transient has disappeared.
Temperature readout continues with DC excitation, but thermal EMF compensation is
lost. Automatic modes require APS100 Stepped, verified sample B / magnet A mapping
and supported sensor inputs. A must already be ON; the program never changes A.

A monitoring or thermal protection interruption ends an automatic-mode run; it
does not automatically continue the interrupted point. Stop first completes APS
cleanup and drains pending DAQ and LS operations, then restores the original B
setting. If restoration fails, the instrument reservation is retained and **Stop**
retries restoration. All magnet and thermal safety checks remain active.
