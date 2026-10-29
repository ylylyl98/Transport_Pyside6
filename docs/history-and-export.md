# History and exports

These instructions use the default Qt Quick frontend. Widgets-specific export behavior is described separately below. For acquisition and safety, see the [Measurement guide](measurement-guide.md); for launch options, see [Setup and launch](user-setup.md).

## Open and browse history

Select **History** to open the independent **Transport — Measurement History (Qt Quick)** window. It defaults to the current operator/device directory, searches its date/type subfolders, and shows **All dates**, newest first. Vds Sweep, Gate Scan, 2D Map, B-field Gate Scan, B-field Sweep and Photocurrent each have their own history page; selections do not mix across types. Use **Browse folder…** for another directory, or **Follow current device** to resume following Sample & files. Date filters and search remain available.

The full-height file list is on the left, with time, filename, conditions and status shown on separate lines. Drag the divider to resize it, or click **Expand list** to use the whole content area. **Compare files** restores the split layout without losing checks or plots. Check files to overlay curves. Choose X, signal and direction; forward/unknown traces are solid and backward traces are dashed. Condition blocks and snake rows stay separate, source order is preserved, and units must match. Selections and display choices are remembered per folder and measurement type. The plot toolbar supports zoom and pan; use **Export PNG + CSV…** above the plots to save the displayed view.

Long filenames wrap at underscores, with character wrapping for oversized components, and row heights follow the list width. When an existing CSV grows, background refresh updates its data without resetting the list's current row or scroll position. Unchanged refreshes do not rewrite saved view preferences. Use **Reload analysis** to apply analysis UI changes while Transport remains open.

Background checks wait four seconds after the previous scan finishes. Unchanged checks keep the current view and export controls available; changes to unselected files update the file list without rebuilding selected curves. Search text appears immediately and filters the list after a 150 ms typing pause. **Refresh** / **F5** still requests an immediate scan.

Closing the history window leaves measurements running. Select **History** again to reopen it. From the project root, the default Quick viewer can also run independently:

```powershell
.\.venv\Scripts\python.exe transport_UI.py --history --folder "D:\path\to\measurements"
```

For an extracted EXE release, open PowerShell in the extracted `Transport` folder and run `.\Transport.exe --history --folder "D:\path\to\measurements"`. The older `python transport_history_viewer.py --folder <measurement-directory>` command opens the retained Widgets viewer; it is not the default Quick history entry.

## Comparison, normalization and Drag/Drive ratios

**Normalize** switches between **Raw**, **Max |Y|** (`Y / max(abs(Y))`, retaining signs) and **0–1** (`(Y - min(Y)) / (max(Y) - min(Y))`). Each displayed direction/condition trace is scaled independently using its finite plotted points across the entire trace, regardless of zoom. Zero traces remain zero; constant traces become zero in 0–1 mode; missing points remain gaps. For 2D maps, **Cut scale** affects comparison cuts while the heatmap retains physical units. Normalization runs in the analysis read worker, preserves original CSV/cache values, and is remembered per measurement type and folder, including analysis reload and PNG view export/restore. Normalized axes are dimensionless and labeled with the method. Automatic measurement PNGs retain physical values.

Comparison legends combine differences in the saved measurement name and JSON settings, including lock-in phase, output amplitude, frequency, sensitivity, preamp gain, separate Drag/Drive settings and scan parameters such as sample count or delay. Shared name fragments are omitted; for example, names that differ only in `ACDCMoTe2E1` / `ACDCMoTe2E2` show those fragments. Without a saved base name, the CSV name is used after removing its acquisition timestamp. Output paths and filename bookkeeping are excluded. Shared conditions appear once in the PNG header. Only runs whose names and saved settings cannot distinguish them use `Run 1`, `Run 2`, etc. Missing metadata is shown as unknown when compared with a recorded setting. Curves use lines with point markers in both the window and PNG exports.

The **Signal** selector includes **Drag ratio X**, **Drag ratio Y** and **Drag ratio R**: saved `I_drag_X / I_drive_X`, `I_drag_Y / I_drive_Y` and `I_drag_R / I_drive_R`. These dimensionless, point-by-point ratios work with curves, maps, cuts and normalization. If R columns are absent, magnitudes are calculated from the saved X/Y components. Zero Drive, missing or nonfinite results remain gaps with a warning. Ratios are calculated in the analysis worker and never written into the original measurement CSV.

## Maps and measured cuts

