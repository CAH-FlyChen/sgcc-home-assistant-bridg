import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from cache_validity import has_useful_legacy_cache_entry


class CacheValidityTestCase(unittest.TestCase):
    def test_empty_suffix_only_cache_is_not_useful(self):
        self.assertFalse(has_useful_legacy_cache_entry({
            "balance": None,
            "last_daily_date": None,
            "last_daily_usage": None,
            "yearly_charge": None,
            "yearly_usage": None,
            "month_charge": None,
            "month_usage": None,
            "timestamp": "2026-06-18T13:53:31",
            "tou_data": {"months": [], "daily": [], "yearly_usage": None, "yearly_charge": None},
            "enhanced_balance": {"as_of": None, "amount_due": None, "user_id": None},
        }))

    def test_metadata_without_business_value_is_not_useful(self):
        self.assertFalse(has_useful_legacy_cache_entry({
            "last_daily_date": "2026-06-17",
            "enhanced_balance": {"as_of": "2026-06-18T14:00:00", "user_id": "5001657384840"},
        }))
        self.assertFalse(has_useful_legacy_cache_entry({
            "tou_data": {"daily": [{"date": "2026-06-17"}], "months": [{"year_month": "2026-05"}]},
        }))

    def test_zero_business_values_are_useful(self):
        self.assertTrue(has_useful_legacy_cache_entry({"balance": 0}))
        self.assertTrue(has_useful_legacy_cache_entry({"enhanced_balance": {"amount_due": 0}}))
        self.assertTrue(has_useful_legacy_cache_entry({"tou_data": {"daily": [{"date": "2026-06-17", "tip_usage": 0}]}}))

    def test_scalar_or_tou_data_cache_is_useful(self):
        self.assertTrue(has_useful_legacy_cache_entry({"last_daily_usage": 1.23}))
        self.assertTrue(has_useful_legacy_cache_entry({"tou_data": {"daily": [{"date": "2026-06-17", "total_usage": 1.23}]}}))


if __name__ == "__main__":
    unittest.main()
