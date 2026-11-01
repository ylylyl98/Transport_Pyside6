"""Read-only filename examples using the acquisition naming helpers."""
from itertools import islice, product
import os

from app.cosweep_output import map_filename_parts
from app.measurement_output import dual_gate_filename_parts
from app.run_output import build_planned_output, gate_scan_filename_parts
from app.signal_chain import signal_chain_filename_parts
from app.quick.parameters import MODES, build_params, defaults

# Numeric so Photocurrent's existing timestamp placement rule also applies.
PREVIEW_RUN_ID = "20000101_000000"


def output_spec(params, mode, chain, field_t=None):
    if mode == 0:
        return "vds_sweep", dual_gate_filename_parts(params, chain)
    if mode in (1, 4):
        return "gate_scan", gate_scan_filename_parts(params, chain, field_t)
    if mode == 2:
        return ("map_2d" if params.axis_slow != "None" else "sweep_1d"), map_filename_parts(params, chain)
    return MODES[mode][0], [f"WL{params.wl_start:g}to{params.wl_stop:g}nm", *signal_chain_filename_parts(chain)]


def preview_output(values, mode, save, chain, *, selected_condition=None):
    """Never reserve run IDs, inspect collisions, create folders or touch hardware."""
    def plan(params, index, field_t=None):
        measurement, parts = output_spec(params, index, chain, field_t)
        return build_planned_output(save, measurement, params.base_name, parts,
                                    run_id=PREVIEW_RUN_ID, freeze=True, create_dir=False)

    def display(path):
        before, marker, after = os.path.basename(path).rpartition(PREVIEW_RUN_ID)
        return before + "{timestamp}" + after if marker else os.path.basename(path)

    params = build_params(values, mode)
    note = "Preview only: {timestamp} and any collision suffix are assigned on Start."
    if mode == 4:
        from app.engine.gate_scan_field_batch import GateScanFieldBatch
        fields = GateScanFieldBatch.parse_fields(values["fields_t"])
        rows = values["conditions"]
        configs = [row for row in rows if row["enabled"]] if rows else [values]
        if not fields or not configs:
            raise ValueError("Add B-field targets and enable at least one scan condition")
        examples = []
        if selected_condition is not None:
            selected = rows[selected_condition]
            examples_iter = [(selected, fields[0])]
            name = selected.get("name") or f"Condition {selected_condition + 1}"
            note += f" Example: {name}, first field target."
            if not selected.get("enabled", True):
                note += " This condition is disabled and is not included in the total."
        else:
            examples_iter = islice(product(configs, fields), 3)
        for row, field_t in examples_iter:
            recipe = build_params({**defaults(4), **row}, 4)
            examples.append(plan(recipe, 4, field_t))
        planned = examples[0]
        csv_paths = [p.csv_path for p in examples]
        count = len(configs) * len(fields)
        metadata, log = planned.metadata_path, planned.log_path
        note += " Each job has metadata and a run log; the batch also writes series files."
    elif mode == 5:
        from app.engine.bfield_transport_sweep import (
            enabled_conditions, transport_output_summary_parts, build_transport_output_paths)
        conditions = enabled_conditions(params.conditions, ratio=params.ratio, ratio_target=params.ratio_target)
        planned = build_planned_output(save, "bfield_transport", params.base_name,
            transport_output_summary_parts(params, chain), run_id=PREVIEW_RUN_ID, freeze=True)
        paths = build_transport_output_paths(planned, conditions)
        csv_paths, count = paths.condition_csv_paths[:3], len(paths.condition_csv_paths)
        metadata, log = paths.manifest_path, paths.log_path
        note += " The series also writes a checkpoint."
    else:
        planned = plan(params, mode)
        metadata, log = planned.metadata_path, planned.log_path
        if mode == 3:
            from app.workers.photocurrent import PhotocurrentWorker
            conditions = [c for c in params.bias_conditions if c.enabled]
            if not conditions:
                raise ValueError("Enable at least one photocurrent condition")
            csv_paths = PhotocurrentWorker.condition_csv_paths(planned.csv_path, conditions[:3], params.use_vds)
            count = len(conditions)
        else:
            csv_paths, count = [planned.csv_path], 1
    return dict(csvNames=[display(path) for path in csv_paths], csvCount=count,
                folder=planned.output_dir, metadata=display(metadata), log=display(log), note=note, error="")
