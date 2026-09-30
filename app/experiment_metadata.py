"""Capture experiment conditions once before acquisition, never per data point."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime

from app.signal_chain import SignalChainSnapshot, signal_chain_metadata


def timestamp():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def capture_run_signal_chain(signal_chain, manager, *, owns_lockin=False, context=None):
    chain = signal_chain_metadata(signal_chain)
    session = manager.get_session("lockin")
    connected = session is not None
    if hasattr(manager, "is_connected"):
        connected = connected and manager.is_connected("lockin")
    if connected:
        claimed = False
        if not owns_lockin:
            claimed, blocked = manager.mark_in_use(["lockin"])
            if not claimed:
                raise RuntimeError("Lock-in is busy; wait for its current operation before starting.")
        try:
            values = deepcopy(session.read_settings())
            if not values:
                raise RuntimeError("Lock-in returned empty settings.")
            snapshot = {
                "source": "instrument", "captured_at": timestamp(),
                "model": getattr(session, "model", None),
                "identity": getattr(session, "identity", None),
                "address": getattr(manager.connections, "lockin", None),
                "scope": "driver-supported front-panel settings", "values": values,
            }
        finally:
            if claimed:
                manager.release(["lockin"])
    else:
        snapshot = deepcopy(chain.get("lockin_settings")) or {
            "source": "saved/manual", "scope": "signal-chain values only; panel unavailable",
            "values": {key: chain.get(key) for key in ("frequency_hz", "lockin_sensitivity_v")},
        }
        snapshot.update(source="saved/manual", captured_at=timestamp())
    changes = {"lockin_settings": snapshot, "experiment_context": deepcopy(context)}
    if isinstance(signal_chain, SignalChainSnapshot):
        return replace(signal_chain, **changes)
    return {**chain, **changes}
