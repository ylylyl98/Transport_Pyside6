"""Exercise the real connection handshake with isolated VISA output queues."""
import unittest
from collections import deque
from unittest.mock import patch

import pyvisa
from pyvisa.constants import InterfaceType, StatusCode

from instruments.SR830 import SRSLockin
from instruments.instrument import InstrumentError


class QueuedSRSResource:
    def __init__(self, model="SR850", *, stale=(), replies=None, interface=InterfaceType.gpib):
        self.identity = f"Stanford_Research_Systems,{model},s/n00111,ver1.000"
        self.interface_type = interface
        self.selected_output = None
        self.queue = deque(stale)
        self.replies = deque(replies or [])
        self.events = []
        self.closed = False
        self.timeout = 5000
        self.read_termination = None
        self.write_termination = None

    def clear(self):
        self.events.append(("clear",))
        self.queue.clear()

    def write(self, command):
        self.events.append(("write", command))
        if command.startswith("OUTX "):
            self.selected_output = int(command.split()[1])

    def query(self, command):
        self.events.append(("query", command))
        self.assert_query(command)
        required_output = 0 if self.interface_type == InterfaceType.asrl else 1
        if self.selected_output == required_output:
            response = self.replies.popleft() if self.replies else self.identity
            if isinstance(response, Exception):
                raise response
            self.queue.append(response)
        if not self.queue:
            raise pyvisa.errors.VisaIOError(StatusCode.error_timeout)
        return self.queue.popleft()

    @staticmethod
    def assert_query(command):
        if command != "*IDN?":
            raise AssertionError(f"Unexpected query during connect: {command}")

    def close(self):
        self.events.append(("close",))
        self.closed = True


