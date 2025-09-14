# -*- coding: utf-8 -*-
# handlers/media_services.py

from telebot import types
import logging
from services.wallet_service import (
    register_user_if_not_exist,
    get_available_balance,
    get_balance,
    create_hold,
)

# طابور الطلبات (اختياري: موجود عندك)
try:
    from services.queue_service import add_pending_request, process_queue
except Exception:
    def add_pending_request(*args, **kwargs): return None
    def process_queue(*args, **kwargs): return None

# كيبورد خدمات الميديا
from handlers.keyboards import media_services_menu

# ----------------------------
# أدوات مساعدة للتطبيع النصّي
# ----------------------------
def _norm(s: str) -> str:
    if not isinstance(s, str):
        return ""
    s = s.strip()
    import re
    # إزالة الإيموجي والمسافات المتعددة
    s = re.sub(r"[\u2600-\u27BF\U0001F300-\U0001FAD6\U0001FA70-\U0001FAFF\U0001F900-\U0001F9FF]", "", s)
    s = re.sub(r"\s+", "", s)
    # توحيد أحرف عربية شائعة
    return (s.replace("أ", "ا")
             .replace("إ", "ا")
             .replace("آ", "ا")
             .replace("ة", "ه")
             .replace("ى", "ي"))

LABELS_MEDIA = {
    _norm("🎭 خدمات سوشيال/ميديا"),
    _norm("🖼️ خدمات إعلانية وتصميم"),
}

# ----------------------------
# حُرّاس وخدمات اختيارية
# ----------------------------
try:
    from services.ui_guards import confirm_guard
except Exception:
    # بديل آمن في حال غياب الحارس
    def confirm_guard(*_args, **_kwargs):
        return False  # لا يمنع شيء (يرجع False = سمح بالمتابعة)

try:
    from services.system_service import is_maintenance, maintenance_message
except Exception:
    def is_maintenance(): return False
    def maintenance_message(): return "🔧 النظام تحت الصيانة مؤقتًا. جرّب لاحقًا."

try:
    from services.feature_flags import block_if_disabled
except Exception:
    def block_if_disabled(bot, chat_id, flag_key, nice_name): return False

# (اختياري) تفعيل/تعطيل منتج معيّن من لوحة الأدمن
try:
    from services.products_admin import get_product_active
except Exception:
    def get_product_active(_pid: int) -> bool: return True

# ----------------------------
# بيانات وعرض
# ----------------------------
BAND = "━━━━━━━━━━━━━━━━━━━━"
CANCEL_HINT = "✋ اكتب /cancel للإلغاء في أي وقت."
user_media_state: dict[int, dict] = {}  # حالة المستخدم ضمن تدفق الميديا

# يمكنك أخذها من .env إن رغبت لاحقًا
USD_RATE = 11000  # سعر الصرف ليرة/دولار

MEDIA_PRODUCTS = {
    "🖼️ تصميم لوغو احترافي": 300,
    "📱 إدارة ونشر يومي": 300,
    "📢 إطلاق حملة إعلانية": 300,
    "🎬 مونتاج فيديو قصير": 150,
    "🧵 خيوط تويتر جاهزة": 80,
    "🎙️ تعليق صوتي احترافي": 120,
    "📰 كتابة محتوى تسويقي": 95,
    # اختياري: لو زرّاك في main.py يحتوي هذول، عيّن قيمهم:
    # "🧾 باقة متكاملة شهرية": 300,
    # "✏️ طلب مخصص": 0,  # 0 = يتفق عليه لاحقاً
}

# (اختياري) لو عندك IDs لهذه الخدمات — لاستعمال get_product_active
MEDIA_PRODUCT_IDS = {
    # "🖼️ تصميم لوغو احترافي": 101,
    # "📱 إدارة ونشر يومي": 102,
}

# ----------------------------
# أدوات مساعدة صغيرة
# ----------------------------
def _name(u):
    n = getattr(u, "first_name", None) or getattr(u, "full_name", None) or ""
    n = (n or "").strip()
    return n if n else "صديقنا"

