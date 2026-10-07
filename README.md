# Transport Measurement

The project root is the official PySide6 version. Launch `Transport_App.bat`
from this directory; the retained `tmp/pyside6_migration` copy is no longer the
release location. See [release and rollback notes](docs/pyside6-release.md).

Desktop application for automated electrical transport and photocurrent measurements. The PySide6 interface coordinates Keithley 2400 source meters, an NI DAQ device, and an SP-2300 monochromator; it provides live plots while saving each measurement to CSV.

> **Laboratory software:** This program can change instrument outputs. Verify cable routing, instrument limits, compliance settings, and the selected hardware addresses before every run. Software safeguards are helpful, but they are not a replacement for laboratory safety procedures or hardware interlocks.

## Measurement modes

| Tab | Purpose |
| --- | --- |
| **Vds Sweep** | Sweep drain-source bias while holding top- and back-gate biases. Vds can come from Keithley G3 or an NI-DAQ analog-output channel. |
| **Gate Scan** | Run a one-dimensional raw-voltage trajectory or a derived doping/electric-field trajectory, with an optional reverse pass. |
| **B-field Sweep** | Sweep APS100 between editable B-field endpoints while holding one or more fixed Doping/E-field/Vds rows; round trip is enabled by default. |
| **2D Map** | Acquire a 1D or 2D grid across `Vtg`, `Vbg`, and/or `Vds`; select fast and slow axes and preview the planned sweep. |
| **Photocurrent** | Sweep monochromator wavelength for one or more enabled Vtg/Vbg recipe conditions, with optional per-condition Vds. |
| **Curve Compare** | Open a separate read-only history viewer for saved scans, maps, and measured line cuts. |

Across the modes, the application averages DAQ readings, plots the selected current channel live, and writes the acquired points to CSV as the run proceeds. The plot can switch between a single selected channel and a four-channel comparison view.

Gate Scan provides a prominent **Raw Voltages** / **Doping / E-field** trajectory selector. For derived values, the coupling ratio can multiply either `Vbg` (the backward-compatible default) or `Vtg`. The displayed equations, computed voltage preview, generated sweep trajectory, plot axes, CSV columns, and run metadata all use the selected definition.

Live plots default to **Follow Sweep** for the x-axis: Gate Scan follows its selected Doping/E-field or single raw-voltage sweep, while 2D Map follows its fast axis. A manual x-axis override can display Step Index, `Vtg`, `Vbg`, `Vds`, Doping, or E-field; changing this display setting replots already collected points without changing the hardware trajectory.

## Hardware and software requirements

