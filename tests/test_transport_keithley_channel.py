import csv
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from instruments.Keithley import Keithley2400VoltMode
from app.engine.bfield_transport_controller import BFieldTransportController
from app.engine.bfield2100_transport_controller import BField2100TransportController
from app.engine.bfield_transport_sweep import TransportCsvWriter
from app.models import BFieldTransportCondition
from app.quick.measurements import MeasurementService
from app.quick.plot import PlotModel


class TransportKeithleyTests(unittest.TestCase):
    def setUp(self):
        with patch('instruments.instrument.pyvisa.ResourceManager', return_value=NS()):
            self.smu = Keithley2400VoltMode(test_mode=True)
        self.smu._query = Mock(return_value='0.1,-2.5e-9')
        self.condition = BFieldTransportCondition(vds_source='Keithley 2400')
        self.context = NS(_check_io_cancel=lambda: None,
                          device_manager=NS(get_session=lambda key: self.smu if key == 'g3' else None))

    def read(self, engine=BFieldTransportController):
        return engine._read_sample(self.context, {'B_measured_T': .1}, self.condition)

    def test_both_backends_publish_standard_channel_from_one_real_driver_read(self):
        for engine in (BFieldTransportController, BField2100TransportController):
            with self.subTest(engine=engine.__name__):
                self.smu._query.reset_mock()
                row = self.read(engine)
                self.assertEqual(row['Ids_Keithley'], -2.5e-9)
                self.assertEqual(row['Keithley_current'], row['Ids_Keithley'])
                self.smu._query.assert_called_once_with('READ?')

    def test_csv_saves_standard_and_legacy_names(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'transport.csv'
            with TransportCsvWriter(str(path), self.condition) as writer:
                writer.write(self.read())
            with path.open(newline='', encoding='utf-8') as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(float(row['Ids_Keithley']), -2.5e-9)
            self.assertEqual(row['Ids_Keithley'], row['Keithley_current'])

    def test_quick_record_produces_visible_keithley_trace(self):
        plot = PlotModel()
        plot.beginRun({'x_axis': 'B-field', 'channels': ['Ids_Keithley'], 'plot_choice': 'Ids_Keithley'})
        port = NS(recordReady=NS(emit=plot.appendRecord))
        MeasurementService.accept_transport_record(port, self.read())
        traces = list(plot.traces('Ids_Keithley'))
        self.assertEqual(len(traces), 1)
        self.assertEqual(list(traces[0][1].values), [.1])
        self.assertEqual(list(traces[0][2].values), [-2.5e-9])

    def test_read_failure_keeps_error_without_fabricating_current(self):
        self.smu._query.side_effect = RuntimeError('read timeout')
        row = self.read()
        self.assertEqual(row['Keithley_read_error'], 'read timeout')
        self.assertNotIn('Ids_Keithley', row)
        self.assertNotIn('Keithley_current', row)


if __name__ == '__main__':
    unittest.main()
