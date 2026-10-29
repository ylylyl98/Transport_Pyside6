# Developer notes

The project root is the official PySide6 / Qt Quick application. The main window, instrument panels, live plots and independent history viewer use Qt Quick/QML; Python retains instrument ownership, acquisition, safety and file writing. The Widgets frontend is retained behind `--widgets`. Do not develop or launch a backup copy under `tmp/`.

Use [Setup and launch](user-setup.md) ([中文](user-setup.zh-CN.md)) for installation, [Measurement guide](measurement-guide.md) for operating instructions and safety, and [History and exports](history-and-export.md) for analysis behavior. Commands and code paths below are relative to the project root.

## Source launch, dependency checks and shortcuts

With the environment activated:

```powershell
python transport_UI.py
```

`main.py` is an equivalent direct entry point. On Windows, double-clicking [`Transport_App.bat`](../Transport_App.bat) creates and reuses a project-local `.venv`, checks the pinned `requirements.txt` locally, and installs dependencies only when needed. It runs a hardware-blocked offscreen startup check after source or environment changes, reuses a matching successful check, and launches the UI. The project shortcut also runs this launcher so dependency setup is not skipped. Python must be available on `PATH` for the first launch. An incompatible existing environment is preserved and reported; the launcher does not delete it.

The launcher also creates/repairs `Transport_App - Shortcut.lnk` in the project folder. This shortcut runs Transport_App.bat to check dependencies before starting the UI, uses the bundled `assets/transport.ico`, and shares the application's Windows taskbar identity. Use it for subsequent launches and taskbar pinning. After moving the checkout, run `Transport_App.bat` again to refresh its paths. To repair only the shortcut without starting the app, run `powershell -NoProfile -ExecutionPolicy RemoteSigned -File .\tools\Update-TransportShortcut.ps1`. This execution-policy setting applies only to that PowerShell process. If an old taskbar pin still shows a generic icon, unpin it and pin the repaired shortcut again. Restart the app when convenient to apply window-icon changes; reloading analysis applies them to the history viewer only.

The launcher reuses a successful offline startup check only when source and environment match. Set `TRANSPORT_VALIDATE_STARTUP=1` to force a fresh preflight. The startup checks block hardware and use an offscreen UI; they do not replace instrument acceptance.

## Project layout

```text
00_启动说明.txt          Quick Chinese guide to the correct launcher and folders
Transport_App.bat       Normal Windows measurement launcher
Transport_App - Shortcut.lnk  Local shortcut to the same launcher
app/                    PySide6 UI, application models, workers, and device manager
app/quick/              Qt Quick presenters, native plotting and QML views
app/ui/tabs/            Retained Widgets measurement tabs for --widgets fallback
app/workers/            Background acquisition and CSV-writing workers
instruments/            Keithley, NI-DAQ, monochromator, and other instrument drivers
transport_UI.py         Primary GUI entry point
requirements.txt        Python dependencies
tools/                  Developer preview and shortcut-maintenance utilities
docs/README.md          Documentation index
docs/guides/            Sample-temperature and thermal-protection guides
docs/reviews/           Audit notes and past failure investigations
docs/manuals/           Instrument manuals
tmp/verification/reports/   Local offline verification JSON reports
tmp/verification/previews/  Local preview PNGs and their companion JSON files
```

Source code and the root `.venv` remain in their existing locations. `tmp/` also
contains local backups and historical validation runs; its contents are not app
entry points. Newly generated files under `tmp/` are excluded from Git.
Measurement data continues to use the configured data directory, independently
of these verification folders.

## Development workflow

### Runtime settings and retained Widgets styles

**View → Runtime settings** in Quick applies the main-window font immediately and exposes B-field endpoint/recovery settings for application while measurements and instrument operations are idle. Changes are saved to the existing per-user configuration. Device addresses and Keithley modes use **Connections & protection** and individual reconnection; driver/SDK and other startup configuration changes still need a normal restart while idle. In the Widgets frontend, runtime settings are under **Tools → Runtime settings…**.

For the retained Widgets frontend, use **View → Reload UI style** (**Ctrl+Alt+R**) after editing `app/ui/app.qss`. It validates and applies the style to the existing main window; invalid or incomplete style files leave the previous appearance in place. QSS reload styles the Widgets UI. The default Quick views are QML in `app/quick/qml/`; use the isolated Quick preview for layout development.

### Isolated previews and reload boundaries

Use `tools/Transport_Preview.bat` to develop layouts in a separate,
hardware-blocked Qt Quick window with synthetic plots and isolated settings.
**View → Rebuild offline preview** (**Ctrl+Shift+R**) replaces only that preview
process and preserves its mode, panel and window size. Normal launch and
dependency repair use `Transport_App.bat`. The previous Widgets preview remains
available as `python -B transport_ui_preview.py`.

In a frozen Widgets release, **Offline UI preview (source checkout only)** is disabled. Develop layouts from the source checkout.

The Quick window remembers its size, instrument/parameter visibility and theme.
No instrument connection, output state or running measurement is restored.
Acquisition/controller changes require a normal restart while idle; analysis code
can be reloaded from View. See [Qt Quick release notes](qt-quick-migration.md)
for preview, regression and rollback commands. The older
[development workflow](development-feedback.md) describes Widgets-specific
QSS reload and remains applicable when using `--widgets`.

