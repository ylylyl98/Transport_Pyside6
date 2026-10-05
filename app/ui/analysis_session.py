"""Small JSON session handoff between successive read-only viewer processes."""
from __future__ import annotations

import json
from pathlib import Path

from app.curve_history import MEASUREMENT_TYPES


def capture_view(page):
    view = {**page._display_state(), "xlim": list(page.ax.get_xlim()), "ylim": list(page.ax.get_ylim()),
            "xscale": page.ax.get_xscale(), "yscale": page.ax.get_yscale()}
    if page._displayed_primary is not None and page.map_ax is not None:
        view.update(primary_path=str(page._displayed_primary.path),
                    map_xlim=list(page.map_ax.get_xlim()), map_ylim=list(page.map_ax.get_ylim()),
                    clim=list(page._colorbar.mappable.get_clim()), cmap=page._colorbar.mappable.get_cmap().name)
    return view


def capture_session(page):
    page.save_state()
    expanded = page.expand_files_button.isChecked()
    state = {"schema": "transport_analysis_session_v1", "folder": str(page.folder),
             "custom_folder": str(page._custom_folder) if page._custom_folder is not None else None,
             "checked": sorted(page.model.checked), "kind": page._current_kind,
             "date": page.date_combo.currentText(), "search": page.search_edit.text(), "views": page._views,
             "splitter": getattr(page, "_splitter_sizes", page.splitter.sizes()) if expanded else page.splitter.sizes(),
             "expanded": expanded,
             "limits": capture_view(page) if page.png_actions._is_ready() else None}
    # Detach mutable dictionaries; only control values, never data arrays, cross IPC.
    return json.loads(json.dumps(state))


def restore_session(page, state):
    if not isinstance(state, dict) or state.get("schema") != "transport_analysis_session_v1" or state.get("kind") not in MEASUREMENT_TYPES:
        raise ValueError("Unsupported analysis session")
    page._custom_folder = Path(state["custom_folder"]) if state.get("custom_folder") is not None else None
    page.follow_button.setEnabled(page._custom_folder is not None)
    folder = Path(state["folder"]) if page._custom_folder is not None else Path(page.folder_callable()).expanduser().absolute()
    page.set_folder(folder)
    # Layout belongs to the viewer rather than a particular device folder.
    if state.get("splitter"):
        page.splitter.setSizes(state["splitter"])
    page.expand_files_button.setChecked(bool(state.get("expanded", False)))
    if page._custom_folder is None and folder != Path(state["folder"]):
        # Follow Transport's current device rather than transplanting another
        # device's selection and zoom over its freshly restored folder state.
        page.refresh()
        return
    page.settings.setValue(page._state_key(), json.dumps(state))
    page._restore_state()
    page.png_actions.restore_view = state.get("limits")
    page.png_actions.restore_sources = []
    page._last_plot_signature = page._desired_plot_signature = None
    page._filters_changed()
    page.refresh()
