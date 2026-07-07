import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sgcc_ha_bridge.diag import DiagnosticCollector, SUMMARY_END, SUMMARY_START, diag_enabled
from sgcc_ha_bridge.model import (
    Account,
    AccountData,
    Balance,
    DailyReading,
    MonthlyReading,
    SessionCheck,
    YearlyReading,
)


class DiagSwitchTestCase(unittest.TestCase):
    def test_sgcc_diag_is_the_only_debug_switch(self):
        with patch.dict(os.environ, {"SGCC_DIAG": "true"}, clear=False):
            self.assertTrue(diag_enabled())
        with patch.dict(os.environ, {"SGCC_DIAG": "false"}, clear=False):
            self.assertFalse(diag_enabled())


class DiagnosticCollectorTestCase(unittest.TestCase):
    def test_emit_writes_redacted_summary_and_field_package(self):
        account_data = AccountData(
            account=Account(
                account_no="1234567890016",
                display_name="张三",
                address="福建省福州市测试路 1 号",
                province="福建",
            ),
            balance=Balance(
                account_no="1234567890016",
                observed_at="2026-07-07 00:00:00",
                balance_cny=86.44,
                prepay_balance_cny=217.7,
                arrears_cny=0.0,
            ),
            yearly=YearlyReading(
                account_no="1234567890016",
                year="2026",
                total_usage_kwh=1234.5,
                total_charge_cny=678.9,
            ),
            monthly=[
                MonthlyReading(
                    account_no="1234567890016",
                    year_month="2026-06",
                    total_usage_kwh=321.0,
                    total_charge_cny=212.33,
                )
            ],
            daily=[
                DailyReading(
                    account_no="1234567890016",
                    date="2026-07-06",
                    total_usage_kwh=8.5,
                )
            ],
        )
        snapshot = {
            "url": "https://95598.cn/osgweb/userAcc?accountNo=1234567890016&token=secret-token",
            "store": {
                "state": {
                    "userAcc": {
                        "accountNo": "1234567890016",
                        "queryTime": "2026-07-07 00:00:00",
                        "accountBalance": "86.44元",
                        "phone": "13800138000",
                        "address": "福建省福州市测试路 1 号",
                        "password": "plain-password",
                        "token": "secret-token",
                    },
                    "mixinGetYuEdata": {
                        "consNo": "1234567890016",
                        "historyOwe": "0.00",
                        "prepayBal": "217.7",
                    },
                },
                "getters": {},
            },
            "components": [
                {
                    "tag": "DIV",
                    "className": "balance-card",
                    "text": "包含页面文字但不应进入诊断包 1234567890016",
                    "data": {
                        "api_key": "sk-secret",
                        "prepayBalance": "217.7",
                    },
                }
            ],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            collector = DiagnosticCollector(trigger_type="manual", output_dir=temp_dir)
            collector.set_run_id(42)
            collector.record_runtime(stage="test")
            collector.record_session(
                "before_login",
                SessionCheck(
                    checked_at="2026-07-07T00:00:00+08:00",
                    status="authenticated",
                    current_url="https://95598.cn/osgweb/userAcc?accountNo=1234567890016",
                    check_method="dom",
                    redirected_to_login=False,
                    evidence_redacted="account 1234567890016 phone 13800138000",
                ),
            )
            collector.record_page("账户余额", snapshot, account_data)
            collector.record_account_saved(account_data)
            collector.record_publish("1234567890016", "mqtt", True, "ok")
            collector.emit("success")

            latest = Path(temp_dir) / "latest"
            summary_text = (latest / "summary.txt").read_text(encoding="utf-8")
            summary_json_text = (latest / "summary.json").read_text(encoding="utf-8")
            fields_text = (latest / "fields.redacted.json").read_text(encoding="utf-8")

        self.assertIn(SUMMARY_START, summary_text)
        self.assertIn(SUMMARY_END, summary_text)
        self.assertIn("run_id=42", summary_text)
        self.assertIn("account=*********0016", summary_text)
        self.assertIn("money_candidates=", summary_text)
        self.assertIn("daily=1(2026-07-06", summary_text)
        self.assertIn("monthly=1(2026-06", summary_text)
        self.assertIn("publish=publisher=mqtt", summary_text)

        payload = json.loads(summary_json_text)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["run_id"], 42)

        combined = summary_text + summary_json_text + fields_text
        self.assertNotIn("1234567890016", combined)
        self.assertNotIn("13800138000", combined)
        self.assertNotIn("plain-password", combined)
        self.assertNotIn("secret-token", combined)
        self.assertNotIn("sk-secret", combined)
        self.assertNotIn("password", combined.lower())
        self.assertNotIn("token", combined.lower())
        self.assertNotIn("api_key", combined.lower())


if __name__ == "__main__":
    unittest.main()
