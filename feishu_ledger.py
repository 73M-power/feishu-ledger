"""Chat-first shared expense ledger for Feishu groups.

The module is intentionally framework-free so it can be used from the
existing http.server based app, tests, or one-off scripts.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path


LEDGER_FILE = "feishu_ledger.json"

CATEGORY_RULES = [
    ("餐饮", ("外卖", "吃饭", "餐", "饭", "奶茶", "咖啡", "早餐", "午餐", "晚餐", "夜宵", "火锅", "烧烤")),
    ("买菜", ("买菜", "菜", "水果", "超市", "便利店", "盒马", "山姆", "菜市场")),
    ("住房", ("房租", "租金", "物业", "水电", "水费", "电费", "燃气", "网费", "宽带")),
    ("交通", ("打车", "地铁", "公交", "停车", "油费", "高铁", "机票", "出租")),
    ("日用品", ("纸巾", "洗衣", "清洁", "垃圾袋", "日用", "家居", "厨房")),
    ("娱乐", ("电影", "游戏", "ktv", "KTV", "演出", "门票", "酒吧")),
    ("医疗", ("药", "医院", "挂号", "体检", "牙", "医保")),
    ("宠物", ("猫", "狗", "宠物", "猫粮", "狗粮", "疫苗")),
]

HELP_TEXT = (
    "共享账本用法:\n"
    "- 今天买菜 83 AA\n"
    "- 昨天外卖 56 我付\n"
    "- 收入3000 工资 / 入账3000\n"
    "- 删除上一笔 / 删除今天买菜83\n"
    "- 账本本月 / 账本明细 / 账本结算 / 账本对比 / 导出本月\n"
    "- 同步飞书表格 / 清理总结行\n"
    "默认按成员 AA；可在 /api/ledger/settings 配成员。"
)

def ledger_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / LEDGER_FILE


def default_ledger() -> dict:
    return {
        "settings": {
            "currency": "CNY",
            "members": [],
            "default_split": "equal",
            "timezone": "Asia/Shanghai",
        },
        "entries": [],
        "seen_events": {},
        "created_at": None,
        "updated_at": None,
    }


def load_ledger(data_dir: str | Path) -> dict:
    path = ledger_path(data_dir)
    if not path.exists():
        return default_ledger()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return default_ledger()
    base = default_ledger()
    if isinstance(data, dict):
        base.update(data)
        base["settings"] = {**default_ledger()["settings"], **(data.get("settings") or {})}
        base.setdefault("entries", [])
        base.setdefault("seen_events", {})
    return base


def save_ledger(data_dir: str | Path, data: dict) -> None:
    path = ledger_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().isoformat(timespec="seconds")
    data.setdefault("created_at", now)
    data["updated_at"] = now
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def _member_names(settings: dict) -> list[str]:
    names = []
    for item in settings.get("members") or []:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = (item.get("name") or item.get("display_name") or item.get("id") or "").strip()
        else:
            name = ""
        if name and name not in names:
            names.append(name)
    return names


def update_settings(data_dir: str | Path, patch: dict) -> dict:
    ledger = load_ledger(data_dir)
    settings = ledger.setdefault("settings", {})
    for key in ("currency", "members", "default_split", "timezone"):
        if key in patch:
            settings[key] = patch[key]
    save_ledger(data_dir, ledger)
    return settings


def _parse_date(text: str, now: datetime) -> tuple[str, str]:
    clean = text
    if "前天" in text:
        d = now.date() - timedelta(days=2)
        clean = clean.replace("前天", "")
        return d.isoformat(), clean
    if "昨天" in text or "昨晚" in text:
        d = now.date() - timedelta(days=1)
        clean = clean.replace("昨天", "").replace("昨晚", "")
        return d.isoformat(), clean
    if "今天" in text or "今晚" in text:
        clean = clean.replace("今天", "").replace("今晚", "")
        return now.date().isoformat(), clean

    m = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", text)
    if m:
        y, mo, da = map(int, m.groups())
        clean = clean.replace(m.group(0), "")
        return date(y, mo, da).isoformat(), clean

    m = re.search(r"(?<!\d)(\d{1,2})[-/.月](\d{1,2})日?", text)
    if m:
        mo, da = map(int, m.groups())
        clean = clean.replace(m.group(0), "")
        return date(now.year, mo, da).isoformat(), clean

    return now.date().isoformat(), clean


AMOUNT_PATTERN = r"(?:¥|￥)?\s*(\d+(?:\.\d+)?)\s*(?:元|块|rmb|RMB)?"


def _amount_candidates(text: str) -> list[tuple[int, int, float, str]]:
    candidates = []
    for m in re.finditer(AMOUNT_PATTERN, text):
        value = float(m.group(1))
        if 0 < value < 1_000_000:
            prev_char = text[m.start() - 1:m.start()]
            next_char = text[m.end():m.end() + 1]
            has_currency = bool(re.search(r"(元|块|rmb|RMB|¥|￥)", m.group(0)))
            if not has_currency and (next_char in ("月", "日", "号") or prev_char in ("年", "/", "-", ".")):
                continue
            candidates.append((m.start(), m.end(), value, m.group(0)))
    return candidates


def _parse_amount(text: str, *, prefer: str = "last") -> float | None:
    candidates = _amount_candidates(text)
    if not candidates:
        return None
    chosen = candidates[0] if prefer == "first" else candidates[-1]
    return round(chosen[2], 2)


def _parse_income_amount(text: str) -> float | None:
    m = re.search(r"^(收入|入账|到账|收到|工资|薪水|薪资|奖金|报销)\s*[:：]?\s*" + AMOUNT_PATTERN, text)
    if m:
        return round(float(m.group(2)), 2)
    return _parse_amount(text, prefer="first")


def _strip_amount(text: str, amount: float) -> str:
    amount_text = str(int(amount)) if float(amount).is_integer() else str(amount)
    return re.sub(rf"(?:¥|￥)?\s*{re.escape(amount_text)}\s*(?:元|块|rmb|RMB)?", "", text, count=1).strip(" ，,。")


def _category(text: str) -> str:
    for cat, keys in CATEGORY_RULES:
        if any(k in text for k in keys):
            return cat
    return "其他"


def _sender_name(sender: dict | None) -> str:
    sender = sender or {}
    for key in ("name", "display_name", "user_name", "open_id", "user_id", "id"):
        value = (sender.get(key) or "").strip()
        if value:
            return value
    return "我"


def _find_named_members(text: str, members: list[str]) -> list[str]:
    found = []
    for name in members:
        if name and name in text and name not in found:
            found.append(name)
    return found


def _parse_payer(text: str, sender_name: str, members: list[str]) -> str:
    if re.search(r"(我|自己)(付|付款|垫|买单|出了)", text):
        return sender_name
    for name in members:
        if re.search(re.escape(name) + r"\s*(付|付款|垫|买单|出了)", text):
            return name
    m = re.search(r"([A-Za-z0-9_\-\u4e00-\u9fff]{1,16})\s*(付|付款|垫|买单|出了)", text)
    if m:
        name = m.group(1)
        return sender_name if name in ("我", "自己") else name
    return sender_name


def _parse_participants(text: str, sender_name: str, payer: str, members: list[str]) -> list[str]:
    named = _find_named_members(text, members)
    if re.search(r"AA|aa|平摊|均摊|分摊|一起", text):
        participants = named or members or [sender_name, payer]
    elif named:
        participants = named
    else:
        participants = members if len(members) == 2 and re.search(r"家庭|家里|共同|一起", text) else [sender_name]

    excludes = []
    for name in members:
        if re.search(r"(不算|除[了外]?|除了)" + re.escape(name), text):
            excludes.append(name)
    out = []
    for name in participants:
        if name and name not in excludes and name not in out:
            out.append(name)
    if not out:
        out = [sender_name]
    return out


def parse_expense(text: str, sender: dict | None = None, settings: dict | None = None,
                  now: datetime | None = None) -> dict:
    now = now or datetime.now()
    settings = settings or {}
    raw = (text or "").strip()
    if not raw:
        raise ValueError("消息为空")
    entry_date, body = _parse_date(raw, now)
    amount = _parse_amount(body)
    if amount is None:
        raise ValueError("没有识别到金额")

    members = _member_names(settings)
    sender_name = _sender_name(sender)
    payer = _parse_payer(body, sender_name, members)
    participants = _parse_participants(body, sender_name, payer, members)
    description = _strip_amount(body, amount)
    description = re.sub(r"\b(AA|aa)\b|平摊|均摊|分摊|我付|我垫|付款|买单|出了", "", description)
    description = re.sub(r"\s+", " ", description).strip(" ，,。") or raw

    per_person = round(amount / len(participants), 2)
    shares = {p: per_person for p in participants}
    rounding = round(amount - sum(shares.values()), 2)
    if rounding:
        shares[participants[-1]] = round(shares[participants[-1]] + rounding, 2)

    sig = f"{entry_date}|{amount}|{payer}|{','.join(participants)}|{description}|{time.time()}"
    return {
        "id": "exp_" + hashlib.sha1(sig.encode("utf-8")).hexdigest()[:12],
        "type": "expense",
        "date": entry_date,
        "time": now.strftime("%H:%M"),
        "amount": amount,
        "currency": settings.get("currency", "CNY"),
        "category": _category(raw),
        "description": description,
        "payer": payer,
        "participants": participants,
        "split_mode": "equal",
        "shares": shares,
        "raw_text": raw,
        "created_at": now.isoformat(timespec="seconds"),
    }


def add_expense(data_dir: str | Path, text: str, sender: dict | None = None,
                source: dict | None = None, now: datetime | None = None) -> dict:
    ledger = load_ledger(data_dir)
    event_id = (source or {}).get("event_id")
    if event_id and event_id in ledger.get("seen_events", {}):
        entry_id = ledger["seen_events"][event_id]
        existing = next((e for e in ledger.get("entries", []) if e.get("id") == entry_id), None)
        return {"ok": True, "duplicate": True, "entry": existing}

    entry = parse_expense(text, sender=sender, settings=ledger.get("settings"), now=now)
    if source:
        entry["source"] = source
    ledger.setdefault("entries", []).append(entry)
    ledger["entries"].sort(key=lambda e: (e.get("date", ""), e.get("time", "")))
    if event_id:
        ledger.setdefault("seen_events", {})[event_id] = entry["id"]
    save_ledger(data_dir, ledger)
    return {"ok": True, "entry": entry, "summary": month_summary(ledger, entry["date"][:7])}


def month_of(dt: datetime | None = None) -> str:
    dt = dt or datetime.now()
    return dt.strftime("%Y-%m")


def _entries_for_month(ledger: dict, month: str) -> list[dict]:
    return [e for e in ledger.get("entries", []) if str(e.get("date", "")).startswith(month)]


def _entry_type(entry: dict) -> str:
    raw = str(entry.get("type") or "expense").strip().lower()
    if raw in ("income", "收入"):
        return "income"
    if raw in ("expense", "支出"):
        return "expense"
    return "unknown"


def _expense_entries_for_month(ledger: dict, month: str) -> list[dict]:
    return [e for e in _entries_for_month(ledger, month) if _entry_type(e) == "expense"]


def _income_entries_for_month(ledger: dict, month: str) -> list[dict]:
    return [e for e in _entries_for_month(ledger, month) if _entry_type(e) == "income"]


def month_summary(ledger: dict, month: str | None = None) -> dict:
    month = month or month_of()
    if str(month).lower() in ("all", "全部", "总计"):
        month_key = "all"
        month_label = "全部"
        entries = list(ledger.get("entries", []))
    else:
        month_key = month
        month_label = month
        entries = _entries_for_month(ledger, month)
    expense_entries = [e for e in entries if _entry_type(e) == "expense"]
    income_entries = [e for e in entries if _entry_type(e) == "income"]
    ignored_entries = [e for e in entries if _entry_type(e) == "unknown"]
    ignored_count = len(ignored_entries) + int(ledger.get("ignored_type_count", 0) or 0)
    by_category = defaultdict(float)
    by_income_category = defaultdict(float)
    by_payer = defaultdict(float)
    owed_by_person = defaultdict(float)
    paid_by_person = defaultdict(float)
    daily = defaultdict(float)
    daily_income = defaultdict(float)

    for e in expense_entries:
        amount = float(e.get("amount") or 0)
        by_category[e.get("category") or "其他"] += amount
        payer = e.get("payer") or "未知"
        by_payer[payer] += amount
        paid_by_person[payer] += amount
        daily[e.get("date", "")] += amount
        for person, share in (e.get("shares") or {}).items():
            owed_by_person[person] += float(share or 0)

    for e in income_entries:
        amount = float(e.get("amount") or 0)
        by_income_category[e.get("category") or "收入"] += amount
        daily_income[e.get("date", "")] += amount

    balances = {}
    for person in set(list(owed_by_person.keys()) + list(paid_by_person.keys())):
        balances[person] = round(paid_by_person[person] - owed_by_person[person], 2)

    expense_total = round(sum(float(e.get("amount") or 0) for e in expense_entries), 2)
    income_total = round(sum(float(e.get("amount") or 0) for e in income_entries), 2)
    return {
        "month": month_label,
        "summary_key": month_key,
        "count": len(expense_entries),
        "entry_count": len(entries),
        "income_count": len(income_entries),
        "ignored_count": ignored_count,
        "total": expense_total,
        "expense_total": expense_total,
        "income_total": income_total,
        "net_total": round(income_total - expense_total, 2),
        "avg_per_entry": round(expense_total / len(expense_entries), 2) if expense_entries else 0,
        "by_category": _round_map(by_category),
        "by_income_category": _round_map(by_income_category),
        "by_payer": _round_map(by_payer),
        "owed_by_person": _round_map(owed_by_person),
        "paid_by_person": _round_map(paid_by_person),
        "balances": balances,
        "settlements": settle_balances(balances),
        "daily": _round_map(daily),
        "daily_income": _round_map(daily_income),
    }


def _round_map(values: dict) -> dict:
    return {k: round(v, 2) for k, v in sorted(values.items(), key=lambda item: str(item[0])) if k}


def settle_balances(balances: dict[str, float]) -> list[dict]:
    creditors = sorted([(p, round(v, 2)) for p, v in balances.items() if v > 0.009], key=lambda x: -x[1])
    debtors = sorted([(p, round(-v, 2)) for p, v in balances.items() if v < -0.009], key=lambda x: -x[1])
    result = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        debtor, debt = debtors[i]
        creditor, credit = creditors[j]
        amount = round(min(debt, credit), 2)
        if amount > 0:
            result.append({"from": debtor, "to": creditor, "amount": amount})
        debt = round(debt - amount, 2)
        credit = round(credit - amount, 2)
        debtors[i] = (debtor, debt)
        creditors[j] = (creditor, credit)
        if debt <= 0.009:
            i += 1
        if credit <= 0.009:
            j += 1
    return result


def month_comparison(ledger: dict, months: int = 6, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now()
    months = max(1, min(int(months or 6), 24))
    result = []
    y, m = now.year, now.month
    for offset in range(months - 1, -1, -1):
        yy, mm = y, m - offset
        while mm <= 0:
            yy -= 1
            mm += 12
        summary = month_summary(ledger, f"{yy:04d}-{mm:02d}")
        prev_total = result[-1]["expense_total"] if result else None
        summary["change_from_prev"] = (
            None if prev_total in (None, 0)
            else round((summary["expense_total"] - prev_total) / prev_total * 100, 1)
        )
        result.append(summary)
    return result


def export_month_csv(ledger: dict, month: str | None = None) -> str:
    month = month or month_of()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["类型", "日期", "时间", "类别", "描述", "金额", "付款人/收款人", "参与人", "分摊", "备注"])
    for e in _entries_for_month(ledger, month):
        is_income = _entry_type(e) == "income"
        shares = "; ".join(f"{p}:{v}" for p, v in (e.get("shares") or {}).items())
        writer.writerow([
            "收入" if is_income else "支出",
            e.get("date", ""),
            e.get("time", ""),
            e.get("category", ""),
            e.get("description", ""),
            e.get("amount", 0),
            e.get("receiver") or e.get("payer", ""),
            "、".join(e.get("participants") or []),
            shares,
            e.get("raw_text", ""),
        ])
    return output.getvalue()


def format_entry_reply(entry: dict, summary: dict | None = None) -> str:
    parts = [
        f"已记账: {entry['description']} {entry['amount']:.2f}元",
        f"类别: {entry['category']} | 付款: {entry['payer']}",
        f"分摊: " + "，".join(f"{p} {v:.2f}" for p, v in entry.get("shares", {}).items()),
    ]
    if summary:
        parts.append(f"{summary['month']} 当前合计: {summary['total']:.2f}元，共 {summary['count']} 笔")
    return "\n".join(parts)



def _money(value: float | int | str | None, signed: bool = False) -> str:
    amount = float(value or 0)
    prefix = "+" if signed and amount > 0 else ""
    return f"{prefix}{amount:,.2f}元"


def _format_breakdown(values: dict, total: float, limit: int = 5) -> str:
    items = sorted((values or {}).items(), key=lambda x: -float(x[1]))[:limit]
    if not items:
        return "暂无"
    parts = []
    total = float(total or 0)
    for name, amount in items:
        amount = float(amount or 0)
        if total > 0:
            parts.append(f"{name} {_money(amount)}（{amount / total * 100:.1f}%）")
        else:
            parts.append(f"{name} {_money(amount)}")
    return "；".join(parts)


def _format_settlement(summary: dict) -> str:
    settlements = summary.get("settlements") or []
    if settlements:
        return "；".join(f"{s['from']} -> {s['to']} {_money(s['amount'])}" for s in settlements)
    people = [p for p in (summary.get("balances") or {}) if p]
    if len(people) <= 1:
        return "个人账本，无需结算"
    return "多人分摊已平衡，暂不需要结算"

def format_summary_reply(summary: dict) -> str:
    expense_total = float(summary.get("expense_total", summary.get("total", 0)) or 0)
    income_total = float(summary.get("income_total", 0) or 0)
    net_total = float(summary.get("net_total", 0) or 0)
    entry_count = int(summary.get("entry_count", 0) or 0)
    expense_count = int(summary.get("count", 0) or 0)
    income_count = int(summary.get("income_count", 0) or 0)
    ignored_count = int(summary.get("ignored_count", 0) or 0)
    cat_text = _format_breakdown(summary.get("by_category") or {}, expense_total)
    income_text = _format_breakdown(summary.get("by_income_category") or {}, income_total, limit=3)
    settlement_text = _format_settlement(summary)
    return (
        f"{summary['month']} 账本\n"
        f"统计口径: 按类型字段计算（收入 {income_count} 笔，支出 {expense_count} 笔）\n"
        f"{f'未识别类型: {ignored_count} 笔，未计入\\n' if ignored_count else ''}"
        f"收入: {_money(income_total)}\n"
        f"支出: {_money(expense_total)}\n"
        f"结余: {_money(net_total, signed=True)}\n"
        f"支出分类: {cat_text}\n"
        f"收入分类: {income_text}\n"
        f"结算: {settlement_text}"
    )


def months_with_entries(ledger: dict) -> list[str]:
    months = set()
    for entry in ledger.get("entries", []):
        date_text = str(entry.get("date") or "")
        if re.match(r"^\d{4}-\d{2}", date_text):
            months.add(date_text[:7])
    return sorted(months, reverse=True)


def format_history_summary_reply(ledger: dict, limit: int = 12) -> str:
    months = months_with_entries(ledger)
    if not months:
        return "历史账本\n暂无记录"
    lines = ["历史账本"]
    for month in months[:limit]:
        summary = month_summary(ledger, month)
        lines.append(
            f"{month}: 收入 {summary.get('income_total', 0):.2f}元，"
            f"支出 {summary.get('expense_total', 0):.2f}元，"
            f"净额 {summary.get('net_total', 0):+.2f}元 "
            f"（收入{summary.get('income_count', 0)}笔，支出{summary.get('count', 0)}笔）"
        )
    if len(months) > limit:
        lines.append(f"还有 {len(months) - limit} 个月未显示")
    return "\n".join(lines)


def build_summary_entries(summary: dict, now: datetime | None = None, raw_text: str = "总结") -> list[dict]:
    now = now or datetime.now()
    month = summary.get("month") or month_of(now)
    summary_key = summary.get("summary_key") or month
    created_at = now.isoformat(timespec="seconds")
    date_text = now.date().isoformat()
    if re.match(r"^\d{4}-\d{2}$", str(summary_key)):
        date_text = f"{summary_key}-01"
    time_text = "00:00" if date_text.endswith("-01") else now.strftime("%H:%M")
    return [
        {
            "id": f"sum_{summary_key}_expense",
            "type": "summary",
            "display_type": "支出",
            "date": date_text,
            "time": time_text,
            "amount": float(summary.get("expense_total") or 0),
            "currency": "CNY",
            "category": "月度总结",
            "description": f"{month} 总支出",
            "payer": "账本总结",
            "participants": [],
            "shares": {},
            "raw_text": raw_text,
            "created_at": created_at,
        },
        {
            "id": f"sum_{summary_key}_net",
            "type": "summary",
            "display_type": "收入" if float(summary.get("net_total") or 0) >= 0 else "支出",
            "date": date_text,
            "time": time_text,
            "amount": float(summary.get("net_total") or 0),
            "currency": "CNY",
            "category": "结余",
            "description": f"{month} 结余",
            "receiver": "账本总结",
            "participants": [],
            "shares": {},
            "raw_text": raw_text,
            "created_at": created_at,
        },
    ]


def format_comparison_reply(comparisons: list[dict]) -> str:
    lines = ["月度对比"]
    for item in comparisons:
        change = item.get("change_from_prev")
        suffix = "" if change is None else f" ({change:+.1f}%)"
        lines.append(
            f"{item['month']}: 支出{item.get('expense_total', item.get('total', 0)):.2f}元 / 收入{item.get('income_total', 0):.2f}元 / 净额{item.get('net_total', 0):+.2f}{suffix}"
        )
    return "\n".join(lines)


def parse_feishu_message(payload: dict) -> dict:
    if payload.get("type") == "url_verification":
        return {"kind": "challenge", "challenge": payload.get("challenge")}
    if payload.get("schema") == "2.0":
        header = payload.get("header") or {}
        event = payload.get("event") or {}
        message = event.get("message") or {}
        sender = event.get("sender") or {}
        content = message.get("content") or ""
        try:
            content_obj = json.loads(content) if isinstance(content, str) else content
        except Exception:
            content_obj = {}
        text = content_obj.get("text") or content_obj.get("content") or ""
        sender_id = sender.get("sender_id") or {}
        return {
            "kind": "message",
            "event_id": header.get("event_id"),
            "create_time": header.get("create_time") or message.get("create_time"),
            "message_id": message.get("message_id"),
            "chat_id": message.get("chat_id"),
            "text": text.strip(),
            "sender": {
                "open_id": sender_id.get("open_id"),
                "user_id": sender_id.get("user_id"),
                "union_id": sender_id.get("union_id"),
                "name": sender.get("sender_type") or sender_id.get("open_id") or "我",
            },
        }
    # Older callbacks usually put event.message directly at top level.
    event = payload.get("event") or {}
    if event.get("message") or event.get("text"):
        message = event.get("message") or {}
        return {
            "kind": "message",
            "event_id": payload.get("uuid") or event.get("event_id"),
            "create_time": event.get("create_time") or message.get("create_time"),
            "message_id": message.get("message_id") or event.get("message_id"),
            "chat_id": message.get("chat_id") or event.get("chat_id"),
            "text": (message.get("text") or event.get("text") or "").strip(),
            "sender": {"name": event.get("user_name") or event.get("open_id") or "我"},
        }
    return {"kind": "unknown"}


def _income_category(text: str) -> str:
    rules = [
        ("工资", ("工资", "薪水", "薪资", "月薪")),
        ("奖金", ("奖金", "绩效", "提成", "年终奖")),
        ("报销", ("报销",)),
        ("兼职", ("兼职", "副业", "外快")),
        ("理财", ("利息", "分红", "基金", "股票", "理财")),
        ("转账", ("转账", "收到", "到账")),
    ]
    for cat, keys in rules:
        if any(k in text for k in keys):
            return cat
    return "收入"


def parse_income(text: str, sender: dict | None = None, settings: dict | None = None,
                 now: datetime | None = None) -> dict:
    now = now or datetime.now()
    raw = (text or "").strip()
    if not raw:
        raise ValueError("消息为空")
    entry_date, body = _parse_date(raw, now)
    amount = _parse_income_amount(body)
    if amount is None:
        raise ValueError("没有识别到收入金额")
    description = _strip_amount(body, amount)
    description = re.sub(r"^(收入|入账|到账|收到|工资|薪水|薪资|奖金|报销)\s*[:：]?\s*", "", description).strip(" ，,。：")
    description = re.sub(r"^(备注|说明|用途)\s*[:：]\s*", "", description)
    description = re.sub(r"\s+", " ", description).strip(" ，。") or _income_category(raw)
    receiver = _sender_name(sender)
    sig = f"income|{entry_date}|{amount}|{receiver}|{description}|{time.time()}"
    return {
        "id": "inc_" + hashlib.sha1(sig.encode("utf-8")).hexdigest()[:12],
        "type": "income",
        "date": entry_date,
        "time": now.strftime("%H:%M"),
        "amount": amount,
        "currency": (settings or {}).get("currency", "CNY"),
        "category": _income_category(raw),
        "description": description,
        "receiver": receiver,
        "raw_text": raw,
        "created_at": now.isoformat(timespec="seconds"),
    }


def add_income(data_dir: str | Path, text: str, sender: dict | None = None,
               source: dict | None = None, now: datetime | None = None) -> dict:
    ledger = load_ledger(data_dir)
    event_id = (source or {}).get("event_id")
    if event_id and event_id in ledger.get("seen_events", {}):
        entry_id = ledger["seen_events"][event_id]
        existing = next((e for e in ledger.get("entries", []) if e.get("id") == entry_id), None)
        return {"ok": True, "duplicate": True, "entry": existing}
    entry = parse_income(text, sender=sender, settings=ledger.get("settings"), now=now)
    if source:
        entry["source"] = source
    ledger.setdefault("entries", []).append(entry)
    ledger["entries"].sort(key=lambda e: (e.get("date", ""), e.get("time", ""), e.get("created_at", "")))
    if event_id:
        ledger.setdefault("seen_events", {})[event_id] = entry["id"]
    save_ledger(data_dir, ledger)
    return {"ok": True, "entry": entry, "summary": month_summary(ledger, entry["date"][:7])}


def _format_entry_name(entry: dict) -> str:
    kind = "收入" if _entry_type(entry) == "income" else "支出"
    return f"{kind} {entry.get('date', '')} {entry.get('description', '')} {float(entry.get('amount') or 0):.2f}元"


def format_income_reply(entry: dict, summary: dict | None = None) -> str:
    parts = [
        f"已入账: {entry['description']} {entry['amount']:.2f}元",
        f"类别: {entry['category']} | 收款: {entry.get('receiver', '')}",
    ]
    if summary:
        parts.append(
            f"{summary['month']} 当前收入: {summary.get('income_total', 0):.2f}元，支出: {summary.get('expense_total', 0):.2f}元，净额: {summary.get('net_total', 0):+.2f}元"
        )
    return "\n".join(parts)


def delete_last_entry(data_dir: str | Path) -> dict:
    ledger = load_ledger(data_dir)
    entries = ledger.get("entries") or []
    if not entries:
        return {"ok": False, "error": "账本里还没有记录可删除"}
    entry = entries.pop()
    save_ledger(data_dir, ledger)
    return {"ok": True, "deleted": entry, "summary": month_summary(ledger, str(entry.get("date", ""))[:7])}


def _entry_matches_query(entry: dict, query: str, amount: float | None, date_text: str | None) -> bool:
    if amount is not None and abs(float(entry.get("amount") or 0) - amount) > 0.009:
        return False
    if date_text and entry.get("date") != date_text:
        return False
    haystack = " ".join(str(entry.get(k) or "") for k in ("description", "category", "raw_text", "payer", "receiver"))
    keywords = [w for w in re.split(r"\s+", query.strip()) if w]
    if not keywords:
        return True
    return all(w in haystack for w in keywords)


def delete_entry_by_query(data_dir: str | Path, text: str, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    query = re.sub(r"^(删除|删掉|撤销|取消)\s*", "", (text or "").strip())
    compact = re.sub(r"\s+", "", query)
    if compact in ("上一笔", "上笔", "最近一笔", "最后一笔", ""):
        return delete_last_entry(data_dir)
    date_text, body = _parse_date(query, now)
    # Only treat the parsed date as a filter if the query explicitly mentioned a date word/number.
    has_date = date_text != now.date().isoformat() or any(k in query for k in ("今天", "昨天", "前天", "月", "-", "/", "."))
    amount = _parse_amount(body)
    if amount is not None:
        body = _strip_amount(body, amount)
    body = re.sub(r"\b(AA|aa)\b|我付|付款|买单|出了|收入|入账|支出", "", body)
    body = re.sub(r"\s+", " ", body).strip(" ，。")

    ledger = load_ledger(data_dir)
    entries = ledger.get("entries") or []
    for idx in range(len(entries) - 1, -1, -1):
        if _entry_matches_query(entries[idx], body, amount, date_text if has_date else None):
            entry = entries.pop(idx)
            save_ledger(data_dir, ledger)
            return {"ok": True, "deleted": entry, "summary": month_summary(ledger, str(entry.get("date", ""))[:7])}
    return {"ok": False, "error": "没有找到匹配的账目；可以试试“删除上一笔”"}


def format_delete_reply(result: dict) -> str:
    if not result.get("ok"):
        return "删除失败: " + result.get("error", "未知错误")
    deleted = result.get("deleted") or {}
    summary = result.get("summary") or {}
    return (
        f"已删除: {_format_entry_name(deleted)}\n"
        f"{summary.get('month', '')} 当前收入 {summary.get('income_total', 0):.2f}元，支出 {summary.get('expense_total', 0):.2f}元，净额 {summary.get('net_total', 0):+.2f}元"
    )


def format_detail_reply(ledger: dict, month: str, limit: int = 10) -> str:
    entries = _entries_for_month(ledger, month)
    if not entries:
        return f"{month} 暂无账目"
    lines = [f"{month} 最近 {min(limit, len(entries))} 笔"]
    for e in entries[-limit:]:
        lines.append("- " + _format_entry_name(e))
    lines.append("删错账可发：删除上一笔，或：删除今天买菜100")
    return "\n".join(lines)


def _export_base_url() -> str:
    base = (
        os.environ.get("PUBLIC_BASE_URL")
        or os.environ.get("FEISHU_LEDGER_URL")
        or os.environ.get("RENDER_EXTERNAL_URL")
        or "https://feishu-ledger.onrender.com"
    )
    return base.rstrip("/")


def format_export_reply(month: str) -> str:
    return (
        f"{month} 表格导出链接:\n"
        f"{_export_base_url()}/api/ledger/export.csv?month={month}\n"
        "Excel/WPS 可以直接打开 CSV。"
    )


def handle_chat_command(data_dir: str | Path, text: str, sender: dict | None = None,
                        source: dict | None = None, now: datetime | None = None) -> dict:
    text = (text or "").strip()
    ledger = load_ledger(data_dir)
    now = now or datetime.now()
    compact = re.sub(r"\s+", "", text)
    if compact in ("账本帮助", "帮助", "使用帮助", "账本使用帮助", "/help"):
        return {"ok": True, "reply": HELP_TEXT}
    if compact in ("账本本月", "本月账本", "账本统计", "账本结算", "结算", "当月总结", "本月总结"):
        summary = month_summary(ledger, month_of(now))
        return {"ok": True, "reply": format_summary_reply(summary), "summary": summary}
    if compact in ("历史总结", "历史账本", "全部总结", "全部账本", "总账本"):
        return {"ok": True, "reply": format_history_summary_reply(ledger), "history": True}
    if compact in ("总结", "账本总结", "账本计算", "计算", "当月总结", "本月总结"):
        summary = month_summary(ledger, month_of(now))
        return {"ok": True, "reply": format_summary_reply(summary), "summary": summary}
    if compact in ("账本明细", "本月明细", "明细", "账本列表", "列表"):
        month = month_of(now)
        return {"ok": True, "reply": format_detail_reply(ledger, month), "entries": _entries_for_month(ledger, month)}
    if compact in ("账本对比", "月度对比", "对比"):
        comparisons = month_comparison(ledger, months=6, now=now)
        return {"ok": True, "reply": format_comparison_reply(comparisons), "comparisons": comparisons}
    if compact in ("导出本月", "账本导出", "导出账本", "导出"):
        return {"ok": True, "reply": format_export_reply(month_of(now))}
    if re.match(r"^(删除|删掉|撤销|取消)", text):
        result = delete_entry_by_query(data_dir, text, now=now)
        return {**result, "reply": format_delete_reply(result)}
    if re.match(r"^(收入|入账|到账|收到|工资|薪水|薪资|奖金|报销)\s*", text):
        result = add_income(data_dir, text, sender=sender, source=source, now=now)
        entry = result.get("entry")
        return {
            **result,
            "reply": "" if result.get("duplicate") else format_income_reply(entry, result.get("summary")),
        }
    result = add_expense(data_dir, text, sender=sender, source=source, now=now)
    entry = result.get("entry")
    return {
        **result,
        "reply": "" if result.get("duplicate") else format_entry_reply(entry, result.get("summary")),
    }