Analysis reload restarts the separate viewer and renderer. Source checkouts can pick up replaced analysis Python files; frozen releases restart the code already included in the bundle. See [Reload behavior](history-and-export.md#process-isolation-and-reload) for queue draining and view restoration.

### Configuration storage

Application configuration is stored in the existing per-user JSON file (on Windows, normally `%APPDATA%\SpectralSweep\config.json`; `SPECTRALSWEEP_CONFIG_PATH` can override it). UI and plot preferences use per-user Qt settings. These storage mechanisms preserve the existing configuration rather than copying instrument state into an export or release.

Before modifying an instrument driver or measurement worker, test against a controlled setup or suitable hardware simulation—never a device whose limits have not been confirmed. Offline validation cannot replace physical instrument acceptance.

## Transport timing and recovery

B-field Transport runs voltage ramps and sample acquisition on one serialized worker, and CSV/checkpoint writes on another. STOP cancels ramps between steps, pauses APS100, then waits for in-flight instrument work, bias zeroing, and output flushing before releasing devices. An unresponsive driver can still delay cleanup, but it no longer blocks Qt telemetry processing.

APS100 and Lake Shore cache completed readings in their polling workers and coalesce superseded UI updates. Temporary stale/missing monitoring requests an acknowledged APS100 pause instead of immediately ending the series. The `mcd.transport_monitor_recovery_timeout_s` default is 30 seconds; missing pause acknowledgement escalates after the communication allowance (normally 5 seconds). Recovery requires `mcd.transport_monitor_recovery_reads` (default 3) new valid pairs of readings after acknowledgement, followed by thermal permission. The commissioned thermal recovery dwell has a separate clock. Real temperature/sensor/quench/voltage faults retain their fault handling. Holds and acquisition gaps are logged, and acquisitions spanning a hold are discarded; no missing samples are fabricated.

Quick plots coalesce display notifications at 33 ms and reduce displayed vertices to the viewport while retaining every recorded sample. CSV writes remain ordered and synchronized; growing checkpoints are coalesced, normally at most once every two seconds during acquisition, with final files flushed before completion. CSV records include acquisition start/end timestamps and Keithley read errors for timing diagnostics.

### Endpoint acceptance and final magnetic mode

B-field Transport continuous sweeps accept the first fresh reading inside the endpoint window approached in the commanded direction, save its measured field, and wait for pause acknowledgement before reversing. The window uses `magnet.field_tolerance_t` (default 0.002 T), capped at 1% of the sweep span with an instrument-resolution floor for short spans.

Successful B-field Transport sweeps always finish in **Driven (leave heater ON)**, holding the final field for the next sweep without persistent-mode cooldown or lead zeroing. A round trip finishes at its starting field. Older recipes and saved Persistent selections are overridden, and the successful final-mode display is fixed to Driven. Stop, error, interlock, and application-shutdown cleanup still use Persistent mode.

Optional endpoint settling is controlled through **View → Runtime settings** in Quick (**Tools → Runtime settings…** in Widgets), or in the saved configuration's `mcd` section: `transport_endpoint_settling_enabled` defaults to `false`, and `transport_endpoint_settling_timeout_s` defaults to `120.0`. When enabled, `endpoint_stable_reads` (default 3) consecutive in-window readings are required. The sweep advances immediately once they pass; excursions reset the count without restarting the timeout. The settings dialog applies supported changes while idle without restarting. Manual JSON edits are read at the next application startup. Existing instrument protections remain active.

Quick-add list/range parsing, endpoint inclusion, condition limits and source validation are documented in [Measurement guide: Quick-add and field-target syntax](measurement-guide.md#quick-add-and-field-target-syntax).

## Validation, packaging and historical evidence

Follow the repository [collaboration instructions](../AGENTS.md). Run each offline test module in a separate process using `.venv\Scripts\python.exe -B verify_offline.py <test-module>`; generated verification reports belong under `tmp/`.

[GitHub tests and releases](github-release.md) describes automated Windows tests, portable EXE and installer checks, and release downloads. Pushes to `main` and pull requests run the offline test modules on separate Windows runners, then build and exercise the packaged application. Version tags (`v*`) publish `Transport-Setup-windows-x64.exe`, `Transport-windows-x64.zip`, and `SHA256SUMS.txt` covering both downloads to [GitHub Releases](https://github.com/ylylyl98/Transport_Pyside6/releases/latest). The per-user installer does not install instrument drivers or remove AppData settings and measurement data on uninstall. Close the application and background workers normally before updating or uninstalling.

[Qt Quick migration and validation](qt-quick-migration.md) records the historical migration baseline, subsequent review corrections and regression/rollback commands. Its [responsiveness update](qt-quick-migration.md#responsiveness-update) includes measured UI behavior and hardware-blocked benchmark commands. The historical 1,099-test / 87-module baseline is recorded there; it is not a count of the current test suite. See also [PySide6 release and rollback notes](pyside6-release.md).

[Documentation index](README.md) · [Project overview](../README.md)
