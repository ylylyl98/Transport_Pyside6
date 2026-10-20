# Developing without repeatedly restarting Transport

Install or repair the environment with the project-root `Transport_App.bat`.
After this update has been loaded once, use the narrower update routes below.
Running Python processes do not pick up this release automatically.

| Change | Apply it with | Main Transport process |
| --- | --- | --- |
| Main-window colors, borders and spacing | Edit `app/ui/app.qss`, then View → Reload UI style / Ctrl+Alt+R | Kept running, including instrument ownership |
| Main-window font size | Tools → Runtime settings → Apply appearance | Kept running |
| B-field endpoint/recovery timing and reading counts | Tools → Runtime settings → Apply next-run settings, while idle | Kept running; active work prevents application |
| Historical analysis, normalization, heatmaps and PNG rendering | Curve Compare → Reload analysis | Kept running; accepted exports drain first |
| Python layout/widget code | Tools → Offline UI preview; rebuild the preview with Ctrl+Shift+R | Kept running during development; restart once to accept the finished main UI |
| Device address or Keithley mode | Existing Devices controls and reconnection for the affected device | Kept running |
| Drivers, acquisition logic, SDK/dependency changes or other constructor-time settings | Relevant isolated tests, then normal close/relaunch while idle | Restart required |

`HistoryViewerLauncher`, `PngExportProcess` and the measurement PNG coordinator
also live in the main process. Editing those classes requires a main-app restart,
even though the code they launch can be updated using Reload analysis.
QSS changes reach an already-open history viewer on its next analysis reload.
Do not use `importlib.reload()` on live controllers, workers, the main window or
the config module: old objects and Qt connections retain their existing identity.

## Runtime configuration

The settings dialog provides a deliberately bounded set of settings:

- Main-window font size, applied immediately.
- Endpoint settling enabled/disabled, consecutive endpoint readings and timeout.
- Monitoring recovery timeout and consecutive recovery readings.
- Minimum sweep-leg timeout and minimum field-progress increment.

Next-run changes are validated, recheck that acquisition and hardware operations
are idle at application time, and update the existing `cfg` objects in place.
The UI saves them atomically to the same per-user JSON configuration as before.
Only edited fields are submitted; displaying a rounded or range-limited value
does not change an untouched stored value. If saving fails, the live values and
complete pre-change stylesheet are restored, and edits remain available to retry.
Edits left in the dialog
while a run is active are not automatically applied later: apply them when idle.
Editing the JSON file externally still requires restart; this is not a general
file watcher for hardware configuration. Voltage/current protections retain
their existing explicit application and readback controls.

## Isolated UI preview

Launch [`tools/Transport_Preview.bat`](../tools/Transport_Preview.bat), or run the
following from the project root:

```powershell
.\.venv\Scripts\python.exe -B transport_ui_preview.py
```

The preview uses the actual window code, synthetic plot data and disabled
acquisition. Its Python/VISA/DAQ/network/native-Qt-process/2100-SDK guards prevent
accidental instrument access through the normal application routes. This is a
development guard for trusted project code, not an operating-system sandbox.
It never opens the production app's user settings. Only the preview's font cache
is reusable; temporary settings survive preview rebuilds and are removed when
the preview supervisor exits.

Use Ctrl+Alt+R for style edits and Ctrl+Shift+R for Python UI changes. Rebuild
restarts the preview child process, not the connected measurement application.
If a Python syntax/import error prevents startup, fix it and relaunch the preview.
Its status bar/title identify it as an offline preview. Measurement starts and
independent history/PNG processes are disabled there.

Render a selected measurement page without showing a desktop window:

```powershell
.\.venv\Scripts\python.exe -B transport_ui_preview.py --tab 1 --offscreen --screenshot tmp\verification\previews\ui-preview.png
.\.venv\Scripts\python.exe -B transport_ui_preview.py --settings --offscreen --screenshot tmp\verification\previews\settings-preview.png
```

Keep preview PNGs and companion JSON files under ignored
`tmp/verification/previews/`. To check display scaling, set
`QT_SCALE_FACTOR` to `1` or `1.5` for the preview process. Remove that override
before a normal production launch if it is no longer wanted.

## Analysis failure recovery

If replacement analysis code fails, correct the code and press **Reload analysis**
again. The original folder, zoom and window handoff are retained until the new
viewer confirms restoration. **Open history** also retries a failed handoff.
If you deliberately close a viewer after restoration fails, later analysis
reloads keep it closed. **Open history** can still restore the retained handoff.

If the PNG replacement fails, exports pause. Existing tasks and new measurements'
export tasks stay on disk, and the error includes the queue folder. A successful
explicit reload resumes them. Prefer recovering before exiting Transport;
closing during this failure preserves the queue but the next app launch does not
automatically discover old temporary queues. Original measurement CSVs are not
modified by this mechanism.

## Validation and final acceptance

Run each applicable module in a separate process using the existing wrapper:

```powershell
.\.venv\Scripts\python.exe -B verify_offline.py tests.test_development_tools
.\.venv\Scripts\python.exe -B verify_offline.py tests.test_runtime_settings
.\.venv\Scripts\python.exe -B verify_offline.py tests.test_workspace_state
.\.venv\Scripts\python.exe -B verify_offline.py tests.test_analysis_reload_recovery
.\.venv\Scripts\python.exe -B verify_offline.py tests.test_main_window_shutdown
```

The wrapper writes its `offline_results*.json` reports under
`tmp/verification/reports/`. These reports and preview images are local generated
artifacts, not measurement data or application launchers. Older dedicated runs
and backups remain under their existing `tmp/` subfolders. See the
[documentation index](README.md) and [developer utilities](../tools/README.md).

Also run the existing startup, workspace, relevant controller and analysis/PNG
process modules for affected behavior. Run process-heavy modules sequentially:
parallel cold Matplotlib/Qt startups can consume their bounded readiness waits.
Tests retain temporary settings and a temporary font cache, independent of the
preview cache. Group a finished set of driver/main-UI changes, validate it offline,
then perform one normal idle restart and controlled hardware acceptance.

The main window restores harmless layout state and the last measurement page;
it does not reconnect or resume an experiment. Failed hardware shutdown keeps
the controller threads available for Stop/retry/reconnection. The 2100 owner
confirms field Stop before Keithley cleanup, without disconnecting its SDK or
terminating its thread. A rejected close invalidates the completed APS Persistent
confirmation, so a later close checks again after any intervening manual control.
After successful hardware shutdown, controls are disabled while accepted exports
finish closing.
