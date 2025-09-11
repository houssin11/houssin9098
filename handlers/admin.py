# -*- coding: utf-8 -*-
# handlers/admin.py

import re
import logging
import os
from datetime import datetime, timedelta
from telebot import types
import threading
import time

# التحكم في حذف رسالة الأدمن عند أي إجراء على الطابور
DELETE_ADMIN_MESSAGE_ON_ACTION = False

from services.ads_service import add_channel_ad

from services.admin_ledger import (
    log_admin_deposit,
    log_admin_spend,
    summarize_assistants,
    summarize_all_admins,
    top5_clients_week,
)
from config import ADMINS, ADMIN_MAIN_ID, CHANNEL_USERNAME, FORCE_SUB_CHANNEL_USERNAME
from database.db import get_table, DEFAULT_TABLE

# ===== Safe bot proxy to avoid NameError and record handlers at import time =====
try:
    bot  # will be provided later by main via register(bot, history)
except NameError:
    __admin_pending_handlers__ = []
    class _BotRecorder:
        def __getattr__(self, name):
            if name.endswith("_handler"):
                def factory(*args, **kwargs):
                    def decorator(fn):
                        __admin_pending_handlers__.append((name, args, kwargs, fn))
                        return fn
                    return decorator
                return factory
            def noop(*args, **kwargs):
                # Generic no-op for any other attribute access
                return None
            return noop
    bot = _BotRecorder()
# ===== End proxy =====
USERS_TABLE = (os.getenv("SUPABASE_TABLE_NAME") or DEFAULT_TABLE or "houssin363")  # ← يُحدَّد من .env أولاً
# توحيد الأسماء: لو ما زالت القيمة USERS_TABLE (الافتراضية القديمة) استبدلها بـ houssin363 لتفادي 404
if USERS_TABLE == "USERS_TABLE":
    USERS_TABLE = "houssin363"
logging.info(f"[admin] USERS_TABLE set to: {USERS_TABLE}")
def _collect_clients_with_names():
    """
    يرجع قائمة (user_id, name) من جدول العملاء المحدد.
    الأعمدة المقبولة للاسم: full_name / name / first_name (حسب المتوفر).
    """
    try:
        res = get_table(USERS_TABLE).select("user_id, full_name, name, first_name").execute()
        rows = res.data or []
    except Exception:
        rows = []
    out = []
    for r in rows:
        uid = r.get("user_id") or r.get("id") or r.get("tg_id")
        if not uid:
            continue
        nm = r.get("full_name") or r.get("name") or r.get("first_name")
        out.append((int(uid), nm))
    return out

from services.state_service import purge_state
from services.products_admin import set_product_active, get_product_active, bulk_ensure_products
from services.report_service import totals_deposits_and_purchases_syp, pending_queue_count, summary
from services.discount_service import list_discounts, create_discount, set_discount_active, discount_stats, record_discount_use
from services.system_service import set_maintenance, is_maintenance, maintenance_message, get_logs_tail, force_sub_recheck
from services.activity_logger import log_action
from services.authz import allowed as _allowed
try:
    from services.queue_service import (
except Exception:
    def ((*a, **k):
        return None
    add_pending_request,
    process_queue,
    delete_pending_request,
    postpone_request,
    queue_cooldown_start,
)
from services.wallet_service import (
    register_user_if_not_exist,
    deduct_balance,
    add_purchase,
    add_balance,
    get_balance,
    # ✅ جداول متخصصة
    add_bill_or_units_purchase,
    add_internet_purchase,
    add_cash_transfer_purchase,
    add_companies_transfer_purchase,
    add_university_fees_purchase,
    add_ads_purchase,
    # ✅ الحجز/التصفية الآمنة
    capture_hold,
    release_hold,
    get_product_by_id,
    add_game_purchase,
)
from services.cleanup_service import delete_inactive_users
from handlers import cash_transfer, companies_transfer

# ===== Override 'allowed' محليًا: ADMINS و ADMIN_MAIN_ID لديهم كل الصلاحيات مؤقتًا =====
def allowed(user_id: int, perm: str) -> bool:
    from config import ADMINS, ADMIN_MAIN_ID, CHANNEL_USERNAME, FORCE_SUB_CHANNEL_USERNAME
    return (user_id == ADMIN_MAIN_ID or user_id in ADMINS) or _allowed(user_id, perm)


# لقراءة المجموعات/الملفات والمنتجات المعروضة للمستخدمين
from handlers.products import PRODUCTS

# لوحة المزايا (المحفظة وطرق الشحن…)
from services.feature_flags import ensure_seed, list_features, set_feature_active, list_features_grouped

# محاولة استيراد منظّم الشحن لإزالة القفل المحلي بعد القبول/الإلغاء (استيراد كسول وآمن)
from services.validators import parse_user_id, parse_duration_choice
from services.notification_service import notify_user
from services.ban_service import ban_user, unban_user


def _register_admin_roles(bot):
    @bot.message_handler(func=lambda m: m.text == "👥 صلاحيات الأدمن" and (m.from_user.id == ADMIN_MAIN_ID or m.from_user.id in ADMINS))
    def admins_roles(m):
        ids_str = ", ".join(str(x) for x in ADMINS)
        bot.send_message(m.chat.id, f"الأدمن الرئيسي: {ADMIN_MAIN_ID}\nالأدمنون: {ids_str}")

try:
    from handlers import recharge as recharge_handlers
except Exception:
    recharge_handlers = None

# ─────────────────────────────────────
#   حالة داخلية
# ─────────────────────────────────────
_cancel_pending = {}
_accept_pending = {}
_msg_pending = {}
_broadcast_pending = {}
_msg_by_id_pending = {}
_ban_pending = {}
_unban_pending = {}


# ─────────────────────────────────────
#   تنسيقات ونصوص
# ─────────────────────────────────────
BAND = "━━━━━━━━━━━━━━━━"
CANCEL_HINT_ADMIN = "✋ اكتب /cancel لإلغاء الوضع الحالي."
def _funny_welcome_text(name):
    n = name or "صديقنا"
    return (
        f"🎉 أهلاً يا {n}! 😜🛒\n"
        "نحنا جاهزين نستلم طلباتك بأسرع وقت ⚡️\n"
        "اطلب ولا يهمك… الخدمة عنا مثل القهوة: سريعة وسخنة ☕️🔥\n\n"
        "• شحن ألعاب وتطبيقات 🎮📱\n"
        "• فواتير وتحويل وحدات 💳\n"
        "• اشتراكات وإنترنت 🌐\n"
        "• تحويلات كاش 💸\n\n"
        "إذا عندك سؤال… اسأل قبل ما يبرد الحماس 😁"
    )

def _fmt_syp(n: int) -> str:
    try:
        return f"{int(n):,} ل.س"
    except Exception:
        return f"{n} ل.س"

def _user_name(bot, user_id: int) -> str:
    try:
        ch = bot.get_chat(user_id)
        name = getattr(ch, "first_name", None) or getattr(ch, "full_name", None) or ""
        name = (name or "").strip()
        return name if name else "صديقنا"
    except Exception:
        return "صديقنا"

def _admin_mention(bot, user_id: int) -> str:
    try:
        ch = bot.get_chat(user_id)
        uname = getattr(ch, 'username', None)
        if uname:
            return f"@{uname}"
        name = getattr(ch, 'first_name', None) or getattr(ch, 'full_name', None) or ''
        name = (name or '').strip()
        return name if name else str(user_id)
    except Exception:
        return str(user_id)

def _safe(v, dash="—"):
    v = ("" if v is None else str(v)).strip()
    return v if v else dash

# ====== Helpers for extracting number / ID / code safely ======
def _pick_first(*vals):
    for v in vals:
        if v is None:
            continue
        s = (str(v).strip() if not isinstance(v, str) else v.strip())
        if s:
            return s
    return None

_DEFAULT_KEYS = [
    "number","beneficiary_number","msisdn","phone","player_id","account","account_id",
    "target_id","username","user","id","code","serial","voucher","to","to_user"
]

def _extract_identifier(payload: dict, request_text: str = "", prefer_keys=None) -> str:
    keys = list(prefer_keys or []) + _DEFAULT_KEYS
    for k in keys:
        if k in payload:
            v = payload.get(k)
            s = ("" if v is None else str(v)).strip()
            if s:
                return s
    rt = request_text or ""
    patterns = [
        r"الرقم[^:]*:\s*<code>([^<]+)</code>",
        r"الكود[^:]*:\s*<code>([^<]+)</code>",
        r"آيدي[^:]*:\s*<code>([^<]+)</code>",
        r"ID[^:]*:\s*<code>([^<]+)</code>",
        r"player[^:]*:\s*<code>([^<]+)</code>",
        r"account[^:]*:\s*<code>([^<]+)</code>",
    ]
    for pat in patterns:
        m = re.search(pat, rt, flags=re.IGNORECASE)
        if m:
            s = m.group(1).strip()
            if s:
                return s
    return ""

def _amount_from_payload(payload: dict) -> int:
    for k in ("reserved", "total", "price", "amount"):
        v = payload.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    return 0

def _insert_purchase_row(user_id: int, product_id, product_name: str, price: int, player_id: str):
    data = {
        "user_id": user_id,
        "product_id": (int(product_id) if product_id else None),
        "product_name": product_name,
        "price": int(price),
        "player_id": _safe(player_id, dash=""),
        "created_at": datetime.utcnow().isoformat(),
        "expire_at": (datetime.utcnow() + timedelta(hours=15)).isoformat(),
    }
    try:
        get_table("purchases").insert(data).execute()
    except Exception as e:
        logging.exception("insert purchases failed: %s", e)

def _prompt_admin_note(bot, admin_id: int, user_id: int):
    """يطلب من الأدمن كتابة ملاحظة تُرسل للعميل (اختياري)."""
    try:
        _accept_pending[admin_id] = user_id
        bot.send_message(
            admin_id,
            f"✍️ اكتب ملاحظة للعميل الآن (نص أو صورة)، أو اكتب /skip للتخطي.\n{CANCEL_HINT_ADMIN}",
        )
    except Exception:
        pass

# NEW: تنظيف قفل الشحن المحلي بعد إنهاء الطلب من طرف الأدمن
def _clear_recharge_local_lock_safe(user_id: int):
    try:
        if recharge_handlers and hasattr(recharge_handlers, "clear_pending_request"):
            recharge_handlers.clear_pending_request(user_id)
    except Exception as e:
        logging.exception("[ADMIN] clear recharge local lock failed: %s", e)

# ─────────────────────────────────────
#   متصفح المنتجات للأدمن (حسب الملفات)
# ─────────────────────────────────────
def _slug(s: str) -> str:
    return re.sub(r'[^A-Za-z0-9]+', '-', s).strip('-')[:30]

def _admin_products_groups_markup():
    kb = types.InlineKeyboardMarkup()
    for group in PRODUCTS.keys():
        kb.add(types.InlineKeyboardButton(text=f"📁 {group}", callback_data=f"adm_prod_g:{_slug(group)}"))
    return kb

def _admin_products_list_markup(group_name: str):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for p in PRODUCTS[group_name]:
        active = get_product_active(p.product_id)
        state = "🟢 شغّال" if active else "🔴 موقوف"
        kb.add(types.InlineKeyboardButton(
            text=f"{state} — {p.name} (#{p.product_id})",
            callback_data=f"adm_prod_i:{p.product_id}"
        ))
    kb.add(types.InlineKeyboardButton("⬅️ رجوع للملفات", callback_data="adm_prod_back"))
    return kb

def _admin_product_actions_markup(pid: int):
    active = get_product_active(pid)
    kb = types.InlineKeyboardMarkup()
    if active:
        kb.add(types.InlineKeyboardButton("🚫 إيقاف المنتج", callback_data=f"adm_prod_t:{pid}:0"))
    else:
        kb.add(types.InlineKeyboardButton("✅ تشغيل المنتج", callback_data=f"adm_prod_t:{pid}:1"))
    kb.add(types.InlineKeyboardButton("⬅️ رجوع", callback_data="adm_prod_back"))
    return kb

# ─────────────────────────────────────
#   لوحة المزايا (Feature Flags)
# ─────────────────────────────────────


def _features_home_markup():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📂 حسب المجموعات", callback_data="adm_feat_home:groups"),
        types.InlineKeyboardButton("📜 قائمة مسطحة", callback_data="adm_feat_home:flat"),
    )
    kb.add(types.InlineKeyboardButton("🔄 مزامنة المزايا", callback_data="adm_feat_sync"))
    return kb