- Windows 10/11 (the supplied launcher is a Windows batch file)
- Python **3.10 or newer**
- [NI-DAQmx](https://www.ni.com/en/support/downloads/drivers/download.ni-daq-mx.html) runtime and a supported NI DAQ device
- A VISA implementation with the required GPIB/serial interfaces available (for example, NI-VISA)
- Up to three Keithley 2400 instruments:
  - G1 / `Vtg` and G2 / `Vbg` are gate sources
  - G3 can serve as the Keithley Vds source
- An SP-2300 monochromator over a serial VISA resource for photocurrent scans

The instrument setup panel can scan for GPIB, serial (`ASRL`), and NI DAQ resources. The defaults in the UI are lab-specific examples; update them to match the connected hardware.

## Installation

From PowerShell, clone the repository and create an isolated Python environment:

```powershell
git clone <repository-url>
cd Transport_Pyside6
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The Python dependencies are listed in [`requirements.txt`](requirements.txt): PySide6, Matplotlib, NumPy, PyVISA, NI-DAQmx, and pythonnet.

## Launch

With the environment activated:

```powershell
python transport_UI.py
```

`main.py` is an equivalent direct entry point. On Windows, double-clicking [`Transport_App.bat`](Transport_App.bat) creates and reuses a project-local `.venv`, installs any missing requirements there, validates the application imports, and launches the UI. Python must be available on `PATH` for the first launch.

The launcher also creates/repairs `Transport_App - Shortcut.lnk` in the project folder. This shortcut starts the project Python directly, uses the bundled `assets/transport.ico`, and shares the application's Windows taskbar identity. Use it for subsequent launches and taskbar pinning. After moving the checkout, run `Transport_App.bat` again to refresh its paths. To repair only the shortcut without starting the app, run `powershell -NoProfile -File .\Update-TransportShortcut.ps1`. If an old taskbar pin still shows a generic icon, unpin it and pin the repaired shortcut again. Restart the app when convenient to apply window-icon changes; reloading analysis applies them to the history viewer only.

## Typical measurement workflow

1. Start the application and open **Devices** in the top command area. Instrument controls are in the left sidebar.
2. Select the GPIB/serial/DAQ addresses and the operating mode for each Keithley. Use **Scan Hardware** to populate detected resources.
3. Set the save location, operator name, device ID, amplifier gain, and lock-in gain as appropriate for the experiment.
4. Click **Connect All** and confirm that the required instruments report an OK status.
5. Use **Manual Controls** in Instrument Setup when needed: type and ramp a gate target, use the ±0.1 V ramp buttons, read one gate on demand, safely ramp a gate or DAQ AO back to 0 V, or move the monochromator. Gate controls are available only in 2-wire voltage-source mode and while no measurement is active.
6. Select a measurement tab, set its sweep bounds, timing, averaging, source, and file name, then review any preview or estimated sweep information. The Photocurrent tab supports an editable bias recipe: unchecked rows are retained but skipped, every enabled condition creates its own CSV file, and Vds values are available only when a compatible Vds source is connected and explicitly enabled.
7. Start the measurement and monitor the live plot and status messages. Use **Stop measurement** for the active run or the fixed top **STOP ALL / ZERO VOLTAGES** button when needed.
8. Review the resulting CSV in the selected save directory.

The application uses dock-managed connections: address or Keithley-mode changes require reconnection before they apply to a measurement.

**Instruments → Magnet** contains separate temperature controls for **attoDRY1000 / LS335** and **attoDRY2100 / SDK**. **Detect & connect system** tries the configured connections and selects the only connected system; if both are online, choose one manually. The main page shows the selected system's read-only summary and a **Temperature controls…** shortcut. The 1000 supports verified sample targets, heater ranges, ramps, optional stability waiting and automatic ranges from commissioned temperature bands. See [sample-temperature instructions](SAMPLE_TEMPERATURE_1000.md).

Long names, paths and numeric lists have a wrapped full-value preview. Use the expand icon or **Alt+Enter** to edit the full text, then Save or **Ctrl+Enter** to apply. Data root also has a folder chooser; visual wrapping preserves the stored value.

## Measurement history and curve comparison

Select **Curve Compare** to open the independent **Transport — Measurement History** window. It defaults to the current operator/device directory, searches its date/type subfolders, and shows **All dates**, newest first. Vds Sweep, Gate Scan, 2D Map, B-field Gate Scan, B-field Sweep and Photocurrent each have their own history page; selections do not mix across types. Use **Browse folder…** for another directory, or **Current device** to resume following Sample / Files. Date filters and search remain available.

The full-height file list is on the left, with time, filename, conditions and status shown on separate lines. Drag the divider to resize it, or click **Expand file list** to use the whole content area. **Return to comparison** restores the split layout without losing checks or plots. Check files to overlay curves. Choose X, signal and direction; forward/unknown traces are solid and backward traces are dashed. Condition blocks and snake rows stay separate, source order is preserved, and units must match. Selections and display choices are remembered per folder and measurement type. The plot toolbar supports zoom/pan and saving figures.

Long filenames wrap at underscores, with character wrapping for oversized components, and row heights follow the list width. When an existing CSV grows, background refresh updates its data without resetting the list's current row or scroll position. Unchanged refreshes do not rewrite saved view preferences. Use **Reload analysis** to apply analysis UI changes while Transport remains open.

**Normalize** switches between **Raw**, **Max |Y|** (`Y / max(abs(Y))`, retaining signs) and **0–1** (`(Y - min(Y)) / (max(Y) - min(Y))`). Each displayed direction/condition trace is scaled independently using its finite plotted points across the entire trace, regardless of zoom. Zero traces remain zero; constant traces become zero in 0–1 mode; missing points remain gaps. For 2D maps, **Normalize cuts** affects comparison cuts while the heatmap retains physical units. Normalization runs in the analysis read worker, preserves original CSV/cache values, and is remembered per measurement type and folder, including analysis reload and PNG view export/restore. Normalized axes are dimensionless and labeled with the method. Automatic measurement PNGs retain physical values.

Comparison legends combine differences in the saved measurement name and JSON settings, including lock-in phase, output amplitude, frequency, sensitivity, preamp gain, separate Drag/Drive settings and scan parameters such as sample count or delay. Shared name fragments are omitted; for example, names that differ only in `ACDCMoTe2E1` / `ACDCMoTe2E2` show those fragments. Without a saved base name, the CSV name is used after removing its acquisition timestamp. Output paths and filename bookkeeping are excluded. Shared conditions appear once in the PNG header. Only runs whose names and saved settings cannot distinguish them use `Run 1`, `Run 2`, etc. Missing metadata is shown as unknown when compared with a recorded setting. Curves use lines with point markers in both the window and PNG exports.

The **Signal** selector includes **Drag ratio X**, **Drag ratio Y** and **Drag ratio R**: saved `I_drag_X / I_drive_X`, `I_drag_Y / I_drive_Y` and `I_drag_R / I_drive_R`. These dimensionless, point-by-point ratios work with curves, maps, cuts and normalization. If R columns are absent, magnitudes are calculated from the saved X/Y components. Zero Drive, missing or nonfinite results remain gaps with a warning. Ratios are calculated in the analysis worker and never written into the original measurement CSV.

For **2D Map**, choose the map's X and **Map Y** coordinates. **Map file** selects the checked map displayed on the left; **Fixed axis** and **Measured value** produce cuts from all checked compatible maps on the right. Only exact measured coordinates are used. **All rows** is the default: native `PassIndex` is a slow-axis row number. A specific **Row / pass** or direction can isolate rows or resolve repeated coordinates. Duplicate points are never averaged, missing cells remain blank, and a file without the requested coordinate/channel is reported separately. Use **None (1D)** for a one-axis scan saved by 2D Map.

History discovery, CSV loading and Matplotlib rendering run in a separate Python/Qt process with lower CPU priority. The measurement app only sends directory, refresh, focus and close notifications; the viewer imports no instrument/control modules and never modifies measurement CSVs or metadata. Header indexing is cached; complete CSVs are loaded only when checked, with a bounded 64 MiB numeric cache. Visible history refreshes periodically and when measurements finish. This separates GUI work and failures from acquisition; CPU, RAM and disk remain shared resources. Large datasets can still contend for disk bandwidth, so this is not a hard real-time guarantee.

Closing the history window leaves measurements running. Selecting Curve Compare again reopens it. The viewer can also run independently with `python transport_history_viewer.py --folder <measurement-directory>`.

## PNG exports

**Curve Compare** contains a persistent **Automatically save channel PNGs after measurements** option, enabled by default. After the acquisition thread or B-field controller completes its cleanup and closes storage, an independent below-normal-priority renderer saves each available `Ids_DC`, `Ids_X`, `Ids_Y` and `Ids_Keithley` channel separately. `Keithley_current` is recognized as the Keithley alias. Empty/non-finite channels produce no image. Each CSV receives `<csv-stem>_<channel>.png` in its measurement directory's `plots` subfolder. Photocurrent condition files and B-field series files use their saved run metadata/manifest; 1D maps use the saved resolved X axis. True 2D grids are not automatically flattened into lines.

In the history window, **Export PNG + CSV** automatically saves beside the source CSV, in a folder named from its stem. New folders shorten `Doping` to `D`, `DragDrive` to `DD`, and `ACout50mV_VacEst11mV` to `AC50mV-est11mV`, preserving values, units, timestamp and sensitivity settings. Existing full-name folders continue to be reused. If two sources abbreviate to the same folder name, the second retains its full name. The menu offers **Heatmap only**, **Cut only**, or **Heatmap + cut**, each with separate PNG and CSV files. PNG names use the same abbreviations and omit the timestamp and LIA/Pre sensitivity fields. If repeating the conditions in the PNG filename would exceed the 259-character absolute path budget, its name contains only the signal, plot type and actual cut coordinate (for example `I_drag_Y_heatmap.png`), inside the same source folder. Source identity and conditions remain in the folder, visible header, PNG metadata and accompanying CSV/view. PNG, CSV, JSON and their version suffixes are checked against this budget; application-facing paths omit filesystem-only `\\?\` prefixes. Short CSV names identify the signal, plot type and actual cut coordinate. No files are silently moved to another export root. **Save as…** is available in the menu or by right-clicking the export button.

CSVs contain exactly three label rows: **Long Name**, **Units**, **Comments**, followed by numeric data. Comments contain the complete source filename/path, acquisition time, saved conditions, selection and normalization. Heatmaps are Y-by-X matrices: the first column holds Y, and the remaining Long Names hold numeric X coordinates. Signal values retain their physical units, full floating-point precision and missing values. Cuts use X/Y column pairs (one pair per compared trace, without aligning unequal grids). All selected samples are exported; plot zoom and colormap affect the PNG only. In Origin, map rows 1/2/3 to Long Name/Units/Comments if auto-detection does not do so, then plot a virtual matrix with X from Long Names and Y from the first column. No Origin scripts or additional data copies are generated.

Unchanged automatic PNGs and CSVs are reused independently; changing a cut does not duplicate its unchanged heatmap, and changing only the colormap/zoom does not duplicate numerical CSV data. Changed artifacts get readable `_v02`, `_v03` suffixes. Existing or externally edited files are preserved. A corresponding `_view.json` records source paths/fingerprints, metadata, display settings and output associations. **Restore view…** reloads those original CSVs and settings; changed-source fingerprints are reported. Exports use the arrays already displayed, so a file being appended or edited cannot silently change an in-flight export. Export actions wait for pending selection/axis changes to finish.

**Source CSV** selects the original measurement file shown in the heatmap. X, Y and Signal each show their range controls directly beside the selector, with independent **Auto** or **Fixed** modes. Auto X/Y shows the full grid; pan/zoom makes that axis fixed. Auto color follows finite Z samples whose measured X/Y coordinates lie inside the current view, including reversed axes. Empty/all-missing views retain the previous color bounds; constant values receive a small display interval. Fixed color bounds stay fixed during zooming; changing Signal resets color to Auto while retaining X/Y ranges. Enter bounds in the displayed units (scientific notation is accepted), then press Enter or leave the field to apply. Background redraws preserve unfinished edits. Automatic values are shown compactly, with full precision available in their tooltips. Settings survive reopening and Restore view. PNG export resolves the same visible-area color limits; CSV always retains the full selected grid/trace and is reused when only these display settings change.

Each analysis PNG is **1200 × 900 pixels**, **150 dpi**, white and opaque, with compact margins and inward ticks on all sides. Heatmaps have no grid or cut guide; line plots use a light major grid. Axis labels use **22 pt**, ticks **20 pt**, and headers/legends **16 pt**; unusually long metadata or crowded legends shrink to fit. Currents use engineering units consistently across a comparison. Measured saved conditions override dormant recipe defaults; unknown fields are omitted. Analysis headers contain common fixed conditions, with differing/varying conditions in curve legends. Stopped or failed data are labeled Partial.

PNG rendering owns no instruments, runs in a serial detached process and never modifies source CSVs or measurement JSON. It does not block acquisition or the next batch run. Accepted jobs drain after a normal app close; the viewer hands its immutable snapshot to disk before Transport closes it. Rendering failures are reported separately from measurement state. CPU, RAM and disk are still shared with acquisition. The history plot-toolbar save action also uses automatic PNG + CSV export and creates a reproducible view sidecar.

**Reload analysis modules** in the Curve Compare entry (or **Reload analysis** in its history window) reloads analysis and PNG-rendering Python code while Transport stays open. Accepted exports finish first; run exports submitted during reload are durably queued for the replacement renderer. The reopened viewer restores its folder/follow-current choice, checked files, measurement tab, filters, primary map, cuts, zoom, color scale, splitter and window geometry. If the current device changes while a following viewer reloads, it follows the new device rather than transplanting the previous device's view. A closed viewer stays closed. Reload errors are shown separately from measurement state. Main-window controls, acquisition/controller code and dependency/environment updates still require a full app restart.

## Data output

Files are written under:

```text
<save base>/<user>/<device ID>/
```

The default base directory is `D:\photocurrent\data`; it is configurable in Instrument Setup. CSV files include the requested biases, raw DAQ channels, converted current channels, and—when Vds is driven by Keithley—the measured Keithley current. Gate Scan and 2D Map also save `Vds_measured` when an NI DAQ AO channel supplies Vds, so the requested trajectory can be compared with the physical AO monitor. Gate scans additionally include derived `Doping`, `Efield`, and sweep `Direction` columns; photocurrent scans include `Wavelength`.

CSV writes are flushed during acquisition, which helps preserve data already collected if a run is interrupted.

## Safety behavior

- The UI limits requested bias values to ±20 V.
- On connection, a Keithley already configured as a voltage source has its existing voltage setpoint read first. Any nonzero setpoint is safely ramped to 0 V without toggling the output, then the 1 µA current-compliance / 20 V source-range baseline is configured. The saved per-gate maximum source-voltage and current-compliance profile is then applied and verified automatically. Those limits remain editable with **Apply** after connection. Nonzero moves remain blocked if protection is unverified or the current-compliance state is tripped; Read and Zero remain available.
- Manual gate moves read the Keithley's present programmed source level, then ramp rather than stepping abruptly. The per-gate **Zero** controls use the stricter safe-ramp step to return that source to 0 V.
- Connecting a DAQ reads each AO's existing voltage and adopts it as the ramp starting point without issuing an AO write. A nonzero output is highlighted in Instrument Setup.
- DAQ AO writes are isolated by channel: changing AO0 does not rewrite AO1. Measured AO readback is kept separate from the held command so readback noise cannot become a new output command.
- Every DAQ AO write is limited to a maximum 50 mV change. Larger moves must use the per-channel **Ramp** or **Zero** controls, which advance in controlled steps; unused AO channels remain untouched.
- Normal DAQ disconnect does not zero or rewrite its AO channels. Normal sweep cleanup ramps only the DAQ channel selected by that sweep. Normal Keithley disconnect and application shutdown ramp voltage-source channels to 0 V, verify the ramp, leave their outputs ON to clamp the sample, and release only the VISA session; they never issue `OUTP OFF` automatically.
- **STOP / ZERO ALL** requests all workers to stop and safely ramps connected Keithley and requested DAQ AO outputs to 0 V.

Always confirm actual instrument state independently after an error, interrupted connection, or emergency stop.

## Project layout

```text
app/                    PySide6 UI, application models, workers, and device manager
app/ui/tabs/            Measurement tabs: Vds Sweep, Gate Scan, 2D Map, Photocurrent
app/workers/            Background acquisition and CSV-writing workers
instruments/            Keithley, NI-DAQ, monochromator, and other instrument drivers
transport_UI.py         Primary GUI entry point
Transport_App.bat       Windows launcher
requirements.txt        Python dependencies
```

## Development notes

B-field Transport runs voltage ramps and sample acquisition on one serialized worker, and CSV/checkpoint writes on another. STOP cancels ramps between steps, pauses APS100, then waits for in-flight instrument work, bias zeroing, and output flushing before releasing devices. An unresponsive driver can still delay cleanup, but it no longer blocks Qt telemetry processing.

APS100 and Lake Shore cache completed readings in their polling workers and coalesce superseded UI updates. Temporary stale/missing monitoring requests an acknowledged APS100 pause instead of immediately ending the series. The `mcd.transport_monitor_recovery_timeout_s` default is 30 seconds; missing pause acknowledgement escalates after the communication allowance (normally 5 seconds). Recovery requires `mcd.transport_monitor_recovery_reads` (default 3) new valid pairs of readings after acknowledgement, followed by thermal permission. The commissioned thermal recovery dwell has a separate clock. Real temperature/sensor/quench/voltage faults retain their fault handling. Holds and acquisition gaps are logged, and acquisitions spanning a hold are discarded; no missing samples are fabricated.

Plots reuse a single trace with at most four refreshes per second. CSV writes remain ordered and synchronized; growing checkpoints are coalesced, normally at most once every two seconds during acquisition, with final files flushed before completion. CSV records include acquisition start/end timestamps and Keithley read errors for timing diagnostics.

B-field Transport continuous sweeps accept the first fresh reading inside the endpoint window approached in the commanded direction, save its measured field, and wait for pause acknowledgement before reversing. The window uses `magnet.field_tolerance_t` (default 0.002 T), capped at 1% of the sweep span with an instrument-resolution floor for short spans.

Successful B-field Transport sweeps always finish in **Driven (leave heater ON)**, holding the final field for the next sweep without persistent-mode cooldown or lead zeroing. A round trip finishes at its starting field. Older recipes and saved Persistent selections are overridden, and the successful final-mode display is fixed to Driven. Stop, error, interlock, and application-shutdown cleanup still use Persistent mode.

Quick-add and B-field Gate Scan accept single numbers, comma/newline-separated lists (`-1,1,5` means three values), point-count ranges (`(-1,1,5)` means five equally spaced points including -1 and +1), and `start:stop:step` ranges including both endpoints. `linspace(-1,1,5)` and `np.linspace(-1,1,5)` are aliases of the parenthesized point-count form. For five doping values from -1 through +1 use `(-1,1,5)` or `-1:1:0.5`. If a step does not divide the span, the exact stop is appended: `0:1:0.3` gives `0,0.3,0.6,0.9,1`; the final interval is shorter. A point count must be a positive integer, with at least two points for distinct endpoints. Ranges can be mixed with explicit values; commas inside parentheses belong to the range.

Doping/E-field (or Vtg/Vbg) and Vds share this syntax; scalar values repeat and lists pair row by row, with at most 100 conditions per addition. B-field Gate Scan retains its 10,000-target limit, duplicate-field rejection and configured field bounds. These limits include range endpoints. The live preview shows the expanded order before running or adding conditions. Older expressions that extended stop to compensate for its previous exclusion must now use the desired actual endpoint (for example, replace `-1:1.5:0.5` with `-1:1:0.5`). Existing settings and saved measurements are not rewritten.

Quick-added Vds uses Keithley 2400; the source remains editable in the conditions table. Connected voltage protections are checked during preview, and full instrument validation still runs before starting a sweep.

Optional endpoint settling is controlled in the saved configuration's `mcd` section: `transport_endpoint_settling_enabled` defaults to `false`, and `transport_endpoint_settling_timeout_s` defaults to `120.0`. When enabled, `endpoint_stable_reads` (default 3) consecutive in-window readings are required. The sweep advances immediately once they pass; excursions reset the count without restarting the timeout. Restart the app after changing these settings. Existing instrument protections remain active.

The app saves user configuration and plot-mode preferences through Qt settings. Before modifying an instrument driver or measurement worker, test against a controlled setup or suitable hardware simulation—never a device whose limits have not been confirmed.

## License

No license file is currently included in this repository. Do not assume permission to redistribute or reuse the code until a license is added by the project owner.
