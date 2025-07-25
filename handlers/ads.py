from telebot import types
from services.wallet_service import get_balance, deduct_balance
from services.queue_service import add_pending_request
import logging

# خيارات الإعلان
AD_OPTIONS = [
    ("✨ إعلان مرة (5000 ل.س)", 1, 5000),
    ("🔥 إعلان مرتين (15000 ل.س)", 2, 15000),
    ("🌟 إعلان 3 مرات (25000 ل.س)", 3, 25000),
    ("🚀 إعلان 4 مرات (40000 ل.س)", 4, 40000),
    ("💎 إعلان 5 مرات (60000 ل.س)", 5, 60000),
    ("🏆 إعلان 10 مرات (100000 ل.س)", 10, 100000),
]

user_ads_state = {}

def register(bot, history):
    # زر القائمة الرئيسية
    @bot.message_handler(func=lambda msg: msg.text == "🗞️ إعلاناتك")
    def open_ads_menu(msg):
        markup = types.InlineKeyboardMarkup()
        for text, times, price in AD_OPTIONS:
            markup.add(types.InlineKeyboardButton(text, callback_data=f"ads_{times}"))
        markup.add(types.InlineKeyboardButton("⬅️ رجوع", callback_data="ads_back"))
        bot.send_message(msg.chat.id, "🟢 اختر نوع إعلانك:", reply_markup=markup)

    # عند اختيار نوع الإعلان
    @bot.callback_query_handler(func=lambda call: call.data.startswith("ads_"))
    def select_ad_type(call):
        user_id = call.from_user.id
        times = int(call.data.split("_")[1])
        for text, t, price in AD_OPTIONS:
            if t == times:
                user_ads_state[user_id] = {
                    "times": times,
                    "price": price,
                    "step": "contact"
                }
                break
        bot.send_message(call.message.chat.id, "✏️ أرسل رقم التواصل، صفحتك أو موقعك (سيظهر للإعلان):")

    # استقبال رقم/رابط التواصل
    @bot.message_handler(func=lambda msg: user_ads_state.get(msg.from_user.id, {}).get("step") == "contact")
    def receive_contact(msg):
        user_id = msg.from_user.id
        user_ads_state[user_id]["contact"] = msg.text.strip()
        user_ads_state[user_id]["step"] = "ad_text"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("تأكيد", callback_data="ads_contact_confirm"))
        markup.add(types.InlineKeyboardButton("إلغاء", callback_data="ads_cancel"))
        bot.send_message(msg.chat.id, f"📞 سيتم عرض للتواصل:\n{msg.text}\n\nهل تريد المتابعة؟", reply_markup=markup)

    # تأكيد/إلغاء وسيلة التواصل
    @bot.callback_query_handler(func=lambda call: call.data in ["ads_contact_confirm", "ads_cancel"])
    def confirm_contact(call):
        user_id = call.from_user.id
        if call.data == "ads_contact_confirm":
            user_ads_state[user_id]["step"] = "ad_text"
            bot.send_message(call.message.chat.id, "📝 أرسل نص إعلانك (سيظهر في القناة):")
        else:
            user_ads_state.pop(user_id, None)
            bot.send_message(call.message.chat.id, "❌ تم إلغاء عملية الإعلان.", reply_markup=types.ReplyKeyboardRemove())

    # استقبال نص الإعلان
    @bot.message_handler(func=lambda msg: user_ads_state.get(msg.from_user.id, {}).get("step") == "ad_text")
    def receive_ad_text(msg):
        user_id = msg.from_user.id
        user_ads_state[user_id]["ad_text"] = msg.text.strip()
        user_ads_state[user_id]["step"] = "images"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("تخطي الصور", callback_data="ads_skip_images"))
        bot.send_message(msg.chat.id, "🖼️ يمكنك إرسال صورة أو صورتين (أو اضغط تخطي):", reply_markup=markup)

    # استقبال الصور
    @bot.message_handler(content_types=["photo"])
    def receive_images(msg):
        user_id = msg.from_user.id
        if user_ads_state.get(user_id, {}).get("step") == "images":
            user_ads_state[user_id].setdefault("images", []).append(msg.photo[-1].file_id)
            if len(user_ads_state[user_id]["images"]) >= 2:
                preview_ad(msg, user_id)
            else:
                bot.send_message(msg.chat.id, "📸 أرسل صورة أخرى أو اضغط تخطي إذا اكتفيت.")

    # تخطي الصور
    @bot.callback_query_handler(func=lambda call: call.data == "ads_skip_images")
    def skip_images(call):
        user_id = call.from_user.id
        preview_ad(call.message, user_id)

    # معاينة الإعلان للعميل
    def preview_ad(msg, user_id):
        data = user_ads_state[user_id]
        ad_preview = (
            "🚀✨✨ إعلان مميز من المتجر العالمي ✨✨🚀\n\n"
            f"{data['ad_text']}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📱 للتواصل:\n"
            f"{data['contact']}\n"
            "━━━━━━━━━━━━━━━━━━"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("✅ تأكيد الإعلان", callback_data="ads_confirm_send"))
        markup.add(types.InlineKeyboardButton("📝 تعديل الإعلان", callback_data="ads_edit"))
        markup.add(types.InlineKeyboardButton("❌ إلغاء", callback_data="ads_cancel"))
        # إذا فيه صور أرسلها كـ media group
        if data.get("images"):
            media = [types.InputMediaPhoto(photo) for photo in data["images"]]
            bot.send_media_group(msg.chat.id, media)
        bot.send_message(msg.chat.id, ad_preview, reply_markup=markup)
        data["step"] = "confirm"

    # تعديل الإعلان يرجع المستخدم لنص الإعلان من جديد
    @bot.callback_query_handler(func=lambda call: call.data == "ads_edit")
    def edit_ad(call):
        user_id = call.from_user.id
        user_ads_state[user_id]["step"] = "ad_text"
        bot.send_message(call.message.chat.id, "🔄 عدل نص إعلانك أو أرسل إعلان جديد:")

    # إلغاء العملية في أي مرحلة
    @bot.callback_query_handler(func=lambda call: call.data == "ads_cancel")
    def cancel_ad(call):
        user_id = call.from_user.id
        user_ads_state.pop(user_id, None)
        bot.send_message(call.message.chat.id, "❌ تم إلغاء عملية الإعلان.", reply_markup=types.ReplyKeyboardRemove())

    # تأكيد ونقل الإعلان للطابور بعد التأكد من الرصيد
    @bot.callback_query_handler(func=lambda call: call.data == "ads_confirm_send")
    def confirm_ad(call):
        user_id = call.from_user.id
        data = user_ads_state[user_id]
        price = data["price"]
        balance = get_balance(user_id)
        if balance is None or balance < price:
            shortage = price - (balance or 0)
            bot.send_message(call.message.chat.id, f"❌ رصيدك غير كافٍ لهذا الإعلان.\nالناقص: {shortage:,} ل.س")
            return
        # حجز الرصيد (الخصم يتم فعليًا عند قبول الإدارة)
        deduct_balance(user_id, price)
        # بناء الطلب للطابور
        payload = {
            "type": "ads",
            "count": data["times"],
            "price": data["price"],
            "contact": data["contact"],
            "ad_text": data["ad_text"],
            "images": data.get("images", []),
        }
        add_pending_request(
            user_id=user_id,
            username=call.from_user.username,
            request_text="إعلان جديد بانتظار الموافقة",
            payload=payload,
        )
        bot.send_message(user_id, "✅ تم إرسال إعلانك إلى الإدارة لمراجعته قبل النشر.")
        user_ads_state.pop(user_id, None)

