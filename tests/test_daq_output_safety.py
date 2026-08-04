from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtWidgets

from app.device_manager import DeviceManager, ManualControlWorker
from app.models import Connections
from app.ui.dock import ConnDock
from instruments.DaqCard import DaqCard
from instruments.instrument import InstrumentError


class FakeChannel:
    def __init__(self, maximum: float):
        self.ai_max = maximum
        self.ao_max = maximum


class FakeChannelCollection:
    def __init__(self, task, kind: str):
        self.task = task
        self.kind = kind

    def add_ai_voltage_chan(self, address: str, max_val: float):
        self.task.addresses.append(address)
        return FakeChannel(max_val)

    def add_ao_voltage_chan(self, address: str, max_val: float):
        self.task.addresses.append(address)
        return FakeChannel(max_val)


class FakeTask:
    def __init__(self, factory):
        self.factory = factory
        self.addresses: list[str] = []
        self.writes: list[float] = []
        self.closed = False
        self.stop_count = 0
        self.ai_channels = FakeChannelCollection(self, "ai")
        self.ao_channels = FakeChannelCollection(self, "ao")
        self.timing = SimpleNamespace(
            delay_from_samp_clk_delay=0.0,
            delay_from_samp_clk_delay_units=None,
        )

    def read(self):
        return list(self.factory.readback)

    def write(self, value):
        self.writes.append(float(value))

    def stop(self):
        self.stop_count += 1

    def close(self):
        self.closed = True


class FakeTaskFactory:
    def __init__(self, readback):
        self.readback = list(readback)
        self.tasks: list[FakeTask] = []

    def __call__(self):
        task = FakeTask(self)
        self.tasks.append(task)
        return task


class DaqOutputSafetyTests(unittest.TestCase):
    def setUp(self):
        # Four external AI values followed by AO0/AO1 internal readbacks.
        self.factory = FakeTaskFactory([0.0, 0.0, 0.0, 0.0, 0.25, -0.40])
        self.task_patch = patch("instruments.DaqCard.nidaqmx.Task", self.factory)
        self.task_patch.start()
        self.daq = DaqCard(ao_channel_indexes=(0, 1), ai_channel_indexes=(0, 1, 2, 3))

    def tearDown(self):
        self.daq.close()
        self.task_patch.stop()

    def test_connect_adopts_existing_outputs_without_writing(self):
        self.daq.connect()

        self.assertAlmostEqual(self.daq.get_ao_value(0), 0.25)
        self.assertAlmostEqual(self.daq.get_ao_value(1), -0.40)
        self.assertEqual(self.factory.tasks[1].writes, [])
        self.assertEqual(self.factory.tasks[2].writes, [])

    def test_refresh_noise_never_becomes_a_command(self):
        self.daq.connect()
        self.factory.readback[-2:] = [0.253, -0.397]

        self.daq.refresh()

        self.assertAlmostEqual(self.daq.get_ao_value(0), 0.25)
        self.assertAlmostEqual(self.daq.get_ao_value(1), -0.40)
        self.assertAlmostEqual(self.daq.get_ao_vs_gnd_value(0), 0.253)

    def test_setting_one_channel_never_writes_the_unused_channel(self):
        self.daq.connect()

        self.daq.set_voltage(0, 0.30)

        self.assertEqual(self.factory.tasks[1].writes, [0.30])
        self.assertEqual(self.factory.tasks[2].writes, [])
        self.assertAlmostEqual(self.daq.get_ao_value(1), -0.40)

    def test_direct_large_step_is_refused_before_any_write(self):
        self.daq.connect()

        with self.assertRaisesRegex(InstrumentError, "safety limit"):
            self.daq.set_voltage(0, 1.0)

        self.assertEqual(self.factory.tasks[1].writes, [])
        self.assertEqual(self.factory.tasks[2].writes, [])

    def test_invalid_ramp_target_is_refused_before_any_write(self):
        self.daq.connect()

        with self.assertRaisesRegex(InstrumentError, "exceeds"):
            self.daq.ramp_voltage(0, 11.0, step=0.05)
        with self.assertRaisesRegex(InstrumentError, "finite"):
            self.daq.ramp_voltage(0, float("nan"), step=0.05)

        self.assertEqual(self.factory.tasks[1].writes, [])
        self.assertEqual(self.factory.tasks[2].writes, [])

    def test_ramp_clamps_every_step_and_leaves_other_channel_untouched(self):
        self.daq.connect()

        self.daq.ramp_voltage(0, 0.41, step=1.0)

        writes = self.factory.tasks[1].writes
        points = [0.25, *writes]
        self.assertTrue(writes)
        self.assertAlmostEqual(writes[-1], 0.41)
        self.assertTrue(all(abs(b - a) <= 0.050000001 for a, b in zip(points, points[1:])))
        self.assertEqual(self.factory.tasks[2].writes, [])

    def test_ramp_resamples_selected_channel_if_hardware_changed_externally(self):
        self.daq.connect()
        self.factory.readback[-2:] = [0.60, -0.40]

        self.daq.ramp_voltage(0, 0.50, step=0.05)

        writes = self.factory.tasks[1].writes
        self.assertEqual(len(writes), 2)
        self.assertAlmostEqual(writes[0], 0.55)
        self.assertAlmostEqual(writes[1], 0.50)
        self.assertEqual(self.factory.tasks[2].writes, [])


