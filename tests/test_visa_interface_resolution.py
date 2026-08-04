from __future__ import annotations

import unittest
from unittest.mock import patch

import pyvisa

from instruments.instrument import InstrumentError, PyvisaInstrument
from instruments.visa_resources import alternate_gpib_resources, resolve_gpib_resource


INTERFACE_NOT_CONFIGURED = -1073807195


class FakeVisaResource:
    def __init__(self):
        self.read_termination = None
        self.write_termination = None
        self.timeout = None


class FakeResourceManager:
    def __init__(self, resources=(), failures=None):
        self.resources = tuple(resources)
        self.failures = dict(failures or {})
        self.open_calls: list[tuple[str, float]] = []
        self.opened = FakeVisaResource()

    def list_resources(self):
        return self.resources

    def open_resource(self, address, timeout):
        self.open_calls.append((address, timeout))
        failure = self.failures.get(address)
        if failure is not None:
            raise failure
        return self.opened


class GpibResourceResolutionTests(unittest.TestCase):
    def test_exact_resource_wins(self):
        resources = ("GPIB0::23::INSTR", "GPIB2::23::INSTR")
        self.assertEqual(resolve_gpib_resource("GPIB0::23::INSTR", resources), "GPIB0::23::INSTR")

    def test_unique_same_device_on_new_interface_is_resolved(self):
        resources = ("GPIB0::23::INSTR", "GPIB0::8::INSTR", "ASRL1::INSTR")
        self.assertEqual(
            alternate_gpib_resources("GPIB1::23::INSTR", resources),
            ("GPIB0::23::INSTR",),
        )
        self.assertEqual(resolve_gpib_resource("GPIB1::23::INSTR", resources), "GPIB0::23::INSTR")

    def test_different_primary_address_is_not_substituted(self):
        configured = "GPIB1::23::INSTR"
        self.assertEqual(resolve_gpib_resource(configured, ("GPIB0::24::INSTR",)), configured)

    def test_ambiguous_interface_matches_are_not_substituted(self):
        configured = "GPIB1::23::INSTR"
        resources = ("GPIB0::23::INSTR", "GPIB2::23::INSTR")
        self.assertEqual(resolve_gpib_resource(configured, resources), configured)


class PyvisaInterfaceRecoveryTests(unittest.TestCase):
    @staticmethod
    def _make_driver(resource_manager):
        with patch("instruments.instrument.pyvisa.ResourceManager", return_value=resource_manager):
            return PyvisaInstrument(
                name="gate 1",
                address="GPIB1::23::INSTR",
                termination="\n",
                timeout=4321,
            )

    def test_connect_retries_unique_same_device_on_configured_interface(self):
        manager = FakeResourceManager(
            resources=("GPIB0::23::INSTR", "GPIB0::8::INSTR"),
            failures={
                "GPIB1::23::INSTR": pyvisa.errors.VisaIOError(INTERFACE_NOT_CONFIGURED),
            },
        )
        driver = self._make_driver(manager)

        result = driver.connect()

        self.assertIs(result, driver)
        self.assertEqual(
            manager.open_calls,
            [("GPIB1::23::INSTR", 4321), ("GPIB0::23::INSTR", 4321)],
        )
        self.assertEqual(driver.requested_address, "GPIB1::23::INSTR")
        self.assertEqual(driver.address, "GPIB0::23::INSTR")
        self.assertEqual(manager.opened.read_termination, "\n")
        self.assertEqual(manager.opened.write_termination, "\n")

    def test_connect_does_not_guess_a_different_device(self):
        manager = FakeResourceManager(
            resources=("GPIB0::24::INSTR",),
            failures={
                "GPIB1::23::INSTR": pyvisa.errors.VisaIOError(INTERFACE_NOT_CONFIGURED),
            },
        )
        driver = self._make_driver(manager)

        with self.assertRaisesRegex(InstrumentError, "GPIB0::24::INSTR"):
            driver.connect()

        self.assertEqual(manager.open_calls, [("GPIB1::23::INSTR", 4321)])

    def test_connect_does_not_choose_between_ambiguous_matches(self):
        manager = FakeResourceManager(
            resources=("GPIB0::23::INSTR", "GPIB2::23::INSTR"),
            failures={
                "GPIB1::23::INSTR": pyvisa.errors.VisaIOError(INTERFACE_NOT_CONFIGURED),
            },
        )
        driver = self._make_driver(manager)

        with self.assertRaisesRegex(InstrumentError, "multiple matching resources"):
            driver.connect()

        self.assertEqual(manager.open_calls, [("GPIB1::23::INSTR", 4321)])

    def test_other_visa_errors_are_not_rewritten(self):
        original = pyvisa.errors.VisaIOError(-1073807339)
        manager = FakeResourceManager(failures={"GPIB1::23::INSTR": original})
        driver = self._make_driver(manager)

        with self.assertRaises(pyvisa.errors.VisaIOError) as raised:
            driver.connect()

        self.assertIs(raised.exception, original)


if __name__ == "__main__":
    unittest.main()
