# Measurement guide

This guide describes the default Qt Quick interface. For installation, EXE downloads and launch options, see [Setup and launch](user-setup.md) ([中文](user-setup.zh-CN.md)). The retained Widgets frontend is selected with `--widgets`; its labels can differ.

The application coordinates Keithley 2400 source meters, an NI DAQ device and an SP-2300 monochromator for electrical transport and photocurrent measurements. It plots acquired data while saving each measurement to CSV.

> **Laboratory software:** This program can change instrument outputs. Verify cable routing, instrument limits, compliance settings, and the selected hardware addresses before every run. Software safeguards are helpful, but they are not a replacement for laboratory safety procedures or hardware interlocks.

## Measurement modes and live plots

| Measurement page | Purpose |
| --- | --- |
| **Vds Sweep** | Sweep drain-source bias while holding top- and back-gate biases. Vds can come from Keithley G3 or an NI-DAQ analog-output channel. |
| **Gate Scan** | Run a one-dimensional raw-voltage trajectory or a derived doping/electric-field trajectory, with an optional reverse pass. |
| **B-field Sweep** | Sweep APS100 between editable B-field endpoints while holding one or more fixed Doping/E-field/Vds rows; round trip is enabled by default. |
| **2D Map** | Acquire a 1D or 2D grid across `Vtg`, `Vbg`, and/or `Vds`; select fast and slow axes and preview the planned sweep. |
| **Photocurrent** | Sweep monochromator wavelength for one or more enabled Vtg/Vbg recipe conditions, with optional per-condition Vds. |
| **B-field Gate Scan** | Execute gate-scan conditions at selected magnetic-field targets. |

Across the modes, the application averages DAQ readings, plots the selected current channel live, and writes the acquired points to CSV as the run proceeds. The plot can switch between a single selected channel and a four-channel comparison view.

In Quick Gate Scan, **Trajectory** selects **Raw** voltages or **Derived** Doping/E-field coordinates. The Widgets selector uses **Raw Voltages** / **Doping / E-field**. For derived values, the coupling ratio can multiply either `Vbg` (the backward-compatible default) or `Vtg`. The displayed equations, computed voltage preview, generated sweep trajectory, plot axes, CSV columns, and run metadata all use the selected definition.

Live plots default to **Follow Sweep** for the x-axis: Gate Scan follows its selected Doping/E-field or single raw-voltage sweep, while 2D Map follows its fast axis. A manual x-axis override can display Step Index, `Vtg`, `Vbg`, `Vds`, Doping, or E-field; changing this display setting replots already collected points without changing the hardware trajectory.

**X range: Full sweep** is the default live view: the complete execution range is shown before the first point, with a small margin. Gate Scan and 2D Map use the generated trajectory, including derived coordinates, additional regions and round-trip step indices. **Acquired data** fits the X range to recorded points; plots without a known plan also follow recorded points. Y limits follow measured values in either mode. Four-channel views share X limits and scale Y independently. The toolbar provides pan, rectangle zoom and figure saving; incoming points preserve manual X/Y limits. **Reset view** (**Reset** in a compact Quick plot, **Home** in Widgets) restores the selected range policy and automatic Y scaling. Changing the X coordinate resets X zoom, and changing the current channel resets Y zoom. Starting a new run clears the previous plan and zoom while retaining the selected range mode. Readback beyond the planned view is included automatically.

Use **History** to open the separate read-only viewer for saved scans, maps and measured line cuts. This is the **Curve Compare** page in the Widgets frontend. See [History and exports](history-and-export.md).

## Typical measurement workflow

1. Start the application and open **Instruments** in the top command area. Select **Connections & protection** in the instrument panel selector.
2. Select the GPIB/serial/DAQ addresses and the operating mode for each Keithley. Use **Scan resources** to list detected resources. Clear the address fields for unused devices before connecting: **Connect all** attempts configured, nonempty addresses.
3. Set the save location, operator name, device ID, amplifier gain, and lock-in gain as appropriate for the experiment.
4. Click **Connect all** and confirm that the required instruments report an OK status.
5. Use **Manual controls** in Instruments when needed: type and ramp a gate target, use the ±0.1 V ramp buttons, read one gate on demand, safely ramp a gate or DAQ AO back to 0 V, or move the monochromator. Gate controls are available only in 2-wire voltage-source mode and while no measurement is active.
6. Select a measurement mode above the parameters, set its sweep bounds, timing, averaging, source, and file name, then review the trajectory summary. Photocurrent supports an editable bias recipe: unchecked rows are retained but skipped, every enabled condition creates its own CSV file, and Vds values require a compatible connected and explicitly enabled source.
7. Start the measurement and monitor the live plot and status messages. Use **Stop measurement** for the active run or the fixed top **STOP ALL / ZERO VOLTAGES** button when needed.
8. Review the resulting CSV in the selected save directory.

The application owns the instrument connections: address or Keithley-mode changes require reconnection before they apply to a measurement.

