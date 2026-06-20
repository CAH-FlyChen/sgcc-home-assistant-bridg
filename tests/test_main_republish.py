import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace

sys.modules.setdefault("schedule", SimpleNamespace())
_data_fetcher_stub = ModuleType("data_fetcher")
_data_fetcher_stub.DataFetcher = object
sys.modules.setdefault("data_fetcher", _data_fetcher_stub)

import main
from config import FetcherConfig
from model import Account, AccountData, DailyReading, FetchRun, MonthlyReading
from store import Store


class FakeMqttPublisher:
    published = []

    def __init__(self, config):
        self.config = config
        self.connected = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def publish_account_data(self, data):
        FakeMqttPublisher.published.append(data)
        return True


class RepublishMqttFromStoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "sgcc.sqlite3")
        self.old_db_path = os.environ.get("SGCC_DB_PATH")
        os.environ["SGCC_DB_PATH"] = self.db_path
        self.old_publisher = main.MqttPublisher
        main.MqttPublisher = FakeMqttPublisher
        FakeMqttPublisher.published.clear()

    def tearDown(self):
        main.MqttPublisher = self.old_publisher
        if self.old_db_path is None:
            os.environ.pop("SGCC_DB_PATH", None)
        else:
            os.environ["SGCC_DB_PATH"] = self.old_db_path
        self.tmpdir.cleanup()

    def _save(self, account_data):
        with Store(self.db_path) as store:
            run_id = store.start_run(FetchRun(trigger_type="test", started_at="2026-06-21T00:00:00+08:00"))
            store.save_account_data(account_data, run_id)

    def test_empty_account_cache_is_not_success(self):
        with Store(self.db_path) as store:
            store.upsert_account(Account(account_no="1234567890123"))

        self.assertFalse(main.republish_mqtt_from_store(FetcherConfig(PUBLISHER="mqtt")))
        self.assertEqual(FakeMqttPublisher.published, [])

    def test_fresh_daily_cache_republishes(self):
        self._save(AccountData(
            account=Account(account_no="1234567890123"),
            daily=[DailyReading(account_no="1234567890123", date=main.datetime.now().strftime("%Y-%m-%d"), total_usage_kwh=1.2)],
        ))

        self.assertTrue(main.republish_mqtt_from_store(FetcherConfig(PUBLISHER="mqtt")))
        self.assertEqual(len(FakeMqttPublisher.published), 1)

    def test_stale_daily_cache_is_not_success(self):
        self._save(AccountData(
            account=Account(account_no="1234567890123"),
            daily=[DailyReading(account_no="1234567890123", date="2020-01-01", total_usage_kwh=1.2)],
        ))

        self.assertFalse(main.republish_mqtt_from_store(FetcherConfig(PUBLISHER="mqtt")))
        self.assertEqual(FakeMqttPublisher.published, [])

    def test_monthly_only_cache_publishes_nowhere_and_forces_fetch(self):
        self._save(AccountData(
            account=Account(account_no="1234567890123"),
            monthly=[MonthlyReading(account_no="1234567890123", year_month="2026-06", total_usage_kwh=12.3)],
        ))

        self.assertFalse(main.republish_mqtt_from_store(FetcherConfig(PUBLISHER="mqtt")))
        self.assertEqual(FakeMqttPublisher.published, [])


class FakeFetcher:
    def __init__(self):
        self.calls = []

    def fetch(self, trigger_type="manual"):
        self.calls.append(trigger_type)
        return "success"

    @staticmethod
    def _redact_text(value):
        return str(value)


class FakeUpdator:
    def __init__(self, republish_result=False):
        self.republish_result = republish_result

    def republish(self):
        return self.republish_result


class RepublishOrFetchGuardTestCase(unittest.TestCase):
    def test_publish_failure_with_fresh_store_cache_does_not_login_again(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "sgcc.sqlite3")
            old_db_path = os.environ.get("SGCC_DB_PATH")
            os.environ["SGCC_DB_PATH"] = db_path
            try:
                with Store(db_path) as store:
                    run_id = store.start_run(FetchRun(trigger_type="test", started_at="2026-06-21T00:00:00+08:00"))
                    store.save_account_data(AccountData(
                        account=Account(account_no="1234567890123"),
                        daily=[DailyReading(
                            account_no="1234567890123",
                            date=main.datetime.now().strftime("%Y-%m-%d"),
                            total_usage_kwh=1.2,
                        )],
                    ), run_id)

                fetcher = FakeFetcher()
                main.republish_or_fetch(None, fetcher, FetcherConfig(PUBLISHER="mqtt"))

                self.assertEqual(fetcher.calls, [])
            finally:
                if old_db_path is None:
                    os.environ.pop("SGCC_DB_PATH", None)
                else:
                    os.environ["SGCC_DB_PATH"] = old_db_path


if __name__ == "__main__":
    unittest.main()
