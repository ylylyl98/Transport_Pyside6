# Drag / Drive with two lock-ins

Choose **Signal chain → Measurement mode** explicitly. A new configuration starts
in **Ordinary (one lock-in)**, which uses the original X/Y/DC conversion and does
not require or read the Drive lock-in. The selection is remembered and shown in
the main Instruments summary. Connecting two instruments does not select a mode.

For Drag/Drive, choose **Drag / Drive (two lock-ins)**, set **Drive Rs** using the
numeric field and **Ω / kΩ / MΩ** selector (initial value **100 kΩ**), then use **Devices →
Connect All**. Reconnecting registers AI4 in the DAQ task. The original GPIB
interface is retained: Drag defaults to `GPIB1::08::INSTR`, Drive to
`GPIB1::09::INSTR`. Change the interface if hardware discovery reports another one.
Each instrument has its own controls under **Instruments → Lock-in →
Drag / Primary** or **Drive**; all existing measurement tabs are reused.

In Drag / Drive mode, **Signal chain** shows separate read-only **Drag sensitivity**
and **Drive sensitivity** values, with each connected address and readback status.
Values use readable voltage units (for example, 50 mV and 100 mV). Each **Refresh**
button runs that instrument's existing background panel refresh. Refresh, Apply,
and Auto readbacks update only the corresponding row; pending edits do not.
After a disconnect, reconnect, or failed read, `--` and an explicit status replace
the unverified value. Ordinary mode retains the original single-instrument field.
These rows display instrument readbacks; run-start calibration still independently
reads and freezes both sensitivities for current conversion.

Changing the Rs display unit converts the numeric value without changing the
actual resistance: **100 kΩ = 100000 Ω = 0.1 MΩ**. The last resistance and display
unit are remembered; existing saved resistance settings still load in kΩ.
Calculations and metadata continue to use Ω. All units retain the existing
0.001 Ω resolution and 0.001 Ω–10¹² Ω input range. Both the value and unit controls
are locked during measurement.

| DAQ input | Signal |
| --- | --- |
| AI0 | Drag lock-in CH1 / X |
| AI1 | Drag lock-in CH2 / Y |
| AI2 | Existing DC signal |
| AI3 | Drive lock-in CH1 / X |
| AI4 | Drive lock-in CH2 / Y |

The Drag branch uses the manually configured SR570 sensitivity. The Drive branch
uses SR551 voltage gain **10**, with both SR551 A/B outputs connected to Drive
lock-in A/B and **A-B** selected. SR551 presence and physical cables cannot be
detected through the lock-in: its gain and presence are user configuration.
This conversion assumes the SR551 measures the voltage across Rs.

Set Drive to **External reference** using the common reference from Drag. Both
lock-ins must report matching frequency and harmonic, CH1/CH2 X/Y routing,
zero X/Y offset, and unity expansion. The app reads these settings and each
instrument's own Sensitivity before starting. It rejects a missing instrument,
duplicate addresses, stale connection, missing AI4, or unsupported output
configuration. No phase, reference, or output settings are changed automatically.

For each average the app makes one DAQ acquisition, then obtains AI0–AI4 from
that reading. This gives corresponding channels at each scan point; it does not
make a multiplexed DAQ into simultaneous-sampling hardware.

With zero offset and unity expansion, let Sdrag and Sdrive be the respective
lock-in input sensitivities in volts, and G570 the preamp gain in V/A:

```
I_drag_X/Y  = V_AI0/1 * Sdrag / (10 * G570)
I_drive_X/Y = V_AI3/4 * Sdrive / (10 * 10 * Rs_ohm)
Ids_DC      = V_AI2 / G570
I_drag_R    = hypot(I_drag_X, I_drag_Y)
I_drive_R   = hypot(I_drive_X, I_drive_Y)
```

In dual mode the Y-axis menu includes Drag and Drive X/Y/R currents and the AI2
DC current (`Ids_DC`). The first use of dual mode selects **4-Channel Compare**:

| Left | Right |
| --- | --- |
| Drag X | Drive X |
| Drag Y | Drive Y |

All four panels show converted current in amperes, share the X axis, and have
independent Y axes. Choose **View → Single Plot** to view any single current,
including DC. Every measurement tab remembers its layout separately for Ordinary
and Drag/Drive modes, including across app restarts. Switching modes restores that
mode's layout; sampling and settings refreshes do not change the chosen layout.
Existing layout preferences are inherited by Ordinary mode only.

B-field Sweep uses the same selection and layout rules. CSVs append raw Drive
X/Y voltages and all six
currents; original `Ids_X/Y` remain aliases of Drag X/Y. History and applicable
PNG exports recognize the new current columns. R is the magnitude of the averaged
X/Y components. Data Analysis offers **Drag ratio X**, **Drag ratio Y** and
**Drag ratio R** in the Signal selector, computed point by point as Drag/Drive
from the saved currents. R uses saved magnitudes, or `hypot(X, Y)` when R columns
are absent. Zero Drive and missing/nonfinite inputs remain gaps. These ratios
are dimensionless, support normalization and PNG comparison exports, and do
not change acquired CSV files or the automatic measurement overview.

With **Automatically save channel PNGs after measurements** enabled (the default
in the History/Compare page), the background exporter waits for acquisition cleanup,
then reads each saved CSV and writes into its adjacent **plots/** directory:

- 1D runs: `<csv_stem>_<channel>.png` for each available current channel, plus
  `<csv_stem>_DragDrive_2x2.png` (Drag X/Y left, Drive X/Y right).
- 2D runs: `<csv_stem>_<channel>_heatmap.png`, plus
  `<csv_stem>_DragDrive_2x2_heatmap.png`, using the saved fast/slow scan coordinates.
  Each panel has its own current scale and colorbar. Serpentine rows are placed at
  their measured coordinates, and missing points remain blank.

The exported channels include Drag/Drive X/Y/R, DC from AI2, and other recorded
current channels. Existing `Ids_X/Y` alias exports are retained. Single images are
1200×900 pixels and overviews are 2400×1800 pixels, all at 150 dpi. Currents use
appropriate A/mA/µA/nA/pA units without changing the saved values. Each condition
CSV in a multi-condition run gets its own images. Stopped/failed runs export the
available data and mark the images as partial; unavailable channels are reported
and shown as blank panels in the overview.

Automatic exports do not use the displayed layout, selected Y channel, zoom, or
live plot buffers. Choosing Single Plot or viewing DC does not omit other channels.
All CSV columns and rows, including raw DAQ values, remain untouched. Duplicate
2D coordinates are reported instead of silently averaging or dropping samples.

Mode and Rs cannot change during a run. The verified snapshot freezes both
sensitivities, Rs, preamp assumptions, channel map, and both instrument identities,
addresses, and supported settings in metadata. Filenames include `DragDrive` and
Rs. Front-panel changes made directly on hardware during a scan are not tracked;
finish the scan and begin a new one after changing calibration.

Verification is offline, with simulated instruments and blocked real VISA/DAQ
access. Hardware acceptance still requires checking the actual CH1/CH2 routing
and a known resistor signal after restarting the app when idle.

References: [SR850 manual](https://www.thinksrs.com/downloads/PDFs/Manuals/SR850m.pdf),
[SR830 manual](https://www.thinksrs.com/downloads/PDFs/Manuals/SR830m.pdf),
[SR551 manual](https://www.thinksrs.com/downloads/pdfs/manuals/SR550551552m.pdf).
