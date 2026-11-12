# Windows source launcher

Double-click `Transport_App.bat` in the source checkout. It creates or repairs a
current-user desktop shortcut for that checkout, then starts Transport. The
shortcut is named `Transport Measurement (Source - <id>)`; the ID distinguishes
separate checkout paths. The desktop shortcut and a taskbar pin made from its
source window both use the same update-and-launch entry point.

The launcher checks for a running source instance first. If one is running, it
focuses that window and does not update the checkout. Otherwise, it fetches and
fast-forwards only when the checkout is a clean `main` branch tracking the remote
`main` branch. It skips the update and shows an English notification when those
conditions are not met or an update cannot be completed. Existing local changes
are not discarded. Git and network access are needed for the update check; the
application can still start from the available checkout when an update is skipped.

For an older application launched with system Python and a relative script path,
Windows may not identify its checkout. In that case the launcher reports the
uncertainty and performs neither an update nor a second launch. Switch to that
application or close it normally before retrying.

The existing batch bootstrap uses a suitable Python 3.12 through 3.14 installation
and the project virtual environment. Creating the shortcut itself does not require
Python. Launching the application may install the project's dependencies as needed.

Source and installed releases use distinct Windows application identities. To pin
the source launcher, launch it and pin **Transport Measurement (Source)** from its
running window. An older pin that launches the wrong entry point is not changed
automatically: unpin it and pin the source window again. Creating or repairing the
shortcut does not itself launch the application or modify taskbar pins.

After moving the checkout, run `Transport_App.bat` from the new location to
create its new desktop shortcut. Unpin the old taskbar entry and pin the new
Source window; the launcher does not pin automatically or modify old pins.

Startup and update logs are stored under
`%LOCALAPPDATA%\TransportMeasurement\Source\<id>\startup.log` and
`%LOCALAPPDATA%\TransportMeasurement\Source\<id>\update.log`, where `<id>`
is the checkout-specific identifier.
