# -*- coding: utf-8 -*-
# handlers/cash_transfer.py — تحويل كاش داخل التطبيق مع /cancel + confirm_guard + رسائل تسويقية

# استيرادات مرنة موجودة عندك
try:
    from anti_spam import too_soon
except Exception:
    try:
        from services.anti_spam import too_soon
    except Exception:
        from handlers.anti_spam import too_soon

try:
    from telegram_safety import remove_inline_keyboard
except Exception:
    try:
        from services.telegram_safety import remove_inline_keyboard
    except Exception:
        from handlers.telegram_safety import remove_inline_keyboard

try:
    from validators import parse_amount
except Exception:
    try:
        from services.validators import parse_amount
    except Exception:
        from handlers.validators import parse_amount

# حارس التأكيد الموحد
try:
    from services.ui_guards import confirm_guard
except Exception:
    # fallback بسيط لو الملف في مسار مختلف
    from ui_guards import confirm_guard

from telebot import types
from services.wallet_service import (
    add_purchase,
    has_sufficient_balance,
    register_user_if_not_exist,
    # هولد
    create_hold,
    # ✅ مهم علشان نتحقق من المتاح (balance - held)
    get_available_balance,
    # لعرض الرصيد في رسالة الأدمن بعد الحجز
    get_balance,
)
from database.db import get_table
from handlers import keyboards
from services.queue_service import add_pending_request, process_queue
import math  # لإدارة صفحات الكيبورد
import logging

from services.state_adapter import UserStateDictLike
user_states = UserStateDictLike()
CASH_TYPES = [
    "تحويل إلى سيرياتيل كاش",
    "تحويل إلى أم تي إن كاش",
    "تحويل إلى شام كاش",
]

CASH_PAGE_SIZE = 3
COMMISSION_PER_50000 = 3500

# ===== مظهر الرسائل + /cancel =====
BAND = "━━━━━━━━━━━━━━━━"
CANCEL_HINT = "✋ اكتب /cancel للإلغاء في أي وقت."

def banner(title: str, lines: list[str]) -> str:
    body = "\n".join(lines)
    return f"{BAND}\n{title}\n{body}\n{BAND}"

def with_cancel_hint(text: str) -> str:
    return f"{text}\n\n{CANCEL_HINT}"

def _name_of(user):
    # محاولة لطيفة لاستخراج اسم العميل
    return (getattr(user, "full_name", None) or getattr(user, "first_name", None) or "صديقنا").strip()

def _fmt(n):
    try:
        return f"{int(n):,} ل.س"
    except Exception:
        return f"{n} ل.س"