class ConnectionTests(unittest.TestCase):
    def driver(self, resource, address="GPIB1::09::INSTR", name="lockin_drive", expected=None):
        class Manager:
            def open_resource(self, _address, timeout):
                resource.timeout = timeout
                return resource
        with patch("instruments.instrument.pyvisa.ResourceManager", return_value=Manager()):
            driver = SRSLockin(name, address, expected_model=expected)
        self.addCleanup(lambda: driver.close() if driver.is_connected() else None)
        return driver

    def test_both_gpib_addresses_connect_with_wrong_reply_port_and_stale_zero(self):
        for model, name, address in (("SR850", "lockin", "GPIB1::08::INSTR"),
                                     ("SR850", "lockin_drive", "GPIB1::09::INSTR"),
                                     ("SR830", "lockin", "GPIB1::08::INSTR")):
            with self.subTest(model=model, address=address):
                resource = QueuedSRSResource(model, stale=("0", "0"))
                driver = self.driver(resource, address, name)
                self.assertIs(driver.connect(), driver)
                self.assertEqual(driver.identity, resource.identity)
                self.assertEqual(driver.model, model)
                self.assertEqual(resource.events[:3], [("clear",), ("write", "OUTX 1"), ("query", "*IDN?")])
                self.assertEqual(list(resource.queue), [])
                self.assertEqual([e[1] for e in resource.events if e[0] == "write"], ["OUTX 1", "OVRM 1"])

    def test_late_zero_or_timeout_is_retried_without_changing_measurement_settings(self):
        for first in ("0", "", pyvisa.errors.VisaIOError(StatusCode.error_timeout)):
            with self.subTest(first=first):
                resource = QueuedSRSResource(replies=[first])
                resource.selected_output = 1
                driver = self.driver(resource)
                driver.connect()
                self.assertEqual(driver.model, "SR850")
                self.assertEqual(resource.events.count(("query", "*IDN?")), 2)
                self.assertEqual(resource.events.count(("clear",)), 2)
                self.assertEqual([e[1] for e in resource.events if e[0] == "write"], ["OUTX 1", "OUTX 1", "OVRM 1"])

    def test_repeated_zero_is_not_accepted_and_reports_role_address_and_reply(self):
        resource = QueuedSRSResource(replies=["0"] * 10)
        resource.selected_output = 1
        driver = self.driver(resource)
        with self.assertRaises(InstrumentError) as raised:
            driver.connect()
        message = str(raised.exception)
        for text in ("lockin_drive", "GPIB1::09::INSTR", "*IDN?", "'0'"):
            self.assertIn(text, message)
        self.assertEqual(resource.events.count(("query", "*IDN?")), 3)
        self.assertTrue(resource.closed)
        self.assertFalse(driver.is_connected())
        self.assertEqual(driver.model, "")
        self.assertNotIn(("write", "OVRM 1"), resource.events)

    def test_real_unsupported_identity_and_expected_model_mismatch_fail_closed(self):
        for model, expected in (("SR860", None), ("SR850", "SR830")):
            with self.subTest(model=model, expected=expected):
                resource = QueuedSRSResource(model)
                resource.selected_output = 1
                driver = self.driver(resource, expected=expected)
                with self.assertRaises(InstrumentError) as raised:
                    driver.connect()
                self.assertIn(model, str(raised.exception))
                self.assertEqual(resource.events.count(("query", "*IDN?")), 1)
                self.assertTrue(resource.closed)
                self.assertEqual(driver.model, "")
                self.assertNotIn(("write", "OVRM 1"), resource.events)

    def test_failed_reconnect_does_not_keep_previous_model(self):
        resource = QueuedSRSResource()
        resource.selected_output = 1
        driver = self.driver(resource)
        driver.connect()
        driver.close()
        resource.replies.extend(["0"] * 10)
        with self.assertRaises(InstrumentError):
            driver.connect()
        self.assertEqual(driver.model, "")
        self.assertEqual(driver.identity, "")

    def test_serial_uses_serial_reply_port_and_carriage_return(self):
        resource = QueuedSRSResource(interface=InterfaceType.asrl)
        driver = self.driver(resource, "ASRL3::INSTR")
        driver.connect()
        self.assertEqual(resource.selected_output, 0)
        self.assertEqual(resource.read_termination, "\r")
        self.assertEqual(driver.model, "SR850")

    def test_non_timeout_transport_failure_is_not_retried(self):
        resource = QueuedSRSResource(replies=[pyvisa.errors.VisaIOError(StatusCode.error_connection_lost)])
        resource.selected_output = 1
        driver = self.driver(resource)
        with self.assertRaises(InstrumentError) as raised:
            driver.connect()
        self.assertIn("GPIB1::09::INSTR", str(raised.exception))
        self.assertEqual(resource.events.count(("query", "*IDN?")), 1)
        self.assertTrue(resource.closed)

    def test_continuous_timeout_stops_after_three_queries_and_closes_session(self):
        resource = QueuedSRSResource(replies=[pyvisa.errors.VisaIOError(StatusCode.error_timeout)] * 10)
        driver = self.driver(resource)
        with self.assertRaises(InstrumentError) as raised:
            driver.connect()
        self.assertIn("GPIB1::09::INSTR", str(raised.exception))
        self.assertIn("VI_ERROR_TMO", str(raised.exception))
        self.assertEqual(resource.events.count(("query", "*IDN?")), 3)
        self.assertTrue(resource.closed)
        self.assertTrue(driver._control_state_uncertain)

    def test_unsupported_clear_is_tolerated_but_actual_clear_transport_errors_are_not(self):
        for code in (StatusCode.error_nonsupported_operation, StatusCode.error_connection_lost):
            with self.subTest(code=code):
                resource = QueuedSRSResource()
                def failed_clear():
                    raise pyvisa.errors.VisaIOError(code)
                resource.clear = failed_clear
                driver = self.driver(resource)
                if code == StatusCode.error_nonsupported_operation:
                    driver.connect()
                    self.assertEqual(driver.model, "SR850")
                else:
                    with self.assertRaises(InstrumentError):
                        driver.connect()
                    self.assertNotIn(("query", "*IDN?"), resource.events)
                    self.assertTrue(resource.closed)


if __name__ == "__main__":
    unittest.main()
