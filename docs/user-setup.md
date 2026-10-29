# Installation and first-time setup

[中文](user-setup.zh-CN.md) · [Project overview](../README.md)

Use the Windows EXE for everyday operation and saved-data analysis. Use the source
launcher when you need to develop or modify Transport. Both open the current
Qt Quick interface by default.

## 1. Download and install on Windows

For everyday use, open the [latest release](https://github.com/ylylyl98/Transport_Pyside6/releases/latest)
and download **`Transport-Setup-windows-x64.exe`** and `SHA256SUMS.txt` from Assets.
Check the installer in PowerShell and compare its matching checksum entry:

```powershell
Get-FileHash .\Transport-Setup-windows-x64.exe -Algorithm SHA256
```

Run the installer for your current Windows account; no administrator access is
required. The default folder is `%LOCALAPPDATA%\Programs\Transport Measurement`.
It creates a Start menu shortcut and offers an optional desktop shortcut. Launch
**Transport Measurement** from the Start menu. The installer does not install
instrument drivers or vendor SDKs; prepare those separately as described below.

The release is currently unsigned. Windows may show an unknown-publisher or
SmartScreen warning. Check the repository, release version, and checksum before
making a trust decision. If the warning is unexpected or you cannot verify the
file, stop and contact the maintainer. Do not disable Windows security checks.

### Portable ZIP alternative

1. Open the [latest release](https://github.com/ylylyl98/Transport_Pyside6/releases/latest)
   and select a version. Under **Assets**, download `Transport-windows-x64.zip`
   and `SHA256SUMS.txt`. The automatically generated **Source code** archives do
   not contain the packaged application.
2. Optionally check the downloaded ZIP in PowerShell:

   ```powershell
   Get-FileHash .\Transport-windows-x64.zip -Algorithm SHA256
   ```

   Compare the hash with the ZIP's entry in `SHA256SUMS.txt`.
3. Extract the **entire** ZIP to a local folder on a Windows x64 computer.
   Open `Transport/Transport.exe` inside the extracted folder. Keep
   `TransportWorker.exe`, `_internal`, and all other bundled files together;
   do not run inside the ZIP or copy only the main EXE.
4. For saved measurements, use **History** in the main window, or the independent
   history command below. Viewing saved data does not require connecting instruments.

The EXE includes its Python runtime and application libraries. You do not need
to install Python, PySide6, Matplotlib, or the Python packages in
`requirements.txt` separately. Hardware drivers and the optional attoDRY2100 SDK
are separate installations, as described below.

### Updates and uninstallation

Transport checks the official GitHub **latest** release in the background about
10 seconds after startup and every 6 hours while running. In Qt Quick, use
**View → Check for updates** for a manual check, and
**View → Check for updates automatically** to turn automatic checks off or on.
In Widgets, use **Tools → Check for updates...** and
**Tools → Automatically check for updates** instead.
A newer version appears in a Qt Quick top banner or the Widgets status bar with
a download link; **Dismiss** hides
it for the current session. Checks do not interrupt measurements with a dialog.
They run only while the application is open; closing Transport stops checks and
there are no system push notifications. Older releases without this module need
one manual update before they can show future release reminders.

Use the banner's official Release download link and follow the idle update steps
below. The application does not automatically download, install, or restart.

Before updating or uninstalling, finish measurements and instrument cleanup, then
close Transport normally, including history windows and background workers. Do not
replace application files while any Transport process is running. Back up the
settings and measurement data described in section 5.

For the installed version, run the new installer using the same installation
folder. Remove it through Windows **Settings → Apps → Installed apps → Transport
Measurement → Uninstall**. Uninstallation removes installed application files and
shortcuts, preserving AppData settings, measurement data, and unknown files.
Updates overlay the existing bundle; files retired by a later release remain
until uninstall. For the portable
version, extract updates to a separate folder and retain the previous complete
bundle until the new version is checked; remove the portable folder only after
closing the application. Keep measurements outside either application folder.
Installer, portable, and source versions share per-user settings under the same
Windows account. `SHA256SUMS.txt` covers both installer and portable ZIP.

## 2. What to install yourself

Install only the rows required by your hardware and intended use. Select driver
versions that support your exact hardware model and Windows version on the
manufacturer's download page.

| Use | Additional installation | Recognition check before connecting in Transport |
| --- | --- | --- |
| Open the EXE or view saved measurements | None of the instrument drivers below; no separate Python installation | Open the application or a saved-data folder |
| Acquire with an NI DAQ | [NI-DAQmx](https://www.ni.com/en/support/downloads/drivers/download.ni-daq-mx.html) | Find the device in NI MAX and note its device name, such as `Dev1` |
| Use VISA instruments: Keithley, SRS lock-ins, serial monochromator, APS100, or LS335 | A compatible VISA implementation, such as [NI-VISA](https://www.ni.com/en/support/downloads/drivers/download.ni-visa.html) | List the corresponding GPIB/ASRL resources in NI MAX or Transport's **Scan resources** |
| Use an NI GPIB interface | [NI-488.2](https://www.ni.com/en/support/downloads/drivers/download.ni-488-2.html), together with the VISA installation | Confirm the NI interface and its GPIB bus number in NI MAX |
| Use a USB-to-serial adapter or vendor USB interface | The driver supplied by that adapter/instrument manufacturer, if Windows has not installed it | Check the actual device and COM number in Windows Device Manager; resolve driver errors first |
| Use attoDRY2100 through the SDK backend | The matching vendor Python SDK and any prerequisites supplied by [attocube](https://www.attocube.com/en/contact) | Check the local SDK folder and configured host; see the dedicated setup below |
| Run or modify the source | [Python 3.13.9](https://www.python.org/downloads/release/python-3139/) **Windows installer (64-bit)**; [Git for Windows](https://git-scm.com/install/windows) is optional | Confirm `python --version`; the BAT creates the local environment and installs application packages |
| Build an EXE locally | Source dependencies plus [requirements-build.txt](../requirements-build.txt) | Use the separate build environment described below |

The Python packages `nidaqmx` and `pyvisa` do not replace the system DAQ/VISA
drivers. NI-488.2 is specific to NI GPIB hardware; other GPIB vendors require
their own supported driver stack. The normal Transport workflow does not use
LightField and does not require a separate LightField/.NET installation merely
because `pythonnet` appears in the Python requirements. Follow any additional
requirements documented for your particular vendor SDK.

Follow each manufacturer's installation order, including a restart if requested.
For initial recognition, inspect device/resource lists only. Do not use output
test panels, resets, or configuration commands as a discovery check.
**Scan resources** enumerates GPIB, serial VISA, and NI DAQ resources; an empty
list can also mean a missing driver or VISA backend. **Connect all** performs real
device initialization and can change source settings, so review the next section
before using it.

## 3. Configure the first session

Open **Instruments** in the top bar. The selector inside that panel contains the
pages named below. Existing settings may have been retained from an earlier EXE
or source installation under the same Windows account.

1. **Sample & files:** set **Operator**, **Device / sample**, and **Data folder**.
   Use **Browse…** to select a writable local data directory, for example
   `C:\Measurements`. Files are grouped under `Data folder / Operator / Device`.
   Operator and device names are sanitized for folder names; use simple names
   containing letters, numbers, `-`, `_`, or `.`. Replace an old or example `D:`
   path if that drive is unavailable on this computer.
2. **Connections & protection:** use **Scan resources**, then enter the actual
   VISA address for each instrument and the NI DAQ device name. A resource such
   as `GPIB0::23::INSTR` or `ASRL6::INSTR` is an example, not a universal default.
   Check the GPIB interface number as well as the instrument address. Clear
   addresses for unused instruments rather than keeping unrelated lab defaults.
3. Review each source's **Mode**, **Maximum source voltage (V)**, and
   **Current compliance (A)** against the sample and wiring before **Connect all**.
   After connection, inspect the status and protection readbacks. Use
   **Apply G1/G2/G3 protection** when a profile needs applying, and reconnect
   after changing an address or source mode. Do not assume a configured limit is
   already verified on the instrument.
4. Check DAQ wiring against the acquisition channel map. Ordinary acquisition
   uses AI0–AI3; enabling Drag / Drive adds AI4 and requires reconnecting the DAQ.
   When choosing **NI DAQ AO** as the Vds source in a measurement form, select
   the AO channel actually wired to the sample and check its output range.
   The input map is not an arbitrary channel picker; see the
   [Drag / Drive wiring guide](drag-drive.md) for the channel assignments.
5. **Signal chain** and **Drag / primary lock-in:** review preamp sensitivity,
   lock-in calibration, reference settings, and any voltage ratio used for sample
   AC estimates. Enable **Use Drive lock-in for Drag / Drive** only when that
   second lock-in and its wiring are present. See
   [lock-in control](lockin-control.md) and [Drag / Drive](drag-drive.md).
6. If using a magnet or temperature controller, complete its configuration below
   first. In **Magnet & temperature**, select the correct system, read
   **Review commissioning details**, and acknowledge
   **I reviewed the configured limits and connections** only after checking the
   real installation. This review is required before connection/control.
7. Review the chosen measurement's trajectory, ranges, source selection, timing,
   and output location before starting. **View → Save channel PNGs after
   measurements** controls automatic image export. **History** opens saved data
   for the configured sample.

Cloud tests validate software with simulated devices. They do not establish the
safe limits or wiring of a new installation. Commission real output, protection,
stop/zero, magnet, and temperature behavior in the laboratory before measurement.

## 4. Magnet, temperature, and attoDRY2100 configuration

Hardware connection and commissioning values live in the shared JSON configuration
described in the next section. The Quick **Runtime settings** page does not edit
SDK paths or these connection parameters. Close Transport normally before editing
the JSON, preserve unrelated keys, and restart to load changes.

| System | Configuration to review |
| --- | --- |
| attoDRY1000 / APS100 | `magnet.visa_resource`, coil constant, field/current/rate limits, and heater timing against the commissioned magnet |
| Lake Shore 335 | `lakeshore335.visa_resource`, sample/reservoir channel assignment, temperature limits, heater range, and thermal interlock settings |
| attoDRY2100 / SDK | `attodry2100.sdk_directory`, `host`, `channel`, field and temperature limits |

For APS100/LS335 commissioning and sample-temperature operation, also read
[Sample temperature on attoDRY1000](guides/SAMPLE_TEMPERATURE_1000.md).

For attoDRY2100:

1. Obtain the SDK appropriate to your controller/firmware from attocube. The
   application expects a Python package exposing `Device`; keep the vendor SDK
   distribution intact. Do not substitute an unrelated attoDRY DLL wrapper.
2. Set `attodry2100.sdk_directory` to a folder containing
   `atto_device/__init__.py`, or to the `atto_device` package folder itself.
   The built-in example `D:\Insturment control v3\CRYO2100` refers to another
   computer and must be replaced. For example, merge this path into the existing
   top-level `attodry2100` object:

   ```json
   {
     "attodry2100": {
       "sdk_directory": "C:\\InstrumentSDKs\\CRYO2100"
     }
   }
   ```

   This is a path example, not a complete commissioning configuration. If the
   configuration file does not exist, create its parent folder and a valid JSON
   object, then add the settings required for your installation. Set the actual
   host and channel and review all limits; do not copy another system's limits.
3. Restart Transport, select **attoDRY2100 / SDK**, and review the displayed
   connection/limit details before connecting. SDK temperature controls depend
   on the capabilities available from that SDK and device.

The attoDRY2100 backend has offline coverage but has **not been accepted on real
hardware**. Compatibility of a particular vendor SDK and its extra dependencies
with the frozen EXE still requires verification. Installing packages into a
separate system Python does not add them to the EXE's bundled runtime. Report
the SDK version and exact load error if additional packaging support is needed.

## 5. Settings, data, and backup

The default Windows locations are:

| Item | Location / contents |
| --- | --- |
| GUI settings | `%APPDATA%\MyLab\TransportUI.ini`: addresses, source profiles, data folder, signal-chain settings, layout, and history preferences |
| Shared configuration | `%APPDATA%\SpectralSweep\config.json`: magnet/temperature configuration, SDK path, and runtime settings |
| Measurements and analysis files | The **Data folder** selected in **Sample & files**, including its operator/sample subfolders |

Paste an `%APPDATA%` path into File Explorer to open it. These are per-user
settings shared by the EXE and source application; they are not stored alongside
each downloaded EXE. The JSON path can be overridden with
`SPECTRALSWEEP_CONFIG_PATH`; that override does not relocate the INI file. An older
project-root `config.json` can be read when the per-user JSON is absent; subsequent
saves use the per-user location.

Before an update, migration, or manual edit, close the application normally and
back up both settings files if present, plus the **complete measurement directory**
with its CSV, metadata, logs, images, and analysis sidecars. To restore, close the
application and copy the saved files back to their corresponding locations.
On a different computer, recheck addresses, drive letters, SDK paths, wiring,
and protection limits before connecting. A software rollback retains these
settings; keep a matching settings backup when evaluating a new version.

## 6. Run from source, use the fallback, or open history separately

Install the standard 64-bit [Python 3.13.9 Windows installer](https://www.python.org/downloads/release/python-3139/)
used by the release build, with Python available on `PATH`. The launcher accepts
Python 3.12–3.14; 3.13.9 is the project's pinned validation/build version.
Download the repository's **Code → Download ZIP** and extract it, or clone it with
optional Git:

```powershell
git clone https://github.com/ylylyl98/Transport_Pyside6.git
cd Transport_Pyside6
python --version
.\Transport_App.bat
```

For a downloaded source ZIP, open the extracted project root and double-click
`Transport_App.bat`. The root BAT creates `.venv`, installs `requirements.txt`
when needed, and checks startup; the first run needs access to the package index.
Use the root launcher, not a migration copy or historical backup.

For installed-app commands below, first run
`Set-Location "$env:LOCALAPPDATA\Programs\Transport Measurement"` in PowerShell
(or enter your chosen installation folder). Portable commands run in the extracted
`Transport` folder; source commands run from the checkout.

After closing the current interface normally, use the retained Widgets frontend:

```powershell
# From the installation folder or extracted portable Transport folder
.\Transport.exe --widgets

# From the source project root
.\Transport_App.bat --widgets
```

To open the independent Qt Quick history viewer, point it at an existing data
folder; no instrument connection is needed:

```powershell
# From the installation folder or extracted portable Transport folder
.\Transport.exe --history --folder "C:\Measurements"

# From the source project root, after the BAT has prepared .venv
.\.venv\Scripts\python.exe -B transport_UI.py --history --folder "C:\Measurements"
```

Local EXE builds are optional. From a Git checkout, using the validated Python,
keep build tools in a separate environment:

```powershell
python -m venv tmp\build-venv
.\tmp\build-venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-build.txt
.\tmp\build-venv\Scripts\python.exe -B tools\build_windows.py
.\tmp\build-venv\Scripts\python.exe -B tools\verify_frozen.py --bundle dist\Transport --output tmp\frozen-smoke
```

The ZIP is written under `dist`. For automated tests, build verification, and
release publication, see [GitHub release instructions](github-release.md).
To build the installer too, run `& .\tools\Install-InnoSetup.ps1` to download,
verify, and install the pinned Inno Setup 7.1.1 x64 compiler for the current user,
then use `tools/build_installer.py` and `tools/verify_installer.py` as documented there.
Inno Setup is only needed for packaging, not for using the application.
For frontend fallback details, see [Qt Quick migration](qt-quick-migration.md).

## 7. Common problems

| Symptom | What to check |
| --- | --- |
| EXE fails after being copied or extracted | Extract the complete release asset again into a new folder; keep both EXEs and `_internal`. Check whether security software quarantined a bundled file and retain the exact error message |
| No VISA resources / missing VISA backend | Install the appropriate VISA/interface drivers, confirm the resource in the vendor utility, and check the actual GPIB bus or COM number. Installing `pyvisa` alone is insufficient |
| NI DAQ missing or named differently | Confirm NI-DAQmx and the device name in NI MAX; update **NI DAQ device**. Verify that the required AI/AO channels and AO readback facilities exist on the hardware |
| Cannot save or the folder does not exist | Choose a writable **Data folder** on this computer; check the drive and available space, then review Operator and Device / sample |
| Protection, source mode, or readiness blocks Start | Read the reported reason, confirm the configured source profile and readbacks, and reconnect/apply the reviewed configuration as appropriate; do not raise limits merely to dismiss the error |
| SDK directory/package not found | Correct the per-user JSON path, verify the `atto_device` package layout, and restart. Keep the SDK version and complete error when reporting a dependency issue |
| An update seems to retain old settings | EXE and source share per-user settings. Inspect the INI/JSON paths and any `SPECTRALSWEEP_CONFIG_PATH` override; back up before changing settings |
| The BAT cannot find Python or install packages | Check `python --version`, the 64-bit installation and `PATH`, plus package-index/network access. Run the root BAT from a terminal to retain its error output |

When reporting an issue, include the release tag or `build-info.json`, Windows
version, relevant driver/SDK version, and the exact message. Include the instrument
model and interface when the problem concerns hardware; remove private data from
logs before sharing.
