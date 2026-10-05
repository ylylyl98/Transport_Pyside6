# SR830 / SR850 front-panel control

Connect the instrument under Devices, open Instruments → Lock-in, then use
**Refresh Panel** to read its settings. The identity reported by `*IDN?`
determines the model-specific controls.

## Connection and identity recovery

Connection selects the reply interface **before** `*IDN?`: `OUTX 1` for GPIB,
`OUTX 0` for RS232. The read terminator follows the instrument's interface
(LF for GPIB, CR for RS232). A VISA device clear targets only the newly opened
resource to remove residual communication data; no instrument `*RST`, bus-wide
reset, or measurement-setting commands are sent. If the backend explicitly does
not support device clear, identification can still proceed.

A stale scalar such as `0`, an empty reply, or a query timeout triggers at most
three identification attempts, each with the same clear/interface/query order.
The driver still requires an actual SR830/SR850 identity; it never infers a model
from the GPIB address. A complete identity naming another device fails immediately.
Failed connections close their session and report the instrument role, address,
and received identity replies. Check the address and competing instrument-control
programs if identification continues to fail after restarting the updated app.

## Sensitivity and Apply

- Edit **Sensitivity (requested)** and click **Apply Changed Settings**.
  Only edited fields are submitted; saved defaults are not written to the instrument.
- **Instrument sensitivity** shows the last actual readback. The pending edits
  list identifies values that have not yet been verified.
- A successful write is followed by command-completion polling and readback.
  A mismatch reports both requested and returned values. Failed requests remain
  editable; **Discard Pending Edits** restores the latest readback.
  Verification accounts for each model's phase resolution and phase wrapping,
  oscillator frequency precision, and 2 mV sine-amplitude steps; the returned
  representable value is shown after a successful apply.
- The signal-chain sensitivity input is read-only while connected. DAQ conversion
  uses the returned sensitivity, including ranges down to 2 nV, rather than the
  pending selection. A timeout or incomplete readback blocks calibration until
  a successful panel refresh. Measurement runs retain their existing resource locks.

## Auto functions and display controls

Both models support Auto Phase (`APHS`), Auto Gain (`AGAN`), Auto Reserve (`ARSV`)
and Auto Offset (`AOFF`). SR830 Auto Gain requires a time constant of 1 s or less;
that restriction is not imposed on SR850. On GPIB the app uses VISA `read_stb()`
(a hardware serial poll) and checks bit 1 (`status & 0x02`) for completion.
It does not repeatedly send `*STB? 1`: that text query can itself clear the
Interface Ready bit and keep replying `0`, causing a false completion timeout.
On RS232, or when the backend explicitly reports serial polling unsupported,
one `*STB?` reply establishes that preceding commands have finished, regardless
of its bit-1 value. The wait uses the operation's timeout and restores the usual
VISA timeout afterward. Command completion does not mean analog settling has
finished, particularly after Auto Phase.

SR850 additionally offers **Auto Scale (display)** (`ASCL`), plus range (`DSCL`)
and center (`DOFF`) for its active Bar/Chart display. Refresh after changing the
active pane or display type at the instrument. A manual display apply checks the
active pane again before writing. These controls do not change measurement
sensitivity or analog-output scaling. SR830 hides these controls.

SR850 Reserve uses Maximum / Manual / Minimum. Its manual level is a relative
index from minimum to +50 dB; the instrument can cap it at its maximum reserve.
Readback remains authoritative. SR830 retains High Reserve / Normal / Low Noise.

DAQ conversion assumes normal X/Y analog outputs with zero offset and unity
expansion. Drag/Drive mode verifies these settings on both instruments before
starting; it does not apply offset/expand correction. See [Drag/Drive setup](drag-drive.md)
for mode selection, addresses, wiring, and per-run calibration.

## Diagnostics and verification

**Last Operation Details** shows the requested values, model/address, commands,
timestamped query responses, status registers and actual settings. Each operation
is also saved under the application's local data directory at
`logs/lockin-control.jsonl`; its exact path appears in the details. One previous
log is retained after rotation at approximately 2 MB. Pre-existing ESR bits are
recorded separately from errors raised by the current operation.

Serial polls are recorded separately as `serial_poll` / `VISA read_stb`, including
the full returned status byte. If Refresh fails after many `*STB? 1` replies of
`0` without reaching any settings queries, restart the updated app to load the
corrected completion check. A real busy timeout or transport failure still leaves
the state unverified; no measurement setting write is retried automatically.

Offline regression command (blocks real VISA/DAQ access and isolates settings):

```powershell
.\.venv\Scripts\python.exe -B verify_offline.py test_srs_connection test_lockin_control test_srs_lockin test_lockin_metadata test_signal_chain_ui test_png_viewer_shutdown
```

Command references: [SR850 manual](https://www.thinksrs.com/downloads/PDFs/Manuals/SR850m.pdf),
[SR830 manual](https://www.thinksrs.com/downloads/PDFs/Manuals/SR830m.pdf).
