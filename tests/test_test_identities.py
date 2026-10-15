from copy import deepcopy
import unittest

import numpy as np

from tools.check_test_identities import decide, evaluate_gallery, validate


class TestIdentityReplayTests(unittest.TestCase):
    def setUp(self):
        v = np.zeros(512)
        v[0] = 1
        self.rows = [
            {"id": "ref", "video": 0, "time": 5, "vector": v.tolist(), "reference_ok": True},
            {"id": "boundary", "video": 0, "time": 7, "vector": v.tolist(), "reference_ok": True},
            {"id": "later", "video": 0, "time": 7.01, "vector": v.tolist(), "reference_ok": True},
            {"id": "other", "video": 1, "time": 5, "vector": v.tolist(), "reference_ok": True},
            {"id": "bad", "video": 0, "time": 10, "vector": None, "reference_ok": False},
        ]
        self.lookup = {r["id"]: r for r in self.rows}
        self.people = [{"id": "test-01", "refs": ["ref"]}]
        self.source = {"complete": True, "created_at": "now", "source_sha256": "abc", "threshold": .55,
                       "observations": self.rows, "videos": [{}, {}]}
        self.session = {"version": 1, "report_created_at": "now", "source_sha256": "abc", "threshold": .55,
                        "sessions": {"0": {"profiles": self.people}}}

    def test_reference_window_and_held_out_rows(self):
        result, control = evaluate_gallery(self.rows, self.people, 0, self.lookup, .55, 2)
        self.assertEqual([r["status"] for r in result],
                         ["excluded", "excluded", "matched", "matched", "no_embedding"])
        self.assertEqual(control, {"exact_reference_ok": 1})
        self.assertEqual(result[2]["reference"], "ref")

    def test_multiple_references_and_id_ambiguity(self):
        result, _ = evaluate_gallery(self.rows, [{"id": "test-01", "refs": ["ref", "boundary"]}],
                                     0, self.lookup, .55, 2)
        self.assertEqual(result[2]["status"], "excluded")
        people = self.people + [{"id": "test-02", "refs": ["boundary"]}]
        result, control = evaluate_gallery(self.rows, people, 0, self.lookup, .55, 2)
        self.assertEqual(result[3]["status"], "ambiguous")
        self.assertEqual(control, {"ambiguous": 2})

    def test_strict_gate_and_unknown(self):
        self.assertEqual(decide(np.array([.55]), ["a"], ["ref"], .55)["status"], "unknown")
        self.assertEqual(decide(np.array([.55001]), ["a"], ["ref"], .55)["status"], "matched")
        self.assertEqual(decide(np.array([.7, .8]), ["a", "b"], ["r1", "r2"], .55)["status"], "ambiguous")

    def test_session_and_vector_validation(self):
        self.assertEqual(len(validate(self.source, self.session)), 5)
        for field, value in [("source_sha256", "different"), ("report_created_at", "old"), ("threshold", .4)]:
            bad = deepcopy(self.session)
            bad[field] = value
            with self.assertRaises(ValueError):
                validate(self.source, bad)
        for ref in ("other", "bad", "missing"):
            bad = deepcopy(self.session)
            bad["sessions"]["0"]["profiles"][0]["refs"] = [ref]
            with self.assertRaises(ValueError):
                validate(self.source, bad)
        bad_source = deepcopy(self.source)
        bad_source["observations"][0]["vector"] = [0]*512
        with self.assertRaises(ValueError):
            validate(bad_source, self.session)

    def test_duplicate_references_and_invalid_gap(self):
        bad = deepcopy(self.session)
        bad["sessions"]["0"]["profiles"].append({"id": "test-02", "refs": ["ref"]})
        with self.assertRaises(ValueError):
            validate(self.source, bad)
        for gap in (-1, float("nan")):
            with self.assertRaises(ValueError):
                evaluate_gallery(self.rows, self.people, 0, self.lookup, .55, gap)


if __name__ == "__main__":
    unittest.main()
