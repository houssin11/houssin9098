import logging
import time
from telebot import types
from handlers import keyboards
from config import BOT_NAME, FORCE_SUB_CHANNEL_USERNAME
from services.wallet_service import register_user_if_not_exist

START_BTN_TEXT = "✨ ستارت"
START_BTN_TEXT_SUB = "✅ تم الاشتراك"
SUB_BTN_TEXT = "🔔 اشترك الآن في القناة"

CB_START = "cb_start_main"
CB_CHECK_SUB = "cb_check_sub"

_sub_status_cache = {}
_sub_status_ttl = 3
_user_start_limit = {}
_rate_limit_seconds = 5

def _reset_user_flows(user_id: int):
    try:
        from handlers import internet_providers
    except Exception as e:
        logging.error(f"[start.py] import error: {e}")
        return
    try:
        internet_providers.user_net_state.pop(user_id, None)
    except Exception as e:
        logging.warning(f"[start.py] user_net_state cleanup error: {e}")
    try:
        po = getattr(internet_providers, "pending_orders", None)
        if isinstance(po, dict):
            for oid in list(po.keys()):
                try:
                    if po[oid].get("user_id") == user_id:
                        po.pop(oid, None)
                except Exception as e:
                    logging.warning(f"[start.py] pending_orders cleanup: {e}")
    except Exception as e:
        logging.warning(f"[start.py] pending_orders main cleanup: {e}")

# --- لوحة تحقق الاشتراك فقط (بدون زر ستارت هنا) ---
def _sub_inline_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    if FORCE_SUB_CHANNEL_USERNAME:
        kb.add(
            types.InlineKeyboardButton(
                SUB_BTN_TEXT,
                url=f"https://t.me/{FORCE_SUB_CHANNEL_USERNAME[1:]}"
            )
        )
    kb.add(types.InlineKeyboardButton(START_BTN_TEXT_SUB, callback_data=CB_CHECK_SUB))
    return kb

# --- لوحة ستارت فقط بعد التأكد من الاشتراك ---
def _welcome_inline_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton(START_BTN_TEXT, callback_data=CB_START))
    return kb

def is_user_subscribed(bot, user_id):
    now = time.time()
    cached = _sub_status_cache.get(user_id)
    if cached:
        status, last_check = cached
        if now - last_check < _sub_status_ttl:
            return status
    try:
        result = bot.get_chat_member(FORCE_SUB_CHANNEL_USERNAME, user_id)
        status = result.status in ["member", "creator", "administrator"]
        _sub_status_cache[user_id] = (status, now)
        return status
    except Exception as e:
        logging.error(f"[start.py] Error get_chat_member: {e}", exc_info=True)
        _sub_status_cache[user_id] = (False, now)
        return False

