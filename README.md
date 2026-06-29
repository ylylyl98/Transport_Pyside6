# Transport Measurement

Desktop application for automated electrical transport and photocurrent measurements. The PyQt6 interface coordinates Keithley 2400 source meters, an NI DAQ device, and an SP-2300 monochromator; it provides live plots while saving each measurement to CSV.

> **Laboratory software:** This program can change instrument outputs. Verify cable routing, instrument limits, compliance settings, and the selected hardware addresses before every run. Software safeguards are helpful, but they are not a replacement for laboratory safety procedures or hardware interlocks.

## Measurement modes

| Tab | Purpose |
| --- | --- |
| **Vds Sweep** | Sweep drain-source bias while holding top- and back-gate biases. Vds can come from Keithley G3 or an NI-DAQ analog-output channel. |
| **Gate Scan** | Run a one-dimensional raw-voltage trajectory or a derived doping/electric-field trajectory, with an optional reverse pass. |
| **2D Map** | Acquire a 1D or 2D grid across `Vtg`, `Vbg`, and/or `Vds`; select fast and slow axes and preview the planned sweep. |
| **Photocurrent** | Sweep monochromator wavelength for one or more enabled Vtg/Vbg recipe conditions, with optional per-condition Vds. |

Across the modes, the application averages DAQ readings, plots the selected current channel live, and writes the acquired points to CSV as the run proceeds. The plot can switch between a single selected channel and a four-channel comparison view.

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

The Python dependencies are listed in [`requirements.txt`](requirements.txt): PyQt6, Matplotlib, NumPy, PyVISA, NI-DAQmx, and pythonnet.

## Launch

With the environment activated:

```powershell
python transport_UI.py
```

`main.py` is an equivalent direct entry point. On Windows, double-clicking [`Transport_App.bat`](Transport_App.bat) creates and reuses a project-local `.venv`, installs any missing requirements there, validates the application imports, and launches the UI. Python must be available on `PATH` for the first launch.

## Typical measurement workflow

1. Start the application and open **Instrument Setup** in the left dock.
2. Select the GPIB/serial/DAQ addresses and the operating mode for each Keithley. Use **Scan Hardware** to populate detected resources.
3. Set the save location, operator name, device ID, amplifier gain, and lock-in gain as appropriate for the experiment.
4. Click **Connect All** and confirm that the required instruments report an OK status.
5. Use **Manual Controls** in Instrument Setup when needed: set G1, G2, or G3 individually, safely ramp any gate back to 0 V, or move the monochromator to a wavelength. Gate controls are available only in 2-wire voltage-source mode and while no measurement is active.
6. Select a measurement tab, set its sweep bounds, timing, averaging, source, and file name, then review any preview or estimated sweep information. The Photocurrent tab supports an editable bias recipe: unchecked rows are retained but skipped, every enabled condition creates its own CSV file, and Vds values are available only when a compatible Vds source is connected and explicitly enabled.
7. Start the measurement and monitor the live plot and status messages. Use the run-level **STOP** or dock-level **STOP / ZERO ALL** if needed.
8. Review the resulting CSV in the selected save directory.

The application uses dock-managed connections: address or Keithley-mode changes require reconnection before they apply to a measurement.

## Data output

Files are written under:

```text
<save base>/<user>/<device ID>/
```

The default base directory is `D:\photocurrent\data`; it is configurable in Instrument Setup. CSV files include the applied biases, raw DAQ channels, converted current channels, and—when Vds is driven by Keithley—the measured Keithley current. Gate scans additionally include derived `Doping`, `Efield`, and sweep `Direction` columns; photocurrent scans include `Wavelength`.

CSV writes are flushed during acquisition, which helps preserve data already collected if a run is interrupted.

## Safety behavior

- The UI limits requested bias values to ±20 V.
- Manual gate moves read the Keithley's present programmed source level, then ramp rather than stepping abruptly. The per-gate **Zero** controls use the stricter safe-ramp step to return that source to 0 V.
- On normal completion, stop, or disconnect, active outputs are ramped back to 0 V while sessions remain open where possible.
- **STOP / ZERO ALL** requests all workers to stop, ramps connected Keithley outputs to 0 V, and zeros the selected DAQ Vds channels.

Always confirm actual instrument state independently after an error, interrupted connection, or emergency stop.

## Project layout

```text
app/                    PyQt6 UI, application models, workers, and device manager
app/ui/tabs/            Measurement tabs: Vds Sweep, Gate Scan, 2D Map, Photocurrent
app/workers/            Background acquisition and CSV-writing workers
instruments/            Keithley, NI-DAQ, monochromator, and other instrument drivers
transport_UI.py         Primary GUI entry point
Transport_App.bat       Windows launcher
requirements.txt        Python dependencies
```

## Development notes

The app saves user configuration and plot-mode preferences through Qt settings. Before modifying an instrument driver or measurement worker, test against a controlled setup or suitable hardware simulation—never a device whose limits have not been confirmed.

## License

No license file is currently included in this repository. Do not assume permission to redistribute or reuse the code until a license is added by the project owner.