def _fmt_syp(n):
    try:
        return f"{int(n):,} ل.س"
    except Exception:
        return f"{n} ل.س"

def _fmt_usd(x):
    try:
        return f"${float(x):.2f}"
    except Exception:
        return f"${x}"

def _with_cancel(text: str) -> str:
    return f"{text}\n\n{CANCEL_HINT}"

def _service_unavailable_guard(bot, chat_id) -> bool:
    """يرجع True إذا الخدمة غير متاحة (صيانة/Flag)."""
    if is_maintenance():
        bot.send_message(chat_id, maintenance_message())
        return True
    if block_if_disabled(bot, chat_id, "media_services", "خدمات سوشيال/ميديا"):
        return True
    return False

def _is_service_enabled(service_label: str) -> bool:
    """لو مُعرّف ID للمنتج يتم احترامه؛ وإلا نعتبره مُفعّلًا."""
    pid = MEDIA_PRODUCT_IDS.get(service_label)
    try:
        return True if pid is None else bool(get_product_active(pid))
    except Exception:
        return True

# ----------------------------
# التسجيل الرئيسي للهاندلرز
# ----------------------------
def register_media_services(bot, history):
    # حارس مرن للدبل-كليك (يتحمّل اختلاف التواقيع)
    def _confirm_once(call, key="media_final_confirm"):
        try:
            # توقيع شائع: confirm_guard(call) -> True لو يمنع
            return bool(confirm_guard(call))
        except TypeError:
            try:
                # توقيع بديل: confirm_guard(bot, call, key)
                return bool(confirm_guard(bot, call, key))
            except Exception:
                return False

    # فتح قائمة خدمات الميديا (يستجيب للتسميتين)
    @bot.message_handler(func=lambda msg: _norm(msg.text) in LABELS_MEDIA)
    def open_media(msg):
        # إنهاء أي رحلة/مسار سابق عالق (اختياري)
        try:
            from handlers.start import _reset_user_flows
            _reset_user_flows(msg.from_user.id)
        except Exception:
            pass

        if _service_unavailable_guard(bot, msg.chat.id):
            return

        user_id = msg.from_user.id
        try:
            register_user_if_not_exist(user_id, _name(msg.from_user))
        except Exception:
            pass

        if history is not None:
            try:
                history.setdefault(user_id, []).append("media_menu")
            except Exception:
                pass

        bot.send_message(
            msg.chat.id,
            _with_cancel(f"🎯 يا {_name(msg.from_user)}، اختر الخدمة الإعلامية المناسبة:\n{BAND}"),
            reply_markup=media_services_menu()
        )
        # حفظ خطوة تقريبية لمنظومة الرجوع العامة (اختياري)
        try:
            from services.state_service import set_state
            set_state(user_id, {"step": "media_menu"})
        except Exception:
            pass

    # اختيار خدمة محددة من القائمة
    @bot.message_handler(func=lambda msg: msg.text in MEDIA_PRODUCTS)
    def handle_selected_service(msg):
        if _service_unavailable_guard(bot, msg.chat.id):
            return

        if not _is_service_enabled(msg.text):
            return bot.send_message(msg.chat.id, "⛔ هذه الخدمة متوقّفة مؤقتًا. جرّب خدمة أخرى أو لاحقًا.")

        user_id = msg.from_user.id
        service = msg.text
        price_usd = MEDIA_PRODUCTS[service]
        price_syp = int(price_usd * USD_RATE)

        user_media_state[user_id] = {
            "step": "confirm_service",
            "service": service,
            "price_usd": price_usd,
            "price_syp": price_syp,
        }

        # أزرار التأكيد + رجوع + إلغاء
        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton("✅ تأكيد الطلب", callback_data="media_final_confirm"),
            types.InlineKeyboardButton("⬅️ رجوع", callback_data="media_back"),
        )
        kb.add(
            types.InlineKeyboardButton("❌ إلغاء", callback_data="media_cancel"),
        )

        bot.send_message(
            msg.chat.id,
            _with_cancel(
                f"✨ اختيار ممتاز يا {_name(msg.from_user)}!\n"
                f"• الخدمة: {service}\n"
                f"• السعر: {_fmt_usd(price_usd)} ≈ {_fmt_syp(price_syp)}\n"
                f"{BAND}\n"
                "لو تمام، اضغط «تأكيد الطلب». أو ارجع واختر خدمة ثانية."
            ),
            reply_markup=kb
        )

    # رجوع للقائمة (لا يعتبر إلغاء، فقط تنظيف الحالة)
    @bot.callback_query_handler(func=lambda c: c.data == "media_back")
    def media_back(c):
        user_media_state.pop(c.from_user.id, None)
        try:
            bot.answer_callback_query(c.id)
        except Exception:
            pass
        bot.send_message(c.from_user.id, "⬅️ رجوعنا لقائمة خدمات الميديا.", reply_markup=media_services_menu())

    # إلغاء كامل (يمسح الحالة ويرجع للقائمة)
    @bot.callback_query_handler(func=lambda c: c.data == "media_cancel")
    def media_cancel(c):
        user_media_state.pop(c.from_user.id, None)
        try:
            bot.answer_callback_query(c.id, "تم الإلغاء.")
        except Exception:
            pass
        bot.send_message(c.from_user.id, _with_cancel("❌ تم الإلغاء. رجعناك لقائمة الميديا ✨"), reply_markup=media_services_menu())

    # تأكيد نهائي
    @bot.callback_query_handler(func=lambda c: c.data == "media_final_confirm")
    def media_final_confirm(c):
        # Debounce (يمنع الدبل-كليك)
        if _confirm_once(c):
            return

        user_id = c.from_user.id
        name = _name(c.from_user)
        state = user_media_state.get(user_id) or {}

        service = state.get("service")
        price_syp = int(state.get("price_syp") or 0)
        price_usd = state.get("price_usd")

        if not service or price_syp <= 0:
            try:
                bot.answer_callback_query(c.id, "❌ الطلب ناقص. جرّب من جديد.")
            except Exception:
                pass
            return

        if not _is_service_enabled(service):
            return bot.send_message(user_id, "⛔ هذه الخدمة متوقّفة مؤقتًا. جرّب خدمة أخرى أو لاحقًا.")

        # التحقق من الرصيد المتاح
        available = 0
        try:
            available = get_available_balance(user_id)
        except Exception:
            pass

        if available < price_syp:
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("💳 شحن المحفظة", callback_data="media_recharge"))
            kb.add(types.InlineKeyboardButton("⬅️ رجوع", callback_data="media_back"))
            text = (
                f"❌ يا {name}، رصيدك المتاح غير كافٍ.\n"
                f"المتاح: {_fmt_syp(available)}\n"
                f"السعر: {_fmt_syp(price_syp)}\n"
                "اشحن المحفظة ثم أكمل الطلب."
            )
            try:
                bot.answer_callback_query(c.id)
            except Exception:
                pass
            return bot.send_message(user_id, _with_cancel(text), reply_markup=kb)

        # إنشاء حجز (Hold)
        hold_id = None
        try:
            resp = create_hold(user_id, price_syp, f"حجز خدمة ميديا — {service}")
            if getattr(resp, "error", None):
                logging.error("create_hold (media) error: %s", resp.error)
                return bot.send_message(user_id, "⚠️ حصل خطأ أثناء الحجز. جرّب بعد قليل.")
            hold_id = getattr(resp, "data", None) or (resp.get("id") if isinstance(resp, dict) else None)
        except Exception as e:
            logging.exception("create_hold (media) exception: %s", e)
            return bot.send_message(user_id, "⚠️ حصل عطل أثناء الحجز. حاول مرة أخرى.")

        if not hold_id:
            return bot.send_message(user_id, "⚠️ لم ينجح الحجز. حاول مرة أخرى.")

        # رسالة موحّدة للإدارة + تفاصيل الحجز
        try:
            balance_now = get_balance(user_id)
        except Exception:
            balance_now = 0

        admin_text = (
            f"🆕 طلب «خدمات ميديا»\n"
            f"👤 الاسم: <code>{c.from_user.full_name}</code>\n"
            f"يوزر: <code>@{c.from_user.username or ''}</code>\n"
            f"آيدي: <code>{user_id}</code>\n"
            f"🎭 الخدمة: {service}\n"
            f"💵 السعر: {price_syp:,} ل.س (≈ {_fmt_usd(price_usd)})\n"
            f"💰 رصيده الآن: {balance_now:,} ل.س\n"
            f"(type=media)"
        )

        add_pending_request(
            user_id=user_id,
            username=c.from_user.username,
            request_text=admin_text,
            payload={
                "type": "media",
                "service": service,
                "product_name": service,
                "count": service,
                "price": price_syp,
                "reserved": price_syp,
                "hold_id": hold_id,
            },
        )
        process_queue(bot)

        try:
            bot.answer_callback_query(c.id, "تم الإرسال 🚀")
        except Exception:
            pass

        user_text = (
            f"✅ تمام يا {name}! أرسلنا طلب «{service}» للإدارة.\n"
            f"{BAND}\n"
            "سنوافيك بالتنفيذ قريبًا."
        )
        bot.send_message(user_id, _with_cancel(user_text))

        # إبقاء أو تحديث الحالة لحين إشعار التنفيذ
        user_media_state[user_id] = {"step": "wait_admin"}

    # زر شحن المحفظة (من شاشة الرصيد غير الكافي)
    @bot.callback_query_handler(func=lambda c: c.data == "media_recharge")
    def media_recharge(c):
        try:
            from handlers import keyboards
            bot.send_message(c.message.chat.id, "💳 اختر طريقة شحن محفظتك:", reply_markup=keyboards.recharge_menu())
        except Exception:
            bot.send_message(c.message.chat.id, "💳 لتعبئة المحفظة: استخدم قائمة الشحن أو تواصل مع الإدارة.")
        try:
            bot.answer_callback_query(c.id)
        except Exception:
            pass

