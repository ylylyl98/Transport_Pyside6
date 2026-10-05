"""Verified SRS front-panel transactions; never retry a setting write."""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone

import pyvisa


AUTO_COMMANDS = {
    'auto_phase': 'APHS', 'auto_gain': 'AGAN', 'auto_reserve': 'ARSV',
    'auto_offset_x': 'AOFF 1', 'auto_offset_y': 'AOFF 2',
    'auto_offset_r': 'AOFF 3', 'auto_scale': 'ASCL',
}


def setting_matches(key, requested, actual, model):
    """Readback equivalence, including the manual's hardware quantization.

    Do not use this to detect edits: even a one-step phase edit must be sent.
    SR850 manual 6-4/6-5 and SR830 manual 5-4 specify these resolutions.
    """
    if actual is None:
        return False
    if key in ('scale', 'center'):
        # Trace units can be fA; an absolute voltage-sized tolerance is invalid.
        return math.isclose(float(requested), float(actual), rel_tol=1e-5, abs_tol=0.)
    if key in ('phase_deg', 'frequency_hz', 'sine_out_v'):
        requested, actual = float(requested), float(actual)
        if not math.isfinite(requested) or not math.isfinite(actual):
            return False
        difference = abs(requested - actual)
        if key == 'phase_deg':
            difference = abs(math.remainder(requested - actual, 360.))
            quantum = .001 if model == 'SR850' else .01
        elif key == 'frequency_hz':
            if requested <= 0:
                return False
            quantum = max(.0001, 10. ** (math.floor(math.log10(requested)) - 4))
        else:
            quantum = .002
        return difference <= quantum / 2 + 4 * max(math.ulp(requested), math.ulp(actual))
    return requested == actual