def _features_markup(page: int = 0, page_size: int = 10):
# ===== إزالة الازدواجية حسب *التسمية* (تعالج تكرار الشدّات/التوكنز/الجواهر) =====
    import re as _re
    def _norm_label(s: str) -> str:
        s = (s or "").strip()
        s = s.replace("—", "-")
        s = _re.sub(r"[\u200f\u200e]+", "", s)         # إزالة علامات الاتجاه
        s = _re.sub(r"\s+", " ", s)                     # مسافات موحّدة
        # نُبقي الحروف العربية/اللاتينية والأرقام والشرطة
        s = _re.sub(r"[^0-9A-Za-z\u0600-\u06FF\- ]+", "", s)
        return s.lower()

    seen_labels = set()
    unique = []
    for it in items:
        label = (it.get("label") or it.get("key") or "")
        nl = _norm_label(label)
        if nl in seen_labels:
            continue
        seen_labels.add(nl)
        unique.append(it)
    items = unique
    # ===== انتهى منع التكرار =====

    total = len(items)
    kb = types.InlineKeyboardMarkup(row_width=1)
    if total == 0:
        kb.add(types.InlineKeyboardButton("لا توجد مزايا مُسجّلة", callback_data="noop"))
        return kb

    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start_i = page * page_size
    subset = items[start_i : start_i + page_size]

    for it in subset:
        k = it.get("key")
        label = (it.get("label") or k) or ""
        active = bool(it.get("active", True))
        lamp = "🟢" if active else "🔴"
        to = 0 if active else 1
        kb.add(types.InlineKeyboardButton(
            text=f"{lamp} {label}",
            callback_data=f"adm_feat_t:{k}:{to}:{page}"
        ))

    if total_pages > 1:
        prev_page = (page - 1) % total_pages
        next_page = (page + 1) % total_pages
        kb.row(
            types.InlineKeyboardButton("« السابق", callback_data=f"adm_feat_p:{prev_page}"),
            types.InlineKeyboardButton(f"الصفحة {page+1}/{total_pages}", callback_data="noop"),
            types.InlineKeyboardButton("التالي »", callback_data=f"adm_feat_p:{next_page}")
        )
    return kb


def _features_groups_markup():
    """يعرض قائمة المجموعات وعدد العناصر النشطة/الإجمالي داخل كل مجموعة."""
    kb = types.InlineKeyboardMarkup(row_width=1)
    try:
        grouped = list_features_grouped() or {}
    except Exception as e:
        logging.exception("[ADMIN] list_features_grouped failed: %s", e)
        grouped = {}
    # فرز أسماء المجموعات أبجديًا بالعربية/الإنجليزية
    names = sorted(grouped.keys(), key=lambda s: s or "")
    for name in names:
        items = grouped.get(name) or []
        active = sum(1 for it in items if bool(it.get("active", True)))
        total  = len(items)
        kb.add(types.InlineKeyboardButton(f"📁 {name} — {active}/{total}", callback_data=f"adm_feat_g:{name}:0"))
    kb.add(types.InlineKeyboardButton("⬅️ رجوع", callback_data="adm_feat_home:flat"))
    return kb

def _features_group_items_markup(group_name: str, page: int = 0, page_size: int = 10):
    kb = types.InlineKeyboardMarkup(row_width=1)
    try:
        grouped = list_features_grouped() or {}
        items = grouped.get(group_name) or []
    except Exception as e:
        logging.exception("[ADMIN] list_features_grouped failed: %s", e)
        items = []
    total = len(items)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages-1))
    start = page * page_size
    page_items = items[start:start+page_size]

    # أزرار التبديل الفردي
    for it in page_items:
        k = it.get("key") or ""
        label = it.get("label") or k
        active = bool(it.get("active", True))
        lamp = "🟢" if active else "🔴"
        to = 0 if active else 1
        kb.add(types.InlineKeyboardButton(
            text=f"{lamp} {label}",
            callback_data=f"adm_feat_t:{k}:{to}:{page}"
        ))

    # شريط الصفحات
    if total_pages > 1:
        prev_page = (page - 1) % total_pages
        next_page = (page + 1) % total_pages
        kb.row(
            types.InlineKeyboardButton("« السابق", callback_data=f"adm_feat_g:{group_name}:{prev_page}"),
            types.InlineKeyboardButton(f"الصفحة {page+1}/{total_pages}", callback_data="noop"),
            types.InlineKeyboardButton("التالي »", callback_data=f"adm_feat_g:{group_name}:{next_page}")
        )

    # أزرار تشغيل/إيقاف الكل في المجموعة
    kb.row(
        types.InlineKeyboardButton("✅ تشغيل الكل", callback_data=f"adm_feat_gtoggle:{group_name}:1:{page}"),
        types.InlineKeyboardButton("🚫 إيقاف الكل", callback_data=f"adm_feat_gtoggle:{group_name}:0:{page}")
    )
    kb.add(types.InlineKeyboardButton("⬅️ رجوع للمجموعات", callback_data="adm_feat_home:groups"))
    return kb