For **2D Map**, choose the map's **X axis** and **Y axis** coordinates. **Map source** selects the checked map displayed on the left; **Fixed axis** and **Value** produce cuts from all checked compatible maps on the right. Only exact measured coordinates are used. **All rows** is the default: native `PassIndex` is a slow-axis row number. A specific row/pass in **Measured cut**, or a direction can isolate rows or resolve repeated coordinates. Duplicate points are never averaged, missing cells remain blank, and a file without the requested coordinate/channel is reported separately. Use **None (1D)** for a one-axis scan saved by 2D Map.

On narrow layouts, **Heatmap** and **Measured cut** tabs replace the side-by-side plots. A missing exact cut does not prevent exporting the available heatmap.

### Axis and color ranges

In Quick, **Map source** selects the original measurement file shown in the heatmap. **Axis / color ranges…** opens independent **Auto** or **Fixed** controls for Map X, Map Y and Signal. The Widgets viewer calls the source selector **Source CSV** and places the range controls beside its X, Y and Signal selectors. Auto X/Y shows the full grid; pan/zoom makes that axis fixed. Auto color follows finite Z samples whose measured X/Y coordinates lie inside the current view, including reversed axes. Empty/all-missing views retain the previous color bounds; constant values receive a small display interval. Fixed color bounds stay fixed during zooming; changing Signal resets color to Auto while retaining X/Y ranges. Enter bounds in the displayed units (scientific notation is accepted), then press Enter or leave the field to apply. Background redraws preserve unfinished edits. In the Widgets controls, automatic values are shown compactly, with full precision available in their tooltips. The Quick dialog shows editable bounds in Fixed mode and preserves in-progress edits during background updates. Settings survive reopening and Restore view. PNG export resolves the same visible-area color limits; CSV always retains the full selected grid/trace. Widgets automatic export reuses that CSV when only these display settings change.

## Automatic measurement PNGs

**View → Save channel PNGs after measurements** is a persistent option in the Quick main window, enabled by default. In the Widgets frontend, the same preference is **Automatically save channel PNGs after measurements** on **Curve Compare**. After the acquisition thread or B-field controller completes its cleanup and closes storage, an independent below-normal-priority renderer saves each available `Ids_DC`, `Ids_X`, `Ids_Y` and `Ids_Keithley` channel separately. `Keithley_current` is recognized as the Keithley alias. Empty/non-finite channels produce no image. Each CSV receives `<csv-stem>_<channel>.png` in its measurement directory's `plots` subfolder. Photocurrent condition files and B-field series files use their saved run metadata/manifest; 1D maps use the saved resolved X axis. True 2D grids are not automatically flattened into lines.

When Drag/Drive channels are available, the renderer also supports their saved channels and a 2×2 Drag/Drive overview. See [Drag/Drive settings](drag-drive.md). Four-channel live-view snapshots are **2400 × 1800 pixels**; single-panel analysis images use the format below.

## Export the displayed Quick view

Click **Export PNG + CSV…** above the plots. A curve opens the save dialog directly. For a map, choose **Heatmap PNG + CSV…**, **Measured cut PNG + CSV…**, or **Heatmap + cut PNG + CSV…**, then choose the PNG destination in the save dialog. Cut choices are enabled only when a measured cut is available.

The dialog suggests a name based on the single source filename, or the measurement type for a comparison, and remembers the last export folder. Quick uses the selected destination. Exporting both a map and a cut produces separate `_heatmap` and `_cut` PNG/CSV pairs plus a `_view.json` sidecar. Existing files are preserved; a conflicting chosen name receives `_02`, `_03`, and subsequent suffixes. This explicit save path does not use the Widgets automatic-folder reuse policy described below.

A corresponding `_view.json` records source paths/fingerprints, metadata, display settings and output associations. **Restore view…** reloads those original CSVs and settings; changed-source fingerprints are reported. Exports use the arrays already displayed, so a file being appended or edited cannot silently change an in-flight export. Export actions wait for pending selection/axis changes to finish.

### CSV format and Origin import

CSVs contain exactly three label rows: **Long Name**, **Units**, **Comments**, followed by numeric data. Comments contain the complete source filename/path, acquisition time, saved conditions, selection and normalization. Heatmaps are Y-by-X matrices: the first column holds Y, and the remaining Long Names hold numeric X coordinates. Signal values retain their physical units, full floating-point precision and missing values. Cuts use X/Y column pairs (one pair per compared trace, without aligning unequal grids). All selected samples are exported; plot zoom and colormap affect the PNG only. In Origin, map rows 1/2/3 to Long Name/Units/Comments if auto-detection does not do so, then plot a virtual matrix with X from Long Names and Y from the first column. No Origin scripts or additional data copies are generated.