def build_cash_menu(page: int = 0):
    total = len(CASH_TYPES)
    pages = max(1, math.ceil(total / CASH_PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    kb = types.InlineKeyboardMarkup()
    start = page * CASH_PAGE_SIZE
    end = start + CASH_PAGE_SIZE
    for idx, label in enumerate(CASH_TYPES[start:end], start=start):
        kb.add(types.InlineKeyboardButton(label, callback_data=f"cash_sel_{idx}"))
    nav = []
    if page > 0:
        nav.append(types.inline_keyboard_button("◀️", f"cash_page_{page-1}"))
    nav.append(types.InlineKeyboardButton(f"{page+1}/{pages}", callback_data="cash_noop"))
    if page < pages - 1:
        nav.append(types.InlineKeyboardButton("▶️", callback_data=f"cash_page_{page+1}"))
    if nav:
        kb.row(*nav)
    kb.add(types.InlineKeyboardButton("❌ إلغاء", callback_data="commission_cancel"))
    return kb

def calculate_commission(amount: int) -> int:
    # حساب بالعدد الصحيح لتفادي float
    blocks = amount // 50000
    remainder = amount % 50000
    commission = blocks * COMMISSION_PER_50000
    # جزء نسبي من العمولة
    commission += (remainder * COMMISSION_PER_50000) // 50000
    return int(commission)

# التفافات بسيطة للرصيد (نحافظ على بنية ملفك الأصلي)
def get_balance_local(user_id):
    from services.wallet_service import get_balance as _get
    return _get(user_id)

def make_inline_buttons(*buttons):
    kb = types.InlineKeyboardMarkup()
    for text, data in buttons:
        kb.add(types.InlineKeyboardButton(text, callback_data=data))
    return kb

def start_cash_transfer(bot, message, history=None):
    user_id = message.from_user.id
    register_user_if_not_exist(user_id, _name_of(message.from_user))
    if history is not None:
        if not isinstance(history.get(user_id), list):
            history[user_id] = []
        history[user_id].append("cash_menu")
    logging.info(f"[CASH][{user_id}] فتح قائمة تحويل كاش")
    bot.send_message(
        message.chat.id,
        with_cancel_hint("💸 جاهز نحرك الفلوس؟ اختار نوع التحويل من محفظتك:"),
        reply_markup=build_cash_menu(0)
    )

def register(bot, history):

    # ===== /cancel العام =====
    @bot.message_handler(commands=['cancel'])
    def cancel_cmd(msg):
        uid = msg.from_user.id
        user_states.pop(uid, None)
        bot.send_message(
            msg.chat.id,
            banner("❌ تم الإلغاء", [f"يا {_name_of(msg.from_user)}، رجعناك للقائمة. اختار اللي يناسبك 👇"]),
            reply_markup=build_cash_menu(0)
        )

    # تنقّل صفحات أنواع التحويل
    @bot.callback_query_handler(func=lambda c: c.data.startswith("cash_page_"))
    def _paginate_cash_menu(call):
        page = int(call.data.split("_")[-1])
        try:
            bot.edit_message_reply_markup(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=build_cash_menu(page)
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id)

    # زر عدّاد صفحات (لا شيء)
    @bot.callback_query_handler(func=lambda c: c.data == "cash_noop")
    def _noop(call):
        bot.answer_callback_query(call.id)

    # اختيار نوع التحويل
    @bot.callback_query_handler(func=lambda c: c.data.startswith("cash_sel_"))
    def _cash_type_selected(call):
        idx = int(call.data.split("_")[-1])
        if idx < 0 or idx >= len(CASH_TYPES):
            logging.warning(f"[CASH][{call.from_user.id}] اختيار نوع كاش غير صالح: {idx}")
            bot.answer_callback_query(call.id, "❌ خيار غير صالح.")
            return
        cash_type = CASH_TYPES[idx]
        user_id = call.from_user.id

        user_states[user_id] = {"step": "show_commission", "cash_type": cash_type}
        if not isinstance(history.get(user_id), list):
            history[user_id] = []
        history[user_id].append("cash_menu")
        logging.info(f"[CASH][{user_id}] اختار نوع تحويل: {cash_type}")
        name = _name_of(call.from_user)
        text = with_cancel_hint(
            f"⚠️ يا {name}، تنويه مهم:\n"
            f"• العمولة لكل 50,000 ليرة = {COMMISSION_PER_50000:,} ل.س.\n\n"
            "لو تمام، دوس موافق وكمل اكتب الرقم اللي هتحوّل له."
        )
        kb = make_inline_buttons(
            ("✅ موافق", "commission_confirm"),
            ("❌ إلغاء", "commission_cancel")
        )
        try:
            bot.edit_message_text(
                text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=kb
            )
        except Exception:
            bot.send_message(call.message.chat.id, text, reply_markup=kb)
        bot.answer_callback_query(call.id)

    # الدخول من زر بالقائمة الرئيسية (لو عندك زر)
    @bot.message_handler(func=lambda msg: msg.text == "💵 تحويل الى رصيد كاش")
    def open_cash_menu(msg):
        start_cash_transfer(bot, msg, history)

    # نفس الفكرة لكن لو المستخدم كتب نوع التحويل كنص
    @bot.message_handler(func=lambda msg: msg.text in CASH_TYPES)
    def handle_cash_type(msg):
        user_id = msg.from_user.id
        cash_type = msg.text
        user_states[user_id] = {"step": "show_commission", "cash_type": cash_type}
        if not isinstance(history.get(user_id), list):
            history[user_id] = []
        history[user_id].append("cash_menu")
        logging.info(f"[CASH][{user_id}] اختار نوع تحويل: {cash_type} (من رسالة)")
        name = _name_of(msg.from_user)
        text = with_cancel_hint(
            f"⚠️ يا {name}، تنويه مهم:\n"
            f"• العمولة لكل 50,000 ليرة = {COMMISSION_PER_50000:,} ل.س.\n\n"
            "لو تمام، اكتب الرقم اللي هتحوّل له."
        )
        kb = make_inline_buttons(
            ("✅ موافق", "commission_confirm"),
            ("❌ إلغاء", "commission_cancel")
        )
        bot.send_message(msg.chat.id, text, reply_markup=kb)

    # إلغاء
    @bot.callback_query_handler(func=lambda call: call.data == "commission_cancel")
    def commission_cancel(call):
        user_id = call.from_user.id
        logging.info(f"[CASH][{user_id}] ألغى عملية التحويل")
        user_states.pop(user_id, None)
        try:
            remove_inline_keyboard(bot, call.message)
        except Exception:
            pass
        bot.send_message(
            call.message.chat.id,
            banner("❌ تم الإلغاء", ["رجعناك للقائمة الرئيسية 👇"]),
            reply_markup=build_cash_menu(0)
        )

    # موافقة على الشروط → اطلب الرقم
    @bot.callback_query_handler(func=lambda call: call.data == "commission_confirm")
    def commission_confirmed(call):
        user_id = call.from_user.id
        user_states[user_id] = {"step": "awaiting_number", **user_states.get(user_id, {})}
        kb = make_inline_buttons(("❌ إلغاء", "commission_cancel"))
        try:
            bot.edit_message_text(
                with_cancel_hint("📲 ابعتلنا الرقم اللي هتحوّل له:"),
                call.message.chat.id, call.message.message_id, reply_markup=kb
            )
        except Exception:
            bot.send_message(call.message.chat.id, with_cancel_hint("📲 ابعتلنا الرقم اللي هتحوّل له:"), reply_markup=kb)

    # استلام الرقم
    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "awaiting_number")
    def get_target_number(msg):
        user_id = msg.from_user.id
        user_states[user_id] = {**user_states.get(user_id, {}), "number": msg.text.strip(), "step": "confirm_number"}
        logging.info(f"[CASH][{user_id}] رقم التحويل: {msg.text}")
        kb = make_inline_buttons(
            ("❌ إلغاء", "commission_cancel"),
            ("✏️ تعديل", "edit_number"),
            ("✔️ تأكيد", "number_confirm")
        )
        bot.send_message(
            msg.chat.id,
            with_cancel_hint(f"🔢 الرقم المدخل: {msg.text}\n\nتمام كده؟"),
            reply_markup=kb
        )

    @bot.callback_query_handler(func=lambda call: call.data == "edit_number")
    def edit_number(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "awaiting_number"
        bot.send_message(call.message.chat.id, with_cancel_hint("📲 اكتب الرقم من جديد:"))

    # بعد تأكيد الرقم → اطلب المبلغ
    @bot.callback_query_handler(func=lambda call: call.data == "number_confirm")
    def number_confirm(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "awaiting_amount"
        kb = make_inline_buttons(("❌ إلغاء", "commission_cancel"))
        try:
            bot.edit_message_text(
                with_cancel_hint("💰 اكتب قيمة التحويل المطلوب (بالأرقام):"),
                call.message.chat.id, call.message.message_id, reply_markup=kb
            )
        except Exception:
            bot.send_message(call.message.chat.id, with_cancel_hint("💰 اكتب قيمة التحويل المطلوب (بالأرقام):"), reply_markup=kb)

    # استلام المبلغ وحساب العمولة
    @bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "awaiting_amount")
    def get_amount_and_confirm(msg):
        user_id = msg.from_user.id
        name = _name_of(msg.from_user)
        amount_text = (msg.text or "").strip()
        try:
            amount = parse_amount(amount_text, min_value=1)
        except Exception:
            logging.warning(f"[CASH][{user_id}] مبلغ غير صالح: {msg.text}")
            bot.send_message(msg.chat.id, with_cancel_hint(f"⚠️ يا {name}، دخّل مبلغ صحيح بالأرقام من غير فواصل/رموز."))
            return

        state = user_states.get(user_id, {})
        commission = calculate_commission(amount)
        total = amount + commission
        state.update({"amount": amount, "commission": commission, "total": total, "step": "confirming"})
        user_states[user_id] = state

        kb = make_inline_buttons(
            ("❌ إلغاء", "commission_cancel"),
            ("✏️ تعديل", "edit_amount"),
            ("✔️ تأكيد", "cash_confirm")
        )
        summary = banner(
            "📤 تأكيد العملية",
            [
                f"• الرقم: {state['number']}",
                f"• المبلغ: {_fmt(amount)}",
                f"• العمولة: {_fmt(commission)}",
                f"• الإجمالي: {_fmt(total)}",
                f"• الطريقة: {state['cash_type']}"
            ]
        )
        bot.send_message(msg.chat.id, with_cancel_hint(summary), reply_markup=kb)

    @bot.callback_query_handler(func=lambda call: call.data == "edit_amount")
    def edit_amount(call):
        user_id = call.from_user.id
        user_states[user_id]["step"] = "awaiting_amount"
        bot.send_message(call.message.chat.id, with_cancel_hint("💰 اكتب المبلغ من جديد:"))

    # تأكيد نهائي → إنشاء هولد + إرسال للطابور
    @bot.callback_query_handler(func=lambda call: call.data == "cash_confirm")
    def confirm_transfer(call):
        user_id = call.from_user.id
        name = _name_of(call.from_user)

        # ✅ قاعدة عامة: عند التأكيد — احذف الكيبورد فقط + Debounce
        if confirm_guard(bot, call, "cash_confirm"):
            return

        data = user_states.get(user_id, {}) or {}
        number = data.get("number")
        cash_type = data.get("cash_type")
        amount = int(data.get('amount') or 0)
        commission = int(data.get('commission') or 0)
        total = int(data.get('total') or 0)

        # فحص الرصيد المتاح (balance - held)
        available = get_available_balance(user_id)
        if available is None:
            return bot.send_message(call.message.chat.id, "❌ حصل خطأ في جلب الرصيد. جرّب تاني.\n\n" + CANCEL_HINT)

        if available < total:
            shortage = total - available
            kb = make_inline_buttons(("💳 شحن المحفظة", "recharge_wallet"), ("⬅️ رجوع", "commission_cancel"))
            return bot.send_message(
                call.message.chat.id,
                with_cancel_hint(
                    f"❌ يا {name}، متاحك الحالي {_fmt(available)} والمطلوب {_fmt(total)}.\n"
                    f"نقصك {_fmt(shortage)} — كمّل شحن ونمشي الطلب سِكة سريعة 😉"
                ),
                reply_markup=kb
            )

        # إنشاء هولد بدل الخصم الفوري (ذرّي من خلال الـ RPC)
        hold_desc = f"حجز تحويل كاش — {cash_type} — رقم {number}"
        r = create_hold(user_id, total, hold_desc)
        if getattr(r, "error", None) or not getattr(r, "data", None):
            logging.error(f"[CASH][{user_id}] create_hold failed: {getattr(r, 'error', r)}")
            return bot.send_message(call.message.chat.id, "❌ معذرة، ماقدرنا نعمل حجز دلوقتي. جرّب بعد شوية.\n\n" + CANCEL_HINT)

        data_resp = getattr(r, "data", None)
        hold_id = (data_resp if isinstance(data_resp, str) else (data_resp.get("id") if isinstance(data_resp, dict) else None))

        # رصيد بعد الحجز (اختياري للعرض)
        try:
            balance_after = get_balance(user_id)
        except Exception:
            balance_after = None

        # رسالة الإدمن الموحّدة
        admin_msg = (
            f"💰 رصيد المستخدم الآن: {_fmt(balance_after) if balance_after is not None else '—'}\n"
            f"🆕 طلب جديد — تحويل كاش\n"
            f"👤 الاسم: <code>{_name_of(call.from_user)}</code>\n"
            f"يوزر: <code>@{call.from_user.username or ''}</code>\n"
            f"آيدي: <code>{user_id}</code>\n"
            f"🔖 النوع: {cash_type}\n"
            f"📲 رقم المستفيد: <code>{number}</code>\n"
            f"💸 المبلغ: {_fmt(amount)}\n"
            f"🧾 العمولة: {_fmt(commission)}\n"
            f"✅ الإجمالي: {_fmt(total)}\n"
            f"🔒 HOLD: <code>{hold_id}</code>"
        )

        add_pending_request(
            user_id=user_id,
            username=call.from_user.username,
            request_text=admin_msg,
            payload={
                "type": "cash_transfer",
                "number": number,
                "cash_type": cash_type,
                "amount": amount,
                "commission": commission,
                "total": total,
                "reserved": total,     # للتوافق مع مسارات قديمة
                "hold_id": hold_id,    # المسار الحديث الآمن
                "hold_desc": hold_desc
            }
        )

        # شغّل الطابور
        process_queue(bot)

        # رسالة للعميل (من غير تعديل/حذف للرسالة السابقة — إحنا شيلنا الكيبورد خلاص)
        bot.send_message(
            call.message.chat.id,
            banner(
                f"✅ تمام يا {name}! بعتنا طلب تحويلك 🚀",
                [
                    "⏱️ التنفيذ عادةً خلال 1–4 دقايق.",
                    "ℹ️ تقدر تبعت طلب جديد لو حابب — كل الطلبات بتحترم الرصيد المتاح 😉",
                ]
            )
        )
        user_states[user_id]["step"] = "waiting_admin"

    # زر شحن المحفظة
    @bot.callback_query_handler(func=lambda call: call.data == "recharge_wallet")
    def show_recharge_methods(call):
        bot.send_message(call.message.chat.id, "💳 اختار طريقة شحن المحفظة:", reply_markup=keyboards.recharge_menu())
