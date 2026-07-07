"""Single-switch diagnostic collection for SGCC fetch runs.

``SGCC_DIAG=true`` enables a run-level diagnostic package and a concise log
summary.  The collector is observational: it records already parsed models and
already captured Vue/Vuex snapshots, applies redaction, and never changes parser
or publisher output.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import sys
from dataclasses import asdict, is_dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Optional

from .ha_mapping import account_data_summary
from .model import AccountData, mask_account_no
from .redact import now_iso, redact_text, redact_url


SUMMARY_START = "========== SGCC DIAG SUMMARY START =========="
SUMMARY_END = "========== SGCC DIAG SUMMARY END =========="

DEFAULT_DIAG_DIR = "/data/diag"
MAX_FIELD_VALUES_PER_PAGE = 600
MAX_SHAPES_PER_PAGE = 260
MAX_LIST_ITEMS_PER_ARRAY = 20
MAX_DICT_ITEMS_PER_OBJECT = 120
MAX_FIELD_DEPTH = 10

_TRUTHY = {"1", "true", "yes", "on"}
_SECRET_KEY_RE = re.compile(
    r"(password|passwd|pwd|token|secret|cookie|authorization|credential|api[_-]?key|access[_-]?key|session[_-]?id)$",
    re.IGNORECASE,
)
_PII_KEY_RE = re.compile(
    r"(phone|mobile|tel|address|addr|elecaddr|custname|consname|display[_-]?name|realname|姓名|地址|手机号|电话)",
    re.IGNORECASE,
)
_ACCOUNT_KEY_RE = re.compile(
    r"^(consNo|consNo_dst|accountNo|acctNo|user_id|userId|selectValue|account_no)$",
    re.IGNORECASE,
)
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
_ACCOUNT_RE = re.compile(r"(?<!\d)(\d{13})(?!\d)")
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def env_truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


def diag_enabled() -> bool:
    return env_truthy("SGCC_DIAG")


def diag_output_root() -> Path:
    return Path(os.getenv("SGCC_DIAG_DIR", DEFAULT_DIAG_DIR))


class DiagnosticCollector:
    """Collect one fetch run's diagnostic evidence and emit redacted files."""

    def __init__(self, trigger_type: str = "manual", output_dir: Optional[Path | str] = None):
        self.trigger_type = trigger_type
        self.output_root = Path(output_dir) if output_dir is not None else diag_output_root()
        self.started_at = now_iso()
        self.generated_at = ""
        self.status = "running"
        self.run_id: Optional[int] = None
        self.runtime: dict[str, Any] = {}
        self.scrape: dict[str, Any] = {
            "selector_option_count": None,
            "fetched_account_count": None,
            "saved_count": 0,
            "skipped_count": 0,
        }
        self.sessions: list[dict[str, Any]] = []
        self.pages: list[dict[str, Any]] = []
        self.field_pages: list[dict[str, Any]] = []
        self.accounts: list[dict[str, Any]] = []
        self.publish: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self.output_paths: dict[str, str] = {}
        self._emitted = False

    def set_run_id(self, run_id: Optional[int]) -> None:
        self.run_id = run_id

    def record_runtime(self, config: Any = None, publisher: Optional[str] = None, stage: str = "runtime") -> None:
        self.runtime.update({
            "stage": stage,
            "package_version": _package_version(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "trigger_type": self.trigger_type,
            "publisher": publisher or getattr(config, "PUBLISHER", None),
            "browser_mode": os.getenv("SGCC_BROWSER_MODE", "local"),
            "debug_mode": os.getenv("DEBUG_MODE", "false"),
            "env": _safe_env_snapshot(),
        })
        if config is not None:
            self.runtime["config"] = {
                "retry_limit": getattr(config, "RETRY_TIMES_LIMIT", None),
                "page_load_timeout": getattr(config, "PAGE_LOAD_TIMEOUT", None),
                "implicit_wait": getattr(config, "DRIVER_IMPLICITY_WAIT_TIME", None),
                "ignored_user_count": len(getattr(config, "IGNORE_USER_ID", []) or []),
                "mqtt_host": redact_text(getattr(config, "MQTT_HOST", "") or ""),
                "mqtt_port": getattr(config, "MQTT_PORT", None),
                "mqtt_discovery_prefix": getattr(config, "MQTT_DISCOVERY_PREFIX", None),
            }

    def record_session(self, stage: str, check: Any) -> None:
        self.sessions.append({
            "stage": stage,
            "checked_at": getattr(check, "checked_at", ""),
            "status": getattr(check, "status", "unknown"),
            "current_url": redact_url(str(getattr(check, "current_url", "") or "")),
            "check_method": getattr(check, "check_method", ""),
            "redirected_to_login": bool(getattr(check, "redirected_to_login", False)),
            "evidence_redacted": redact_scalar(getattr(check, "evidence_redacted", "") or "", "evidence"),
        })

    def record_selector_options(self, count: int) -> None:
        self.scrape["selector_option_count"] = count

    def record_fetched_accounts(self, count: int) -> None:
        self.scrape["fetched_account_count"] = count

    def record_page(self, label: str, snapshot: dict[str, Any], parsed: AccountData) -> None:
        try:
            from .money_candidates import collect_money_candidates

            candidates = collect_money_candidates(
                store=snapshot.get("store"),
                components=snapshot.get("components"),
            )
        except Exception as exc:
            candidates = []
            self.record_error(exc, stage=f"page:{label}:money_candidates")

        parsed_summary = account_data_diag_summary(parsed)
        inventory = collect_snapshot_field_inventory(snapshot)
        page_summary = {
            "label": label,
            "url": redact_url(str(snapshot.get("url") or "")),
            "parsed": parsed_summary,
            "money_candidate_count": len(candidates),
            "field_value_count": inventory["field_value_count"],
            "field_truncated": inventory["truncated"],
        }
        self.pages.append(page_summary)
        self.field_pages.append({
            **page_summary,
            "money_candidates": [_money_candidate_to_dict(item) for item in candidates],
            "source_shapes": inventory["shapes"],
            "field_values": inventory["fields"],
        })

    def record_account_saved(self, account_data: AccountData) -> None:
        self.scrape["saved_count"] = int(self.scrape.get("saved_count") or 0) + 1
        self.accounts.append({
            "event": "saved",
            "summary_text": account_data_summary(account_data),
            **account_data_diag_summary(account_data),
        })

    def record_account_skipped(self, account_data: Optional[AccountData], reason: str) -> None:
        self.scrape["skipped_count"] = int(self.scrape.get("skipped_count") or 0) + 1
        item: dict[str, Any] = {"event": "skipped", "reason": redact_text(reason)}
        if account_data is not None:
            item.update(account_data_diag_summary(account_data))
        self.accounts.append(item)

    def record_publish(
        self,
        account_no: str,
        publisher: str,
        success: bool,
        detail: str = "",
    ) -> None:
        self.publish.append({
            "account": mask_account_no(account_no),
            "publisher": publisher,
            "success": bool(success),
            "detail": redact_scalar(detail, "detail"),
        })

    def record_error(self, error: Any, message: Optional[str] = None, stage: str = "") -> None:
        error_type = error if isinstance(error, str) else type(error).__name__
        error_message = message if message is not None else str(error)
        self.errors.append({
            "stage": stage,
            "type": str(error_type),
            "message": redact_scalar(error_message, "message"),
        })

    def emit(self, status: Optional[str] = None) -> None:
        if self._emitted:
            return
        self._emitted = True
        if status:
            self.status = status
        self.generated_at = now_iso()

        run_dir = self._run_dir()
        latest_dir = self.output_root / "latest"
        self.output_paths = {
            "run_dir": str(run_dir),
            "latest_dir": str(latest_dir),
            "summary_txt": str(latest_dir / "summary.txt"),
            "summary_json": str(latest_dir / "summary.json"),
            "fields_json": str(latest_dir / "fields.redacted.json"),
        }

        summary_text = self.summary_text()
        summary_json = self.summary_json()
        fields_json = self.fields_json()

        try:
            self._write_outputs(run_dir, latest_dir, summary_text, summary_json, fields_json)
        except Exception as exc:
            logging.warning(f"SGCC DIAG 写入诊断包失败: {redact_text(exc)}")
        logging.info("\n%s", summary_text.rstrip())

    def summary_text(self) -> str:
        lines = [
            SUMMARY_START,
            f"status={self.status}",
            f"generated_at={self.generated_at or now_iso()}",
            f"run_id={self.run_id if self.run_id is not None else '-'}",
            f"trigger_type={self.trigger_type}",
            f"publisher={self.runtime.get('publisher') or '-'}",
            f"browser_mode={self.runtime.get('browser_mode') or '-'}",
        ]
        if self.output_paths:
            lines.append(f"diag_latest={self.output_paths.get('latest_dir')}")

        if self.sessions:
            first = self.sessions[0]
            last = self.sessions[-1]
            lines.append(
                "session="
                f"{first.get('stage')}:{first.get('status')} -> "
                f"{last.get('stage')}:{last.get('status')}"
            )

        lines.append(
            "scrape="
            f"selector_options={_dash_none(self.scrape.get('selector_option_count'))}, "
            f"fetched={_dash_none(self.scrape.get('fetched_account_count'))}, "
            f"saved={self.scrape.get('saved_count')}, "
            f"skipped={self.scrape.get('skipped_count')}"
        )

        for index, account in enumerate(self.accounts, 1):
            lines.append(
                f"account[{index}]="
                f"event={account.get('event')}, "
                f"account={account.get('account') or '-'}, "
                f"balance={_money_summary(account.get('balance'))}, "
                f"daily={_series_summary(account.get('daily'))}, "
                f"monthly={_series_summary(account.get('monthly'))}, "
                f"yearly={_yearly_summary(account.get('yearly'))}"
            )

        for index, page in enumerate(self.pages, 1):
            parsed = page.get("parsed") or {}
            lines.append(
                f"page[{index}]="
                f"label={page.get('label')}, "
                f"url={page.get('url') or '-'}, "
                f"account={parsed.get('account') or '-'}, "
                f"money_candidates={page.get('money_candidate_count')}, "
                f"fields={page.get('field_value_count')}"
                f"{' truncated' if page.get('field_truncated') else ''}"
            )

        for item in self.publish:
            lines.append(
                "publish="
                f"publisher={item.get('publisher')}, "
                f"account={item.get('account') or '-'}, "
                f"success={str(item.get('success')).lower()}, "
                f"detail={item.get('detail') or '-'}"
            )

        for item in self.errors:
            lines.append(
                "error="
                f"stage={item.get('stage') or '-'}, "
                f"type={item.get('type')}, "
                f"message={item.get('message')}"
            )

        lines.append(SUMMARY_END)
        return "\n".join(lines) + "\n"

    def summary_json(self) -> dict[str, Any]:
        return redact_structure({
            "status": self.status,
            "started_at": self.started_at,
            "generated_at": self.generated_at,
            "run_id": self.run_id,
            "trigger_type": self.trigger_type,
            "runtime": self.runtime,
            "scrape": self.scrape,
            "sessions": self.sessions,
            "accounts": self.accounts,
            "pages": self.pages,
            "publish": self.publish,
            "errors": self.errors,
            "outputs": self.output_paths,
        })

    def fields_json(self) -> dict[str, Any]:
        return redact_structure({
            "status": self.status,
            "generated_at": self.generated_at,
            "run_id": self.run_id,
            "pages": self.field_pages,
        })

    def _run_dir(self) -> Path:
        stamp = re.sub(r"[^0-9T]", "", (self.started_at or now_iso()).split("+", 1)[0].replace("-", "").replace(":", ""))
        run = f"run{self.run_id}" if self.run_id is not None else "run-unknown"
        return self.output_root / f"{stamp}-{run}"

    def _write_outputs(
        self,
        run_dir: Path,
        latest_dir: Path,
        summary_text: str,
        summary_json: dict[str, Any],
        fields_json: dict[str, Any],
    ) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        _write_package(run_dir, summary_text, summary_json, fields_json)

        if latest_dir.exists() or latest_dir.is_symlink():
            if latest_dir.is_symlink() or latest_dir.is_file():
                latest_dir.unlink()
            else:
                shutil.rmtree(latest_dir)
        latest_dir.mkdir(parents=True, exist_ok=True)
        _write_package(latest_dir, summary_text, summary_json, fields_json)


def account_data_diag_summary(account_data: Optional[AccountData]) -> dict[str, Any]:
    if account_data is None:
        return {}
    account_no = account_data.account.account_no if account_data.account else ""
    latest_daily = _latest_by(account_data.daily, "date")
    latest_monthly = _latest_by(account_data.monthly, "year_month")
    balance = account_data.balance
    yearly = account_data.yearly
    return {
        "account": mask_account_no(account_no),
        "balance": {
            "present": balance is not None,
            "balance_cny": balance.balance_cny if balance else None,
            "prepay_balance_cny": balance.prepay_balance_cny if balance else None,
            "arrears_cny": balance.arrears_cny if balance else None,
            "observed_at": redact_text(balance.observed_at) if balance else "",
        },
        "daily": {
            "count": len(account_data.daily),
            "range": _range_text([getattr(row, "date", "") for row in account_data.daily]),
            "latest_date": getattr(latest_daily, "date", None) if latest_daily else None,
            "latest_usage_kwh": getattr(latest_daily, "total_usage_kwh", None) if latest_daily else None,
        },
        "monthly": {
            "count": len(account_data.monthly),
            "range": _range_text([getattr(row, "year_month", "") for row in account_data.monthly]),
            "latest_month": getattr(latest_monthly, "year_month", None) if latest_monthly else None,
            "latest_usage_kwh": getattr(latest_monthly, "total_usage_kwh", None) if latest_monthly else None,
            "latest_charge_cny": getattr(latest_monthly, "total_charge_cny", None) if latest_monthly else None,
        },
        "yearly": {
            "present": yearly is not None,
            "year": yearly.year if yearly else None,
            "usage_kwh": yearly.total_usage_kwh if yearly else None,
            "charge_cny": yearly.total_charge_cny if yearly else None,
        },
    }


def collect_snapshot_field_inventory(snapshot: dict[str, Any]) -> dict[str, Any]:
    fields: list[dict[str, Any]] = []
    shapes: list[dict[str, Any]] = []
    state = {"truncated": False}

    for root_path, root_value in _iter_snapshot_roots(snapshot):
        _walk_field_inventory(root_value, root_path, fields, shapes, state, depth=0)
        if len(fields) >= MAX_FIELD_VALUES_PER_PAGE and len(shapes) >= MAX_SHAPES_PER_PAGE:
            state["truncated"] = True
            break

    return {
        "field_value_count": len(fields),
        "shape_count": len(shapes),
        "truncated": state["truncated"],
        "fields": fields,
        "shapes": shapes,
    }


def redact_structure(value: Any, key: str = "") -> Any:
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, child in value.items():
            key_text = str(raw_key)
            safe_key = _safe_output_key(key_text)
            result[safe_key] = redact_structure(child, key_text)
        return result
    if isinstance(value, list):
        return [redact_structure(item, key) for item in value]
    if isinstance(value, tuple):
        return [redact_structure(item, key) for item in value]
    return redact_scalar(value, key)


def redact_scalar(value: Any, key: str = "") -> Any:
    if value is None or isinstance(value, bool):
        return value
    key_text = str(key or "")
    if isinstance(value, (int, float)) and not (
        _is_secret_key(key_text) or _is_account_key(key_text) or _is_pii_key(key_text)
    ):
        return value
    text = str(value)
    if _is_secret_key(key_text):
        return "<redacted>"
    if _is_account_key(key_text):
        return _mask_accounts_and_phones(text)
    if _is_pii_key(key_text):
        if _ACCOUNT_RE.search(text) or _PHONE_RE.search(text):
            return _mask_accounts_and_phones(text)
        return "<redacted>"
    if _URL_RE.search(text):
        return _mask_accounts_and_phones(redact_url(text))
    return _truncate(_mask_accounts_and_phones(redact_text(text)), 240)


def _safe_env_snapshot() -> dict[str, Any]:
    keys = (
        "PYTHON_IN_DOCKER",
        "PUBLISHER",
        "MQTT_HOST",
        "MQTT_PORT",
        "MQTT_DISCOVERY_PREFIX",
        "JOB_START_TIME",
        "SGCC_DAILY_RUNS",
        "SGCC_BROWSER_MODE",
        "SGCC_CDP_ADDRESS",
        "SGCC_CDP_HOST",
        "SGCC_CDP_PORT",
        "SGCC_BROWSER_SERVICE_URL",
        "SGCC_BROWSER_SERVICE_STOP_ON_RELEASE",
        "SGCC_DB_PATH",
        "SCRAPER_SETTLE_SECONDS",
        "DEBUG_MODE",
        "SGCC_DIAG",
        "SGCC_DIAG_DIR",
        "SGCC_LOGIN_COOLDOWN_ENABLED",
        "SGCC_QRCODE_FALLBACK_UNATTENDED",
    )
    return {key: redact_scalar(os.getenv(key, ""), key) for key in keys if key in os.environ}


def _write_package(
    directory: Path,
    summary_text: str,
    summary_json: dict[str, Any],
    fields_json: dict[str, Any],
) -> None:
    (directory / "summary.txt").write_text(summary_text, encoding="utf-8")
    (directory / "summary.json").write_text(
        json.dumps(summary_json, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    (directory / "fields.redacted.json").write_text(
        json.dumps(fields_json, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _iter_snapshot_roots(snapshot: dict[str, Any]):
    store = snapshot.get("store") if isinstance(snapshot, dict) else None
    if isinstance(store, dict):
        if isinstance(store.get("state"), dict):
            yield "store.state", store["state"]
        if isinstance(store.get("getters"), dict):
            yield "store.getters", store["getters"]
        if isinstance(store.get("route"), dict):
            yield "store.route", store["route"]
        for index, snap in enumerate(store.get("snapshots") or []):
            if isinstance(snap, dict):
                if isinstance(snap.get("state"), dict):
                    yield f"store.snapshots[{index}].state", snap["state"]
                if isinstance(snap.get("getters"), dict):
                    yield f"store.snapshots[{index}].getters", snap["getters"]
    elif store:
        yield "store", store

    components = snapshot.get("components") if isinstance(snapshot, dict) else None
    if isinstance(components, list):
        for index, component in enumerate(components):
            if not isinstance(component, dict):
                yield f"component[{index}]", component
                continue
            meta = {
                key: component.get(key)
                for key in ("tag", "id", "className")
                if component.get(key) not in (None, "")
            }
            if meta:
                yield f"component[{index}].meta", meta
            if isinstance(component.get("data"), dict):
                yield f"component[{index}].data", component["data"]
    elif isinstance(components, dict):
        if isinstance(components.get("data"), dict):
            yield "component.data", components["data"]
        else:
            yield "components", components


def _walk_field_inventory(
    value: Any,
    path: str,
    fields: list[dict[str, Any]],
    shapes: list[dict[str, Any]],
    state: dict[str, bool],
    depth: int,
) -> None:
    if len(fields) >= MAX_FIELD_VALUES_PER_PAGE:
        state["truncated"] = True
        return
    if depth > MAX_FIELD_DEPTH:
        state["truncated"] = True
        _add_shape(shapes, {"path": path, "type": _type_name(value), "truncated": "max_depth"})
        return

    if isinstance(value, dict):
        keys = list(value.keys())
        _add_shape(shapes, {
            "path": path,
            "type": "dict",
            "size": len(keys),
            "keys": [_safe_output_key(str(key)) for key in keys[:60]],
            "keys_truncated": len(keys) > 60,
        })
        for index, (key, child) in enumerate(value.items()):
            if index >= MAX_DICT_ITEMS_PER_OBJECT:
                state["truncated"] = True
                break
            child_path = f"{path}.{_path_key(key)}"
            _walk_field_inventory(child, child_path, fields, shapes, state, depth + 1)
            if len(fields) >= MAX_FIELD_VALUES_PER_PAGE:
                state["truncated"] = True
                break
        return

    if isinstance(value, list):
        _add_shape(shapes, {"path": path, "type": "list", "length": len(value)})
        for index, child in enumerate(value[:MAX_LIST_ITEMS_PER_ARRAY]):
            _walk_field_inventory(child, f"{path}[{index}]", fields, shapes, state, depth + 1)
            if len(fields) >= MAX_FIELD_VALUES_PER_PAGE:
                state["truncated"] = True
                break
        if len(value) > MAX_LIST_ITEMS_PER_ARRAY:
            state["truncated"] = True
        return

    if isinstance(value, tuple):
        _walk_field_inventory(list(value), path, fields, shapes, state, depth)
        return

    fields.append({
        "path": path,
        "key": _path_leaf(path),
        "type": _type_name(value),
        "value": redact_scalar(value, _path_leaf(path)),
    })


def _add_shape(shapes: list[dict[str, Any]], shape: dict[str, Any]) -> None:
    if len(shapes) < MAX_SHAPES_PER_PAGE:
        shapes.append(shape)


def _money_candidate_to_dict(candidate: Any) -> dict[str, Any]:
    return {
        "category": getattr(candidate, "category", ""),
        "source": getattr(candidate, "source", ""),
        "key": getattr(candidate, "key", ""),
        "value": getattr(candidate, "value", None),
        "raw_value": redact_scalar(getattr(candidate, "raw_value", ""), getattr(candidate, "key", "")),
        "label": redact_scalar(getattr(candidate, "label", ""), "label"),
        "account": getattr(candidate, "account", ""),
        "time": redact_text(getattr(candidate, "time", "")),
        "period": redact_text(getattr(candidate, "period", "")),
    }


def _package_version() -> str:
    try:
        return metadata.version("sgcc-home-assistant-bridge")
    except metadata.PackageNotFoundError:
        return ""


def _latest_by(rows: list[Any], attr: str) -> Any:
    values = [row for row in rows if getattr(row, attr, "")]
    return max(values, key=lambda row: getattr(row, attr, "")) if values else None


def _range_text(values: list[str]) -> str:
    clean = sorted(value for value in values if value)
    if not clean:
        return "-"
    if len(clean) == 1:
        return clean[0]
    return f"{clean[0]}..{clean[-1]}"


def _money_summary(balance: Any) -> str:
    if not isinstance(balance, dict) or not balance.get("present"):
        return "no"
    return (
        "yes("
        f"amount={_dash_none(balance.get('balance_cny'))}, "
        f"prepay={_dash_none(balance.get('prepay_balance_cny'))}, "
        f"arrears={_dash_none(balance.get('arrears_cny'))}"
        ")"
    )


def _series_summary(series: Any) -> str:
    if not isinstance(series, dict):
        return "0(-)"
    latest = series.get("latest_date") or series.get("latest_month") or "-"
    return f"{series.get('count', 0)}({series.get('range') or '-'}, latest={latest})"


def _yearly_summary(yearly: Any) -> str:
    if not isinstance(yearly, dict) or not yearly.get("present"):
        return "no"
    return f"{yearly.get('year') or '-'} usage={_dash_none(yearly.get('usage_kwh'))} charge={_dash_none(yearly.get('charge_cny'))}"


def _dash_none(value: Any) -> str:
    return "-" if value is None else str(value)


def _mask_accounts_and_phones(text: str) -> str:
    text = _ACCOUNT_RE.sub(lambda m: mask_account_no(m.group(1)), str(text))
    return _PHONE_RE.sub(lambda m: mask_account_no(m.group(1), keep_last=2), text)


def _is_secret_key(key: str) -> bool:
    if key == "<sensitive>":
        return True
    return bool(_SECRET_KEY_RE.search(key or ""))


def _is_pii_key(key: str) -> bool:
    return bool(_PII_KEY_RE.search(key or ""))


def _is_account_key(key: str) -> bool:
    return bool(_ACCOUNT_KEY_RE.search(key or ""))


def _safe_output_key(key: str) -> str:
    if _is_secret_key(key):
        return "<sensitive>"
    return _truncate(key, 80)


def _path_key(key: Any) -> str:
    text = _safe_output_key(str(key))
    if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", text):
        return text
    if text == "<sensitive>":
        return text
    return "['" + text.replace("'", "\\'") + "']"


def _path_leaf(path: str) -> str:
    if "." in path:
        return path.rsplit(".", 1)[-1].strip("[]'")
    return path.strip("[]'")


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int) and not isinstance(value, bool):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    return type(value).__name__


def _truncate(text: str, limit: int) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
