from __future__ import annotations

import unittest
from pathlib import Path

from capcov.claims.evaluator import evaluate

from .adapter import load_fixture


ROOT = Path(__file__).parent / "corpus"


class CorpusEvaluatorSmokeTests(unittest.TestCase):
    def test_all_reviewed_bundles_are_evaluable(self) -> None:
        for path in sorted(ROOT.glob("[0-9][0-9]-*.json")):
            with self.subTest(case=path.stem):
                bundle = load_fixture(path)
                first = evaluate(bundle)
                second = evaluate(bundle)
                self.assertEqual(first.as_dict(), second.as_dict())
                self.assertEqual(len(first.claims), len(bundle.claims))


if __name__ == "__main__":
    unittest.main()