# نقطة دخول قياسية يستدعيها main.py
def register(bot, history):
    register_media_services(bot, history)

# تُستدعى من main.py عندما يضغط المستخدم على خدمة محددة (لوغو/إدارة/حملة/…)
def show_media_services(bot, msg, user_state=None):
    # حارس الصيانة + Flag
    if is_maintenance():
        bot.send_message(msg.chat.id, maintenance_message()); return
    if block_if_disabled(bot, msg.chat.id, "media_services", "خدمات سوشيال/ميديا"):
        return

    prod = msg.text or ""
    if prod not in MEDIA_PRODUCTS:
        return bot.send_message(msg.chat.id, "⚠️ خيار غير معروف. اختر من القائمة.", reply_markup=media_services_menu())

    price_usd = MEDIA_PRODUCTS[prod]
    price_syp = int(price_usd * USD_RATE)
    uid = msg.from_user.id

    user_media_state[uid] = {
        "product": prod,
        "service": prod,
        "price_usd": price_usd,
        "price_syp": price_syp,
        "step": "confirm_service",
    }
    if isinstance(user_state, dict):
        user_state.setdefault(uid, {})["step"] = "media_confirm"

    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ تأكيد الطلب", callback_data="media_final_confirm"),
        types.InlineKeyboardButton("⬅️ رجوع", callback_data="media_back"),
    )
    kb.add(types.InlineKeyboardButton("❌ إلغاء", callback_data="media_cancel"))

    bot.send_message(
        msg.chat.id,
        _with_cancel(
            f"🎯 <b>{prod}</b>\n{BAND}\n"
            f"السعر التقديري: {_fmt_usd(price_usd)} (~{_fmt_syp(price_syp)})\n\n"
            f"هل تريد المتابعة؟"
        ),
        reply_markup=kb
    )