The CSV describes the selected displayed analysis, including any requested curve/cut normalization. A map heatmap retains its physical signal units. This is separate from the original measurement CSV, which is never rewritten by the viewer.

### Image format

Each single-panel analysis PNG is **1200 × 900 pixels**, **150 dpi**, white and opaque, with compact margins and inward ticks on all sides. Heatmaps have no grid or cut guide; line plots use a light major grid. Axis labels use **22 pt**, ticks **20 pt**, and headers/legends **16 pt**; unusually long metadata or crowded legends shrink to fit. Currents use engineering units consistently across a comparison. Measured saved conditions override dormant recipe defaults; unknown fields are omitted. Analysis headers contain common fixed conditions, with differing/varying conditions in curve legends. Stopped or failed data are labeled Partial.

## Widgets automatic export naming and reuse

In the retained Widgets history window, **Export PNG + CSV** automatically saves beside the source CSV, in a folder named from its stem. New folders shorten `Doping` to `D`, `DragDrive` to `DD`, and `ACout50mV_VacEst11mV` to `AC50mV-est11mV`, preserving values, units, timestamp and sensitivity settings. Existing full-name folders continue to be reused. If two sources abbreviate to the same folder name, the second retains its full name. The menu offers **Heatmap only**, **Cut only**, or **Heatmap + cut**, each with separate PNG and CSV files. PNG names use the same abbreviations and omit the timestamp and LIA/Pre sensitivity fields. If repeating the conditions in the PNG filename would exceed the 259-character absolute path budget, its name contains only the signal, plot type and actual cut coordinate (for example `I_drag_Y_heatmap.png`), inside the same source folder. Source identity and conditions remain in the folder, visible header, PNG metadata and accompanying CSV/view. PNG, CSV, JSON and their version suffixes are checked against this budget; application-facing paths omit filesystem-only `\\?\` prefixes. Short CSV names identify the signal, plot type and actual cut coordinate. No files are silently moved to another export root. **Save as…** is available in the menu or by right-clicking the export button.

Unchanged automatic PNGs and CSVs are reused independently; changing a cut does not duplicate its unchanged heatmap, and changing only the colormap/zoom does not duplicate numerical CSV data. Changed artifacts get readable `_v02`, `_v03` suffixes. Existing or externally edited files are preserved.

These source-adjacent naming, abbreviation, reuse and `_v02` rules apply to the Widgets automatic export action. Choosing **Save as…** uses an explicit output path, like the Quick save dialog.

## Process isolation and reload

History discovery, CSV loading and native Quick plotting run in a separate Qt process (Python in a source checkout, the bundled worker in an EXE release) with lower CPU priority. The measurement app only sends directory, refresh, focus and close notifications; the viewer imports no instrument/control modules and never modifies measurement CSVs or metadata. Header indexing is cached; complete CSVs are loaded only when checked, with a bounded 64 MiB numeric cache. Visible history refreshes periodically and when measurements finish. This separates GUI work and failures from acquisition; CPU, RAM and disk remain shared resources. Large datasets can still contend for disk bandwidth, so this is not a hard real-time guarantee.

PNG rendering owns no instruments, runs in a serial detached process and never modifies source CSVs or measurement JSON. It does not block acquisition or the next batch run. Accepted jobs drain after a normal app close; the viewer hands its immutable snapshot to disk before Transport closes it. Rendering failures are reported separately from measurement state. CPU, RAM and disk are still shared with acquisition. The Widgets history plot-toolbar save action also uses automatic PNG + CSV export and creates a reproducible view sidecar. Quick uses the explicit export button above the plots.

**Reload analysis** in the View menu (or **Reload analysis** in its history window) reloads analysis and PNG-rendering Python code while Transport stays open. Accepted exports finish first; run exports submitted during reload are durably queued for the replacement renderer. The reopened viewer restores its folder/follow-current choice, checked files, measurement tab, filters, primary map, cuts, zoom, color scale, splitter and window geometry. If the current device changes while a following viewer reloads, it follows the new device rather than transplanting the previous device's view. A closed viewer stays closed. Reload errors are shown separately from measurement state. Main-window controls, acquisition/controller code and dependency/environment updates still require a full app restart.

In a source checkout, replacing analysis Python files before reloading lets the new viewer and renderer import those changes. An EXE release restarts its bundled analysis code; installing a new EXE version requires a normal application restart while idle.

[Documentation index](README.md) · [Project overview](../README.md)