class SRSControlMixin:
    def _query(self, command, **kwargs):
        return self._trace_io('query', command, lambda: super(SRSControlMixin, self)._query(command, **kwargs))

    def _write(self, command, **kwargs):
        return self._trace_io('write', command, lambda: super(SRSControlMixin, self)._write(command, **kwargs))

    def _trace_io(self, kind, command, operation):
        entry = {'kind': kind, 'command': command, 'at': datetime.now(timezone.utc).isoformat()}
        try:
            result = operation()
            if kind in ('query', 'serial_poll'):
                entry['response'] = str(result).strip()
            return result
        except Exception as ex:
            entry['error'] = str(ex)
            raise
        finally:
            trace = getattr(self, '_control_trace', None)
            if trace is not None:
                trace.append(entry)

    def wait_command_complete(self, timeout=15., poll_interval=.1, progress=None, cancelled=None):
        """Poll IFC out of band on GPIB; a text reply is a completion barrier.

        SR850 6-2 / SR830 5-2: only GPIB serial polls run during a command.
        A *STB? reply already means preceding commands finished, even if the
        query itself makes IFC read as zero. Do not poll that text reply bit.
        """
        resource = self._my_instr
        serial_poll = resource.interface_type == pyvisa.constants.InterfaceType.gpib
        original_timeout = resource.timeout
        deadline = time.monotonic() + timeout
        last_progress = 0.
        try:
            while True:
                if cancelled and cancelled():
                    raise RuntimeError('Waiting cancelled; the instrument command may still be running.')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Command completion timed out; instrument state is unverified. Refresh before continuing.')
                # Bound each I/O by the remaining operation time. Text queries
                # may wait for a long Auto command, beyond the normal 5 s limit.
                remaining_ms = max(1, math.ceil(remaining * 1000))
                resource.timeout = min(original_timeout, remaining_ms) if serial_poll else remaining_ms
                if not serial_poll:
                    self._query_index('*STB?')
                    return
                try:
                    status = self._trace_io('serial_poll', 'VISA read_stb', resource.read_stb)
                except pyvisa.errors.VisaIOError as ex:
                    if ex.error_code != pyvisa.constants.StatusCode.error_nonsupported_operation:
                        raise
                    serial_poll = False
                    continue
                if status & 0x02:
                    return
                now = time.monotonic()
                if progress and now - last_progress >= 1.:
                    progress('Waiting for the instrument to finish…')
                    last_progress = now
                time.sleep(min(poll_interval, max(0., deadline - now)))
        finally:
            resource.timeout = original_timeout

    def read_display(self):
        if self.model != 'SR850':
            return {}
        mode = self._query_index('SMOD?')
        pane = self._query_index('ADSP?')
        visible = pane == 0 if mode == 0 else pane in (1, 2)
        kind = self._query_index(f'DTYP? {pane}')
        result = {'pane': pane, 'type': kind, 'can_scale': visible and kind in (2, 3)}
        if result['can_scale']:
            result.update(scale=self._query_float(f'DSCL? {pane}'),
                          center=self._query_float(f'DOFF? {pane}'))
        return result

    def run_control(self, action, payload=None, *, timeout=None, poll_interval=.1,
                    progress=None, cancelled=None):
        """Serialize write, completion check, status check and readback as one operation.

        On rejection retain any successfully read actual values in the result.
        On transport/timeout errors do not invent or publish a fresh readback.
        """
        requested = dict(payload or {})
        data = {}
        control = {'action': action, 'requested': requested, 'ok': False,
                   'verified_keys': [], 'model': self.model, 'address': self.address,
                   'started_at': datetime.now(timezone.utc).isoformat(), 'trace': []}
        with self.lock:
            self._control_state_uncertain = True
            self._control_trace = control['trace']
            try:
                self.wait_command_complete(timeout=timeout if timeout is not None else 15.,
                                           poll_interval=poll_interval, progress=progress, cancelled=cancelled)
                # ESR is read-to-clear; preserve the pre-existing status in the log.
                control['prior_esr'] = self._query_index('*ESR?')
                before = self.read_settings()
                if action == 'apply':
                    changes = {k: v for k, v in requested.items() if v != before.get(k)}
                    if 'frequency_hz' in changes:
                        reference = changes.get('ref_source', before['ref_source'])
                        if reference != self.capabilities['internal_reference_code']:
                            raise ValueError('Fixed frequency requires Internal reference.')
                    if 'reserve_level' in changes and changes.get('reserve', before['reserve']) != 1:
                        raise ValueError('SR850 reserve level requires Manual reserve mode.')
                    self.apply_settings(changes)
                elif action == 'apply_display':
                    display = self.read_display()
                    if not display.get('can_scale') or display['pane'] != requested.get('pane'):
                        raise ValueError('Active display changed or is not Bar/Chart. Refresh the panel.')
                    for key in ('scale', 'center'):
                        if key in requested and (not math.isfinite(float(requested[key])) or
                                               (key == 'scale' and not 1e-18 < float(requested[key]) < 1e18)):
                            raise ValueError('Display scale must be between 1e-18 and 1e18; scale and center must be finite.')
                    for key, command in (('scale', 'DSCL'), ('center', 'DOFF')):
                        if key in requested:
                            self._write(f'{command} {display["pane"]},{float(requested[key]):.8g}')
                elif action in AUTO_COMMANDS:
                    if action == 'auto_gain' and self.model == 'SR830' and before['time_constant'] > 10:
                        raise ValueError('SR830 Auto Gain requires time constant <= 1 s.')
                    if action == 'auto_scale' and not self.read_display().get('can_scale'):
                        raise ValueError('Auto Scale requires SR850 with an active Bar or Chart display.')
                    self._write(AUTO_COMMANDS[action])
                elif action != 'refresh':
                    raise ValueError(f'Unsupported lock-in action: {action}')
                tc_index = before['time_constant']
                tc_seconds = (1 if tc_index % 2 == 0 else 3) * 10 ** (tc_index // 2 - 5)
                operation_timeout = (max(15., min(300., 20 * tc_seconds))
                                     if action in AUTO_COMMANDS else 15.)
                self.wait_command_complete(timeout=timeout if timeout is not None else operation_timeout,
                                           poll_interval=poll_interval, progress=progress, cancelled=cancelled)
                control['esr'] = self._query_index('*ESR?')
                data = self.read_front_panel()
                self._control_state_uncertain = False
                data['display'] = self.read_display()
                if control['esr'] & 0x30:
                    raise RuntimeError(f'Instrument rejected a command (ESR={control["esr"]}: command/execution error).')
                if action in ('apply', 'apply_display'):
                    actual = data['settings'] if action == 'apply' else data['display']
                    mismatches = []
                    for key, value in requested.items():
                        if setting_matches(key, value, actual.get(key), self.model):
                            control['verified_keys'].append(key)
                        else:
                            mismatches.append(f'{key}: requested {value}, read back {actual.get(key)}')
                    if mismatches:
                        raise RuntimeError('Readback mismatch: ' + '; '.join(mismatches))
                control['ok'] = True
            except Exception as ex:
                control['error'] = str(ex)
            finally:
                self._control_trace = None
        data['control'] = control
        return data
