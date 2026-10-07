import unittest
from unittest.mock import patch

from app.numeric_series import parse_numeric_series
from app.engine.bfield_transport_sweep import parse_condition_series
from app.engine.gate_scan_field_batch import GateScanFieldBatch


class NumericSeriesTests(unittest.TestCase):
    def parse(self, text, maximum=100):
        return parse_numeric_series(text, "Test", maximum=maximum)

    def test_parenthesized_count_is_distinct_from_explicit_list(self):
        self.assertEqual(self.parse("(0 , 1 , 5)"), (0, .25, .5, .75, 1))
        self.assertEqual(self.parse("0, 1, 5"), (0, 1, 5))
        self.assertEqual(self.parse("[0, 1, 5]"), (0, 1, 5))

    def test_named_aliases_have_identical_endpoint_inclusive_values(self):
        for expression in ("(0,1,5)", "linspace(0,1,5)", "np.linspace(0,1,5)"):
            with self.subTest(expression=expression):
                self.assertEqual(self.parse(expression), (0, .25, .5, .75, 1))

    def test_groups_mix_with_lists_and_step_ranges_without_splitting_inner_commas(self):
        expression = "(0, 1, 3), 2:3:0.5\n4; np.linspace(5,6,2)"
        expected = (0, .5, 1, 2, 2.5, 3, 4, 5, 6)
        self.assertEqual(self.parse(expression), expected)
        self.assertEqual(parse_condition_series(expression, "Vds"), expected)
        self.assertEqual(GateScanFieldBatch.parse_fields(expression), expected)

    def test_step_ranges_include_exact_endpoints_without_overshooting(self):
        for expression, expected in (
            ("0:1:.25", (0, .25, .5, .75, 1)),
            ("0:1:.3", (0, .3, .6, .9, 1)),
            ("1:0:-.3", (1, .7, .4, .1, 0)),
            ("0:.26:.25", (0, .25, .26)),
            ("0:1:2", (0, 1)),
            ("2:2:-.1", (2,)),
            ("0:.3:.1", (0, .1, .2, .3)),
            ("-.3:.3:.1", (-.3, -.2, -.1, 0, .1, .2, .3)),
        ):
            with self.subTest(expression=expression):
                self.assertEqual(self.parse(expression), expected)
                self.assertEqual(parse_condition_series(expression, "Vds"), expected)
                self.assertEqual(GateScanFieldBatch.parse_fields(expression), expected)

    def test_descending_point_counts_and_scientific_notation(self):
        self.assertEqual(self.parse("(1,-1,5)"), (1, .5, 0, -.5, -1))
        self.assertEqual(self.parse("(0,1e-3,3)"), (0, .0005, .001))
        self.assertEqual(self.parse("(0,1,5.0)"), (0, .25, .5, .75, 1))
        self.assertEqual(self.parse("(\n0,\n1,\n5\n)"), (0, .25, .5, .75, 1))

    def test_single_point_requires_equal_endpoints(self):
        self.assertEqual(self.parse("(2,2,1)"), (2,))
        self.assertEqual(self.parse("(2,2,3)"), (2, 2, 2))
        with self.assertRaisesRegex(ValueError, "include both endpoints"):
            self.parse("(0,1,1)")

    def test_invalid_counts_and_nonfinite_values_are_rejected(self):
        for expression in ("(0,1,0)", "(0,1,-2)", "(0,1,2.5)", "(0,1,nan)",
                           "(0,inf,5)", "(nan,1,5)", "0:1:0", "0:1:-.1", "0:nan:.1"):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                self.parse(expression)

    def test_syntax_is_validated_without_evaluating_python(self):
        expressions = ("(0,1)", "(0,1,5,7)", "(0,1,5", "0,1,5)",
                       "((0,1,5))", "(0,1,2+3)", "np.arange(0,1,5)",
                       "__import__('os').getcwd()", "np.linspace(0,1,5,endpoint=False)")
        with patch("builtins.eval", side_effect=AssertionError("must not eval")) as evaluate:
            for expression in expressions:
                with self.subTest(expression=expression), self.assertRaises(ValueError):
                    self.parse(expression)
            evaluate.assert_not_called()

    def test_caps_include_endpoints_and_combined_groups_before_allocation(self):
        self.assertEqual(self.parse("0:99:1"), tuple(range(100)))
        self.assertEqual(len(self.parse("(0,1,100)")), 100)
        for expression in ("0:100:1", "0:1:1e-300", "-1e308:1e308:1e-308", "1,(2,3,100)"):
            with self.subTest(expression=expression), self.assertRaisesRegex(ValueError, "cannot exceed"):
                self.parse(expression)
        with patch("app.numeric_series.np.linspace", side_effect=AssertionError("must check before allocating")) as allocate:
            for expression in ("(0,1,1000000000)", "1,(2,3,100)"):
                with self.subTest(expression=expression), self.assertRaisesRegex(ValueError, "cannot exceed"):
                    self.parse(expression)
            allocate.assert_not_called()

    def test_gate_field_limits_apply_to_added_endpoint_and_linspace(self):
        with patch("app.engine.gate_scan_field_batch.cfg.magnet.safe_control_max_field_t", .5):
            for expression in ("0:.50001:.5", "(0,.50001,2)"):
                with self.subTest(expression=expression), self.assertRaisesRegex(ValueError, "within"):
                    GateScanFieldBatch.parse_fields(expression)

    def test_duplicate_gate_targets_still_block_including_shared_range_endpoints(self):
        for expression in ("0:1:.5,1", "(0,1,3),1", "(2,2,3)"):
            with self.subTest(expression=expression), self.assertRaisesRegex(ValueError, "Duplicate"):
                GateScanFieldBatch.parse_fields(expression)
        self.assertEqual(parse_condition_series("(2,2,3)", "Vds"), (2, 2, 2))

    def test_exact_gate_cap_and_endpoint_count(self):
        fields = GateScanFieldBatch.parse_fields("(0,8,10000)")
        self.assertEqual(len(fields), 10000)
        self.assertEqual((fields[0], fields[-1]), (0, 8))
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            GateScanFieldBatch.parse_fields("0:8:0.0008")

    def test_nonfinite_numpy_intermediate_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "finite"):
            self.parse("(-1e308,1e308,3)")