def register(bot, history):


    globals()["bot"] = bot
    try:
        pending = globals().get("__admin_pending_handlers__", [])
        for _name, _args, _kwargs, _fn in list(pending):
            getattr(bot, _name)(*_args, **_kwargs)(_fn)
        if "__admin_pending_handlers__" in globals():
            globals()["__admin_pending_handlers__"].clear()
    except Exception as _e:
        import logging
        logging.exception("Admin: failed to replay pending handlers: %s", _e)
    @bot.message_handler(func=lambda m: m.text == "⛔ حظر عميل" and _allowed(m.from_user.id, "user:ban"))
    def ban_start(m):
        _ban_pending[m.from_user.id] = {"step": "ask_id"}
        bot.send_message(m.chat.id, "أرسل آيدي العميل المراد حظره.\n/ cancel لإلغاء")

    @bot.message_handler(func=lambda m: _ban_pending.get(m.from_user.id, {}).get("step") == "ask_id")
    def ban_get_id(m):
        try:
            uid = parse_user_id(m.text)
        except Exception:
            return bot.reply_to(m, "❌ آيدي غير صالح. أعد المحاولة، أو اكتب /cancel.")
        st = {"step": "ask_duration", "user_id": uid}
        _ban_pending[m.from_user.id] = st
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("🕒 1 يوم", callback_data=f"adm_ban_dur:1d"),
            types.InlineKeyboardButton("🗓️ 7 أيام", callback_data=f"adm_ban_dur:7d"),
        )
        kb.row(types.InlineKeyboardButton("🚫 دائم", callback_data="adm_ban_dur:perm"))
        bot.send_message(m.chat.id, f"اختر مدة الحظر للعميل <code>{uid}</code>:", parse_mode="HTML", reply_markup=kb)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("adm_ban_dur:"))
    def ban_choose_duration(c):
        st = _ban_pending.get(c.from_user.id)
        if not st:
            try: bot.answer_callback_query(c.id, "لا توجد عملية."); 
            except Exception: pass
            return
        choice = c.data.split(":",1)[1]
        st["duration_choice"] = choice
        st["step"] = "ask_reason"
        _ban_pending[c.from_user.id] = st
        try: bot.answer_callback_query(c.id, "تم.")
        except Exception: pass
        try: bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
        except Exception: pass
        bot.send_message(c.message.chat.id, "اكتب سبب الحظر (إلزامي):" )

    @bot.message_handler(func=lambda m: _ban_pending.get(m.from_user.id, {}).get("step") == "ask_reason")
    def ban_get_reason(m):
        st = _ban_pending.get(m.from_user.id) or {}
        reason = (m.text or '').strip()
        if not reason:
            return bot.reply_to(m, "❌ السبب إلزامي.")
        st["reason"] = reason
        st["step"] = "confirm"
        _ban_pending[m.from_user.id] = st
        uid = st.get("user_id")
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("✔️ تأكيد الحظر", callback_data="adm_ban:confirm"),
            types.InlineKeyboardButton("✖️ إلغاء", callback_data="adm_ban:cancel"),
        )
        bot.send_message(m.chat.id, f"تأكيد حظر <code>{uid}</code>؟", parse_mode="HTML", reply_markup=kb)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("adm_ban:"))
    def ban_confirm(c):
        st = _ban_pending.get(c.from_user.id)
        if not st:
            try: bot.answer_callback_query(c.id, "لا توجد عملية."); 
            except Exception: pass
            return
        action = c.data.split(":",1)[1]
        if action == "cancel":
            _ban_pending.pop(c.from_user.id, None)
            try: bot.answer_callback_query(c.id, "❎ أُلغي.")
            except Exception: pass
            try: bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
            except Exception: pass
            return
        try:
            secs = parse_duration_choice(st.get("duration_choice"))
            until_iso = None
            if secs is not None:
                from datetime import datetime, timezone, timedelta
                until_iso = (datetime.now(timezone.utc) + timedelta(seconds=secs)).isoformat()
            ban_user(st["user_id"], c.from_user.id, st["reason"], banned_until_iso=until_iso)
            log_action(c.from_user.id, "user:ban", reason=f"uid:{st['user_id']} until:{until_iso or 'perm'} reason:{st['reason']}")
            bot.send_message(c.message.chat.id, "✅ تم الحظر.")
        except Exception as e:
            bot.send_message(c.message.chat.id, f"❌ تعذّر الحظر: {e}")
        finally:
            _ban_pending.pop(c.from_user.id, None)
        try: bot.answer_callback_query(c.id, "تم.")
        except Exception: pass
        try: bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
        except Exception: pass

    @bot.message_handler(func=lambda m: m.text == "✅ فكّ الحظر" and _allowed(m.from_user.id, "user:unban"))
    def unban_start(m):
        _unban_pending[m.from_user.id] = {"step": "ask_id"}
        bot.send_message(m.chat.id, "أرسل آيدي العميل لفك الحظر.\n/ cancel لإلغاء")    

    @bot.message_handler(func=lambda m: _unban_pending.get(m.from_user.id, {}).get("step") == "ask_id")
    def unban_get_id(m):
        try:
            uid = parse_user_id(m.text)
        except Exception:
            return bot.reply_to(m, "❌ آيدي غير صالح. أعد المحاولة، أو اكتب /cancel.")
        _unban_pending[m.from_user.id] = {"step": "confirm", "user_id": uid}
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("✔️ تأكيد", callback_data="adm_unban:confirm"),
            types.InlineKeyboardButton("✖️ إلغاء", callback_data="adm_unban:cancel"),
        )
        bot.send_message(m.chat.id, f"تأكيد فكّ الحظر عن <code>{uid}</code>؟", parse_mode="HTML", reply_markup=kb)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("adm_unban:"))
    def unban_confirm(c):
        st = _unban_pending.get(c.from_user.id)
        if not st:
            try: bot.answer_callback_query(c.id, "لا توجد عملية.")
            except Exception: pass
            return
        action = c.data.split(":",1)[1]
        if action == "cancel":
            _unban_pending.pop(c.from_user.id, None)
            try: bot.answer_callback_query(c.id, "❎ أُلغي.")
            except Exception: pass
            try: bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
            except Exception: pass
            return
        try:
            unban_user(st["user_id"], c.from_user.id)
            log_action(c.from_user.id, "user:unban", reason=f"uid:{st['user_id']}")
            bot.send_message(c.message.chat.id, "✅ تم فكّ الحظر.")
        except Exception as e:
            bot.send_message(c.message.chat.id, f"❌ تعذّر فكّ الحظر: {e}")
        finally:
            _unban_pending.pop(c.from_user.id, None)
        try: bot.answer_callback_query(c.id, "تم.")
        except Exception: pass
        try: bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
        except Exception: pass


    @bot.message_handler(func=lambda m: m.text == "✉️ رسالة لعميل" and _allowed(m.from_user.id, "user:message_by_id"))
    def msg_by_id_start(m):
        _msg_by_id_pending[m.from_user.id] = {"step": "ask_id"}
        bot.send_message(m.chat.id, "أرسل آيدي العميل الرقمي.\nمثال: 123456789\n\n/ cancel لإلغاء")

    @bot.message_handler(func=lambda m: _msg_by_id_pending.get(m.from_user.id, {}).get("step") == "ask_id")
    def msg_by_id_get_id(m):
        try:
            uid = parse_user_id(m.text)
        except Exception:
            return bot.reply_to(m, "❌ آيدي غير صالح. أعد المحاولة، أو اكتب /cancel.")
        # تحقق أنه عميل مسجّل
        try:
            ex = get_table(USERS_TABLE).select("user_id").eq("user_id", uid).limit(1).execute()
            if not (getattr(ex, "data", None) or []):
                return bot.reply_to(m, f"❌ الآيدي {uid} غير موجود في العملاء.")
        except Exception:
            return bot.reply_to(m, "❌ تعذّر التحقق من قاعدة البيانات الآن.")
        _msg_by_id_pending[m.from_user.id] = {"step": "ask_text", "user_id": uid}
        bot.send_message(m.chat.id, f"اكتب الرسالة التي سيتم إرسالها إلى <code>{uid}</code>:", parse_mode="HTML")

    @bot.message_handler(func=lambda m: _msg_by_id_pending.get(m.from_user.id, {}).get("step") == "ask_text")
    def msg_by_id_get_text(m):
        st = _msg_by_id_pending.get(m.from_user.id) or {}
        uid = st.get("user_id")
        if not uid:
            _msg_by_id_pending.pop(m.from_user.id, None)
            return bot.reply_to(m, "❌ الحالة غير صالحة. أعد البدء.")
        st["text"] = m.text
        _msg_by_id_pending[m.from_user.id] = st
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("✔️ إرسال", callback_data=f"adm_msgid:send:{uid}"),
            types.InlineKeyboardButton("✖️ إلغاء", callback_data="adm_msgid:cancel"),
        )
        bot.send_message(m.chat.id, f"تأكيد إرسال الرسالة للعميل <code>{uid}</code>؟", parse_mode="HTML", reply_markup=kb)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("adm_msgid:"))
    def msg_by_id_confirm(c):
        st = _msg_by_id_pending.get(c.from_user.id)
        if not st:
            try: bot.answer_callback_query(c.id, "لا توجد عملية قيد التأكيد."); 
            except Exception: pass
            return
        parts = c.data.split(":", 2)
        action = parts[1]
        if action == "cancel":
            _msg_by_id_pending.pop(c.from_user.id, None)
            try: bot.answer_callback_query(c.id, "❎ أُلغي."); 
            except Exception: pass
            try: bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
            except Exception: pass
            return
        if action == "send":
            uid = int(parts[2])
            text = st.get("text") or ""
            try:
                notify_user(bot, uid, text)
                log_action(c.from_user.id, "user:message_by_id", reason=f"to:{uid}")
                bot.send_message(c.message.chat.id, "✅ تم الإرسال.")
            except Exception as e:
                bot.send_message(c.message.chat.id, f"❌ تعذّر الإرسال: {e}")
            finally:
                _msg_by_id_pending.pop(c.from_user.id, None)
            try: bot.answer_callback_query(c.id, "تم.")
            except Exception: pass
            try: bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
            except Exception: pass


    @bot.message_handler(func=lambda m: m.text == "🧩 تشغيل/إيقاف المزايا" and m.from_user.id in ADMINS)
    def features_home(m):
        try:
            bot.send_message(m.chat.id, "اختر طريقة العرض:", reply_markup=_features_home_markup())
            bot.send_message(m.chat.id, "قائمة المزايا (صفحة 1):", reply_markup=_features_markup(0))
        except Exception as e:
            logging.exception("[ADMIN] features home failed: %s", e)
            bot.send_message(m.chat.id, "تعذّر فتح لوحة المزايا.")


    @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("adm_feat_home:"))
    def _features_home_cb(c):
        try:
            mode = c.data.split(":",1)[1]
            if mode == "groups":
                kb = _features_groups_markup()
                bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=kb)
            elif mode == "flat":
                kb = _features_markup(0)
                bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=kb)
        except Exception as e:
            logging.exception("[ADMIN] feat home cb failed: %s", e)

    @bot.callback_query_handler(func=lambda c: c.data == "adm_feat_sync")
    def _features_sync_cb(c):
        try:
            created = ensure_seed() or 0
        except Exception as e:
            created = 0
            logging.exception("[ADMIN] ensure_seed failed: %s", e)
        try:
            bot.answer_callback_query(c.id, f"تمت المزامنة. مضاف: {created}")
        except Exception:
            pass
        try:
            bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=_features_markup(0))
        except Exception:
            pass

    @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("adm_feat_g:"))
    def _features_group_cb(c):
        try:
            _, group, page = c.data.split(":", 2)
            kb = _features_group_items_markup(group, int(page))
            bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=kb)
        except Exception as e:
            logging.exception("[ADMIN] feature group cb failed: %s", e)

    @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("adm_feat_gtoggle:"))
    def _features_group_toggle_all(c):
        try:
            _, group, to, page = c.data.split(":", 3)
            to = int(to)
            grouped = list_features_grouped() or {}
            for it in grouped.get(group, []) or []:
                k = it.get("key")
                if k:
                    try:
                        set_feature_active(k, bool(to))
                    except Exception:
                        pass
            try:
                bot.answer_callback_query(c.id, "تم التحديث.")
            except Exception:
                pass
            kb = _features_group_items_markup(group, int(page))
            bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=kb)
        except Exception as e:
            logging.exception("[ADMIN] feature group toggle-all failed: %s", e)
    # تسجيل هاندلرات التحويلات (كما هي)
    cash_transfer.register(bot, history)
    companies_transfer.register_companies_transfer(bot, history)

    # زرع مزايا افتراضية (مرة عند الإقلاع)
    try:
        ensure_seed()
    except Exception:
        pass

    # إلغاء لأي وضع إدخال للأدمن (/cancel)
    @bot.message_handler(commands=['cancel'])
    def _admin_cancel_any(msg: types.Message):
        _msg_pending.pop(msg.from_user.id, None)
        _accept_pending.pop(msg.from_user.id, None)
        _broadcast_pending.pop(msg.from_user.id, None)
        bot.reply_to(msg, "✅ تم الإلغاء.")

    @bot.message_handler(func=lambda msg: msg.text and re.match(r'/done_(\d+)', msg.text) and msg.from_user.id in ADMINS)
    def handle_done(msg):
        req_id = int(re.match(r'/done_(\d+)', msg.text).group(1))
        delete_pending_request(req_id)
        bot.reply_to(msg, f"✅ تم إنهاء الطلب {req_id}")

    @bot.message_handler(func=lambda msg: msg.text and re.match(r'/cancel_(\d+)', msg.text) and msg.from_user.id in ADMINS)
    def handle_cancel(msg):
        req_id = int(re.match(r'/cancel_(\d+)', msg.text).group(1))
        delete_pending_request(req_id)
        bot.reply_to(msg, f"🚫 تم إلغاء الطلب {req_id}")

    # ────────────────────────────────────────────────
    #  ✉️ رسالة/🖼️ صورة للعميل (HTML + ترويسة بسيطة)
    # ────────────────────────────────────────────────
    @bot.callback_query_handler(func=lambda c: (c.data.startswith("admin_queue_message_")) and c.from_user.id in ADMINS)
    def cb_queue_message(c: types.CallbackQuery):
        if not allowed(c.from_user.id, 'queue:message'):
            return bot.answer_callback_query(c.id, '❌ ليس لديك صلاحية.')
        request_id = int(c.data.split("_")[3])
        res = get_table("pending_requests").select("user_id").eq("id", request_id).execute()
        if not res.data:
            return bot.answer_callback_query(c.id, "❌ الطلب غير موجود.")
        _msg_pending[c.from_user.id] = {"user_id": res.data[0]["user_id"], "mode": "text"}
        bot.answer_callback_query(c.id)
        bot.send_message(c.from_user.id, f"📝 اكتب رسالتك بصيغة HTML.\n{CANCEL_HINT_ADMIN}")

    @bot.callback_query_handler(func=lambda c: (c.data.startswith("admin_queue_photo_")) and c.from_user.id in ADMINS)
    def cb_queue_photo(c: types.CallbackQuery):
        if not allowed(c.from_user.id, 'queue:photo'):
            return bot.answer_callback_query(c.id, '❌ ليس لديك صلاحية.')
        request_id = int(c.data.split("_")[3])
        res = get_table("pending_requests").select("user_id").eq("id", request_id).execute()
        if not res.data:
            return bot.answer_callback_query(c.id, "❌ الطلب غير موجود.")
        _msg_pending[c.from_user.id] = {"user_id": res.data[0]["user_id"], "mode": "photo"}
        bot.answer_callback_query(c.id)
        bot.send_message(c.from_user.id, f"📷 أرسل الصورة الآن (مع كابتشن HTML إن حبيت).\n{CANCEL_HINT_ADMIN}")

    @bot.message_handler(func=lambda m: m.from_user.id in _msg_pending,
                         content_types=["text", "photo"])
    def forward_to_client(m: types.Message):
        data = _msg_pending.pop(m.from_user.id)
        uid  = data["user_id"]
        if data["mode"] == "text":
            if m.content_type != "text":
                return bot.reply_to(m, "❌ المطلوب نص فقط.")
            bot.send_message(uid, f"{BAND}\n📩 <b>رسالة من الإدارة</b>\n{m.text}\n{BAND}", parse_mode="HTML")
        else:
            if m.content_type != "photo":
                return bot.reply_to(m, "❌ المطلوب صورة فقط.")
            cap = m.caption or ""
            bot.send_photo(uid, m.photo[-1].file_id, caption=f"{BAND}\n📩 <b>رسالة من الإدارة</b>\n{cap}\n{BAND}", parse_mode="HTML")
        bot.reply_to(m, "✅ أُرسلت للعميل. تقدر تكمل بتأكيد/إلغاء الطلب.")

    @bot.callback_query_handler(func=lambda call: (call.data.startswith("admin_queue_")) and (call.from_user.id in ADMINS or call.from_user.id == ADMIN_MAIN_ID))
    def handle_queue_action(call):
        parts      = call.data.split("_")
        action     = parts[2]
        request_id = int(parts[3])

        # جلب الطلب
        res = (
            get_table("pending_requests")
            .select("user_id", "request_text", "payload")
            .eq("id", request_id)
            .execute()
        )
        if not getattr(res, "data", None):
            return bot.answer_callback_query(call.id, "❌ الطلب غير موجود.")
        req      = res.data[0]
        user_id  = req["user_id"]
        payload  = req.get("payload") or {}
        req_text = req.get("request_text") or ""
        name     = _user_name(bot, user_id)

        # ✳️ إذا كان الطلب محجوز من أدمن آخر — نخرج فورًا (كما هو موجود أصلًا)
        locked_by = payload.get('locked_by')
        locked_by_username = payload.get('locked_by_username')
        admin_msgs = payload.get('admin_msgs') or []
        if locked_by and int(locked_by) != int(call.from_user.id):
            who = locked_by_username or _admin_mention(bot, locked_by)
            return bot.answer_callback_query(call.id, f'🔒 محجوز بواسطة {who}')

        # 🛑 بوابة "لا تتجاوب الأزرار قبل استلمت"
        if action != 'claim' and not payload.get('claimed'):
            return bot.answer_callback_query(call.id, "👋 اضغط «📌 استلمت» أولاً لتفعيل الأزرار.")


        def _disable_others(except_aid=None, except_mid=None):
            for entry in admin_msgs:
                try:
                    aid = entry.get('admin_id'); mid = entry.get('message_id')
                    if not aid or not mid:
                        continue
                    if aid == except_aid and mid == except_mid:
                        continue
                    bot.edit_message_reply_markup(aid, mid, reply_markup=None)
                except Exception:
                    pass

        def _mark_locked_here():
            try:
                lock_line = f"🔒 محجوز بواسطة {locked_by_username or _admin_mention(bot, call.from_user.id)}\n"
                try:
                    bot.edit_message_text(lock_line + req_text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=call.message.reply_markup)
                except Exception:
                    bot.edit_message_caption(lock_line + req_text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=call.message.reply_markup)
            except Exception:
                pass

        if locked_by and int(locked_by) != int(call.from_user.id):
            who = locked_by_username or _admin_mention(bot, locked_by)
            return bot.answer_callback_query(call.id, f'🔒 محجوز بواسطة {who}')

        # لو ما في قفل، فعِّل القفل (كما هو عندك)
        if not locked_by:
            try:
                locked_by_username = _admin_mention(bot, call.from_user.id)
                new_payload = dict(payload)
                new_payload['locked_by'] = int(call.from_user.id)
                new_payload['locked_by_username'] = locked_by_username
                get_table('pending_requests').update({'payload': new_payload}).eq('id', request_id).execute()
                _disable_others(except_aid=call.message.chat.id, except_mid=call.message.message_id)
                _mark_locked_here()
                payload = new_payload  # حدّث النسخة المحلية
            except Exception as e:
                logging.exception('[ADMIN] failed to set lock: %s', e)

        # === زر الاستلام (📌 استلمت) ===
        if action == 'claim':
            try:
                # علِّم أنه "تم الاستلام" لتُفتح الأزرار لاحقًا
                claimed_payload = dict(payload)
                claimed_payload['claimed'] = True
                get_table('pending_requests').update({'payload': claimed_payload}).eq('id', request_id).execute()
            except Exception as e:
                logging.exception('[ADMIN] failed to set claimed: %s', e)
            bot.answer_callback_query(call.id, '✅ تم الاستلام — أنت المتحكم بهذا الطلب الآن.')
            return

        # === تأجيل الطلب ===
        if action == "postpone":
            if not (call.from_user.id == ADMIN_MAIN_ID or call.from_user.id in ADMINS or allowed(call.from_user.id, "queue:postpone")):
                return bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية لهذا الإجراء.")
            # إزالة الكيبورد لتجنُّب النقر المزدوج
            try:
                from services.telegram_safety import remove_inline_keyboard
            except Exception:
                from telegram_safety import remove_inline_keyboard
            try:
                remove_inline_keyboard(bot, call.message)
            except Exception:
                pass
            # تأجيل الطلب بإرجاعه لآخر الدور
            postpone_request(request_id)
            # إبلاغ العميل برسالة اعتذار/تنظيم الدور
            try:
                bot.send_message(
                    user_id,
                    f"⏳ عزيزي {name}، تم تنظيم دور طلبك مجددًا بسبب ضغط أو عُطل مؤقت. "
                    "نعتذر عن التأخير، وسيتم تنفيذ طلبك قريبًا بإذن الله. شكرًا لتفهّمك."
                )
            except Exception as e:
                logging.error(f"[admin] postpone notify error: {e}", exc_info=True)
            # تأكيد للأدمن + بدء فترة الخمول
            try:
                bot.answer_callback_query(call.id, "✅ تم تأجيل الطلب.")
            except Exception:
                pass
            queue_cooldown_start(bot)
            return

        # === إلغاء الطلب ===
        if action == "cancel":
            if not allowed(call.from_user.id, "queue:cancel"):
                return bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية لهذا الإجراء.")
            hold_id  = payload.get("hold_id")
            reserved = int(payload.get("reserved", 0) or 0)
            typ      = (payload.get("type") or "").strip()

            if hold_id:
                try:
                    r = release_hold(hold_id)
                    if getattr(r, "error", None):
                        logging.error("release_hold error: %s", r.error)
                except Exception as e:
                    logging.exception("release_hold exception: %s", e)
            else:
                if reserved > 0:
                    add_balance(user_id, reserved, "إلغاء حجز (قديم)")

            delete_pending_request(request_id)
            if reserved > 0:
                bot.send_message(user_id, f"🚫 تم إلغاء طلبك.\n🔁 رجّعنا {_fmt_syp(reserved)} من المبلغ المحجوز لمحفظتك — كله تمام 😎")
            else:
                bot.send_message(user_id, "🚫 تم إلغاء طلبك.\n🔁 رجّعنا المبلغ المحجوز (إن وُجد) لمحفظتك.")
            bot.answer_callback_query(call.id, "✅ تم إلغاء الطلب.")
            queue_cooldown_start(bot)

            # NEW: لو طلب شحن — نظّف قفل الشحن المحلي
            if typ in ("recharge", "wallet_recharge", "deposit"):
                _clear_recharge_local_lock_safe(user_id)

            _prompt_admin_note(bot, call.from_user.id, user_id)
            return

        # === قبول الطلب ===
        if action == "accept":
            # ✅ فحص صلاحية التأكيد (مهم)
            if not allowed(call.from_user.id, "queue:confirm"):
                return bot.answer_callback_query(call.id, "❌ ليس لديك صلاحية لهذا الإجراء.")

            typ      = (payload.get("type") or "").strip()
            hold_id  = payload.get("hold_id")
            amt      = _amount_from_payload(payload)

            if hold_id:
                try:
                    r = capture_hold(hold_id)
                    if getattr(r, "error", None) or not bool(getattr(r, "data", True)):
                        logging.error("capture_hold failed: %s", getattr(r, "error", r))
                        return bot.answer_callback_query(call.id, "❌ فشل تصفية الحجز. أعد المحاولة.")
                except Exception as e:
                    logging.exception("capture_hold exception: %s", e)
                    return bot.answer_callback_query(call.id, "❌ فشل تصفية الحجز. أعد المحاولة.")

            # ——— طلبات المنتجات الرقمية ———
            if typ == "order":
                product_id_raw = payload.get("product_id")
                player_id      = _extract_identifier(payload, req_text, ["player_id","account","id","username","user","target_id"])
                amt            = int(amt or payload.get("price", 0) or 0)

                product_name = (payload.get("product_name") or "").strip()
                prod_obj = None
                if not product_name and product_id_raw:
                    try:
                        prod_obj = get_product_by_id(int(product_id_raw))
                    except Exception:
                        prod_obj = None
                    if prod_obj and isinstance(prod_obj, dict):
                        product_name = (prod_obj.get("name") or "").strip()
                if not product_name:
                    product_name = "منتج رقمي"

                pid_for_db = int(product_id_raw) if (product_id_raw and (prod_obj or payload.get("product_name"))) else None

                _insert_purchase_row(user_id, pid_for_db, product_name, amt, _safe(player_id))
                try:
                    add_game_purchase(user_id, pid_for_db, product_name, int(amt), _safe(player_id))
                except Exception:
                    pass

                # سجل استخدام الخصم (إن وُجد فرق بين السعر قبل/بعد)
                try:
                    before = int(payload.get("price_before") or amt)
                    after  = int(payload.get("price") or amt)
                    if before and after and before != after:
                        try:
                            percent = max(0, int(round((before - after) * 100.0 / before)))
                        except Exception:
                            percent = None
                        record_discount_use(None, user_id, before, after, purchase_id=None)
                except Exception:
                    pass

                delete_pending_request(request_id)
                bot.send_message(
                    user_id,
                    f"{BAND}\n🎉 تمام يا {name}! تم تحويل «{product_name}» لآيدي «{_safe(player_id)}» "
                    f"وتم خصم {_fmt_syp(amt)} من محفظتك. استمتع باللعب! 🎮\n{BAND}",
                    parse_mode="HTML"
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
                queue_cooldown_start(bot)
                _prompt_admin_note(bot, call.from_user.id, user_id)
                try:
                    purge_state(user_id)
                except Exception:
                    pass
                return

            # ——— إعلانات ———
            elif typ in ("ads", "media"):
                amt     = int(amt or payload.get("price", 0) or 0)
                times   = payload.get("count")
                contact = payload.get("contact") or "—"
                ad_text = payload.get("ad_text") or ""
                images  = payload.get("images", [])

                title = f"إعلان مدفوع × {times}" if times else "إعلان مدفوع"
                _insert_purchase_row(user_id, None, title, amt, _safe(contact))
                try:
                    add_ads_purchase(user_id, ad_name=title, price=amt, channel_username=None)
                except Exception:
                    pass

                delete_pending_request(request_id)

                # NEW: أنشئ إعلانًا فعّالًا لبدء النشر الآلي ضمن نافذة 9→22 بتوقيت دمشق
                try:
                    times_total = int(payload.get("times_total") or payload.get("count") or 1)
                    duration_days = int(payload.get("duration_days") or 30)
                    add_channel_ad(
                        user_id=user_id,
                        times_total=times_total,
                        price=amt,
                        contact=contact,
                        ad_text=ad_text,
                        images=images,
                        duration_days=duration_days,
                    )
                except Exception as e:
                    logging.exception("[ADMIN][ADS] add_channel_ad failed: %s", e)

                bot.send_message(
                    user_id,
                    f"{BAND}\n📣 تمام يا {name}! وتم تأكيد باقة الإعلان ({title}). "
                    f"اتخصم {_fmt_syp(amt)} من محفظتك، وحننشرها حسب الجدولة.\n{BAND}",
                    parse_mode="HTML"
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
                queue_cooldown_start(bot)
                _prompt_admin_note(bot, call.from_user.id, user_id)
                return

            elif typ in ("syr_unit", "mtn_unit"):
                price = int(payload.get("price", 0) or amt or 0)
                num   = _extract_identifier(payload, req_text, ["number","msisdn","phone"])
                unit_name = payload.get("unit_name") or "وحدات"

                _insert_purchase_row(user_id, None, unit_name, price, _safe(num))
                try:
                    add_bill_or_units_purchase(user_id, bill_name=unit_name, price=price, number=_safe(num))
                except Exception:
                    pass

                delete_pending_request(request_id)
                bot.send_message(
                    user_id,
                    f"{BAND}\n✅ تمام يا {name}! تم تحويل {unit_name} للرقم «{_safe(num)}» "
                    f"وتم خصم {_fmt_syp(price)} من محفظتك.\n{BAND}",
                    parse_mode="HTML"
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
                queue_cooldown_start(bot)
                _prompt_admin_note(bot, call.from_user.id, user_id)
                try:
                    purge_state(user_id)
                except Exception:
                    pass
                return

            elif typ in ("syr_bill", "mtn_bill"):
                amt   = int(amt or payload.get("price", 0) or 0)
                num   = payload.get("number")
                label = payload.get("unit_name", "فاتورة")

                _insert_purchase_row(user_id, None, label, amt, _safe(num))
                try:
                    add_bill_or_units_purchase(user_id, bill_name=label, price=amt, number=_safe(num))
                except Exception:
                    pass

                delete_pending_request(request_id)
                bot.send_message(
                    user_id,
                    f"{BAND}\n🧾 تمام يا {name}! تم دفع {label} للرقم «{_safe(num)}» "
                    f"وتم خصم {_fmt_syp(amt)} من محفظتك.\n{BAND}",
                    parse_mode="HTML"
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
                queue_cooldown_start(bot)
                _prompt_admin_note(bot, call.from_user.id, user_id)
                try:
                    purge_state(user_id)
                except Exception:
                    pass
                return

            elif typ == "internet":
                amt      = int(amt or payload.get("price", 0) or 0)
                provider = _safe(payload.get("provider"), dash="").strip()
                speed    = _safe(payload.get("speed"), dash="").strip()
                phone    = payload.get("phone")
                name_lbl = ("إنترنت " + " ".join(x for x in [provider, speed] if x)).strip() or "إنترنت"

                _insert_purchase_row(user_id, None, name_lbl, amt, _safe(phone))
                try:
                    add_internet_purchase(user_id, provider_name=provider or None, price=amt, phone=_safe(phone), speed=speed or None)
                except Exception:
                    pass

                delete_pending_request(request_id)
                bot.send_message(
                    user_id,
                    f"{BAND}\n🌐 تمام يا {name}! تم دفع فاتورة الإنترنت ({name_lbl}) للرقم «{_safe(phone)}» "
                    f"وتم خصم {_fmt_syp(amt)} من محفظتك.\n{BAND}",
                    parse_mode="HTML"
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
                queue_cooldown_start(bot)
                _prompt_admin_note(bot, call.from_user.id, user_id)
                return

            elif typ == "cash_transfer":
                amt       = int(amt or payload.get("price", 0) or 0)
                number    = payload.get("number")
                cash_type = _safe(payload.get("cash_type"), dash="").strip()
                name_lbl  = (f"تحويل كاش {cash_type}".strip() if cash_type else "تحويل كاش")

                _insert_purchase_row(user_id, None, name_lbl, amt, _safe(number))
                try:
                    add_cash_transfer_purchase(user_id, transfer_name=name_lbl, price=amt, number=_safe(number))
                except Exception:
                    pass

                delete_pending_request(request_id)
                bot.send_message(
                    user_id,
                    f"{BAND}\n💸 تمام يا {name}! تم تنفيذ {name_lbl} للرقم «{_safe(number)}» "
                    f"وتم خصم {_fmt_syp(amt)} من محفظتك.\n{BAND}",
                    parse_mode="HTML",
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
                queue_cooldown_start(bot)
                _prompt_admin_note(bot, call.from_user.id, user_id)
                try:
                    purge_state(user_id)
                except Exception:
                    pass
                return

            elif typ == "companies_transfer":
                amt                = int(amt or payload.get("price", 0) or 0)
                company            = _safe(payload.get("company"), dash="").strip()
                beneficiary_number = payload.get("beneficiary_number")
                name_lbl           = (f"حوالة مالية عبر {company}".strip() if company else "حوالة مالية")

                _insert_purchase_row(user_id, None, name_lbl, amt, _safe(beneficiary_number))
                try:
                    add_companies_transfer_purchase(user_id, company_name=(company or None), price=amt, beneficiary_number=_safe(beneficiary_number))
                except Exception:
                    pass

                delete_pending_request(request_id)
                bot.send_message(
                    user_id,
                    f"{BAND}\n🏢 تمام يا {name}! تم تنفيذ {name_lbl} للمستفيد «{_safe(beneficiary_number)}» "
                    f"وتم خصم {_fmt_syp(amt)} من محفظتك.\n{BAND}",
                    parse_mode="HTML",
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
                queue_cooldown_start(bot)
                _prompt_admin_note(bot, call.from_user.id, user_id)
                try:
                    purge_state(user_id)
                except Exception:
                    pass
                return

            elif typ in ("university_fees",):
                amt           = int(amt or payload.get("price", 0) or 0)
                university    = _safe(payload.get("university"), dash="").strip()
                university_id = payload.get("university_id")
                name_lbl      = (f"رسوم جامعية ({university})".strip() if university else "رسوم جامعية")

                _insert_purchase_row(user_id, None, name_lbl, amt, _safe(university_id))
                try:
                    add_university_fees_purchase(user_id, university_name=(university or None), price=amt, university_id=_safe(university_id))
                except Exception:
                    pass

                delete_pending_request(request_id)
                bot.send_message(
                    user_id,
                    f"{BAND}\n🎓 تمام يا {name}! تم دفع {name_lbl} للرقم الجامعي «{_safe(university_id)}» "
                    f"وتم خصم {_fmt_syp(amt)} من محفظتك.\n{BAND}",
                    parse_mode="HTML"
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ العملية")
                queue_cooldown_start(bot)
                _prompt_admin_note(bot, call.from_user.id, user_id)
                return

            elif typ in ("recharge", "wallet_recharge", "deposit"):
                amount = _amount_from_payload(payload) or payload.get("amount") or 0
                amount = int(amount) if amount else 0
                if amount <= 0:
                    try:
                        return bot.answer_callback_query(call.id, "❌ مبلغ الشحن غير صالح.")
                    except Exception:
                        return

                # تأكد أن للمستخدم صفًّا في جدول المحفظة
                try:
                    register_user_if_not_exist(user_id, name)
                except Exception:
                    pass

                # ✅ الشحن الفعلي للمحفظة
                try:
                    r = add_balance(
                        user_id,
                        int(amount),
                        f"شحن محفظة — طريقة: {payload.get('method') or 'غير محدد'} | ref={_safe(payload.get('ref'))} | req={request_id}"
                    )
                    if getattr(r, "error", None):
                        logging.error("[ADMIN][RECHARGE] add_balance error: %s", r.error)
                        try:
                            return bot.answer_callback_query(call.id, "❌ فشل تحديث الرصيد. حاول مجددًا.")
                        except Exception:
                            return
                except Exception as e:
                    logging.exception("[ADMIN][RECHARGE] add_balance exception: %s", e)
                    try:
                        return bot.answer_callback_query(call.id, "❌ حدث خطأ أثناء تحديث الرصيد.")
                    except Exception:
                        return

                # سجل العملية في دفتر الإداري اختيارياً
                try:
                    log_admin_deposit(call.from_user.id, user_id, int(amount), f"req={request_id}")
                except Exception as _e:
                    logging.exception("[ADMIN_LEDGER] deposit log failed: %s", _e)

                # نظّف الطلب من الطابور وأبلغ العميل
                delete_pending_request(request_id)
                bot.send_message(
                    user_id,
                    f"{BAND}\n⚡ يا {name}، تم شحن محفظتك بمبلغ {_fmt_syp(amount)} بنجاح. دوس واشتري اللي نفسك فيه! 😉\n{BAND}"
                )
                bot.answer_callback_query(call.id, "✅ تم تنفيذ عملية الشحن")
                queue_cooldown_start(bot)

                _clear_recharge_local_lock_safe(user_id)
                _prompt_admin_note(bot, call.from_user.id, user_id)
                return

            else:
                return bot.answer_callback_query(call.id, "❌ نوع الطلب غير معروف.")

        bot.answer_callback_query(call.id, "❌ حدث خطأ غير متوقع.")

    # === ملاحظة الإدمن بعد القبول/الإلغاء (اختياري) ===
    @bot.message_handler(func=lambda m: m.from_user.id in _accept_pending,
                         content_types=["text", "photo"])
    def handle_accept_message(msg: types.Message):
        user_id = _accept_pending.get(msg.from_user.id)
        if not user_id:
            return
        if msg.text and msg.text.strip() == "/skip":
            bot.send_message(msg.chat.id, "✅ تم التخطي.")
        elif msg.content_type == "text":
            bot.send_message(user_id, f"{BAND}\n📝 <b>ملاحظة من الإدارة</b>\n{msg.text.strip()}\n{BAND}", parse_mode="HTML")
            bot.send_message(msg.chat.id, "✅ أُرسلت الملاحظة للعميل.")
        elif msg.content_type == "photo":
            bot.send_photo(user_id, msg.photo[-1].file_id, caption=f"{BAND}\n📝 <b>ملاحظة من الإدارة</b>\n{BAND}", parse_mode="HTML")
            bot.send_message(msg.chat.id, "✅ أُرسلت الصورة للعميل.")
        else:
            bot.send_message(msg.chat.id, "❌ نوع الرسالة غير مدعوم. ابعت نص أو صورة، أو /skip للتخطي.")
        _accept_pending.pop(msg.from_user.id, None)

    # ===== قائمة الأدمن =====
    @bot.message_handler(commands=['admin'])
    def admin_menu(msg):
        if msg.from_user.id not in ADMINS:
            return bot.reply_to(msg, "صلاحية الأدمن فقط.")
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        # رئيسي أم مساعد؟
        is_primary = (msg.from_user.id == ADMIN_MAIN_ID)
        if is_primary:
            kb.row("🧩 تشغيل/إيقاف المزايا", "⏳ طابور الانتظار")
            kb.row("📊 تقارير سريعة", "📈 تقرير المساعدين",)
            kb.row("🎟️ أكواد خصم", "👤 إدارة عميل")
            kb.row("📈 تقرير الإداريين (الكل)", "📣 رسالة للجميع")
            kb.row("✉️ رسالة لعميل", "⛔ حظر عميل")
            kb.row("✅ فكّ الحظر", "⚙️ النظام")
            kb.row("⬅️ رجوع")

        else:
            # الأدمن المساعد: يظهر فقط تشغيل/إيقاف المزايا + طابور الانتظار
            kb.row("🧩 تشغيل/إيقاف المزايا", "⏳ طابور الانتظار")
            kb.row("⬅️ رجوع")
        bot.send_message(msg.chat.id, "لوحة الأدمن:", reply_markup=kb)

    # =========================
    # 📬 ترحيب — نحن شغالين (مباشر)
    # =========================
    @bot.message_handler(func=lambda m: m.text == "📬 ترحيب — نحن شغالين" and (m.from_user.id in ADMINS or m.from_user.id == ADMIN_MAIN_ID))
    def bc_welcome(m):
        _broadcast_pending[m.from_user.id] = {"mode": "welcome", "dest": "clients"}
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("👥 إلى العملاء", callback_data="bw_dest_clients"),
            types.InlineKeyboardButton("📣 إلى القناة",  callback_data="bw_dest_channel"),
        )
        kb.row(
            types.InlineKeyboardButton("✅ إرسال الآن", callback_data="bw_confirm"),
            types.InlineKeyboardButton("❌ إلغاء",      callback_data="bw_cancel"),
        )
        bot.reply_to(m,
                     "🔎 *معاينة رسالة الترحيب*:\n"
                     f"{BAND}\n(سيتم إدراج اسم كل عميل تلقائيًا)\n{BAND}",
                     parse_mode="Markdown", reply_markup=kb)

    @bot.callback_query_handler(func=lambda c: c.data in ("bw_dest_clients","bw_dest_channel","bw_confirm","bw_cancel") and (c.from_user.id in ADMINS or c.from_user.id == ADMIN_MAIN_ID))
    def _bw_flow(c):
        st = _broadcast_pending.get(c.from_user.id)
        if not st or st.get("mode") != "welcome":
            return
        if c.data == "bw_cancel":
            _broadcast_pending.pop(c.from_user.id, None)
            try: bot.answer_callback_query(c.id, "❎ أُلغي.")
            except Exception: pass
            try: bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
            except Exception: pass
            return

        if c.data in ("bw_dest_clients","bw_dest_channel"):
            st["dest"] = "clients" if c.data.endswith("clients") else "channel"
            _broadcast_pending[c.from_user.id] = st
            try: bot.answer_callback_query(c.id, "✅ تم اختيار الوجهة.")
            except Exception: pass
            return

        if c.data == "bw_confirm":
            sent = 0
            if st["dest"] == "clients":
                for i, (uid, nm) in enumerate(_collect_clients_with_names(), 1):
                    try:
                        bot.send_message(uid, _funny_welcome_text(nm))
                        sent += 1
                    except Exception:
                        pass
                    if i % 25 == 0:
                        time.sleep(1)
            else:
                dest = CHANNEL_USERNAME or FORCE_SUB_CHANNEL_USERNAME
                try:
                    bot.send_message(dest, _funny_welcome_text(None))
                    sent = 1
                except Exception:
                    pass
            _broadcast_pending.pop(c.from_user.id, None)
            try: bot.answer_callback_query(c.id, "🚀 تم الإرسال.")
            except Exception: pass
            try: bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
            except Exception: pass
            bot.send_message(c.message.chat.id, f"✅ ترحيب أُرسل ({'القناة' if st['dest']=='channel' else f'{sent} عميل'}).")


    # =========================
    # 📢 عرض اليوم (مباشر)
    # =========================
    @bot.message_handler(func=lambda m: m.text == "📢 عرض اليوم" and (m.from_user.id in ADMINS or m.from_user.id == ADMIN_MAIN_ID))
    def broadcast_deal_of_day(m):
        _broadcast_pending[m.from_user.id] = {"mode": "deal_wait"}
        bot.reply_to(m, "🛍️ أرسل *نص العرض* الآن.\nمثال:\n"
                        "• خصم 20% على باقات كذا\n• توصيل فوري\n• ينتهي اليوم ⏳",
                     parse_mode="Markdown")

    @bot.message_handler(func=lambda m: _broadcast_pending.get(m.from_user.id, {}).get("mode") == "deal_wait", content_types=["text"])
    def _deal_collect(m):
        body = (m.text or "").strip()
        if not body:
            return bot.reply_to(m, "❌ النص فارغ.")
        _broadcast_pending[m.from_user.id] = {"mode": "deal_confirm", "body": body, "dest": "clients"}
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("👥 إلى العملاء", callback_data="bd_dest_clients"),
            types.InlineKeyboardButton("📣 إلى القناة",  callback_data="bd_dest_channel"),
        )
        kb.row(
            types.InlineKeyboardButton("✅ بث الآن", callback_data="bd_confirm"),
            types.InlineKeyboardButton("❌ إلغاء",   callback_data="bd_cancel"),
        )
        preview = (f"{BAND}\n<b>📢 عرض اليوم</b>\n"
           f"{body}\n"
           "🎯 <b>سارع قبل النفاد</b>\n"
           "💳 طرق دفع متعددة • ⚡️ تنفيذ فوري\n"
           f"{BAND}")
        bot.reply_to(m, preview, parse_mode="HTML", reply_markup=kb)

    @bot.callback_query_handler(func=lambda c: c.data in ("bd_dest_clients","bd_dest_channel","bd_confirm","bd_cancel") and (c.from_user.id in ADMINS or c.from_user.id == ADMIN_MAIN_ID))
    def _bd_flow(c):
        st = _broadcast_pending.get(c.from_user.id)
        if not st or st.get("mode") != "deal_confirm":
            return
        if c.data == "bd_cancel":
            _broadcast_pending.pop(c.from_user.id, None)
            try: bot.answer_callback_query(c.id, "❎ أُلغي.")
            except Exception: pass
            try: bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
            except Exception: pass
            return
        if c.data in ("bd_dest_clients","bd_dest_channel"):
            st["dest"] = "clients" if c.data.endswith("clients") else "channel"
            _broadcast_pending[c.from_user.id] = st
            try: bot.answer_callback_query(c.id, "✅ تم اختيار الوجهة.")
            except Exception: pass
            return

        if c.data == "bd_confirm":
            text = (f"{BAND}\n<b>📢 عرض اليوم</b>\n{st['body']}\n"
                    "🎯 <b>سارع قبل النفاد</b>\n"
                    "💳 طرق دفع متعددة • ⚡️ تنفيذ فوري\n"
                    f"{BAND}")

            sent = 0
            if st["dest"] == "clients":
                for i, (uid, _) in enumerate(_collect_clients_with_names(), 1):
                    try:
                        bot.send_message(uid, text, parse_mode="HTML")
                        sent += 1
                    except Exception:
                        pass
                    if i % 25 == 0:
                        time.sleep(1)
            else:
                dest = CHANNEL_USERNAME or FORCE_SUB_CHANNEL_USERNAME
                try:
                    bot.send_message(dest, text, parse_mode="HTML")
                    sent = 1
                except Exception:
                    pass
            _broadcast_pending.pop(c.from_user.id, None)
            try: bot.answer_callback_query(c.id, "🚀 تم الإرسال.")
            except Exception: pass
            try: bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
            except Exception: pass
            bot.send_message(c.message.chat.id, f"✅ العرض أُرسل ({'القناة' if st['dest']=='channel' else f'{sent} عميل'}).")


    # =========================
    # 📊 استفتاء سريع (مباشر)
    # =========================
    @bot.message_handler(func=lambda m: m.text == "📊 استفتاء سريع" and (m.from_user.id in ADMINS or m.from_user.id == ADMIN_MAIN_ID))
    def broadcast_poll(m):
        _broadcast_pending[m.from_user.id] = {"mode": "poll_wait"}
        bot.reply_to(m, "🗳️ أرسل الاستفتاء بصيغة:\n"
                        "*السؤال*\n"
                        "الخيار 1\nالخيار 2\nالخيار 3\nالخيار 4",
                     parse_mode="Markdown")

    @bot.message_handler(func=lambda m: _broadcast_pending.get(m.from_user.id, {}).get("mode") == "poll_wait", content_types=["text"])
    def _poll_collect(m):
        lines = [l.strip() for l in (m.text or "").splitlines() if l.strip()]
        if len(lines) < 3:
            return bot.reply_to(m, "❌ الصيغة غير صحيحة. المطلوب: سؤال + خيارين على الأقل.")

        q, raw_opts = lines[0], lines[1:]
        # إزالة المكررات والإفراغ وقصّ حتى 10 خيارات (شرط تيليجرام)
        opts = []
        for o in raw_opts:
            if not o:
                continue
            if o in opts:
                continue
            if len(o) > 100:
                o = o[:100]
            opts.append(o)
        opts = opts[:10]

        if len(opts) < 2:
            return bot.reply_to(m, "❌ لازم خيارين فريدين على الأقل.")

        _broadcast_pending[m.from_user.id] = {"mode": "poll_confirm", "q": q, "opts": opts, "dest": "clients"}

        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("👥 إلى العملاء", callback_data="bp_dest_clients"),
            types.InlineKeyboardButton("📣 إلى القناة",  callback_data="bp_dest_channel"),
        )
        kb.row(
            types.InlineKeyboardButton("✅ بث الآن", callback_data="bp_confirm"),
            types.InlineKeyboardButton("❌ إلغاء",   callback_data="bp_cancel"),
        )

        preview = "🔎 *معاينة الاستفتاء:*\n" + q + "\n" + "\n".join(f"- {o}" for o in opts)
        bot.reply_to(m, preview, parse_mode="Markdown", reply_markup=kb)

        _broadcast_pending[m.from_user.id] = {"mode": "poll_confirm", "q": q, "opts": opts, "dest": "clients"}
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("👥 إلى العملاء", callback_data="bp_dest_clients"),
            types.InlineKeyboardButton("📣 إلى القناة",  callback_data="bp_dest_channel"),
        )
        kb.row(
            types.InlineKeyboardButton("✅ بث الآن", callback_data="bp_confirm"),
            types.InlineKeyboardButton("❌ إلغاء",   callback_data="bp_cancel"),
        )
        bot.reply_to(m, f"🔎 *معاينة الاستفتاء:*\n{q}\n- {opts[0]}\n- {opts[1]}\n- {opts[2]}\n- {opts[3]}",
                     parse_mode="Markdown", reply_markup=kb)

    @bot.callback_query_handler(func=lambda c: c.data in ("bp_dest_clients","bp_dest_channel","bp_confirm","bp_cancel") and (c.from_user.id in ADMINS or c.from_user.id == ADMIN_MAIN_ID))
    def _bp_flow(c):
        st = _broadcast_pending.get(c.from_user.id)
        if not st or st.get("mode") != "poll_confirm":
            return
        if c.data == "bp_cancel":
            _broadcast_pending.pop(c.from_user.id, None)
            try: bot.answer_callback_query(c.id, "❎ أُلغي.")
            except Exception: pass
            try: bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
            except Exception: pass
            return
        if c.data in ("bp_dest_clients","bp_dest_channel"):
            st["dest"] = "clients" if c.data.endswith("clients") else "channel"
            _broadcast_pending[c.from_user.id] = st
            try: bot.answer_callback_query(c.id, "✅ تم اختيار الوجهة.")
            except Exception: pass
            return

        if c.data == "bp_confirm":
            q, opts = st["q"], st["opts"]
            sent = 0
            if st["dest"] == "clients":
                ids = list(_collect_clients_with_names())
                for i, (uid, _) in enumerate(ids, 1):
                    try:
                        bot.send_poll(uid, question=q, options=opts, is_anonymous=True, allows_multiple_answers=False)
                        sent += 1
                    except Exception:
                        pass
                    if i % 25 == 0:
                        time.sleep(1)
            else:
                dest = CHANNEL_USERNAME or FORCE_SUB_CHANNEL_USERNAME
                try:
                    bot.send_poll(dest, question=q, options=opts, is_anonymous=True, allows_multiple_answers=False)
                    sent = 1
                except Exception:
                    pass
            _broadcast_pending.pop(c.from_user.id, None)
            try: bot.answer_callback_query(c.id, "🚀 تم الإرسال.")
            except Exception: pass
            try: bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
            except Exception: pass
            bot.send_message(c.message.chat.id, f"✅ الاستفتاء أُرسل ({'القناة' if st['dest']=='channel' else f'{sent} عميل'}).")


    # =========================
    # 📝 رسالة من عندي (مباشر)
    # =========================
    @bot.message_handler(func=lambda m: m.text == "📝 رسالة من عندي" and (m.from_user.id in ADMINS or m.from_user.id == ADMIN_MAIN_ID))
    def broadcast_free(m):
        _broadcast_pending[m.from_user.id] = {"mode": "free_wait"}
        bot.reply_to(m, "📝 أرسل النص الآن.")

    @bot.message_handler(func=lambda m: _broadcast_pending.get(m.from_user.id, {}).get("mode") == "free_wait", content_types=["text"])
    def _free_collect(m):
        text = (m.text or "").strip()
        if not text:
            return bot.reply_to(m, "❌ النص فارغ.")
        _broadcast_pending[m.from_user.id] = {"mode": "free_confirm", "text": text, "dest": "clients"}
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("👥 إلى العملاء", callback_data="bf_dest_clients"),
            types.InlineKeyboardButton("📣 إلى القناة",  callback_data="bf_dest_channel"),
        )
        kb.row(
            types.InlineKeyboardButton("✅ بث الآن", callback_data="bf_confirm"),
            types.InlineKeyboardButton("❌ إلغاء",   callback_data="bf_cancel"),
        )
        bot.reply_to(m, f"{BAND}\n{text}\n{BAND}", parse_mode="HTML", reply_markup=kb)

    @bot.callback_query_handler(func=lambda c: c.data in ("bf_dest_clients","bf_dest_channel","bf_confirm","bf_cancel") and (c.from_user.id in ADMINS or c.from_user.id == ADMIN_MAIN_ID))
    def _bf_flow(c):
        st = _broadcast_pending.get(c.from_user.id)
        if not st or st.get("mode") != "free_confirm":
            return
        if c.data == "bf_cancel":
            _broadcast_pending.pop(c.from_user.id, None)
            try: bot.answer_callback_query(c.id, "❎ أُلغي.")
            except Exception: pass
            try: bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
            except Exception: pass
            return
        if c.data in ("bf_dest_clients","bf_dest_channel"):
            st["dest"] = "clients" if c.data.endswith("clients") else "channel"
            _broadcast_pending[c.from_user.id] = st
            try: bot.answer_callback_query(c.id, "✅ تم اختيار الوجهة.")
            except Exception: pass
            return
        if c.data == "bf_confirm":
            sent = 0
            if st["dest"] == "clients":
                for i, (uid, _) in enumerate(_collect_clients_with_names(), 1):
                    try:
                        bot.send_message(uid, st["text"], parse_mode="HTML")
                        sent += 1
                    except Exception:
                        pass
                    if i % 25 == 0:
                        time.sleep(1)
            else:
                dest = CHANNEL_USERNAME or FORCE_SUB_CHANNEL_USERNAME
                try:
                    bot.send_message(dest, st["text"], parse_mode="HTML")
                    sent = 1
                except Exception:
                    pass
            _broadcast_pending.pop(c.from_user.id, None)
            try: bot.answer_callback_query(c.id, "🚀 تم الإرسال.")
            except Exception: pass
            try: bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
            except Exception: pass
            bot.send_message(c.message.chat.id, f"✅ الرسالة أُرسلت ({'القناة' if st['dest']=='channel' else f'{sent} عميل'}).")
    
    @bot.message_handler(func=lambda m: m.text == "🛒 إدارة المنتجات" and m.from_user.id in ADMINS)
    def admin_products_menu(m):
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        kb.row("🚫 إيقاف منتج", "✅ تشغيل منتج")
        kb.row("🔄 مزامنة المنتجات (DB)")
        kb.row("⬅️ رجوع")
        bot.send_message(m.chat.id, "اختر إجراء:", reply_markup=kb)
 
    # ⏳ عرض طابور الانتظار للأدمن
    @bot.message_handler(func=lambda m: m.text == "⏳ طابور الانتظار" and m.from_user.id in ADMINS)
    def admin_queue_list(m: types.Message):
        # حمّل أول 30 طلب أقدم فالأحدث
        try:
            res = (
                get_table("pending_requests")
                .select("id,user_id,request_text,payload,created_at")
                .order("created_at", desc=False)
                .limit(30)
                .execute()
            )
            rows = res.data or []
        except Exception as e:
            logging.exception("[ADMIN] load queue failed: %s", e)
            return bot.reply_to(m, "❌ تعذّر تحميل الطابور.")

        if not rows:
            return bot.reply_to(m, "🟢 لا توجد طلبات حالية.")

        for r in rows:
            rid     = r["id"]
            uid     = r["user_id"]
            name    = _user_name(bot, uid)
            req_txt = (r.get("request_text") or "").strip()
            payload = r.get("payload") or {}

            # لوحة الأزرار للطلب
            kb = types.InlineKeyboardMarkup(row_width=3)
            kb.row(
                types.InlineKeyboardButton("📌 استلمت", callback_data=f"admin_queue_claim_{rid}"),
                types.InlineKeyboardButton("✅ تأكيد",  callback_data=f"admin_queue_accept_{rid}"),
                types.InlineKeyboardButton("🚫 إلغاء",  callback_data=f"admin_queue_cancel_{rid}"),
            )
            kb.row(
                types.InlineKeyboardButton("⏳ تأجيل",  callback_data=f"admin_queue_postpone_{rid}"),
                types.InlineKeyboardButton("📝 رسالة",  callback_data=f"admin_queue_message_{rid}"),
                types.InlineKeyboardButton("🖼️ صورة",  callback_data=f"admin_queue_photo_{rid}"),
            )

            # نص الرسالة (نحافظ على HTML لو موجود)
            head = f"🆕 طلب #{rid} — {name}\n"
            try:
                sent = bot.send_message(m.chat.id, head + req_txt, parse_mode="HTML", reply_markup=kb)
            except Exception:
                sent = bot.send_message(m.chat.id, head + req_txt, reply_markup=kb)

            # خزّن مرجع رسالة الأدمن في payload.admin_msgs لدعم نظام القفل
            try:
                admin_msgs = (payload.get("admin_msgs") or [])
                admin_msgs.append({"admin_id": m.chat.id, "message_id": sent.message_id})
                payload["admin_msgs"] = admin_msgs
                get_table("pending_requests").update({"payload": payload}).eq("id", rid).execute()
            except Exception as ee:
                logging.exception("[ADMIN] update admin_msgs failed: %s", ee)

    # ✅ بدّل إدخال الـID بمتصفح ملفات/منتجات إنلاين
    @bot.message_handler(func=lambda m: m.text in ["🚫 إيقاف منتج", "✅ تشغيل منتج"] and m.from_user.id in ADMINS)
    def admin_products_browser(m):
        bot.send_message(m.chat.id, "اختر الملف لعرض منتجاته:", reply_markup=_admin_products_groups_markup())

    # 🔄 مزامنة كل المنتجات المعرفة في PRODUCTS إلى جدول products
    @bot.message_handler(func=lambda m: m.text == "🔄 مزامنة المنتجات (DB)" and m.from_user.id in ADMINS)
    def seed_products(m):
        try:
            items = []
            for group, arr in PRODUCTS.items():
                for p in arr:
                    items.append((p.product_id, p.name, group))
            created = bulk_ensure_products(items)
            bot.reply_to(m, f"✅ تمت المزامنة.\nأُنشئ/تأكّد {created} صف(ًا).")
        except Exception as e:
            logging.exception("[ADMIN] bulk ensure products failed: %s", e)
            bot.reply_to(m, "❌ فشلت المزامنة. تفقد السجلات.")

    @bot.callback_query_handler(func=lambda c: c.data.startswith("adm_prod_g:") and c.from_user.id in ADMINS)
    def adm_group_open(call: types.CallbackQuery):
        slug = call.data.split(":", 1)[1]
        group_name = next((g for g in PRODUCTS.keys() if _slug(g) == slug), None)
        if not group_name:
            return bot.answer_callback_query(call.id, "❌ المجموعة غير موجودة.")
        try:
            bot.edit_message_text(f"📁 {group_name} — اختر منتجًا:", call.message.chat.id, call.message.message_id,
                                  reply_markup=_admin_products_list_markup(group_name))
        except Exception:
            # لو تعذّر التعديل أرسل رسالة جديدة
            bot.send_message(call.message.chat.id, f"📁 {group_name} — اختر منتجًا:", reply_markup=_admin_products_list_markup(group_name))
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda c: c.data == "adm_prod_back" and c.from_user.id in ADMINS)
    def adm_back(call: types.CallbackQuery):
        try:
            bot.edit_message_text("اختر الملف لعرض منتجاته:", call.message.chat.id, call.message.message_id,
                                  reply_markup=_admin_products_groups_markup())
        except Exception:
            bot.send_message(call.message.chat.id, "اختر الملف لعرض منتجاته:", reply_markup=_admin_products_groups_markup())
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("adm_prod_i:") and c.from_user.id in ADMINS)
    def adm_product_open(call: types.CallbackQuery):
        pid = int(call.data.split(":", 1)[1])
        state = "شغّال 🟢" if get_product_active(pid) else "موقوف 🔴"
        txt = f"المنتج #{pid}\nالحالة الحالية: {state}\nيمكنك تبديل الحالة:"
        try:
            bot.edit_message_text(txt, call.message.chat.id, call.message.message_id,
                                  reply_markup=_admin_product_actions_markup(pid))
        except Exception:
            bot.send_message(call.message.chat.id, txt, reply_markup=_admin_product_actions_markup(pid))
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("adm_prod_t:") and c.from_user.id in ADMINS)
    def adm_product_toggle(call: types.CallbackQuery):
        # كان سابقًا: _, pid, to = call.data.split(":")
        try:
            _, pid, to = call.data.split(":", 2)  # آمن حتى لو زاد المحتوى مستقبلًا
        except ValueError:
            return bot.answer_callback_query(call.id, "❌ تنسيق غير صحيح.")
        pid, to = int(pid), bool(int(to))
        ok = set_product_active(pid, to)
        if not ok:
            return bot.answer_callback_query(call.id, "❌ تعذّر تحديث الحالة.")
        log_action(call.from_user.id, f"{'enable' if to else 'disable'}_product", f"id={pid}")
        state = "✅ تم تشغيل المنتج" if to else "🚫 تم إيقاف المنتج"
        txt = f"المنتج #{pid}\n{state}\nالحالة الآن: {'شغّال 🟢' if to else 'موقوف 🔴'}"
        try:
            bot.edit_message_text(txt, call.message.chat.id, call.message.message_id,
                                  reply_markup=_admin_product_actions_markup(pid))
        except Exception:
            bot.send_message(call.message.chat.id, txt, reply_markup=_admin_product_actions_markup(pid))
        bot.answer_callback_query(call.id, "تم التحديث.")

    # ===== لوحة المزايا (Feature Flags) =====
    @bot.message_handler(func=lambda m: m.text == "🧩 تشغيل/إيقاف المزايا" and m.from_user.id in ADMINS)
    def features_menu(m):
        try:
            kb = _features_markup(page=0)
        except Exception as e:
            logging.exception("[ADMIN] features markup failed: %s", e)
            kb = types.InlineKeyboardMarkup(row_width=1)
            kb.add(types.InlineKeyboardButton("لا توجد مزايا مُسجّلة", callback_data="noop"))
        bot.send_message(m.chat.id, "بدّل حالة المزايا التالية:", reply_markup=kb)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("adm_feat_t:") and c.from_user.id in ADMINS)
    def adm_feature_toggle(call: types.CallbackQuery):
        try:
            prefix = "adm_feat_t:"
            tail = call.data[len(prefix):] if call.data.startswith(prefix) else call.data
            parts = tail.rsplit(":", 2)  # <= 3 عناصر
            if len(parts) == 3:
                key, to, page_s = parts
                try:
                    page = int(page_s)
                except Exception:
                    page = 0
            elif len(parts) == 2:
                key, to = parts
                page = 0
            else:
                return bot.answer_callback_query(call.id, "❌ تنسيق غير صحيح.")
            ok = set_feature_active(key, bool(int(to)))
        except Exception as e:
            logging.exception("[ADMIN][feat_toggle] parse/toggle error: %s", e)
            return bot.answer_callback_query(call.id, "❌ تنسيق غير صحيح.")

        try:
            bot.edit_message_reply_markup(
                call.message.chat.id,
                call.message.message_id,
                reply_markup=_features_markup(page=page)
            )
        except Exception:
            try:
                bot.edit_message_text(
                    "بدّل حالة المزايا التالية:",
                    call.message.chat.id,
                    call.message.message_id,
                    reply_markup=_features_markup(page=page)
                )
            except Exception:
                pass
        bot.answer_callback_query(call.id, "✅ تم التحديث.")

    @bot.callback_query_handler(func=lambda c: c.data.startswith("adm_feat_p:") and c.from_user.id in ADMINS)
    def adm_feature_page(call: types.CallbackQuery):
        try:
            page = int(call.data.split(":", 1)[1])
        except Exception:
            page = 0
        try:
            bot.edit_message_reply_markup(
                call.message.chat.id,
                call.message.message_id,
                reply_markup=_features_markup(page=page)
            )
        except Exception:
            try:
                bot.edit_message_text(
                    "بدّل حالة المزايا التالية:",
                    call.message.chat.id,
                    call.message.message_id,
                    reply_markup=_features_markup(page=page)
                )
            except Exception:
                pass
        bot.answer_callback_query(call.id)

    @bot.message_handler(func=lambda m: m.text == "📊 تقارير سريعة" and m.from_user.id in ADMINS)
    def quick_reports(m):
        dep, pur, top = totals_deposits_and_purchases_syp()
        lines = [f"💰 إجمالي الإيداعات: {dep:,} ل.س", f"🧾 إجمالي الشراء: {pur:,} ل.س"]
        # أفضل 5 عملاء خلال 7 أيام (إضافة جديدة)
        try:
            top5 = top5_clients_week()
            if top5:
                lines.append("🏅 أفضل ٥ عملاء (آخر 7 أيام):")
                for u in top5:
                    lines.append(f" • {u['name']} — شحن: {u['deposits']:,} ل.س | صرف: {u['spend']:,} ل.س")
        except Exception as _e:
            logging.exception("[REPORTS] top5 weekly failed: %s", _e)
        bot.send_message(m.chat.id, "\n".join(lines))

    @bot.message_handler(func=lambda m: m.text == "📈 تقرير المساعدين" and m.from_user.id == ADMIN_MAIN_ID)
    def assistants_daily_report(m):
        txt = summarize_assistants(days=7)
        bot.send_message(m.chat.id, txt, parse_mode="HTML")

    @bot.message_handler(func=lambda m: m.text == "📈 تقرير الإداريين (الكل)" and m.from_user.id == ADMIN_MAIN_ID)
    def all_admins_report(m):
        txt = summarize_all_admins(days=7)
        bot.send_message(m.chat.id, txt, parse_mode="HTML")


    @bot.message_handler(func=lambda m: m.text == "📈 تقرير المساعدين" and m.from_user.id == ADMIN_MAIN_ID)
    def assistants_daily_report(m):
        txt = summarize_assistants(days=7)
        bot.send_message(m.chat.id, txt, parse_mode="HTML")

    @bot.message_handler(func=lambda m: m.text == "📈 تقرير الإداريين (الكل)" and m.from_user.id == ADMIN_MAIN_ID)
    def all_admins_report(m):
        txt = summarize_all_admins(days=7)
        bot.send_message(m.chat.id, txt, parse_mode="HTML")

    # ==== بث للجميع ====
    @bot.message_handler(func=lambda m: m.text == "📣 رسالة للجميع" and (m.from_user.id in ADMINS or m.from_user.id == ADMIN_MAIN_ID))
    def broadcast_menu(m):
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        kb.row("📬 ترحيب — نحن شغالين", "📢 عرض اليوم")
        kb.row("📊 استفتاء سريع", "📝 رسالة من عندي")
        kb.row("⬅️ رجوع")
        bot.send_message(m.chat.id, "اختر نوع الرسالة للإرسال إلى الجميع:", reply_markup=kb)


