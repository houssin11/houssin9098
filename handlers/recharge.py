from telebot import types
from config import ADMIN_MAIN_ID
from services.recharge_service import apply_recharge
from handlers import keyboards  # ✅ الكيبورد الموحد
from services.wallet_service import register_user_if_not_exist, get_balance  # ✅ إضافة get_balance لرسالة الأدمن
from types import SimpleNamespace
from services.queue_service import add_pending_request, process_queue
from services.validators import parse_amount
from services.telegram_safety import remove_inline_keyboard
from services.anti_spam import too_soon
import logging

recharge_requests = {}
recharge_pending = set()

# ✅ الحد الأدنى للشحن
MIN_RECHARGE = 15000

SYRIATEL_NUMBERS = ["0011111", "0022222", "0033333", "0044444"]
MTN_NUMBERS = ["0005555", "0006666", "0006666", "0007777"]
SHAMCASH_CODES = ["000xz55XH55", "00YI06MB666"]
PAYEER_CODES = ["0PPWY0777JG7"]

# ==== Helpers للرسائل ====
def _name_from_user(u) -> str:
    n = getattr(u, "first_name", None) or getattr(u, "full_name", None) or ""
    n = (n or "").strip()
    return n if n else "صديقنا"

def _fmt_syp(n: int) -> str:
    try:
        return f"{int(n):,} ل.س"
    except Exception:
        return f"{n} ل.س"

ETA_TEXT = "من 1 إلى 4 دقائق"

def get_method_instructions(method):
    if method == "سيرياتيل كاش":
        text = (
            "📲 *سيرياتيل كاش*\n"
            "حوّل المبلغ إلى أحد الأرقام التالية عبر (الدفع اليدوي):\n"
            f"🔢 {'   -   '.join(f'`{num}`' for num in SYRIATEL_NUMBERS)}\n"
            "⚠️ لسنا مسؤولين عن تحويل الوحدات (اتّبع التعليمات بدقة)\n\n"
            "يمكنك نسخ الرقم المطلوب بسهولة."
        )
    elif method == "أم تي إن كاش":
        text = (
            "📲 *أم تي إن كاش*\n"
            "حوّل المبلغ إلى أحد الأرقام التالية عبر (الدفع اليدوي):\n"
            f"🔢 {'   -   '.join(f'`{num}`' for num in MTN_NUMBERS)}\n"
            "⚠️ لسنا مسؤولين عن تحويل الوحدات (اتّبع التعليمات بدقة)\n\n"
            "يمكنك نسخ الرقم المطلوب بسهولة."
        )
    elif method == "شام كاش":
        text = (
            "📲 *شام كاش*\n"
            "حوّل المبلغ إلى أحد الأكواد التالية:\n"
            f"🔢 {'   -   '.join(f'`{code}`' for code in SHAMCASH_CODES)}\n"
            "يمكنك نسخ الكود المطلوب بسهولة."
        )
    elif method == "Payeer":
        text = (
            "💳 *Payeer*\n"
            "حوّل المبلغ إلى الكود التالي:\n"
            f"🔢 {'   -   '.join(f'`{code}`' for code in PAYEER_CODES)}\n"
            "يمكنك نسخ الكود بسهولة."
        )
    else:
        text = "حدث خطأ في تحديد طريقة الشحن."
    return text

def clear_pending_request(user_id):
    recharge_pending.discard(user_id)
    recharge_requests.pop(user_id, None)

def start_recharge_menu(bot, message, history=None):
    uid = message.from_user.id

    # ✅ تطبيع history[uid] ليكون دائمًا قائمة قبل أي append
    if history is not None:
        current = history.get(uid)
        if isinstance(current, list):
            pass
        elif current is None:
            history[uid] = []
        elif isinstance(current, str):
            history[uid] = [current]
        else:
            history[uid] = []
        history[uid].append("recharge_menu")

    name = _name_from_user(message.from_user)
    logging.info(f"[RECHARGE][{uid}] فتح قائمة الشحن")
    bot.send_message(
        message.chat.id,
        f"💳 يا {name}، اختار طريقة شحن محفظتك:",
        reply_markup=keyboards.recharge_menu()
    )

