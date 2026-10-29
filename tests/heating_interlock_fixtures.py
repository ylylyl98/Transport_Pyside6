"""Explicit safe observations for offline controller tests."""
from app.sample_heating_interlock import SampleHeatingInterlock
from app.devices.aps100_attodry1000_adapter import MockAPS100Adapter
from utils.config import cfg
from dataclasses import replace


def heater_off_state():
    return dict(channel=cfg.lakeshore335.sample_channel, control_mode=1,
                range=0, heater_output=0., ramp_enabled=False)


def zero_magnet_snapshot():
    snapshot = MockAPS100Adapter().read_snapshot()
    return replace(snapshot, heater_on=False, heater_state=0, sweep_state='standby',
                   status=replace(snapshot.status, raw=2, standby=True))


def safe_interlock(**kwargs):
    gate = SampleHeatingInterlock(**kwargs)
    gate.note_magnet(zero_magnet_snapshot())
    gate.note_control(heater_off_state())
    return gate