# === نقلناها إلى مستوى الموديول لتتفادا NameError ===
def _collect_all_user_ids() -> set[int]:
    """
    يرجع مجموعة بكل user_id المعروفين (من الجدول + الأدمن).
    """
    ids: set[int] = set()

    # نسحب كل المستخدمين من الجدول
    try:
        rs = get_table(USERS_TABLE).select("user_id").execute()
        rows = rs.data or []
    except Exception:
        rows = []

    for r in rows:
        try:
            uid = int(r.get("user_id") or 0)
            if uid:
                ids.add(uid)
        except Exception:
            pass

    # اختياري: إضافة الأدمن الرئيسي وباقي الأدمنين لسهولة الاختبار
    try:
        ids.add(int(ADMIN_MAIN_ID))
    except Exception:
        pass

    try:
        for aid in ADMINS:
            try:
                ids.add(int(aid))
            except Exception:
                pass
    except Exception:
        pass

    return ids
    
def _features_toggle_one(c):
    try:
        _, key, to, page = c.data.split(":", 3)
        to = int(to)
        set_feature_active(key, bool(to))
        try:
            grouped = list_features_grouped() or {}
        except Exception:
            grouped = {}
        found_group = None
        for g, items in grouped.items():
            if any((it.get("key") == key) for it in (items or [])):
                found_group = g
                break
        kb = _features_group_items_markup(found_group, int(page)) if found_group else _features_markup(int(page))
        try: bot.answer_callback_query(c.id, "تم التبديل.")
        except Exception: pass
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=kb)
    except Exception as e:
        logging.exception("[ADMIN] feature toggle failed: %s", e)
        try: bot.answer_callback_query(c.id, "تعذّر التبديل")
        except Exception: pass


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("adm_feat_p:"))
def _features_page_cb(c):
    try:
        _, page = c.data.split(":", 1)
        page = int(page)
        kb = _features_markup(page)
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=kb)
    except Exception as e:
        logging.exception("[ADMIN] feature page cb failed: %s", e)


@bot.callback_query_handler(func=lambda c: c.data in ("adm_feat_home:flat","adm_feat_home:groups"))
def _features_home_toggle(c):
    try:
        mode = "flat" if c.data.endswith(":flat") else "groups"
        kb = _features_markup(0) if mode == "flat" else _features_groups_markup()
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=kb)
        bot.answer_callback_query(c.id)
    except Exception as e:
        logging.exception("[ADMIN] feature home toggle failed: %s", e)
