# Interface previews

These are native Qt window captures of the current application, rendered at
1520 × 940 pixels with the Fusion style, light theme and 100% scale. They contain
synthetic examples only; no measurement was run and no instrument was connected.

- `measurement-preview.png`: `app/quick/qml/Main.qml` with the real
  `ApplicationBackend(offline=True)`, a 401-point simulated gate sweep and the
  four-channel plot layout. Operator, sample and output path are demo values.
- `history-preview.png`: `app/quick/qml/HistoryWindow.qml` with the real history
  parser and three generated CSVs at 5, 10 and 20 mV, each with forward/backward
  trajectories. The folder label is a generic synthetic-data label; the status
  explicitly identifies the data as simulated.

The capture harness installed `OfflineSandbox(preview=True)` before application
imports, isolating settings and blocking hardware, network and child processes.
PNG files were saved directly using Qt `grabWindow().save()` without image
generation or post-processing. Both captures completed with zero QML warnings
and zero Python callback exceptions and were visually inspected.

The local, ignored capture harness used for these exact fixtures was run with:

```powershell
.\.venv\Scripts\python.exe -B tmp/readme-preview/capture_readme.py measurement
.\.venv\Scripts\python.exe -B tmp/readme-preview/capture_readme.py history
```

For a stock hardware-blocked main-window preview from a fresh checkout:

```powershell
.\.venv\Scripts\python.exe -B transport_quick_preview.py --mode 1 --screenshot tmp/measurement-preview.png
```
