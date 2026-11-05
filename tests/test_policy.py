import json
from pathlib import Path
import tempfile
import unittest

from policy import CannotVerify, Review, Settings, brl, check_review, parse_brl, positive_int


class MoneyTests(unittest.TestCase):
    def test_brazilian_prices(self):
        for text, cents in [
            ("R$ 4.500,00", 450000), ("R$\xa04.499,99", 449999),
            ("Total do pedido: R$ 9.000,00", 900000), ("R$ 0,01", 1),
            ("R$ 4500,00", 450000),
        ]:
            self.assertEqual(parse_brl(text), cents)

    def test_unknown_or_ambiguous_values_are_rejected(self):
        for text in ("Grátis", "", "4.500", "R$ 4,500.00", "R$ 4.500,000",
                     "-R$ 1,00", "- R$ 1,00", "R$ 10,00 R$ 9,00"):
            with self.subTest(text=text), self.assertRaises(CannotVerify):
                parse_brl(text)

    def test_display_preserves_cents(self):
        for cents, expected in [(1, "R$ 0,01"), (99999, "R$ 999,99"),
                                (450000, "R$ 4.500,00"), (900000, "R$ 9.000,00")]:
            self.assertEqual(brl(cents), expected)
            self.assertEqual(parse_brl(brl(cents)), cents)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings("B0HK2KVYD7", 450000, 120)

    def test_per_unit_budget_includes_shipping_and_accepts_exact_boundary(self):
        check_review(Review(self.settings.asin, 2, 900000), self.settings, 2)

    def test_one_cent_above_boundary_is_rejected_without_rounding(self):
        with self.assertRaises(CannotVerify):
            check_review(Review(self.settings.asin, 2, 900001), self.settings, 2)

    def test_wrong_item_or_quantity_cannot_pass(self):
        for review in [Review("B000000000", 2, 1), Review(self.settings.asin, 1, 450000),
                       Review(self.settings.asin, 2, 0)]:
            with self.assertRaises(CannotVerify):
                check_review(review, self.settings, 2)

    def test_quantity_validation(self):
        for value in (0, -1, 1.5, True, 1000, "2;echo"):
            with self.assertRaises(ValueError):
                positive_int(value, "Quantidade")

    def test_configuration_rejects_invalid_values(self):
        for data in [
            {"asin": "../invalid", "max_unit_cents": 450000, "interval_seconds": 120},
            {"asin": self.settings.asin, "max_unit_cents": -1, "interval_seconds": 120},
            {"asin": self.settings.asin, "max_unit_cents": 450000, "interval_seconds": 1},
        ]:
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "config.json"
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(ValueError):
                    Settings.load(path)


if __name__ == "__main__":
    unittest.main()
