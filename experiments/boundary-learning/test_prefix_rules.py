import unittest

from prefix_rules import hypotheses


class PrefixRulesTest(unittest.TestCase):
    def test_preserves_exceptions_and_challenges_missing_operand(self):
        samples = [("!a", "a"), ("!b", "b"), ("!c", "c"), ("!c", "different")]
        rule, = hypotheses(samples)
        self.assertEqual(rule["support"], ["!a", "!b"])
        self.assertEqual(rule["exceptions"], ["!c"])
        self.assertEqual(rule["probes"], ["!"])
        known, = hypotheses(samples + [("!", "unknown marker")])
        self.assertEqual(known["probes"], [])

    def test_duplicate_samples_are_one_witness(self):
        self.assertEqual(list(hypotheses([("!a", "a")] * 4)), [])