def register(bot, history):

    @bot.message_handler(func=lambda msg: msg.text == "💳 شحن محفظتي")
    def open_recharge(msg):
        start_recharge_menu(bot, msg, history)

    @bot.message_handler(func=lambda msg: msg.text in [
        "📲 سيرياتيل كاش", "📲 أم تي إن كاش", "📲 شام كاش", "💳 Payeer"
    ])
    def request_invoice(msg):
        user_id = msg.from_user.id
        name = _name_from_user(msg.from_user)
        if user_id in recharge_pending:
            logging.warning(f"[RECHARGE][{user_id}] محاولة شحن جديدة أثناء وجود طلب معلق")
            bot.send_message(msg.chat.id, f"⚠️ يا {name}، عندك طلب شحن قيد المعالجة. استنى شوية لو سمحت.")
            return

        method = msg.text.replace("📲 ", "").replace("💳 ", "")
        recharge_requests[user_id] = {"method": method}
        instructions = get_method_instructions(method)
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("✅ تأكيد التحويل", callback_data="confirm_recharge_method"),
            types.InlineKeyboardButton("❌ إلغاء", callback_data="cancel_recharge_method")
        )
        logging.info(f"[RECHARGE][{user_id}] بدأ شحن بطريقة: {method}")
        bot.send_message(
            msg.chat.id,
            instructions,
            parse_mode="Markdown",
            reply_markup=markup
        )

    @bot.callback_query_handler(func=lambda call: call.data in ["confirm_recharge_method", "cancel_recharge_method"])
    def handle_method_confirm_cancel(call):
        user_id = call.from_user.id
        remove_inline_keyboard(bot, call.message)
        if too_soon(user_id, 'handle_method_confirm_cancel', seconds=2):
            return bot.answer_callback_query(call.id, '⏱️ تم الاستلام..')
        user_id = call.from_user.id
        name = _name_from_user(call.from_user)
        if call.data == "confirm_recharge_method":
            logging.info(f"[RECHARGE][{user_id}] أكد طريقة الشحن، بانتظار الصورة")
            bot.send_message(
                call.message.chat.id,
                f"📸 يا {name}، ابعت صورة إشعار الدفع (سكرين/لقطة شاشة):",
                reply_markup=keyboards.recharge_menu()
            )
        else:
            clear_pending_request(user_id)
            logging.info(f"[RECHARGE][{user_id}] ألغى الشحن من شاشة اختيار الطريقة")
            bot.send_message(
                call.message.chat.id,
                f"❌ تم الإلغاء يا {name}. تقدر تبدأ من جديد في أي وقت.",
                reply_markup=keyboards.recharge_menu()
            )

    @bot.message_handler(content_types=["photo"])
    def handle_photo(msg):
        user_id = msg.from_user.id
        if user_id not in recharge_requests or "photo" in recharge_requests[user_id]:
            return
        photo_id = msg.photo[-1].file_id
        recharge_requests[user_id]["photo"] = photo_id
        name = _name_from_user(msg.from_user)
        logging.info(f"[RECHARGE][{user_id}] أرسل صورة إشعار الدفع")
        bot.send_message(msg.chat.id, f"🔢 تمام يا {name}! ابعت رقم الإشعار / رمز العملية:", reply_markup=keyboards.recharge_menu())

    @bot.message_handler(
        func=lambda msg: msg.from_user.id in recharge_requests 
        and "photo" in recharge_requests[msg.from_user.id] 
        and "ref" not in recharge_requests[msg.from_user.id]
    )
    def get_reference(msg):
        recharge_requests[msg.from_user.id]["ref"] = msg.text
        logging.info(f"[RECHARGE][{msg.from_user.id}] أرسل رقم الإشعار: {msg.text}")
        bot.send_message(msg.chat.id, "💰 ابعت مبلغ الشحن (بالليرة السورية):", reply_markup=keyboards.recharge_menu())

    @bot.message_handler(
        func=lambda msg: msg.from_user.id in recharge_requests 
        and "ref" in recharge_requests[msg.from_user.id] 
        and "amount" not in recharge_requests[msg.from_user.id]
    )
    def get_amount(msg):
        user_id = msg.from_user.id
        name = _name_from_user(msg.from_user)
        amount_text = (msg.text or "").strip()

        if not amount_text.isdigit():
            logging.warning(f"[RECHARGE][{user_id}] محاولة إدخال مبلغ شحن غير صالح: {amount_text}")
            bot.send_message(
                msg.chat.id,
                f"❌ يا {name}، دخّل المبلغ أرقام فقط (من غير فواصل/نقاط/رموز).",
                reply_markup=keyboards.recharge_menu()
            )
            return

        amount = int(amount_text)
        # ✅ رفض مبكر قبل إرسال الطلب للأدمن/الطابور
        if amount < MIN_RECHARGE:
            bot.send_message(
                msg.chat.id,
                f"⚠️ يا {name}، الحد الأدنى للشحن هو <b>{_fmt_syp(MIN_RECHARGE)}</b>.\n"
                f"اكتب مبلغ أكبر أو يساويه، وبنبقى ننفّذ طلبك {ETA_TEXT}.",
                parse_mode="HTML",
                reply_markup=keyboards.recharge_menu()
            )
            return

        data = recharge_requests[user_id]
        data["amount"] = amount

        confirm_text = (
            f"🔎 **راجع تفاصيل طلب الشحن:**\n"
            f"💳 الطريقة: {data['method']}\n"
            f"🔢 رقم الإشعار: `{data['ref']}`\n"
            f"💵 المبلغ: {amount:,} ل.س\n\n"
            f"لو كل حاجة تمام، ابعت الطلب للإدارة."
        )

        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("✅ تأكيد", callback_data="user_confirm_recharge"),
            types.InlineKeyboardButton("🔁 تعديل", callback_data="user_edit_recharge"),
            types.InlineKeyboardButton("❌ إلغاء", callback_data="user_cancel_recharge")
        )

        logging.info(f"[RECHARGE][{user_id}] تأكيد معلومات الشحن: مبلغ {amount}")
        photo_id = data.get("photo")
        if photo_id:
            bot.send_photo(
                msg.chat.id,
                photo_id,
                caption=confirm_text,
                parse_mode="Markdown",
                reply_markup=markup
            )
        else:
            bot.send_message(
                msg.chat.id,
                confirm_text,
                parse_mode="Markdown",
                reply_markup=markup
            )

    @bot.callback_query_handler(
        func=lambda call: call.data in ["user_confirm_recharge", "user_edit_recharge", "user_cancel_recharge"]
    )
    def handle_user_recharge_action(call):
        user_id = call.from_user.id
        name = _name_from_user(call.from_user)

        if call.data == \"user_confirm_recharge\":
            remove_inline_keyboard(bot, call.message)
            data = recharge_requests.get(user_id)
            if not data:
                logging.warning(f"[RECHARGE][{user_id}] تأكيد طلب شحن بدون بيانات")
                bot.answer_callback_query(call.id, "لا يوجد طلب قيد المعالجة.")
                return

            amount = int(data.get("amount") or 0)
            # ✅ حراسة إضافية: منع تمرير مبلغ أقل من الحد الأدنى
            if amount < MIN_RECHARGE:
                # لا نرسل للأدمن ولا نضيف للطابور
                recharge_requests[user_id].pop("amount", None)
                bot.answer_callback_query(call.id, "المبلغ أقل من الحد الأدنى.")
                bot.send_message(
                    user_id,
                    f"⚠️ يا {name}، الحد الأدنى للشحن هو <b>{_fmt_syp(MIN_RECHARGE)}</b>.\n"
                    f"من فضلك ادخل مبلغ جديد أكبر أو يساويه.",
                    parse_mode="HTML",
                    reply_markup=keyboards.recharge_menu()
                )
                return

            # ✅ تأكيد التسجيل وضبط رصيد المستخدم (للإظهار فقط في رسالة الأدمن)
            register_user_if_not_exist(user_id, name)
            balance = 0
            try:
                balance = int(get_available_balance(user_id))
            except Exception:
                pass

            # ===== رسالة الأدمن بالقالب الموحّد =====
            admin_msg = (
                f"💰 رصيد المستخدم: {balance:,} ل.س\n"
                f"🆕 طلب جديد\n"
                f"👤 الاسم: <code>{call.from_user.full_name}</code>\n"
                f"يوزر: <code>@{call.from_user.username or ''}</code>\n"
                f"آيدي: <code>{user_id}</code>\n"
                f"آيدي اللاعب: <code>—</code>\n"
                f"🔖 المنتج: شحن محفظة\n"
                f"التصنيف: محفظة\n"
                f"💵 السعر: {amount:,} ل.س\n"
                f"(recharge)"
            )
            admin_msg += (
                f"\n\n"
                f"🔢 رقم الإشعار: <code>{data['ref']}</code>\n"
                f"💳 الطريقة: <code>{data['method']}</code>"
            )

            logging.info(f"[RECHARGE][{user_id}] إرسال طلب الشحن للإدارة")
            add_pending_request(
                user_id=user_id,
                username=call.from_user.username,
                request_text=admin_msg,
                payload={
                    "type": "recharge",
                    "amount": amount,
                    "method": data['method'],
                    "ref": data['ref'],
                    "photo": data.get("photo"),
                }
            )
            process_queue(bot)

            # ===== رسالة موحّدة للعميل =====
            bot.send_message(
                user_id,
                f"✅ تمام يا {name}! استلمنا طلب شحن محفظتك بقيمة <b>{_fmt_syp(amount)}</b>.\n"
                f"⏱️ سيتم تنفيذ الطلب {ETA_TEXT}.\n"
                f"لو في أي ملاحظة هنبعتلك فورًا 💬",
                parse_mode="HTML",
                reply_markup=keyboards.recharge_menu()
            )
            recharge_pending.add(user_id)
            bot.edit_message_reply_markup(
                call.message.chat.id,
                call.message.message_id,
                reply_markup=None
            )

        elif call.data == "user_edit_recharge":
            if user_id in recharge_requests:
                recharge_requests[user_id].pop("amount", None)
                recharge_requests[user_id].pop("ref", None)
                logging.info(f"[RECHARGE][{user_id}] تعديل طلب الشحن")
                bot.send_message(
                    user_id,
                    "🔄 ابعت رقم الإشعار / رمز العملية من جديد:",
                    reply_markup=keyboards.recharge_menu()
                )
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)

        elif call.data == "user_cancel_recharge":
            clear_pending_request(user_id)
            logging.info(f"[RECHARGE][{user_id}] ألغى طلب الشحن نهائياً")
            bot.send_message(
                user_id,
                f"❌ تم إلغاء الطلب يا {name}. تقدر تبدأ من جديد وقت ما تحب.",
                reply_markup=keyboards.recharge_menu()
            )
            # تصحيح history قبل استدعاء start_recharge_menu
            if not isinstance(history.get(user_id), list):
                history[user_id] = []

            fake_msg = SimpleNamespace()
            fake_msg.from_user = SimpleNamespace()
            fake_msg.from_user.id = user_id
            fake_msg.from_user.first_name = name
            fake_msg.chat = SimpleNamespace()
            fake_msg.chat.id = user_id

            start_recharge_menu(bot, fake_msg, history)
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
