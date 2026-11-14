"""Optional preparation waits for continuous APS100 transport branches."""
import math
import time

from app.engine.transport_stability import TemperatureWindow
from app.thermal_safety import _sensor_status_is_clear
from utils.config import cfg
from app.engine.field_sampling import FIELD_SAMPLE_COLUMNS

CONTINUOUS_COLUMNS = ("B_readback_monotonic_s", "Observed_sweep_rate_Oe_per_s", "Continuous_segment",
                      "Field_approach", "Field_approach_phase", "Endpoint_approach") + FIELD_SAMPLE_COLUMNS


class ContinuousTransportMixin:
    def _approach_timeout_s(self):
        expected = self._expected_leg_duration_s()
        # Nonlinear stages reduce the programmed rate and require acknowledged
        # pauses. The worker separately enforces fresh progress at every stage.
        return max(3600., expected * (8. if self._field_approach() != "linear" else 2.) + 300.)

    def _continuous_before_leg(self, leg, target):
        if getattr(self, "_continuous_wait_bypass", False):
            self._continuous_wait_bypass = False
            return False
        p = self.plan.params
        resuming = getattr(self, "_continuous_resume_pending", False)
        self._continuous_resume_pending = False
        delay = p.pre_sweep_settle_s + (p.turnaround_delay_s if leg and not resuming else 0.)
        interrupted_wait = getattr(self, "_continuous_wait", None) if resuming else None
        if interrupted_wait is not None:
            delay = interrupted_wait["delay"]
        if not resuming and not p.wait_for_temperature and delay <= 0:
            return False
        now = time.monotonic()
        self._leg_index, self._target = int(leg), float(target)
        self._continuous_wait = dict(leg=leg, target=target, started=now, ready=None, delay=delay,
            start_field=None if resuming else (p.start_field_t if leg == 0 else p.stop_field_t),
            window=TemperatureWindow(p, float(cfg.lakeshore335.maximum_reading_age_s)), last_message=None)
        self._measurement_phase = "sweep_wait"
        self._phase_started = now
        self._set_transport_polling(True)
        self._refresh_snapshot()
        self._telemetry_watchdog.start()
        self._checkpoint_runtime("sweep_wait", "Waiting before continuous branch")
        self.state_changed.emit("sweep_wait", "Waiting for stable temperature" if p.wait_for_temperature else "Waiting before ramp")
        return True

    def _continuous_wait_tick(self):
        if self._measurement_phase != "sweep_wait":
            return False
        if not self._active or self._cleanup_in_progress or self._thermal_hold or self._monitor_hold is not None:
            return True
        wait, p, now = self._continuous_wait, self.plan.params, time.monotonic()
        if wait is None:
            return True
        timeout = p.temperature_timeout_s if p.wait_for_temperature else p.field_timeout_s
        if now - wait["started"] >= timeout + wait["delay"]:
            self._fail("Pre-sweep stability wait timed out; no ramp started")
            return True
        permitted, detail = self._thermal_permission()
        if not permitted:
            wait["ready"] = None
            wait["window"].reset()
            if isinstance(detail, str):
                self._fail(detail)
            else:
                self._request_thermal_pause()
            return True
        magnet = self._latest_magnet()
        stamp, field = getattr(magnet, "monotonic_s", None), getattr(magnet, "field_t", None)
        status = getattr(magnet, "status", None)
        stationary = (all(isinstance(v, (int, float)) and math.isfinite(v) for v in (stamp, field))
                      and wait["started"] < stamp <= now and now - stamp <= 3.
                      and getattr(status, "sweep_active", None) is False
                      and not any(getattr(status, k, False) for k in ("quench", "power_module_failure", "faulted"))
                      and getattr(magnet, "heater_on", None) is True)
        if stationary and wait["start_field"] is None:
            lo, hi = sorted((p.start_field_t, p.stop_field_t))
            if self._field_approach() == "oscillate":
                from app.engine.field_approach import approach_targets
                from app.engine.bfield_transport_sweep import aps100_transport_field_limit
                origin = p.start_field_t if wait["leg"] == 0 else p.stop_field_t
                path = (origin, wait["target"], *approach_targets(origin, wait["target"],
                    self._endpoint_tolerance_t(), "oscillate", aps100_transport_field_limit()))
                lo, hi = min(path), max(path)
            tolerance = self._position_tolerance_t()
            if not lo-tolerance <= field <= hi+tolerance:
                self._fail("Paused field is outside the continuous sweep range")
                return True
            wait["start_field"] = field
        stationary = stationary and abs(field-wait["start_field"]) <= self._position_tolerance_t()
        ready = stationary
        if p.wait_for_temperature:
            lake = self._latest_lakeshore()
            lake_stamp = getattr(lake, "monotonic_s", None)
            valid = (isinstance(lake_stamp, (int, float)) and lake_stamp > wait["started"]
                     and getattr(lake, "connected", False) and getattr(lake, "communication_valid", False)
                     and _sensor_status_is_clear(getattr(lake, "sample_sensor_status", None)))
            if valid:
                ready = wait["window"].observe(lake_stamp, getattr(lake, "sample_temperature_k", None), now) and ready
            else:
                wait["window"].reset()
                ready = False
        if not ready:
            wait["ready"] = None
            message = "Waiting for stationary start field" if not stationary else "Waiting for stable temperature"
        else:
            if wait["ready"] is None:
                wait["ready"] = now
            remaining = wait["delay"] - (now - wait["ready"])
            if remaining <= 0:
                self._continuous_wait = None
                self._continuous_wait_bypass = True
                self._begin_leg(wait["leg"], wait["target"])
                return True
            message = f"Settling before ramp · {math.ceil(remaining)} s remaining"
        if message != wait["last_message"]:
            wait["last_message"] = message
            self.state_changed.emit("sweep_wait", message)
        return True

    def _note_continuous_interruption(self, reason):
        if not self._is_stepped() and self._measurement_phase == "sweep_wait":
            self._continuous_resume_pending = True
        if not self._is_stepped() and self._measurement_phase in {"starting_sweep", "sweeping"}:
            self._continuous_resume_pending = True
            event = dict(condition=self._condition_index + 1, leg=self._leg_index,
                         reason=str(reason), monotonic_s=time.monotonic())
            self.plan.validation.setdefault("continuous_interruptions", []).append(event)
            self._log("Continuous branch interrupted: " + str(reason) + "; resumed data is not an uninterrupted ramp")

    def _continuous_sample_metadata(self, snapshot):
        """Signed finite-difference rate between acquired APS field readbacks."""
        stamp = getattr(snapshot, "field_monotonic_s", None)
        if stamp is None:
            stamp = getattr(snapshot, "monotonic_s", None)
        field = getattr(snapshot, "field_t", None)
        previous = getattr(self, "_continuous_previous_readback", None)
        observed = None
        if all(isinstance(v, (int, float)) and math.isfinite(v) for v in (stamp, field)):
            if previous is not None and stamp > previous[0]:
                observed = (field-previous[1]) * 10000. / (stamp-previous[0])
            if previous is None or stamp > previous[0]:
                self._continuous_previous_readback = (stamp, field)
        else:
            stamp = None
            self._continuous_previous_readback = None
        phase = getattr(snapshot, "approach_phase", "") or "ramp"
        return dict(B_readback_monotonic_s=stamp, Observed_sweep_rate_Oe_per_s=observed,
                    Continuous_segment=getattr(self, "_continuous_segment", 0),
                    Field_approach=self._field_approach(), Field_approach_phase=phase,
                    Endpoint_approach=phase in {"approach", "oscillate", "settled"})

    def _verify_continuous_rates(self, rates, coil):
        """Reject a branch whose stored range limits would silently slow it."""
        if self._is_stepped() or self._field_approach() != "linear":
            return
        p = self.plan.params
        low = 0. if p.start_field_t * p.stop_field_t <= 0 else min(abs(p.start_field_t), abs(p.stop_field_t))
        high = max(abs(p.start_field_t), abs(p.stop_field_t))
        unit = float(coil) * 60. * .0001
        requested = math.floor(p.rate_t_per_min / unit + 1e-10) * unit
        previous = 0.
        relevant = []
        for index in sorted(rates):
            boundary, rate = rates[index]
            if boundary * coil >= low and previous * coil < high:
                relevant.append(float(rate))
            previous = boundary
        if not relevant or any(abs(math.floor(rate / unit + 1e-10) * unit-requested) > 1e-9 for rate in relevant):
            allowed = min(relevant) * 10000. / 60. if relevant else 0.
            raise ValueError(f"Continuous rate is limited by APS100 ranges. Set at most {allowed:g} Oe/s and restart, or use Stepped.")
        self.plan.validation["continuous_rate_oe_per_s"] = requested * 10000. / 60.
