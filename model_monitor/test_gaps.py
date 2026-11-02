import unittest
from model_monitor import test_monitor as fixtures


class EvidenceGapAcceptance(unittest.TestCase):
    setUp = fixtures.ModelAcceptance.setUp
    turn = fixtures.ModelAcceptance.turn
    call = fixtures.ModelAcceptance.call
    scan = fixtures.ModelAcceptance.scan

    def test_22_missing_evidence_counted(self):
        self.call(upstream=None)
        d=self.scan()
        self.assertEqual(d['gaps']['selection_missing'],1)
        self.assertEqual(d['gaps']['upstream_model_missing'],1)
        self.assertEqual(d['unrecorded_threads'],1)
        self.assertEqual(d['error_count'],0)
        self.assertIsNotNone(d['last_success'])
