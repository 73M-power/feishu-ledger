"""Feishu Bitable sync helpers for the shared ledger service."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Callable


OPEN_API = "https://open.feishu.cn/open-apis"
CST = timezone(timedelta(hours=8))
_APP_TOKEN = None
_RECORD_CACHE = None


def enabled() -> bool:
    return os.environ.get("FEISHU_DOC_SYNC_ENABLED", "0").lower() in ("1", "true", "yes", "on")


def table_id() -> str:
    return os.environ.get("FEISHU_BITABLE_TABLE_ID", "").strip()


def _request_json(method: str, path: str, token: str, payload: dict | None = None,
                  query: dict | None = None) -> dict:
    if query:
        sep = "&" if "?" in path else "?"
        path = path + sep + urllib.parse.urlencode(query)
    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    url = OPEN_API + path
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        detail = raw.decode("utf-8", errors="replace") if raw else str(exc)
        try:
            result = json.loads(detail)
            msg = result.get("msg") or result.get("message") or detail
            code = result.get("code")
            raise RuntimeError(f"{method} {path} failed: code={code}, msg={msg}") from exc
        except json.JSONDecodeError:
            raise RuntimeError(f"{method} {path} failed: HTTP {exc.code}, body={detail[:300]}") from exc
    result = json.loads(raw) if raw else {}
    if result.get("code") not in (None, 0):
        raise RuntimeError(f"{method} {path} failed: code={result.get('code')}, msg={result.get('msg') or result}")
    return result


def _data_node(result: dict) -> dict:
    data = result.get("data") or {}
    return data.get("node") or data


def app_token(token_provider: Callable[[], str | None]) -> str:
    global _APP_TOKEN
    configured = os.environ.get("FEISHU_BITABLE_APP_TOKEN", "").strip()
    if configured:
        return configured
    if _APP_TOKEN:
        return _APP_TOKEN
    wiki_token = os.environ.get("FEISHU_WIKI_TOKEN", "").strip()
    if not wiki_token:
        raise RuntimeError("missing FEISHU_BITABLE_APP_TOKEN or FEISHU_WIKI_TOKEN")
    token = token_provider()
    if not token:
        raise RuntimeError("missing tenant access token")
    result = _request_json("GET", "/wiki/v2/spaces/get_node", token, query={"token": wiki_token})
    node = _data_node(result)
    obj_token = (node.get("obj_token") or node.get("objToken") or "").strip()
    if not obj_token:
        raise RuntimeError("wiki node did not return obj_token")
    _APP_TOKEN = obj_token
    return _APP_TOKEN


def configured() -> tuple[bool, str | None]:
    if not enabled():
        return False, "FEISHU_DOC_SYNC_ENABLED is disabled"
    if not table_id():
        return False, "missing FEISHU_BITABLE_TABLE_ID"
    if not (os.environ.get("FEISHU_BITABLE_APP_TOKEN") or os.environ.get("FEISHU_WIKI_TOKEN")):
        return False, "missing FEISHU_BITABLE_APP_TOKEN or FEISHU_WIKI_TOKEN"
    return True, None


def _date_ms(value: str) -> int | str:
    try:
        dt = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=CST)
        return int(dt.timestamp() * 1000)
    except Exception:
        return value


def _entry_type(entry: dict) -> str:
    return (entry.get("type") or "expense").lower()


def _display_type(entry: dict) -> str:
    explicit = (entry.get("display_type") or "").strip()
    if explicit:
        return explicit
    return "收入" if _entry_type(entry) == "income" else "支出"


def entry_fields(entry: dict) -> dict:
    shares = entry.get("shares") or {}
    share_text = "; ".join(f"{person}:{amount}" for person, amount in shares.items())
    participants = entry.get("participants") or []
    return {
        "记录ID": entry.get("id", ""),
        "类型": _display_type(entry),
        "日期": _date_ms(entry.get("date", "")),
        "时间": entry.get("time", ""),
        "类别": entry.get("category", ""),
        "描述": entry.get("description", ""),
        "金额": float(entry.get("amount") or 0),
        "付款人/收款人": entry.get("receiver") or entry.get("payer", ""),
        "参与人": "、".join(participants),
        "分摊": share_text,
        "原始消息": entry.get("raw_text", ""),
    }


def _record_id_from_entry(entry: dict) -> str:
    return (
        entry.get("bitable_record_id")
        or ((entry.get("sync") or {}).get("bitable") or {}).get("record_id")
        or ""
    )


def _records_path(app: str) -> str:
    return f"/bitable/v1/apps/{urllib.parse.quote(app, safe='')}/tables/{urllib.parse.quote(table_id(), safe='')}/records"


def list_records(token_provider: Callable[[], str | None], force: bool = False) -> dict[str, str]:
    global _RECORD_CACHE
    if _RECORD_CACHE is not None and not force:
        return dict(_RECORD_CACHE)
    ok, error = configured()
    if not ok:
        raise RuntimeError(error or "bitable sync is not configured")
    token = token_provider()
    if not token:
        raise RuntimeError("missing tenant access token")
    app = app_token(token_provider)
    records = {}
    page_token = ""
    for _ in range(20):
        query = {"page_size": 500}
        if page_token:
            query["page_token"] = page_token
        result = _request_json("GET", _records_path(app), token, query=query)
        data = result.get("data") or {}
        for item in data.get("items") or []:
            fields = item.get("fields") or {}
            ledger_id = str(fields.get("记录ID") or "").strip()
            record_id = item.get("record_id") or ""
            if ledger_id and record_id:
                records[ledger_id] = record_id
        if not data.get("has_more"):
            break
        page_token = data.get("page_token") or ""
        if not page_token:
            break
    _RECORD_CACHE = dict(records)
    return records



def _field_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "".join(_field_text(item) for item in value).strip()
    if isinstance(value, dict):
        for key in ("text", "name", "value", "title", "email", "link"):
            if key in value and value.get(key) is not None:
                return _field_text(value.get(key))
        return " ".join(_field_text(v) for v in value.values()).strip()
    return str(value).strip()


def _field_number(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = _field_text(value).replace(",", "")
    try:
        return float(text)
    except Exception:
        return 0.0


def _field_date(value) -> str:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value) / 1000, CST).date().isoformat()
        except Exception:
            return ""
    text = _field_text(value)
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text[:10], fmt).date().isoformat()
        except Exception:
            pass
    return text[:10].replace("/", "-").replace(".", "-")


def _parse_shares(text: str) -> dict:
    shares = {}
    for part in (text or "").split(";"):
        if ":" not in part:
            continue
        name, amount = part.split(":", 1)
        name = name.strip()
        if not name:
            continue
        try:
            shares[name] = float(amount.strip())
        except Exception:
            pass
    return shares


def record_item_to_entry(item: dict) -> dict | None:
    fields = item.get("fields") or {}
    ledger_id = _field_text(fields.get("记录ID")) or item.get("record_id") or ""
    if ledger_id.startswith("sum_"):
        return None
    category = _field_text(fields.get("类别"))
    description = _field_text(fields.get("描述"))
    if category in ("月度总结", "结余") or description.endswith(("总支出", "结余")):
        return None
    display_type = _field_text(fields.get("类型"))
    if display_type not in ("收入", "支出"):
        # The summary is based strictly on the table's type field. Do not
        # silently classify blank or custom types as expenses.
        return None
    amount = _field_number(fields.get("金额"))
    if not ledger_id or not amount:
        return None
    date_text = _field_date(fields.get("日期"))
    person = _field_text(fields.get("付款人/收款人")) or "user"
    participants_text = _field_text(fields.get("参与人"))
    participants = [p for p in participants_text.replace(",", "、").split("、") if p]
    shares = _parse_shares(_field_text(fields.get("分摊")))
    entry_type = "income" if display_type == "收入" else "expense"
    entry = {
        "id": ledger_id,
        "type": entry_type,
        "date": date_text,
        "time": _field_text(fields.get("时间")),
        "amount": amount,
        "currency": "CNY",
        "category": category or ("收入" if entry_type == "income" else "其他"),
        "description": description or ledger_id,
        "raw_text": _field_text(fields.get("原始消息")),
        "created_at": datetime.now(CST).isoformat(timespec="seconds"),
        "bitable_record_id": item.get("record_id") or "",
    }
    if entry_type == "income":
        entry["receiver"] = person
    else:
        entry["payer"] = person
        entry["participants"] = participants or ([person] if person else [])
        entry["shares"] = shares or ({person: amount} if person else {})
        entry["split_mode"] = "equal"
    return entry


def fetch_entries(token_provider: Callable[[], str | None]) -> dict:
    ok, error = configured()
    if not ok:
        return {"ok": False, "error": error, "entries": []}
    try:
        token = token_provider()
        if not token:
            raise RuntimeError("missing tenant access token")
        app = app_token(token_provider)
        entries = []
        skipped_type_count = 0
        page_token = ""
        for _ in range(20):
            query = {"page_size": 500}
            if page_token:
                query["page_token"] = page_token
            result = _request_json("GET", _records_path(app), token, query=query)
            data = result.get("data") or {}
            for item in data.get("items") or []:
                entry = record_item_to_entry(item)
                if entry:
                    entries.append(entry)
                else:
                    fields = item.get("fields") or {}
                    ledger_id = _field_text(fields.get("记录ID")) or item.get("record_id") or ""
                    display_type = _field_text(fields.get("类型"))
                    if ledger_id and not ledger_id.startswith("sum_") and display_type not in ("收入", "支出"):
                        skipped_type_count += 1
            if not data.get("has_more"):
                break
            page_token = data.get("page_token") or ""
            if not page_token:
                break
        return {
            "ok": True,
            "entries": entries,
            "count": len(entries),
            "skipped_type_count": skipped_type_count,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200], "entries": []}


def sync_entry(entry: dict, token_provider: Callable[[], str | None], update_existing: bool = False) -> dict:
    ok, error = configured()
    if not ok:
        return {"ok": False, "skipped": True, "error": error}
    if not entry or not entry.get("id"):
        return {"ok": False, "error": "missing entry id"}
    try:
        token = token_provider()
        if not token:
            raise RuntimeError("missing tenant access token")
        app = app_token(token_provider)
        existing = _record_id_from_entry(entry) or list_records(token_provider).get(entry["id"], "")
        if existing:
            entry["bitable_record_id"] = existing
            if update_existing:
                path = _records_path(app) + "/" + urllib.parse.quote(existing, safe="")
                _request_json("PUT", path, token, payload={"fields": entry_fields(entry)})
                return {"ok": True, "created": False, "updated": True, "record_id": existing}
            return {"ok": True, "created": False, "record_id": existing}
        result = _request_json("POST", _records_path(app), token, payload={"fields": entry_fields(entry)})
        record = (result.get("data") or {}).get("record") or {}
        record_id = record.get("record_id") or ""
        if not record_id:
            raise RuntimeError("bitable create record returned no record_id")
        global _RECORD_CACHE
        if _RECORD_CACHE is not None:
            _RECORD_CACHE[entry["id"]] = record_id
        entry["bitable_record_id"] = record_id
        return {"ok": True, "created": True, "record_id": record_id}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


def delete_entry(entry: dict, token_provider: Callable[[], str | None]) -> dict:
    ok, error = configured()
    if not ok:
        return {"ok": False, "skipped": True, "error": error}
    if not entry or not entry.get("id"):
        return {"ok": False, "error": "missing entry id"}
    try:
        token = token_provider()
        if not token:
            raise RuntimeError("missing tenant access token")
        record_id = _record_id_from_entry(entry) or list_records(token_provider, force=True).get(entry["id"], "")
        if not record_id:
            return {"ok": True, "deleted": False, "error": "no matching bitable record"}
        app = app_token(token_provider)
        path = _records_path(app) + "/" + urllib.parse.quote(record_id, safe="")
        _request_json("DELETE", path, token)
        global _RECORD_CACHE
        if _RECORD_CACHE is not None:
            _RECORD_CACHE.pop(entry["id"], None)
        return {"ok": True, "deleted": True, "record_id": record_id}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


def delete_records_by_prefix(prefix: str, token_provider: Callable[[], str | None]) -> dict:
    """Delete existing Bitable records whose ledger id starts with prefix."""
    ok, error = configured()
    if not ok:
        return {"ok": False, "skipped": True, "error": error, "deleted": 0, "failed": 0}
    if not prefix:
        return {"ok": False, "error": "missing record id prefix", "deleted": 0, "failed": 0}
    try:
        token = token_provider()
        if not token:
            raise RuntimeError("missing tenant access token")
        records = list_records(token_provider, force=True)
        targets = [(ledger_id, record_id) for ledger_id, record_id in records.items() if ledger_id.startswith(prefix)]
        app = app_token(token_provider)
        deleted = failed = 0
        errors = []
        global _RECORD_CACHE
        for ledger_id, record_id in targets:
            try:
                path = _records_path(app) + "/" + urllib.parse.quote(record_id, safe="")
                _request_json("DELETE", path, token)
                deleted += 1
                if _RECORD_CACHE is not None:
                    _RECORD_CACHE.pop(ledger_id, None)
            except Exception as exc:
                failed += 1
                errors.append(f"{ledger_id}: {str(exc)[:120]}")
        return {
            "ok": failed == 0,
            "prefix": prefix,
            "targets": len(targets),
            "deleted": deleted,
            "failed": failed,
            "error": "; ".join(errors[:3]),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200], "deleted": 0, "failed": 0}


def sync_entries(entries: list[dict], token_provider: Callable[[], str | None]) -> dict:
    ok, error = configured()
    if not ok:
        return {"ok": False, "error": error, "created": 0, "existing": 0, "failed": 0}
    created = existing = failed = 0
    errors = []
    for entry in entries:
        result = sync_entry(entry, token_provider)
        if result.get("ok") and result.get("created"):
            created += 1
        elif result.get("ok"):
            existing += 1
        else:
            failed += 1
            if result.get("error"):
                errors.append(result["error"])
    return {
        "ok": failed == 0,
        "created": created,
        "existing": existing,
        "failed": failed,
        "error": "; ".join(errors[:3]),
    }
