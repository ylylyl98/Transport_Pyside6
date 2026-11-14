"""DAQ averaging cadence without instrument access or real delays."""
import threading
import unittest
from types import SimpleNamespace as NS

from app.drag_drive import acquire_current_sample
from app.engine.bfield_transport_controller import BFieldTransportController
from app.engine.bfield_transport_sweep import TransportSweepPlan
from app.models import BFieldTransportCondition, BFieldTransportParams


class ReadDelayTests(unittest.TestCase):
    def sample(self, count, stop_in_wait=False):
        events = []
        class Cancel:
            stopped = False
            def wait(self, delay):
                events.append(('delay', delay))
                self.stopped = stop_in_wait
                return self.stopped
        cancel = Cancel()
        def check():
            if cancel.stopped:
                raise RuntimeError('Transport operation cancelled')
        class Daq:
            reads = 0
            def acquire(self):
                self.reads += 1
                events.append(('read', self.reads))
                return [self.reads, 2*self.reads, 3*self.reads]
        daq = Daq()
        p = BFieldTransportParams(averages=count, read_delay_s=.2)
        c = NS(plan=NS(params=p), _io_cancel=cancel, _check_io_cancel=check,
               _calibration=(1., 1.), _signal_chain=None,
               device_manager=NS(get_session=lambda name: daq if name == 'daq' else None))
        return c, events, BFieldTransportCondition(vds_source='None')

    def test_five_reads_have_four_delays_and_correct_mean_in_both_modes(self):
        for mode in ('continuous', 'stepped'):
            with self.subTest(mode=mode):
                c, events, condition = self.sample(5)
                c.plan.params.measurement_mode = mode
                row = BFieldTransportController._read_sample(c, {}, condition)
                self.assertEqual(events, [('read', 1), ('delay', .2), ('read', 2),
                    ('delay', .2), ('read', 3), ('delay', .2), ('read', 4), ('delay', .2), ('read', 5)])
                self.assertAlmostEqual(row['raw_X'], 3.)
                self.assertAlmostEqual(row['raw_DC'], 9.)

    def test_one_read_has_no_delay(self):
        c, events, condition = self.sample(1)
        BFieldTransportController._read_sample(c, {}, condition)
        self.assertEqual(events, [('read', 1)])

    def test_stop_during_delay_prevents_next_read(self):
        c, events, condition = self.sample(5, stop_in_wait=True)
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            BFieldTransportController._read_sample(c, {}, condition)
        self.assertEqual(events, [('read', 1), ('delay', .2)])

    def test_actual_cancel_event_interrupts_long_delay(self):
        c, _, condition = self.sample(5)
        c._io_cancel = threading.Event()
        waiting = threading.Event()
        real_wait = c._io_cancel.wait
        c.plan.params.read_delay_s = 60.
        def wait(delay):
            waiting.set()
            return real_wait(delay)
        c._io_cancel.wait = wait
        c._check_io_cancel = lambda: BFieldTransportController._check_io_cancel(c)
        errors = []
        def acquire():
            try:
                BFieldTransportController._read_sample(c, {}, condition)
            except RuntimeError as exc:
                errors.append(str(exc))
        worker = threading.Thread(target=acquire, daemon=True)
        worker.start()
        try:
            self.assertTrue(waiting.wait(1.))
        finally:
            c._io_cancel.set()
            worker.join(1.)
        self.assertFalse(worker.is_alive())
        self.assertIn('cancelled', errors[0])

    def test_shared_acquisition_keeps_existing_no_delay_default(self):
        reads = []
        daq = NS(acquire=lambda: reads.append(1) or [1., 2., 3.])
        row = acquire_current_sample(daq, 3, 1., 1.)
        self.assertEqual(len(reads), 3)
        self.assertEqual(row['raw_X'], 1.)

    def test_delay_budget_and_invalid_settings(self):
        c, _, _ = self.sample(5)
        c.plan.params.read_delay_s = 100.
        self.assertGreaterEqual(BFieldTransportController._acquisition_timeout_s(c), 580.)
        for delay in (-.1, float('nan'), float('inf')):
            for backend in ('1000', '2100'):
                with self.subTest(delay=delay, backend=backend), self.assertRaisesRegex(ValueError, 'DAQ read delay'):
                    TransportSweepPlan.from_params(BFieldTransportParams(read_delay_s=delay, magnet_backend=backend))


if __name__ == '__main__':
    unittest.main()