def register(bot, user_history):

    @bot.message_handler(commands=['start'])
    def send_welcome(message):
        user_id = message.from_user.id
        now = time.time()
        last = _user_start_limit.get(user_id, 0)
        if now - last < _rate_limit_seconds:
            try:
                bot.send_message(message.chat.id, "يرجى الانتظار قبل إعادة المحاولة.")
            except Exception as e:
                logging.error(f"[start.py] rate limit send_message: {e}")
            return
        _user_start_limit[user_id] = now

        _reset_user_flows(user_id)
        
        # --- التقاط رابط الإحالة /start ref-<referrer_id>-<token> ---
        try:
            parts = (message.text or "").split(maxsplit=1)
            if len(parts) == 2 and parts[1].startswith("ref-"):
                _, ref_uid, token = parts[1].split("-", 2)
                ref_uid = int(ref_uid)
                from services.referral_service import attach_referred_start
                attach_referred_start(ref_uid, token, message.from_user.id)

                # أرسل لوحة الاشتراك + زر "تحققت" (يُعالجه هاندلر referrals)
                kb = types.InlineKeyboardMarkup(row_width=1)
                if FORCE_SUB_CHANNEL_USERNAME:
                    kb.add(types.InlineKeyboardButton(
                        "🔔 اشترك الآن في القناة",
                        url=f"https://t.me/{FORCE_SUB_CHANNEL_USERNAME[1:]}"
                    ))
                kb.add(types.InlineKeyboardButton("✅ تحققت", callback_data="ref:checked"))
                bot.send_message(message.chat.id, "اشترك في القناة ثم اضغط «تحققت».", reply_markup=kb)
                # لو حاب توقف مسار /start الاعتيادي هنا:
                # return
        except Exception as e:
            logging.error(f"[start.py] referral payload parse: {e}")

        # تحقق الاشتراك فقط هنا
        if FORCE_SUB_CHANNEL_USERNAME:
            if not is_user_subscribed(bot, user_id):
                try:
                    bot.send_message(
                        message.chat.id,
                        f"⚠️ للاستخدام الكامل لبوت {BOT_NAME}\nيرجى الاشتراك بالقناة أولاً.",
                        reply_markup=_sub_inline_kb()
                    )
                except Exception as e:
                    logging.error(f"[start.py] send sub msg: {e}")
                return

        # بعد الاشتراك أو إذا لم يكن هناك شرط اشتراك
        try:
            bot.send_message(
                message.chat.id,
                WELCOME_MESSAGE,
                parse_mode="Markdown",
                reply_markup=_welcome_inline_kb()
            )
        except Exception as e:
            logging.error(f"[start.py] send welcome msg: {e}")

        user_history[user_id] = []

    # ---- Callback: إعادة فحص الاشتراك ----
    @bot.callback_query_handler(func=lambda c: c.data == CB_CHECK_SUB)
    def cb_check_subscription(call):
        user_id = call.from_user.id
        _sub_status_cache.pop(user_id, None)  # force recheck now
        _reset_user_flows(user_id)

        if FORCE_SUB_CHANNEL_USERNAME:
            if not is_user_subscribed(bot, user_id):
                try:
                    bot.answer_callback_query(call.id, "لم يتم العثور على اشتراك. اشترك ثم أعد المحاولة.", show_alert=True)
                except Exception as e:
                    logging.error(f"[start.py] answer cb_check_sub: {e}")
                return

        # لو وصلنا هنا، مشترك!
        try:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=WELCOME_MESSAGE,
                parse_mode="Markdown",
                reply_markup=_welcome_inline_kb()
            )
        except Exception as e:
            logging.error(f"[start.py] edit_message_text cb_check_sub: {e}")
        user_history[user_id] = []

    # ---- Callback: ستارت (القائمة الرئيسية) ----
    @bot.callback_query_handler(func=lambda c: c.data == CB_START)
    def cb_start_main(call):
        user_id = call.from_user.id
        name = getattr(call.from_user, "full_name", None) or call.from_user.first_name
        _reset_user_flows(user_id)
        try:
            register_user_if_not_exist(user_id, name)
        except Exception as e:
            logging.error(f"[start.py] register_user_if_not_exist: {e}")

        try:
            bot.answer_callback_query(call.id)
            bot.send_message(
                call.message.chat.id,
                "✨ تم تسجيلك بنجاح! هذه القائمة الرئيسية.",
                reply_markup=keyboards.main_menu(call.from_user.id)  # ← تمرير user_id لإظهار لوحة الأدمن
            )
        except Exception as e:
            logging.error(f"[start.py] cb_start_main: {e}")

    # ---- روابط / تعليمات / رجوع ----
    @bot.message_handler(commands=['help'])
    def send_help(message):
        bot.send_message(
            message.chat.id,
            "📝 للمساعدة والدعم، راسل الإدارة على الخاص أو تحقق من القناة الرسمية.",
            reply_markup=keyboards.main_menu(message.from_user.id)  # ← تمرير user_id
        )
        
    @bot.message_handler(commands=['about'])
    def send_about(message):
        bot.send_message(
            message.chat.id,
            f"🤖 هذا البوت من تطوير {BOT_NAME}.\n"
            "نحن نقدم أفضل الخدمات بأقل الأسعار!",
            reply_markup=keyboards.main_menu(message.from_user.id)  # ← تمرير user_id
        )

    @bot.message_handler(func=lambda msg: msg.text == "⬅️ رجوع")
    def back_to_main_menu(message):
        bot.send_message(
            message.chat.id,
            "تم الرجوع إلى القائمة الرئيسية.",
            reply_markup=keyboards.main_menu(message.from_user.id)  # ← تمرير user_id
        )

# ---- رسالة الترحيب ----
WELCOME_MESSAGE = (
    f"مرحبًا بك في {BOT_NAME}, وجهتك الأولى للتسوق الإلكتروني!\n\n"
    "🚀 نحن هنا نقدم لك تجربة تسوق لا مثيل لها:\n"
    "💼 منتجات عالية الجودة.\n"
    "⚡ سرعة في التنفيذ.\n"
    "📞 دعم فني خبير تحت تصرفك.\n\n"
    "🌟 لماذا نحن الأفضل؟\n"
    "1️⃣ توفير منتجات رائعة بأسعار تنافسية.\n"
    "2️⃣ تجربة تسوق آمنة وسهلة.\n"
    "3️⃣ فريق محترف جاهز لخدمتك على مدار الساعة.\n\n"
    "🚨 *تحذيرات هامة لا يمكن تجاهلها!*\n"
    "1️⃣ أي معلومات خاطئة ترسلها... عليك تحمل مسؤوليتها.\n"
    "2️⃣ *سيتم حذف محفظتك* إذا لم تقم بأي عملية شراء خلال 40 يومًا.\n"
    "3️⃣ *لا تراسل الإدارة* إلا في حالة الطوارئ!\n\n"
    "🔔 *هل أنت جاهز؟* لأننا على استعداد تام لتلبية احتياجاتك!\n"
    "👇 اضغط على زر ✨ للمتابعة."
)
