import unittest
from types import SimpleNamespace as NS

from app.field_thermal_policy import FieldThermalComparison, temperature_ceiling
from app.thermal_safety import ThermalSafetyEvaluator, ThermalState
from utils.config import LakeShore335Config


class FieldThermalPolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = LakeShore335Config()
        self.policy = FieldThermalComparison(self.config)

    def inputs(self, field=-0.4, temp=4.833, now=100):
        t = NS(monotonic_s=now, connected=True, communication_valid=True,
               reservoir_temperature_k=temp, sample_temperature_k=3.8,
               sample_sensor_status="000", reservoir_sensor_status="000")
        m = NS(monotonic_s=now, field_t=field, heater_on=True,
               output_field_t=0, status=NS(quench=False, power_module_failure=False))
        return t, m

    def assess(self, field=-0.4, temp=4.833, now=100, **kwargs):
        return self.policy.evaluate(*self.inputs(field, temp, now), now=now, **kwargs)

    def configured(self):
        # Explicit test-only settings, never production recommendations.
        self.config.field_warning_margin_k = .1
        self.config.field_recovery_hysteresis_k = .1
        self.config.field_recovery_dwell_s = 2
        self.config.field_hold_timeout_s = 20
        self.config.field_maximum_recoveries = 1

    def test_bands_both_polarities_and_invalid_fields(self):
        for field, expected in [(0,5.5),(6,5.5),(6.0001,5),(7,5),(7.0001,4.5),
                                (8,4.5),(8.0001,4.2),(9,4.2)]:
            for sign in (-1,1):
                self.assertEqual(temperature_ceiling(sign*field), expected)
        for field in (9.001,-10,float('nan'),float('inf'),None):
            self.assertIsNone(temperature_ceiling(field))

    def test_exact_temperature_boundaries_are_excluded(self):
        for field,temp in [(0,5.5),(7,5),(8,4.5),(9,4.2)]:
            self.policy.reset()
            self.assertEqual(self.assess(field,temp).state,'FAULT')

    def test_recorded_low_field_event_differs_from_high_field(self):
        self.assertEqual(self.assess().state,'WITHIN_ENVELOPE')
        self.assertEqual(self.assess(8).state,'FAULT')

    def test_future_field_and_zero_crossing_are_checked(self):
        self.assertEqual(self.assess(target_t=8).state,'TRAJECTORY_BLOCKED')
        self.assertEqual(self.assess(field=-8,temp=4.4,target_t=8).trajectory_ceiling_k,4.5)
        self.assertEqual(self.assess(target_t=10).state,'MONITOR_FAULT')

    def test_persistent_field_not_zero_supply_is_used(self):
        t,m = self.inputs(field=8)
        m.heater_on=False
        self.assertEqual(self.policy.evaluate(t,m,now=100).state,'FAULT')

    def test_precharge_is_distinct_and_first_stage_must_be_fresh(self):
        self.assertEqual(self.assess(temp=4.3,phase='precharge').state,'PRECHARGE_BLOCKED')
        self.assertEqual(self.assess(temp=4.1,phase='precharge').state,'PRECHARGE_READY')
        for stage,state in [(64,'PRECHARGE_READY'),(65,'PRECHARGE_BLOCKED')]:
            self.assertEqual(self.assess(temp=4.1,phase='precharge',first_stage_k=stage,
                                        first_stage_monotonic_s=100).state,state)

    def test_measurement_dwell_separate_from_envelope_permission(self):
        self.config.required_stable_recovery_dwell_s=2
        self.assertEqual(self.assess(phase='measurement').state,'MEASUREMENT_WAIT')
        self.assertEqual(self.assess(temp=3.8,now=101,phase='measurement').state,'MEASUREMENT_WAIT')
        self.assertEqual(self.assess(temp=3.8,now=103,phase='measurement').state,'MEASUREMENT_READY')

    def test_hold_recovery_and_retry_limit(self):
        self.configured()
        self.assertEqual(self.assess(temp=5.41).state,'THERMAL_HOLD')
        self.assertEqual(self.assess(temp=5.2,now=101).state,'RECOVERY_VERIFY')
        self.assertEqual(self.assess(temp=5.2,now=103).state,'CONTINUE')
        self.assertEqual(self.assess(temp=5.41,now=104).state,'THERMAL_HOLD')
        self.assess(temp=5.2,now=105)
        self.assertEqual(self.assess(temp=5.2,now=107).state,'FAULT')

    def test_continued_warming_and_timeout_latch_fault(self):
        self.configured()
        self.assess(temp=5.41)
        self.assertEqual(self.assess(temp=5.5,now=101).state,'FAULT')
        self.assertEqual(self.assess(temp=3.5,now=102).state,'FAULT')
        self.policy.reset()
        self.assess(temp=5.41)
        self.assertEqual(self.assess(temp=5.41,now=120).state,'FAULT')

    def test_stale_sensor_fault_and_hardware_fault(self):
        for obj, key, value in [('t','monotonic_s',90),('m','monotonic_s',90),
                                ('t','sample_sensor_status','1'),('t','communication_valid',False),
                                ('m','heater_on',None),('t','reservoir_temperature_k',float('nan'))]:
            t,m=self.inputs();setattr(t if obj=='t' else m,key,value)
            self.assertEqual(self.policy.evaluate(t,m,now=100).state,'MONITOR_FAULT')
        t,m=self.inputs();m.status.quench=True
        self.assertEqual(self.policy.evaluate(t,m,now=100).state,'FAULT')

    def test_shadow_cannot_change_live_decision(self):
        self.config.field_envelope_enabled=False  # Legacy policy remains isolated from shadow state.
        live=ThermalSafetyEvaluator(self.config,clock=lambda:100)
        t,m=self.inputs()
        before=live.evaluate(t)
        report=live.new_field_comparison().evaluate(t,m,now=100)
        self.assertEqual(report.state,'WITHIN_ENVELOPE')
        self.assertEqual(before.state,ThermalState.TRIPPED)
        self.assertEqual(live.evaluate(t),before)


if __name__=='__main__':
    unittest.main()
