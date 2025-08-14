# services/admin_ledger.py
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List
from database.db import get_table, DEFAULT_TABLE
from config import ADMINS, ADMIN_MAIN_ID

LEDGER_TABLE = "admin_ledger"
TRANSACTION_TABLE = "transactions"

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

# ────────────────────────────────────────────────────────────
# سجلات دائمة لإقرار الإداريين (إيداع/صرف)
# ────────────────────────────────────────────────────────────
def log_admin_deposit(admin_id: int, user_id: int, amount: int, note: str = "") -> None:
    # يسجل إيداع وافق عليه الأدمن (مبلغ موجب)
    get_table(LEDGER_TABLE).insert({
        "admin_id": int(admin_id),
        "user_id": int(user_id),
        "action": "deposit",
        "amount": int(amount),
        "note": note,
        "created_at": _now_iso(),
    }).execute()

def log_admin_spend(admin_id: int, user_id: int, amount: int, note: str = "") -> None:
    # يسجل صرف من المحفظة وافق عليه الأدمن (مبلغ موجب يمثل المبلغ المصروف)
    get_table(LEDGER_TABLE).insert({
        "admin_id": int(admin_id),
        "user_id": int(user_id),
        "action": "spend",
        "amount": int(amount),
        "note": note,
        "created_at": _now_iso(),
    }).execute()

def _fmt(amount: int) -> str:
    try:
        return f"{int(amount):,} ل.س"
    except Exception:
        return f"{amount} ل.س"

# ────────────────────────────────────────────────────────────
# تقارير الإداريين
# ────────────────────────────────────────────────────────────
def summarize_assistants(days: int = 7) -> str:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    assistants = [a for a in ADMINS if a != ADMIN_MAIN_ID]
    if not assistants:
        return "لا يوجد أدمن مساعد لإظهار تقريره."
    # اجمع لكل أدمن
    rows = (
        get_table(LEDGER_TABLE)
        .select("admin_id, action, amount, created_at")
        .gte("created_at", since.isoformat())
        .execute()
    )
    data = rows.data or []
    totals: Dict[int, Dict[str,int]] = {aid: {"deposit":0,"spend":0} for aid in assistants}
    for r in data:
        try:
            aid = int(r.get("admin_id") or 0)
        except Exception:
            continue
        if aid not in totals:
            continue
        act = (r.get("action") or "").strip()
        amt = int(r.get("amount") or 0)
        if act in ("deposit","spend"):
            totals[aid][act] += amt
    # صياغة
    lines = [f"<b>📈 تقرير الأدمن المساعد — آخر {days} يومًا</b>"]
    for aid in assistants:
        t = totals.get(aid, {"deposit":0,"spend":0})
        lines.append(f"• <code>{aid}</code> — شحن: {_fmt(t['deposit'])} | صرف: {_fmt(t['spend'])}")
    lines.append("—"*10)
    lines.append(f"ملاحظة: الأرقام أعلاه تُبنى على سجلات <code>{LEDGER_TABLE}</code> الدائمة.")
    return "\n".join(lines)

def summarize_all_admins(days: int = 7) -> str:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        get_table(LEDGER_TABLE)
        .select("admin_id, action, amount, created_at")
        .gte("created_at", since.isoformat())
        .execute()
    )
    data = rows.data or []
    per_admin: Dict[int, Dict[str,int]] = {}
    grand_dep = 0
    grand_sp = 0
    for r in data:
        try:
            aid = int(r.get("admin_id") or 0)
        except Exception:
            continue
        act = (r.get("action") or "").strip()
        amt = int(r.get("amount") or 0)
        d = per_admin.setdefault(aid, {"deposit":0,"spend":0})
        if act == "deposit":
            d["deposit"] += amt; grand_dep += amt
        elif act == "spend":
            d["spend"] += amt; grand_sp += amt
    lines = [f"<b>📈 تقرير الإداريين (الكل) — آخر {days} يومًا</b>"]
    for aid, t in sorted(per_admin.items(), key=lambda kv:(kv[1]['deposit']+kv[1]['spend']), reverse=True):
        lines.append(f"• <code>{aid}</code> — شحن: {_fmt(t['deposit'])} | صرف: {_fmt(t['spend'])}")
    lines.append("—"*10)
    lines.append(f"<b>الإجمالي</b> — شحن: {_fmt(grand_dep)} | صرف: {_fmt(grand_sp)}")
    return "\n".join(lines)

# ────────────────────────────────────────────────────────────
# مساعد مرن لجلب أسماء/تسميات المستخدمين من جدول المستخدمين
# دون افتراض وجود أعمدة محددة (first_name قد لا يكون موجودًا)
# ────────────────────────────────────────────────────────────
def _load_user_map(user_ids) -> Dict[int, str]:
    user_ids = list({int(u) for u in user_ids if u is not None})
    if not user_ids:
        return {}
    # نجرب عدة صيغ اختيار آمنة حتى لا تُرمى 400 من Supabase
    candidates = [
        ("user_id", "user_id,username"),
        ("user_id", "user_id"),
        ("id",      "id,username"),
        ("id",      "id"),
    ]
    for key, sel in candidates:
        try:
            q = get_table(DEFAULT_TABLE).select(sel)
            # بعض العملاء لديهم in_ في عميل Postgrest
            if hasattr(q, "in_"):
                q = q.in_(key, user_ids)
            rows = q.execute().data or []
            if rows:
                m = {}
                for r in rows:
                    uid = r.get(key)
                    try:
                        uid = int(uid)
                    except Exception:
                        continue
                    label = r.get("username") or f"مستخدم #{uid}"
                    m[uid] = label
                return m
        except Exception:
            # جرّب صيغة أخرى
            continue
    # لو فشلت كل المحاولات
    return {int(uid): f"مستخدم #{int(uid)}" for uid in user_ids}

# ────────────────────────────────────────────────────────────
# أفضل ٥ عملاء أسبوعيًا
# ────────────────────────────────────────────────────────────
def top5_clients_week() -> List[Dict[str, Any]]:
    """
    أفضل 5 عملاء خلال 7 أيام: لكل مستخدم مجموع الشحن (amount>0) والصرف (amount<0) من جدول transactions.
    لا نفترض وجود عمود first_name في جدول المستخدمين، بل نستخدم username إن وُجد، أو تسمية افتراضية.
    """
    since = datetime.now(timezone.utc) - timedelta(days=7)
    tx = (
        get_table(TRANSACTION_TABLE)
        .select("user_id, amount, timestamp")
        .gte("timestamp", since.isoformat())
        .execute()
    )
    data = tx.data or []
    agg: Dict[int, Dict[str,int]] = {}
    for r in data:
        try:
            uid = int(r.get("user_id") or 0)
        except Exception:
            continue
        amt = int(r.get("amount") or 0)
        a = agg.setdefault(uid, {"deposits":0,"spend":0})
        if amt > 0:
            a["deposits"] += amt
        elif amt < 0:
            a["spend"] += abs(amt)

    # اجلب أسماء المستخدمين (مرن)
    name_map = _load_user_map(agg.keys())

    rows: List[Dict[str, Any]] = []
    for uid, v in agg.items():
        rows.append({
            "user_id": uid,
            "name": name_map.get(uid, str(uid)),
            "deposits": int(v.get("deposits", 0)),
            "spend": int(v.get("spend", 0)),
        })

    rows.sort(key=lambda r: (r["deposits"] + r["spend"]), reverse=True)
    return rows[:5]
