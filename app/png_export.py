"""Hardware-free PNG rendering shared by acquisition outputs and analysis views."""
from __future__ import annotations

import csv
import json
import os
import textwrap
from dataclasses import asdict
from pathlib import Path

import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.ticker import MaxNLocator

from app.curve_history import HistoryRecord, comparison_traces, read_csv_snapshot, resolve_column
from app.curve_map import load_map
from app.curve_normalization import comparison_y_label
from app.plot_export_labels import captions, trace_label, comparison_labels
from app.plot_style import style_heatmap_axes
from app.saved_conditions import summarize_conditions
from app.drag_drive import DUAL_SIGNALS
from app.analysis_csv_export import csv_bytes, validate_csv_path, temporary_path, write_bytes_atomic
from app.export_paths import ordinary_path, validate_export_path

CHANNELS = ("Ids_DC", "Ids_X", "Ids_Y", "Ids_Keithley", *DUAL_SIGNALS)
OVERVIEW_CHANNELS = (("I_drag_X", "Drag X"), ("I_drag_Y", "Drag Y"),
                     ("I_drive_X", "Drive X"), ("I_drive_Y", "Drive Y"))
DPI = 150


def save_snapshot(snapshot, path):
    """Keep arrays out of IPC and forbid pickled objects in the archive."""
    arrays = {}
    def encode(value):
        if isinstance(value, np.ndarray):
            key = f"array_{len(arrays)}"
            arrays[key] = value
            return {"array_key": key}
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, dict):
            return {key: encode(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [encode(item) for item in value]
        if isinstance(value, np.generic):
            return value.item()
        return value
    data = encode(snapshot)
    with Path(path).open("wb") as stream:
        np.savez(stream, metadata=np.array(json.dumps(data)), **arrays)
    return str(path)


def load_snapshot(path):
    with np.load(path, allow_pickle=False) as archive:
        def decode(value):
            if isinstance(value, dict):
                if set(value) == {"array_key"}:
                    return archive[value["array_key"]].copy()
                return {key: decode(item) for key, item in value.items()}
            if isinstance(value, list):
                return [decode(item) for item in value]
            return value
        return decode(json.loads(str(archive["metadata"])))


def _current_scale(snapshot):
    if snapshot.get("y_unit") != "A":
        return 1., snapshot.get("y_unit", "")
    values = [np.asarray(t["y"]) for t in snapshot["traces"]]
    if snapshot.get("map"):
        values.append(np.asarray(snapshot["map"]["z"]))
    maximum = max((float(np.max(np.abs(v[np.isfinite(v)]))) for v in values if np.any(np.isfinite(v))), default=0)
    for factor, name in ((1., "A"), (1e-3, "mA"), (1e-6, "µA"), (1e-9, "nA"), (1e-12, "pA")):
        if maximum >= factor or name == "pA":
            return factor, name
    return 1., "A"


def _label(name, unit):
    return name + (f" ({unit})" if unit else "")


def _style(ax, heatmap=False):
    ax.tick_params(direction="in", top=True, right=True, labelsize=20)
    ax.xaxis.set_major_locator(MaxNLocator(5))
    ax.yaxis.set_major_locator(MaxNLocator(5))
    ax.xaxis.get_offset_text().set_fontsize(16)
    ax.yaxis.get_offset_text().set_fontsize(16)
    ax.set_axisbelow(True)
    if heatmap:
        style_heatmap_axes(ax)
    else:
        ax.grid(True, which="major", color="#d9dde3", linewidth=.6, alpha=.65)


def _fit_header(fig, text, left, top, right, color="black"):
    artist = fig.text(left, top, text, fontsize=16, va="top", color=color)
    # Reflow at each font size so long instrument snapshots use the full width.
    fig.canvas.draw()
    available = (right - left) * fig.bbox.width
    renderer = fig.canvas.get_renderer()
    for size in (16, 14, 12, 10, 8):
        artist.set_fontsize(size)
        artist.set_text(text)
        width = artist.get_window_extent(renderer).width
        if width > available:
            columns = max(12, int(len(text) * available / width * .96))
            artist.set_text(textwrap.fill(text, columns, break_long_words=True))
            while artist.get_window_extent(renderer).width > available and columns > 12:
                columns -= 1
                artist.set_text(textwrap.fill(text, columns, break_long_words=True))
        bounds = artist.get_window_extent(renderer)
        if bounds.width <= available and bounds.height <= fig.bbox.height * .075:
            break
    return artist


def _heatmap_snapshot(snapshot):
    """Keep standalone maps in physical units and label only their source."""
    data = snapshot["map"]
    records = [record for record in snapshot["records"] if record["path"] == data.get("path")]
    return {**snapshot, "x_name": data["x_name"], "x_unit": data["x_unit"],
            "y_unit": data.get("signal_unit", snapshot.get("y_unit", "")),
            "records": records or snapshot["records"], "traces": [],
            "view": {**snapshot["view"], "normalization": "raw", "fixed_value": None}}


def _draw_analysis_traces(fig, snapshot, rect, automatic, differences, pair):
    ax = fig.add_axes(rect)
    factor, y_unit = _current_scale(snapshot)
    records = {record["path"]: record for record in snapshot["records"]}
    colors = {}
    excluded = (snapshot["x_name"],)
    if snapshot.get("map"):
        excluded += (snapshot["map"]["x_name"], snapshot["map"]["y_name"])
    labels = (comparison_labels(snapshot["records"], snapshot["traces"], excluded) if not automatic else
              [trace_label(trace, records[trace["path"]], differences, True) for trace in snapshot["traces"]])
    for trace, label in zip(snapshot["traces"], labels):
        color = colors.setdefault(trace["path"], f"C{len(colors) % 10}")
        if automatic and len(records) == 1:
            color = "#dc8500" if trace.get("direction") == "backward" else "#1967d2"
        label = textwrap.fill(label, 42 if not pair else 38)
        ax.plot(trace["x"], np.asarray(trace["y"]) / factor, color=color, lw=1.45,
                linestyle="--" if trace.get("direction") == "backward" else "-", label=label,
                marker=".", markersize=4)
    ax.set_xlabel(_label(snapshot["x_name"], snapshot.get("x_unit", "")), fontsize=22)
    ax.set_ylabel(comparison_y_label(snapshot["signal"], y_unit, snapshot["view"].get("normalization", "raw")), fontsize=22)
    _style(ax)
    view = snapshot["view"]
    if view.get("xlim"):
        ax.set_xlim(view["xlim"])
    if view.get("ylim"):
        ax.set_ylim(np.asarray(view["ylim"]) / factor)
    ax.set_xscale(view.get("xscale", "linear"))
    ax.set_yscale(view.get("yscale", "linear"))
    if view.get("legend", True) and ax.lines:
        # More curves need compact text; the default two-direction export stays 16 pt.
        handles, labels = ax.get_legend_handles_labels()
        unique = dict(zip(labels, handles))
        legend = ax.legend(unique.values(), unique.keys(), loc="best", frameon=False, fontsize=16,
                  ncol=2 if automatic and len(ax.lines) <= 2 else 1, columnspacing=.8, handlelength=1.5)
        fig.canvas.draw()
        for size in (16, 14, 12, 10, 8):
            for text in legend.get_texts():
                text.set_fontsize(size)
            bounds = legend.get_window_extent(fig.canvas.get_renderer())
            if bounds.width < ax.bbox.width * .95 and bounds.height < ax.bbox.height * .8:
                break
    return ax


def build_analysis_figure(snapshot, cut_only=False, automatic=False, *, map_only=False):
    from app.map_view import resolved_map_view
    snapshot = resolved_map_view(snapshot)
    if map_only:
        snapshot = _heatmap_snapshot(snapshot)
    draw_map = bool(snapshot.get("map")) and not cut_only
    pair = draw_map and not map_only
    fig = Figure(figsize=(16 if pair else 8, 6), dpi=DPI, facecolor="white")
    FigureCanvasAgg(fig)
    title, detail, differences = captions(snapshot, automatic)
    left, right = (.085, .97) if pair else (.145, .96)
    heading = _fit_header(fig, title, left, .985, right)
    fig.canvas.draw()
    detail_top = .985 - heading.get_window_extent(fig.canvas.get_renderer()).height / fig.bbox.height - .012
    subheading = _fit_header(fig, detail, left, detail_top, right, ".30")
    fig.canvas.draw()
    plot_top = min(.89, detail_top - subheading.get_window_extent(fig.canvas.get_renderer()).height / fig.bbox.height - .025)
    height = max(.35, plot_top - .13)
    axes = []
    if not map_only:
        rect = (.59, .13, .37, height) if pair else (.145, .13, .815, height)
        axes.append(_draw_analysis_traces(fig, snapshot, rect, automatic, differences, pair))
    view = snapshot["view"]
    if draw_map:
        data = snapshot["map"]
        map_factor, map_unit = _current_scale({"y_unit": data.get("signal_unit", snapshot.get("y_unit", "")),
                                              "traces": [], "map": data})
        map_ax = fig.add_axes((.085, .13, .33, height) if pair else (.145, .13, .65, height))
        axes.append(map_ax)
        image = map_ax.pcolormesh(data["x"], data["y"], np.asarray(data["z"]) / map_factor,
                                  shading="auto", cmap=view.get("cmap", "RdBu_r"))
        if view.get("clim"):
            image.set_clim(np.asarray(view["clim"]) / map_factor)
        map_ax.set_xlabel(_label(data["x_name"], data["x_unit"]), fontsize=22)
        map_ax.set_ylabel(_label(data["y_name"], data["y_unit"]), fontsize=22)
        _style(map_ax, heatmap=True)
        if view.get("map_xlim"):
            map_ax.set_xlim(view["map_xlim"])
        if view.get("map_ylim"):
            map_ax.set_ylim(view["map_ylim"])
        cax = fig.add_axes((.437, .13, .012, height) if pair else (.82, .13, .025, height))
        bar = fig.colorbar(image, cax=cax)
        bar.ax.tick_params(labelsize=14)
        bar.set_label(_label(snapshot["signal"], map_unit), fontsize=16)
        bar.ax.yaxis.get_offset_text().set_fontsize(14)
    # Expand the left margin only when scientific notation/long labels need it.
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    for axis in axes:
        bounds = axis.yaxis.label.get_window_extent(renderer)
        if bounds.x0 < 6:
            rect = axis.get_position()
            extra = (6 - bounds.x0) / fig.bbox.width
            axis.set_position((rect.x0 + extra, rect.y0, rect.width - extra, rect.height))
    return fig


def _write_png(fig, output, title, detail, sources=None):
    temporary = None
    try:
        output = validate_export_path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = temporary_path(output)
        metadata = {"Title": title, "Description": detail}
        if sources:
            metadata['SourceFiles'] = json.dumps(sources, ensure_ascii=False)
        fig.savefig(temporary, dpi=DPI, format="png", transparent=False, facecolor="white",
                    metadata=metadata, bbox_inches=fig.bbox_inches)
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        fig.clear()


def _description(snapshot, detail):
    _factor, unit = _current_scale(snapshot)
    map_data = snapshot.get("map")
    map_y = f"Y: {_label(map_data['y_name'], map_data['y_unit'])}" if map_data else ""
    signal_label = comparison_y_label(snapshot['signal'], unit, snapshot['view'].get('normalization', 'raw'))
    return " · ".join(filter(None, (detail, f"X: {_label(snapshot['x_name'], snapshot.get('x_unit', ''))}", map_y, f"Signal: {signal_label}")))


def build_run_figure(snapshot, panels=None):
    """A standalone heatmap or fixed Drag/Drive overview, with no live view state."""
    overview = panels is not None
    fig = Figure(figsize=(16, 12) if overview else (8, 6), dpi=DPI, facecolor="white")
    FigureCanvasAgg(fig)
    title, detail, _ = captions(snapshot, automatic=True)
    left = .065 if overview else .145
    heading = _fit_header(fig, title, left, .985, .96)
    detail_top = .985 - heading.get_window_extent(fig.canvas.get_renderer()).height / fig.bbox.height - .012
    subheading = _fit_header(fig, detail, left, detail_top, .96, ".30")
    fig.canvas.draw()
    plot_top = min(.91 if overview else .89,
                   detail_top - subheading.get_window_extent(fig.canvas.get_renderer()).height / fig.bbox.height - .025)
    fig.set_layout_engine("constrained", rect=(0, 0, 1, plot_top), h_pad=.12, w_pad=.12)
    if overview:
        axes = fig.subplots(2, 2, sharex=True, sharey=False).ravel(order="F")
        items = [(panels.get(signal), label) for signal, label in OVERVIEW_CHANNELS]
    else:
        axes, items = [fig.subplots()], [(snapshot, "")]
    for ax, (data, label) in zip(axes, items):
        if label:
            ax.set_title(label, fontsize=20, pad=10)
        _style(ax, heatmap=bool((data or snapshot).get("map")))
        if data is None:
            ax.text(.5, .5, "No finite data", ha="center", va="center", transform=ax.transAxes, fontsize=16, color=".4")
            continue
        factor, unit = _current_scale(data)
        ax.set_xlabel(_label(data["x_name"], data.get("x_unit", "")), fontsize=22)
        if data.get("map"):
            grid = data["map"]
            # Mask missing cells explicitly; no interpolation or averaging of repeated points.
            mesh = ax.pcolormesh(grid["x"], grid["y"], np.ma.masked_invalid(np.asarray(grid["z"]) / factor),
                                shading="auto", cmap="viridis")
            ax.set_ylabel(_label(grid["y_name"], grid["y_unit"]), fontsize=22)
            bar = fig.colorbar(mesh, ax=ax, pad=.025, fraction=.05)
            bar.set_label(_label(label or data["signal"], unit), fontsize=18)
            bar.ax.tick_params(labelsize=16)
            bar.ax.yaxis.get_offset_text().set_fontsize(14)
        else:
            record = data["records"][0]
            for trace in data["traces"]:
                backward = trace.get("direction") == "backward"
                ax.plot(trace["x"], np.asarray(trace["y"]) / factor,
                        color="#dc8500" if backward else "#1967d2", linestyle="--" if backward else "-",
                        lw=1.45, marker=".", markersize=4,
                        label=textwrap.fill(trace_label(trace, record, {}, True), 36))
            ax.set_ylabel(_label(label or data["signal"], unit), fontsize=22)
            # Long scans can contain hundreds of row/pass segments: keep the legend bounded.
            if len(ax.lines) <= 8:
                ax.legend(loc="best", frameon=False, fontsize=12)
    fig.canvas.draw()
    return fig


def _run_snapshot(record, x_name, signal, slow_axis=None):
    snapshot = {"mode": "map" if slow_axis else "curve", "kind": record.measurement, "signal": signal,
                "x_name": x_name, "records": [asdict(record)], "traces": [], "view": {"legend": True}}
    snapshot["records"][0]["path"] = str(record.path)
    if slow_axis:
        grid = load_map(record.path, x_name, slow_axis, signal)
        snapshot.update(x_unit=grid.x_unit, y_unit=grid.signal_unit, map=asdict(grid))
        snapshot["map"]["path"] = str(grid.path)
        return snapshot, [f"{record.path.name}: {warning}" for warning in grid.warnings]
    traces, warnings = comparison_traces([record], x_name, signal)
    if not traces:
        return None, warnings
    snapshot.update(x_unit=traces[0].x_unit or record.units.get(x_name, ""),
                    y_unit=traces[0].y_unit or record.units.get(resolve_column(record.columns, signal), ""),
                    traces=[asdict(trace) for trace in traces])
    for trace in snapshot["traces"]:
        trace["path"] = str(trace["path"])
    return snapshot, warnings


def export_analysis(snapshot, output=None, cut_only=False, *, heatmap_only=False):
    from app.map_view import resolved_map_view
    snapshot = resolved_map_view(snapshot)
    if heatmap_only and (cut_only or not snapshot.get("map")):
        raise ValueError("Heatmap-only export requires a displayed map and cannot be combined with cut-only export")
    if not snapshot.get("traces") and (cut_only or not snapshot.get("map")):
        raise ValueError("No displayed curves to export")
    if output is None:
        from app.analysis_export import export_automatic
        return export_automatic(snapshot, cut_only=cut_only, heatmap_only=heatmap_only)
    output = ordinary_path(output).with_suffix(".png")
    output.parent.mkdir(parents=True, exist_ok=True)
    separate_map = bool(snapshot.get("map")) and not cut_only and not heatmap_only
    kinds = (["heatmap"] + (["cut"] if snapshot.get("traces") else [])) if separate_map else ["heatmap" if heatmap_only else "cut" if cut_only else "curve"]
    original, count = output, 1
    while True:
        sidecar = output.with_name(output.stem + "_view.json")
        outputs = [output.with_name(f"{output.stem}_{kind}.png") for kind in kinds] if separate_map else [output]
        csv_outputs = [target.with_suffix('.csv') for target in outputs]
        for target in csv_outputs:
            validate_csv_path(target)
        for target in [sidecar, *outputs]:
            validate_export_path(target)
        reserved = []
        try:
            for target in [sidecar, *outputs, *csv_outputs]:
                with target.open("xb"):
                    pass
                reserved.append(target)
            break
        except OSError as exc:
            for target in reserved:
                target.unlink(missing_ok=True)
            if not isinstance(exc, FileExistsError):
                raise
        count += 1
        output = original.with_name(f"{original.stem}_{count:02d}.png")
    temporary = None
    try:
        view = {**snapshot["view"], "signal": snapshot["signal"], "x": snapshot.get("map", {}).get("x_name", snapshot["x_name"]),
                "kind": snapshot["kind"], "map_y": snapshot.get("map", {}).get("y_name", "None (1D)"),
                "cut_only": bool(cut_only), "heatmap_only": bool(heatmap_only)}
        payload = {"schema": "transport_plot_view_v1", "sources": snapshot["records"], "view": view,
                   "image": {"width": 1200, "height": 900, "dpi": DPI},
                   "images": [{"path": str(target), "kind": kind} for target, kind in zip(outputs, kinds)],
                   "csv_files": [{"path": str(target), "kind": kind} for target, kind in zip(csv_outputs, kinds)],
                   "warnings": snapshot.get("warnings", [])}
        temporary = temporary_path(sidecar)
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        for target, kind in zip(outputs, kinds):
            rendered = _heatmap_snapshot(snapshot) if kind == "heatmap" else snapshot
            title, detail, _ = captions(rendered)
            figure = build_analysis_figure(rendered, cut_only=kind == "cut", map_only=kind == "heatmap")
            _write_png(figure, target, title, _description(rendered, detail), [record['path'] for record in rendered['records']])
        for target, kind in zip(csv_outputs, kinds):
            write_bytes_atomic(target, csv_bytes(snapshot, kind))
        os.replace(temporary, sidecar)
        return {"outputs": [str(target) for target in outputs], "csv_outputs": [str(target) for target in csv_outputs],
                "view_path": str(sidecar), "warnings": snapshot.get("warnings", [])}
    except Exception:
        for target in reserved:
            target.unlink(missing_ok=True)
        raise
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _run_record(path, metadata):
    path = ordinary_path(path)
    headers, units, rows, _warnings = read_csv_snapshot(path)
    if metadata.get("measurement") == "bfield_transport" and not units:
        units = {name: "A" if name in CHANNELS or name == "Keithley_current" else "T" if name in {"B_measured_T", "B_target_T"} else "V" if name in {"Vtg", "Vbg", "Vds", "Doping", "E-field"} else "" for name in headers}
    metadata = {**metadata, "csv_conditions": summarize_conditions(headers, units, rows)}
    stat = path.stat()
    return HistoryRecord(path, metadata.get("measurement", ""), metadata.get("created_at", metadata.get("updated_at", "")),
                         metadata.get("status", "unknown"), "", headers, units, metadata, (stat.st_mtime_ns, stat.st_size))


def export_run(job):
    meta_path = job.get("metadata_path")
    metadata = json.loads(Path(meta_path).read_text(encoding="utf-8-sig")) if meta_path and Path(meta_path).exists() else {}
    if metadata.get("status") == "running":
        return {"outputs": [], "warnings": ["Run metadata is still running; PNG skipped until cleanup completes"]}
    if not metadata.get("measurement"):
        metadata["measurement"] = job.get("measurement", "bfield_transport" if metadata.get("schema") == "bfield_transport_manifest_v1" else "")
    if job.get("device_id") and not metadata.get("save_root"):
        metadata["save_root"] = {"device_id": job["device_id"]}
    paths = metadata.get("csv_paths") or job.get("csv_paths") or ([metadata["csv_path"]] if metadata.get("csv_path") else [])
    params = metadata.get("params", {})
    slow_axis = params.get("axis_slow") if metadata.get("measurement") in {"map_2d", "sweep_1d"} else None
    slow_axis = None if slow_axis in (None, "None", "") else slow_axis
    outputs, warnings = [], []
    for path in dict.fromkeys(paths):
        try:
            record = _run_record(path, metadata)
            chain = metadata.get("signal_chain") or metadata.get("validation", {}).get("calibration", {}).get("signal_chain", {})
            dual = bool(chain.get("drag_drive", {}).get("enabled")) or all(signal in record.columns for signal, _label in OVERVIEW_CHANNELS)
            # Preserve the existing ordinary-mode export policy; dual grids get true heatmaps.
            if slow_axis and not dual:
                continue
            default = {"vds_sweep": "Vds", "photocurrent": "Wavelength", "bfield_transport": "B_measured_T"}.get(record.measurement, params.get("axis_fast", "Vtg"))
            x_name = params.get("axis_fast") if slow_axis else params.get("plot_x_resolved") or default
            if resolve_column(record.columns, x_name) not in record.columns and x_name != "Step Index":
                raise ValueError(f"Saved X axis {x_name} is absent")
            panels = {}
            for signal in CHANNELS:
                if resolve_column(record.columns, signal) not in record.columns:
                    continue
                try:
                    snapshot, problems = _run_snapshot(record, x_name, signal, slow_axis)
                    if snapshot is None:
                        if signal in DUAL_SIGNALS:
                            warnings.extend(f"{signal}: {problem}" for problem in problems)
                        continue
                    warnings.extend(problems)
                    panels[signal] = snapshot
                    suffix = "_heatmap" if slow_axis else ""
                    target = record.path.parent / "plots" / f"{record.path.stem}_{signal}{suffix}.png"
                    title, detail, _ = captions(snapshot, automatic=True)
                    figure = build_run_figure(snapshot) if slow_axis else build_analysis_figure(snapshot, automatic=True)
                    _write_png(figure, target, title, _description(snapshot, detail))
                    outputs.append(str(target))
                except (OSError, ValueError, csv.Error) as exc:
                    warnings.append(f"{record.path.name} · {signal}: {exc}")
            if dual and panels:
                template = {**next(iter(panels.values())), "signal": "Drag / Drive"}
                title, detail, _ = captions(template, automatic=True)
                descriptions = []
                for signal, label in OVERVIEW_CHANNELS:
                    if signal in panels:
                        descriptions.append(_description(panels[signal], label))
                    else:
                        warnings.append(f"{record.path.name} · {signal}: no finite data for overview")
                        descriptions.append(f"{label}: No finite data")
                suffix = "_heatmap" if slow_axis else ""
                target = record.path.parent / "plots" / f"{record.path.stem}_DragDrive_2x2{suffix}.png"
                _write_png(build_run_figure(template, panels), target, title, " · ".join(filter(None, (detail, *descriptions))))
                outputs.append(str(target))
        except (OSError, ValueError, csv.Error) as exc:
            warnings.append(f"{Path(path).name}: {exc}")
    return {"outputs": outputs, "warnings": list(dict.fromkeys(warnings))}