class FakeDaqSession:
    ao_channel_indexes = [0, 1]

    def __init__(self):
        self.values = {0: 0.20, 1: -0.30}
        self.writes: list[tuple[int, float]] = []
        self.closed = False

    def get_ao_value(self, index):
        return self.values[index]

    def set_voltage(self, index, value):
        self.writes.append((index, float(value)))
        self.values[index] = float(value)

    def acquire(self):
        return {}

    def close(self):
        self.closed = True

    def get_ao_state(self, index):
        return {
            "commanded_v": self.values[index],
            "measured_v": self.values[index],
            "initial_v": 0.20 if index == 0 else -0.30,
        }


class DaqOutputIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_manual_worker_safely_ramps_only_selected_channel(self):
        session = FakeDaqSession()
        worker = ManualControlWorker("daq_ao0", session, 0.0)

        with patch("app.utils.time.sleep"):
            worker.run()

        self.assertTrue(worker.success)
        self.assertAlmostEqual(session.values[0], 0.0)
        self.assertAlmostEqual(session.values[1], -0.30)
        self.assertTrue(all(index == 0 for index, _value in session.writes))
        points = [0.20, *(value for _index, value in session.writes)]
        self.assertTrue(all(abs(b - a) <= 0.050000001 for a, b in zip(points, points[1:])))
        self.assertIn("other AO channels were unchanged", worker.message)

    def test_connection_detail_warns_that_existing_output_was_preserved(self):
        session = FakeDaqSession()

        detail = DeviceManager._daq_detail(session)

        self.assertIn("Existing DAQ output detected and preserved", detail)
        self.assertIn("AO0=+0.2 V", detail)
        self.assertIn("AO1=-0.3 V", detail)

    def test_normal_disconnect_does_not_zero_or_rewrite_daq_outputs(self):
        class Emitter:
            def emit(self, *_args):
                pass

        session = FakeDaqSession()
        manager = DeviceManager(Connections())
        manager.sessions = {
            "g1": None,
            "g2": None,
            "g3": None,
            "daq": session,
            "mono": None,
            "lockin": None,
        }
        manager._disconnect_all_in_thread(Emitter())

        self.assertEqual(session.writes, [])
        self.assertTrue(session.closed)

    def test_setup_panel_shows_preserved_voltage_and_enables_safe_actions(self):
        session = FakeDaqSession()
        manager = DeviceManager(Connections())
        manager.sessions["daq"] = session
        manager.states["daq"] = "ok"
        manager.details["daq"] = manager._daq_detail(session)

        dock = ConnDock(manager)
        ao0_spin, ao0_ramp, ao0_zero, ao0_state = dock._manual_daq_controls[0]
        ao1_spin, _ao1_ramp, _ao1_zero, ao1_state = dock._manual_daq_controls[1]

        self.assertAlmostEqual(ao0_spin.value(), 0.20)
        self.assertAlmostEqual(ao1_spin.value(), -0.30)
        self.assertIn("Held +0.200000 V", ao0_state.text())
        self.assertIn("Held -0.300000 V", ao1_state.text())
        self.assertEqual(dock.lbl_daq_manual_hint.property("role"), "warning-hint")
        self.assertTrue(ao0_ramp.isEnabled())
        self.assertTrue(ao0_zero.isEnabled())
        dock.close()


if __name__ == "__main__":
    unittest.main()
