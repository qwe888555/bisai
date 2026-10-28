from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase

from selection.common import BusinessError
from selection.enums import GroupType, RoundType
from selection.services.scoring import calculate_composite, calculate_total


class ScoringFormulaTests(SimpleTestCase):
    def test_weighted_total(self):
        items = [
            SimpleNamespace(
                score=Decimal("90"),
                max_score=Decimal("100"),
                weight=Decimal("30"),
            ),
            SimpleNamespace(
                score=Decimal("80"),
                max_score=Decimal("100"),
                weight=Decimal("30"),
            ),
            SimpleNamespace(
                score=Decimal("70"),
                max_score=Decimal("100"),
                weight=Decimal("20"),
            ),
            SimpleNamespace(
                score=Decimal("60"),
                max_score=Decimal("100"),
                weight=Decimal("20"),
            ),
        ]
        self.assertEqual(calculate_total(items), Decimal("77.00"))

    def test_weight_is_normalized(self):
        # Weights total 40, not 100; the formula normalizes by the sum.
        items = [
            SimpleNamespace(
                score=Decimal("90"),
                max_score=Decimal("100"),
                weight=Decimal("10"),
            ),
            SimpleNamespace(
                score=Decimal("80"),
                max_score=Decimal("100"),
                weight=Decimal("10"),
            ),
            SimpleNamespace(
                score=Decimal("80"),
                max_score=Decimal("100"),
                weight=Decimal("10"),
            ),
            SimpleNamespace(
                score=Decimal("90"),
                max_score=Decimal("100"),
                weight=Decimal("10"),
            ),
        ]
        self.assertEqual(calculate_total(items), Decimal("85.00"))

    def test_non_100_max_score(self):
        # 18/20 = 90% and 40/50 = 80%, weighted 50/50 -> 85.
        items = [
            SimpleNamespace(
                score=Decimal("18"),
                max_score=Decimal("20"),
                weight=Decimal("50"),
            ),
            SimpleNamespace(
                score=Decimal("40"),
                max_score=Decimal("50"),
                weight=Decimal("50"),
            ),
        ]
        self.assertEqual(calculate_total(items), Decimal("85.00"))

    def test_composite(self):
        self.assertEqual(
            calculate_composite(
                Decimal("80"),
                Decimal("90"),
                Decimal("0.4"),
                Decimal("0.6"),
            ),
            Decimal("86.00"),
        )

    def test_composite_falls_back_to_one_round(self):
        self.assertEqual(
            calculate_composite(
                Decimal("88"), None, Decimal("0.4"), Decimal("0.6")
            ),
            Decimal("88.00"),
        )
        self.assertEqual(
            calculate_composite(
                None, Decimal("91"), Decimal("0.4"), Decimal("0.6")
            ),
            Decimal("91.00"),
        )

    def test_composite_rejects_zero_weight_sum(self):
        with self.assertRaises(BusinessError):
            calculate_composite(
                Decimal("80"), Decimal("90"), Decimal("0"), Decimal("0")
            )

    def test_score_above_max_is_rejected(self):
        items = [
            SimpleNamespace(
                score=Decimal("110"),
                max_score=Decimal("100"),
                weight=Decimal("100"),
            )
        ]
        with self.assertRaises(BusinessError):
            calculate_total(items)


class EnumParsingTests(SimpleTestCase):
    def test_group_aliases(self):
        self.assertEqual(GroupType.parse("中学组"), GroupType.MIDDLE)
        self.assertEqual(GroupType.parse("中学"), GroupType.MIDDLE)
        self.assertEqual(GroupType.parse("primary"), GroupType.PRIMARY)

    def test_round_aliases(self):
        self.assertEqual(RoundType.parse("初选"), RoundType.INITIAL)
        self.assertEqual(RoundType.parse("复赛"), RoundType.FINAL)

    def test_invalid_group(self):
        with self.assertRaises(BusinessError):
            GroupType.parse("幼儿园")
