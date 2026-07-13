#!/usr/bin/env python3
"""Standalone Feishu shared ledger service."""

from __future__ import annotations

import argparse
import json
import os
import time
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import feishu_ledger as ledger
import feishu_bitable as bitable

CST = timezone(timedelta(hours=8))
MAX_BODY_BYTES = 2 * 1024 * 1024
APP_CODE_VERSION = "type-based-summary-v1"
_TOKEN = None
_TOKEN_EXPIRY = 0
_EVENT_LOCK = threading.Lock()
MAX_CALLBACK_AGE_SECONDS = int(os.environ.get("FEISHU_MAX_CALLBACK_AGE_SECONDS", "600"))
SUMMARY_SYNC_ENABLED = False


def now_cst() -> datetime:
    return datetime.now(CST)


def data_dir() -> Path:
    path = Path(os.environ.get("LEDGER_DATA_DIR", Path(__file__).parent / "data"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def feishu_config() -> dict:
    return {
        "app_id": os.environ.get("FEISHU_APP_ID", ""),
        "app_secret": os.environ.get("FEISHU_APP_SECRET", ""),
        "verification_token": os.environ.get("FEISHU_VERIFICATION_TOKEN", ""),
        "reply_enabled": os.environ.get("FEISHU_REPLY_ENABLED", "1").lower() not in ("0", "false", "no"),
    }


def tenant_access_token() -> str | None:
    global _TOKEN, _TOKEN_EXPIRY
    if _TOKEN and time.time() < _TOKEN_EXPIRY - 60:
        return _TOKEN
    cfg = feishu_config()
    if not cfg["app_id"] or not cfg["app_secret"]:
        return None
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": cfg["app_id"], "app_secret": cfg["app_secret"]}).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        token = result.get("tenant_access_token")
        if token:
            _TOKEN = token
            _TOKEN_EXPIRY = time.time() + int(result.get("expire", 7200))
        return _TOKEN
    except Exception as exc:
        print(f"[Feishu] tenant token failed: {exc}")
        return None


def reply_feishu_message(message_id: str | None, text: str | None) -> tuple[bool, str | None]:
    cfg = feishu_config()
    if not cfg["reply_enabled"] or not message_id or not text:
        return False, "reply disabled or missing message_id"
    token = tenant_access_token()
    if not token:
        return False, "missing FEISHU_APP_ID/FEISHU_APP_SECRET"
    url = "https://open.feishu.cn/open-apis/im/v1/messages/" + urllib.parse.quote(message_id, safe="") + "/reply"
    payload = {"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
        result = json.loads(raw) if raw else {}
        if result.get("code") not in (None, 0):
            return False, result.get("msg") or str(result)[:120]
        return True, None
    except Exception as exc:
        print(f"[Feishu] reply failed: {exc}")
        return False, str(exc)[:120]


def _sync_reply_text(sync: dict, action: str = "sync") -> str:
    if not bitable.enabled():
        return ""
    if sync.get("ok"):
        if action == "delete":
            return "\n飞书表格: 已删除同步记录" if sync.get("deleted") else "\n飞书表格: 未找到对应记录"
        if sync.get("created") is False:
            return "\n飞书表格: 已存在，未重复新增"
        return "\n飞书表格: 已同步"
    return "\n飞书表格: 同步失败 - " + str(sync.get("error") or "未知错误")[:120]


def _save_entry_sync(data_path: Path, entry_id: str, record_id: str) -> None:
    if not entry_id or not record_id:
        return
    data = ledger.load_ledger(data_path)
    changed = False
    for entry in data.get("entries", []):
        if entry.get("id") == entry_id:
            entry["bitable_record_id"] = record_id
            changed = True
            break
    if changed:
        ledger.save_ledger(data_path, data)



def _event_seen(data_path: Path, event_id: str | None) -> bool:
    if not event_id:
        return False
    with _EVENT_LOCK:
        data = ledger.load_ledger(data_path)
        return event_id in data.get("seen_callbacks", {})


def _claim_event(data_path: Path, event_id: str | None, marker: str = "processing") -> bool:
    if not event_id:
        return True
    with _EVENT_LOCK:
        data = ledger.load_ledger(data_path)
        seen = data.setdefault("seen_callbacks", {})
        if event_id in seen:
            return False
        seen[event_id] = marker
        ledger.save_ledger(data_path, data)
        return True


def _mark_event_seen(data_path: Path, event_id: str | None, marker: str | None = None) -> None:
    if not event_id:
        return
    with _EVENT_LOCK:
        data = ledger.load_ledger(data_path)
        seen = data.setdefault("seen_callbacks", {})
        current = seen.get(event_id)
        if current is None or current == "processing":
            seen[event_id] = marker or "cmd"
            ledger.save_ledger(data_path, data)


def _event_created_at(value) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        ts = float(value)
        while ts > 10_000_000_000:
            ts = ts / 1000
        return datetime.fromtimestamp(ts, CST)
    except Exception:
        pass
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return dt.astimezone(CST)
    except Exception:
        return None


def _message_is_stale(msg: dict, now: datetime | None = None) -> bool:
    created_at = _event_created_at(msg.get("create_time"))
    if not created_at:
        return False
    now = now or now_cst()
    return (now - created_at).total_seconds() > MAX_CALLBACK_AGE_SECONDS

def _sync_command_result(data_path: Path, result: dict) -> dict:
    if not bitable.enabled() or not result.get("ok"):
        return {}
    if result.get("duplicate"):
        return {}
    entries_to_sync = result.get("entries_to_sync") or []
    if entries_to_sync:
        created = updated = existing = failed = 0
        errors = []
        for item in entries_to_sync:
            sync = bitable.sync_entry(item, tenant_access_token, update_existing=True)
            if sync.get("ok"):
                if sync.get("created"):
                    created += 1
                elif sync.get("updated"):
                    updated += 1
                else:
                    existing += 1
            else:
                failed += 1
                if sync.get("error"):
                    errors.append(sync["error"])
        summary_sync = {
            "ok": failed == 0,
            "created": created,
            "updated": updated,
            "existing": existing,
            "failed": failed,
            "error": "; ".join(errors[:3]),
        }
        result["bitable_sync"] = summary_sync
        if result.get("reply"):
            if summary_sync["ok"]:
                result["reply"] += f"\n飞书表格: 汇总已同步（新增 {created}，更新 {updated}，已存在 {existing}）"
            else:
                result["reply"] += f"\n飞书表格: 汇总同步失败 {failed} 条 - {summary_sync['error'][:120]}"
        return summary_sync
    entry = result.get("entry")
    if entry:
        sync = bitable.sync_entry(entry, tenant_access_token)
        if sync.get("record_id"):
            _save_entry_sync(data_path, entry.get("id", ""), sync["record_id"])
        result["bitable_sync"] = sync
        if result.get("reply"):
            result["reply"] += _sync_reply_text(sync)
        return sync
    deleted = result.get("deleted")
    if deleted:
        sync = bitable.delete_entry(deleted, tenant_access_token)
        result["bitable_sync"] = sync
        if result.get("reply"):
            result["reply"] += _sync_reply_text(sync, action="delete")
        return sync
    return {}



def _is_current_summary_command(text: str | None) -> bool:
    compact = "".join(str(text or "").split())
    return compact in ("总结", "账本总结", "账本计算", "计算", "当月总结", "本月总结", "账本本月", "本月账本")


def _is_history_summary_command(text: str | None) -> bool:
    compact = "".join(str(text or "").split())
    return compact in ("历史总结", "历史账本", "全部总结", "全部账本", "总账本")


def _is_summary_command(text: str | None) -> bool:
    return _is_current_summary_command(text) or _is_history_summary_command(text)


def _is_cleanup_summary_rows_command(text: str | None) -> bool:
    compact = "".join(str(text or "").split())
    return compact in ("清理总结行", "删除总结行", "清理汇总行", "删除汇总行", "清理sum行", "清理SUM行")


def _cleanup_summary_rows_result() -> dict:
    result = bitable.delete_records_by_prefix("sum_", tenant_access_token)
    if result.get("ok"):
        reply = (
            f"已清理流水表里的旧总结行：删除 {result.get('deleted', 0)} 条。\n"
            "以后总结只读取真实流水，不再写入流水表。"
        )
    else:
        reply = "清理旧总结行失败: " + str(result.get("error") or "未知错误")[:160]
    return {"ok": result.get("ok"), "reply": reply, "bitable_cleanup": result}


def _summary_source_data(data_path: Path) -> tuple[dict, str, str]:
    data = ledger.load_ledger(data_path)
    source = "local"
    read_error = ""
    if bitable.enabled():
        fetched = bitable.fetch_entries(tenant_access_token)
        if fetched.get("ok"):
            data = {
                **data,
                "entries": fetched["entries"],
                "ignored_type_count": fetched.get("skipped_type_count", 0),
            }
            source = "bitable"
        elif not fetched.get("ok"):
            read_error = fetched.get("error") or "unknown error"
    return data, source, read_error


def _append_summary_source(reply: str, source: str, read_error: str) -> str:
    if read_error:
        return reply + "\n飞书表格: 读取流水失败，已使用本地数据 - " + read_error[:120]
    if source == "bitable":
        return reply + "\n数据来源: 飞书多维表格"
    return reply


def _summary_command_result(data_path: Path, text: str | None, now: datetime) -> dict:
    data, source, read_error = _summary_source_data(data_path)
    if _is_history_summary_command(text):
        result = {
            "ok": True,
            "reply": ledger.format_history_summary_reply(data),
            "history": True,
            "summary_source": source,
        }
        result["reply"] = _append_summary_source(result["reply"], source, read_error)
        result["reply"] += "\n说明: 总结只读取流水，不写入流水表。"
        return result
    summary = ledger.month_summary(data, ledger.month_of(now))
    result = {
        "ok": True,
        "reply": ledger.format_summary_reply(summary),
        "summary": summary,
        "summary_source": source,
    }
    result["reply"] = _append_summary_source(result["reply"], source, read_error)
    result["reply"] += "\n说明: 总结只读取流水，不写入流水表。"
    return result


def _is_sync_all_command(text: str | None) -> bool:
    compact = "".join(str(text or "").split())
    return compact in ("同步飞书表格", "同步表格", "同步账本", "重同步飞书表格")


def _sync_all_bitable(data_path: Path) -> dict:
    data = ledger.load_ledger(data_path)
    result = bitable.sync_entries(data.get("entries", []), tenant_access_token)
    # sync_entries mutates entries with bitable_record_id when it succeeds.
    ledger.save_ledger(data_path, data)
    if result.get("ok"):
        reply = f"飞书表格同步完成: 新增 {result['created']} 条，已存在 {result['existing']} 条。"
    else:
        reply = f"飞书表格同步完成但有失败: 新增 {result.get('created', 0)} 条，已存在 {result.get('existing', 0)} 条，失败 {result.get('failed', 0)} 条。"
        if result.get("error"):
            reply += "\n" + result["error"][:160]
    return {"ok": result.get("ok"), "reply": reply, "bitable_sync": result}


class Handler(BaseHTTPRequestHandler):
    def _json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        size = int(self.headers.get("Content-Length") or 0)
        if size > MAX_BODY_BYTES:
            raise ValueError("request body too large")
        if size <= 0:
            return {}
        return json.loads(self.rfile.read(size))

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        if path in ("/healthz", "/api/ping"):
            self._json({"ok": True})
            return
        if path == "/api/version":
            self._json({
                "ok": True,
                "code_version": APP_CODE_VERSION,
                "render_git_commit": os.environ.get("RENDER_GIT_COMMIT", ""),
                "doc_sync_enabled": bitable.enabled(),
                "has_wiki_token": bool(os.environ.get("FEISHU_WIKI_TOKEN")),
                "has_bitable_app_token": bool(os.environ.get("FEISHU_BITABLE_APP_TOKEN")),
                "has_bitable_table_id": bool(os.environ.get("FEISHU_BITABLE_TABLE_ID")),
                "bitable_table_id": os.environ.get("FEISHU_BITABLE_TABLE_ID", ""),
                "summary_sync_enabled": SUMMARY_SYNC_ENABLED,
            })
            return
        if path == "/api/ledger":
            data = ledger.load_ledger(data_dir())
            month = qs.get("month", [ledger.month_of(now_cst())])[0]
            months = int(qs.get("months", ["6"])[0])
            self._json({
                "ok": True,
                "data": data,
                "summary": ledger.month_summary(data, month),
                "comparison": ledger.month_comparison(data, months=months, now=now_cst()),
            })
            return
        if path == "/api/ledger/summary":
            data = ledger.load_ledger(data_dir())
            month = qs.get("month", [ledger.month_of(now_cst())])[0]
            self._json({"ok": True, "data": ledger.month_summary(data, month)})
            return
        if path == "/api/ledger/comparison":
            data = ledger.load_ledger(data_dir())
            months = int(qs.get("months", ["6"])[0])
            self._json({"ok": True, "data": ledger.month_comparison(data, months=months, now=now_cst())})
            return
        if path == "/api/ledger/export.csv":
            data = ledger.load_ledger(data_dir())
            month = qs.get("month", [ledger.month_of(now_cst())])[0]
            body = ("\ufeff" + ledger.export_month_csv(data, month)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f"attachment; filename=ledger-{month}.csv")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            body = self._body()
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)}, 400)
            return

        if path == "/api/feishu/ledger/webhook":
            cfg = feishu_config()
            token = cfg.get("verification_token")
            if token and body.get("token") and body.get("token") != token:
                self._json({"ok": False, "error": "invalid feishu verification token"}, 403)
                return
            msg = ledger.parse_feishu_message(body)
            if msg.get("kind") == "challenge":
                self._json({"challenge": msg.get("challenge")})
                return
            if msg.get("kind") != "message" or not msg.get("text"):
                self._json({"ok": True, "ignored": True})
                return
            try:
                data_path = data_dir()
                request_now = now_cst()
                event_id = msg.get("event_id")
                if _message_is_stale(msg, request_now):
                    self._json({"ok": True, "ignored": True, "stale": True})
                    return
                if not _claim_event(data_path, event_id):
                    self._json({"ok": True, "duplicate": True, "ignored": True})
                    return
                if _is_sync_all_command(msg.get("text")):
                    result = _sync_all_bitable(data_path)
                    _mark_event_seen(data_path, event_id, "sync")
                elif _is_cleanup_summary_rows_command(msg.get("text")):
                    result = _cleanup_summary_rows_result()
                    _mark_event_seen(data_path, event_id, "cleanup_summary_rows")
                elif _is_summary_command(msg.get("text")):
                    result = _summary_command_result(data_path, msg.get("text"), request_now)
                    _mark_event_seen(data_path, event_id, "summary")
                else:
                    result = ledger.handle_chat_command(
                        data_path,
                        msg.get("text"),
                        sender=msg.get("sender"),
                        source={
                            "platform": "feishu",
                            "event_id": msg.get("event_id"),
                            "message_id": msg.get("message_id"),
                            "chat_id": msg.get("chat_id"),
                        },
                        now=request_now,
                    )
                    _sync_command_result(data_path, result)
                    _mark_event_seen(data_path, event_id, (result.get("entry") or {}).get("id") or "cmd")
                reply_ok, reply_error = reply_feishu_message(msg.get("message_id"), result.get("reply"))
                self._json({"ok": True, "data": result, "reply_sent": reply_ok, "reply_error": reply_error})
            except Exception as exc:
                text = "没有记上：" + str(exc)[:160] + "\n发送“账本帮助”查看格式。"
                reply_feishu_message(msg.get("message_id"), text)
                self._json({"ok": False, "error": str(exc)[:200]}, 400)
            return

        if path == "/api/ledger/settings":
            self._json({"ok": True, "data": ledger.update_settings(data_dir(), body)})
            return
        if path == "/api/ledger/entry":
            text = (body.get("text") or "").strip()
            sender = body.get("sender") or {"name": "我"}
            try:
                data_path = data_dir()
                if _is_sync_all_command(text):
                    result = _sync_all_bitable(data_path)
                elif _is_cleanup_summary_rows_command(text):
                    result = _cleanup_summary_rows_result()
                elif _is_summary_command(text):
                    result = _summary_command_result(data_path, text, now_cst())
                else:
                    result = ledger.handle_chat_command(data_path, text, sender=sender, source={"platform": "api"}, now=now_cst())
                    _sync_command_result(data_path, result)
                self._json(result)
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)[:200]}, 400)
            return
        self._json({"ok": False, "error": "not found"}, 404)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Feishu shared ledger service")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8787")))
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Feishu ledger running at http://{args.host}:{args.port}")
    print(f"Data dir: {data_dir()}")
    server.serve_forever()


if __name__ == "__main__":
    main()