For **2D Map**, the **Keithley 2400** source option supports both 2400 and 2401 instruments. The app checks the detected model and confirmed source range before starting, including fixed Vds values. If a supported source has no cached range, Start refreshes its readback in the background; review the result and press Start again. Resolution messages show which source, model, range or readback error needs attention. The 2401 uses the 0.2, 2 and 20 V source ranges; its resolution check does not accept a 200 V range.

**Instruments → Magnet & temperature** contains separate controls for **attoDRY1000 / LS335** and **attoDRY2100 / SDK**. **Detect & connect** tries the configured connections and selects the only connected system; if both are online, choose one manually. The header shows the selected system's read-only summary. The 1000 supports verified sample targets, heater ranges, ramps, optional stability waiting and automatic ranges from commissioned temperature bands. See [sample-temperature instructions](guides/SAMPLE_TEMPERATURE_1000.md).

## Workspace and editing

The Quick workspace separates **Measurement setup** on the left, **Live data** in the center, and **Instruments** on the right. Open Instruments, then use **Pin** (also available in View) to keep the panel beside the plot; this works at 1920×1080 with 125% scaling. Narrower windows show instruments in a right-hand drawer while keeping the global Stop and measurement actions reachable. Drag the section dividers to resize. **Hide** in the setup header expands the plot area; **View → Show parameters** restores the setup panel. The measurement log has its own panel and Close control; opening it in a narrow window selects Live plot automatically.

Each measurement page groups its trajectory first and folds advanced ramps, output and plot settings; field errors must be corrected before Start. Recipe editors are visually separated from the condition list and actions. B-field Gate Scan edits the selected execution condition, and B-field Sweep displays Vtg/Vbg calculated from its shared coupling ratio.

Long names, paths and numeric lists offer an inline **Expand** action or **Alt+Enter** to view/edit the full text, then Save or **Ctrl+Enter** to apply. Read-only values can also be expanded and copied. Data root has an inline folder chooser (**Alt+B**); visual wrapping preserves the stored value.

## Quick-add and field-target syntax

Quick-add and B-field Gate Scan accept single numbers, comma/newline-separated lists (`-1,1,5` means three values), point-count ranges (`(-1,1,5)` means five equally spaced points including -1 and +1), and `start:stop:step` ranges including both endpoints. `linspace(-1,1,5)` and `np.linspace(-1,1,5)` are aliases of the parenthesized point-count form. For five doping values from -1 through +1 use `(-1,1,5)` or `-1:1:0.5`. If a step does not divide the span, the exact stop is appended: `0:1:0.3` gives `0,0.3,0.6,0.9,1`; the final interval is shorter. A point count must be a positive integer, with at least two points for distinct endpoints. Ranges can be mixed with explicit values; commas inside parentheses belong to the range.

Doping/E-field (or Vtg/Vbg) and Vds share this syntax; scalar values repeat and lists pair row by row, with at most 100 conditions per addition. B-field Gate Scan retains its 10,000-target limit, duplicate-field rejection and configured field bounds. These limits include range endpoints. The live preview shows the expanded order before running or adding conditions. Older expressions that extended stop to compensate for its previous exclusion must now use the desired actual endpoint (for example, replace `-1:1.5:0.5` with `-1:1:0.5`). Existing settings and saved measurements are not rewritten.

Quick-added Vds uses Keithley 2400; the source remains editable in the conditions table. Connected voltage protections are checked during preview, and full instrument validation still runs before starting a sweep.

## Data output

Files are written under:

```text
<save base>/<user>/<device ID>/
```

The default base directory is `D:\photocurrent\data`; it is configurable under **Instruments → Sample & files**. CSV files include the requested biases, raw DAQ channels, converted current channels, and—when Vds is driven by Keithley—the measured Keithley current. Gate Scan and 2D Map also save `Vds_measured` when an NI DAQ AO channel supplies Vds, so the requested trajectory can be compared with the physical AO monitor. Gate scans additionally include derived `Doping`, `Efield`, and sweep `Direction` columns; photocurrent scans include `Wavelength`.

CSV writes are flushed during acquisition, which helps preserve data already collected if a run is interrupted.

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

Successful B-field Transport sweeps always finish in **Driven (leave heater ON)**, holding the final field for the next sweep without persistent-mode cooldown or lead zeroing. A round trip finishes at its starting field. Older recipes and saved Persistent selections are overridden, and the successful final-mode display is fixed to Driven. Stop, error, interlock, and application-shutdown cleanup still use Persistent mode.

B-field Transport runs voltage ramps and sample acquisition on one serialized worker, and CSV/checkpoint writes on another. STOP cancels ramps between steps, pauses APS100, then waits for in-flight instrument work, bias zeroing, and output flushing before releasing devices. An unresponsive driver can still delay cleanup, but it no longer blocks Qt telemetry processing.

Monitoring-loss recovery, endpoint windows and optional settling have specific acknowledgement and timeout rules. See [Developer notes: transport timing and recovery](developer-notes.md#transport-timing-and-recovery), [sample-temperature controls](guides/SAMPLE_TEMPERATURE_1000.md) and [thermal protection](guides/FIELD_THERMAL_COMPARISON.md).

[Documentation index](README.md) · [Project overview](../README.md)
