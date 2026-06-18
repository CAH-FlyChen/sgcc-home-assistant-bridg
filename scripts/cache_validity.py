"""Helpers for deciding whether legacy REST cache entries contain real SGCC data."""
from __future__ import annotations

from typing import Any

SCALAR_DATA_KEYS = (
    "balance",
    "last_daily_usage",
    "yearly_charge",
    "yearly_usage",
    "month_charge",
    "month_usage",
)

TOU_ROW_DATA_KEYS = (
    "total_usage",
    "total_usage_kwh",
    "usage",
    "charge",
    "total_charge",
    "total_charge_cny",
    "valley_usage",
    "valley_usage_kwh",
    "flat_usage",
    "flat_usage_kwh",
    "peak_usage",
    "peak_usage_kwh",
    "tip_usage",
    "tip_usage_kwh",
)


def _has_value(value: Any) -> bool:
    return value not in (None, "")


def _rows_have_business_value(rows: Any) -> bool:
    if not isinstance(rows, list):
        return False
    for row in rows:
        if not isinstance(row, dict):
            continue
        if any(_has_value(row.get(key)) for key in TOU_ROW_DATA_KEYS):
            return True
    return False


def has_useful_legacy_cache_entry(values: Any) -> bool:
    """Return True only when a legacy sgcc_cache entry has publishable data."""
    if not isinstance(values, dict):
        return False

    if any(_has_value(values.get(key)) for key in SCALAR_DATA_KEYS):
        return True

    tou_data = values.get("tou_data")
    if isinstance(tou_data, dict):
        if _rows_have_business_value(tou_data.get("months")):
            return True
        if _rows_have_business_value(tou_data.get("daily")):
            return True
        for key in ("yearly_usage", "yearly_charge", "recent_total_usage"):
            if _has_value(tou_data.get(key)):
                return True

    enhanced_balance = values.get("enhanced_balance")
    if isinstance(enhanced_balance, dict):
        if _has_value(enhanced_balance.get("amount_due")):
            return True

    return False
