import os
import sys
import logging
import telebot
from config import API_TOKEN, ADMINS
from telebot import types
import threading
import http.server
import socketserver
from handlers import referrals  # <-- جديد
from services.scheduled_tasks import post_ads_task
from services.error_log_setup import install_global_error_logging
from services.state_adapter import UserStateDictLike
from services.commands_setup import setup_bot_commands

# NEW: عُمّال الإشعارات والصيانة
from services.outbox_worker import start_outbox_worker
from services.maintenance_worker import start_housekeeping

# ✅ تعديل بسيط ليتوافق مع ويندوز: تشغيل الخادم الوهمي يصبح اختياريًا
ENABLE_DUMMY_SERVER = os.environ.get("ENABLE_DUMMY_SERVER", "0") == "1"

PORT = 8081

def run_dummy_server():
    handler = http.server.SimpleHTTPRequestHandler
    # ✅ السماح بإعادة استخدام المنفذ لتفادي OSError: [Errno 98]
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), handler) as httpd:
        print(f"🔌 Dummy server listening on port {PORT}")
        httpd.serve_forever()

# تشغيل الخادم في ثريد منفصل حتى لا يوقف البوت الأساسي
# (لن يعمل على ويندوز إلا إذا ENABLE_DUMMY_SERVER=1)
if ENABLE_DUMMY_SERVER:
    threading.Thread(target=run_dummy_server, daemon=True).start()
else:
    print("🖥️ Local run: dummy server is disabled (ENABLE_DUMMY_SERVER=0).")

# ---------------------------------------------------------
# تسجيل الأخطاء لظهورها في سجلّ Render
# ---------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

def _unhandled_exception_hook(exc_type, exc_value, exc_tb):
    """طباعة أي استثناء غير مُعالج بالكامل في اللوجز."""
    logging.critical("❌ Unhandled exception:", exc_info=(exc_type, exc_value, exc_tb))

sys.excepthook = _unhandled_exception_hook

# ---------------------------------------------------------
# التحقق من صحة التوكن وجلب هوية البوت قبل التشغيل
# ---------------------------------------------------------
def check_api_token(token):
    try:
        test_bot = telebot.TeleBot(token)
        me = test_bot.get_me()
        print(f"✅ التوكن سليم. هوية البوت: @{me.username} (ID: {me.id})")
        return True
    except Exception as e:
        logging.critical(f"❌ التوكن غير صالح أو لا يمكن الاتصال بـ Telegram API: {e}")
        sys.exit(1)

if not check_api_token(API_TOKEN):
    sys.exit(1)

# ---------------------------------------------------------
# إنشاء كائن البوت وحذف أي Webhook سابق لتجنب خطأ 409
# ---------------------------------------------------------
bot = telebot.TeleBot(API_TOKEN, parse_mode="HTML")
try:
    bot.delete_webhook(drop_pending_updates=True)
except Exception as e:
    logging.warning(f"⚠️ لم يتم حذف Webhook بنجاح: {e}")

# ---------------------------------------------------------
# تسجيل حالة المستخدم (تخزين في Supabase عبر الـ adapter)
# ---------------------------------------------------------
user_state = UserStateDictLike()
history: dict[int, list] = {}

# ---------------------------------------------------------
# استيراد جميع الهاندلرز بعد تهيئة البوت
# ---------------------------------------------------------
from handlers import (start,
    wallet,
    support,
    admin,
    ads,
    recharge,
    cash_transfer,
    companies_transfer,
    products,
    media_services,
    wholesale,
    university_fees,
    internet_providers,
    bill_and_units,
    links as links_handler, 
    tournaments,
    tournament_invite_start,
    tournaments_admin,
)
from handlers.keyboards import (
    main_menu,
    products_menu,
    game_categories,
    recharge_menu,
    companies_transfer_menu,
    cash_transfer_menu,
    syrian_balance_menu,
    wallet_menu,
    support_menu,
    links_menu,
    media_services_menu,
    transfers_menu,
)
# هاندلر الإلغاء المركزي
from handlers import cancel as cancel_handler

# ---------------------------------------------------------
# تسجيل جميع الهاندلرز (تمرير user_state أو history حسب الحاجة)
# ---------------------------------------------------------
start.register(bot, history)
referrals.register(bot, history)
wallet.register(bot, history)
support.register(bot, history)
admin.register(bot, history)
ads.register(bot, history)
recharge.register(bot, history)
cash_transfer.register(bot, history)
companies_transfer.register_companies_transfer(bot, history)
bill_and_units.register_bill_and_units(bot, history)
links_handler.register(bot, history)
# ✅ تسجيل المنتجات مرة واحدة وتمرير admin_ids هنا
products.register(bot, history, admin_ids=[6935846121])
tournament_invite_start.register(bot, history)
tournaments.register(bot, history)
tournaments_admin.register(bot, history)
media_services.register(bot, history)
wholesale.register(bot, history)
university_fees.register_university_fees(bot, history)
internet_providers.register(bot)

# ✅ تسجيل هاندلر /cancel بعد تعريف bot و history
cancel_handler.register(bot, history)

CHANNEL_USERNAME = "@shop100sho"
def notify_channel_on_start(bot):
    # تم تعطيل رسالة القناة مؤقتًا
    pass

notify_channel_on_start(bot)
# تفعيل سجل الأخطاء + قائمة الأوامر الثابتة
install_global_error_logging()
setup_bot_commands(bot, list(ADMINS))

# بعد اكتمال التسجيل وتشغيل البوت، شغّل مهمة الإعلانات المجدولة
post_ads_task(bot)

# NEW: تشغيل عامل الإشعارات من outbox وعامل الصيانة (بديل pg_cron داخل التطبيق)
start_outbox_worker(bot)   # يمرّ على notifications_outbox ويُرسل الرسائل
start_housekeeping(bot)    # تنظيف 14 ساعة + تنبيهات/حذف المحافظ بعد 33 يوم خمول

# ---------------------------------------------------------
# زر الرجوع الذكي (بدون تعديل)
# ---------------------------------------------------------
@bot.message_handler(func=lambda msg: msg.text == "⬅️ رجوع")
def handle_back(msg):
    user_id = msg.from_user.id
    state = user_state.get(user_id, {}).get("step", "main_menu")

    if state == "products_menu":
        bot.send_message(msg.chat.id, "⬅️ عدت إلى المنتجات.", reply_markup=products_menu())
        user_state[user_id]['step'] = "products_menu"
    elif state == "main_menu":
        bot.send_message(msg.chat.id, "⬅️ عدت إلى القائمة الرئيسية.", reply_markup=main_menu())
    elif state == "game_menu":
        bot.send_message(msg.chat.id, "⬅️ عدت إلى قائمة المنتجات.", reply_markup=products_menu())
        user_state[user_id]['step'] = "products_menu"
    elif state == "cash_menu":
        bot.send_message(msg.chat.id, "⬅️ عدت إلى قائمة التحويلات.", reply_markup=transfers_menu())
        user_state[user_id]['step'] = "transfers_menu"
    elif state == "syrian_transfer":
        bot.send_message(msg.chat.id, "⬅️ عدت إلى قائمة المنتجات.", reply_markup=products_menu())
        user_state[user_id]['step'] = "products_menu"
    else:
        bot.send_message(msg.chat.id, "⬅️ عدت إلى البداية.", reply_markup=main_menu())
        user_state[user_id]['step'] = "main_menu"

# ---------------------------------------------------------
# ربط أزرار المنتجات بالخدمات الخاصة بها
# ---------------------------------------------------------
@bot.message_handler(func=lambda msg: msg.text == "تحويلات كاش و حوالات")
def handle_transfers(msg):
    bot.send_message(
        msg.chat.id,
        "من خلال هذه الخدمة تستطيع تحويل رصيد محفظتك إليك أو لأي شخص آخر عن طريق شركات الحوالات (كالهرم)، أو كرصيد كاش (سيرياتيل/MTN)."
    )
    bot.send_message(msg.chat.id, "اختر نوع التحويل:", reply_markup=transfers_menu())
    user_state[msg.from_user.id]['step'] = "transfers_menu"

@bot.message_handler(func=lambda msg: msg.text == "💵 تحويل الى رصيد كاش")
def handle_cash_transfer(msg):
    from handlers.cash_transfer import start_cash_transfer
    start_cash_transfer(bot, msg, history)
    user_state[msg.from_user.id]['step'] = "cash_menu"

@bot.message_handler(func=lambda msg: msg.text == "حوالة مالية عبر شركات")
def handle_companies_transfer(msg):
    from handlers.companies_transfer import register_companies_transfer
    register_companies_transfer(bot, history)

@bot.message_handler(func=lambda msg: msg.text == "🌐 دفع مزودات الإنترنت ADSL")
def handle_internet(msg):
    from handlers.internet_providers import start_internet_provider_menu
    start_internet_provider_menu(bot, msg)

@bot.message_handler(func=lambda msg: msg.text == "🎓 دفع رسوم جامعية")
def handle_university_fees(msg):
    from handlers.university_fees import start_university_fee
    start_university_fee(bot, msg)

@bot.message_handler(func=lambda msg: msg.text in [
    "🖼️ تصميم لوغو احترافي",
    "📱 إدارة ونشر يومي",
    "📢 إطلاق حملة إعلانية",
    "🧾 باقة متكاملة شهرية",
    "✏️ طلب مخصص"
])
def handle_media(msg):
    from handlers.media_services import show_media_services
    show_media_services(bot, msg, user_state)

# أزرار الشركات الجديدة (حسب النصوص الموجودة)
@bot.message_handler(func=lambda msg: msg.text == "شركة الهرم")
def handle_al_haram(msg):
    bot.send_message(
        msg.chat.id,
        "💸 هذه الخدمة تخولك إلى استلام حوالتك المالية عبر **شركة الهرم**.\n"
        "يتم إضافة مبلغ 1500 ل.س على كل 50000 ل.س.\n\n"
        "تابع العملية أو ألغِ الطلب.",
        reply_markup=telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add(
            "✔️ تأكيد حوالة الهرم", "❌ إلغاء"
        )
    )
    user_state[msg.from_user.id]['step'] = "alharam_start"

@bot.message_handler(func=lambda msg: msg.text == "شركة الفؤاد")
def handle_alfouad(msg):
    bot.send_message(
        msg.chat.id,
        "💸 هذه الخدمة تخولك إلى استلام حوالتك المالية عبر **شركة الفؤاد**.\n"
        "يتم إضافة مبلغ 1500 ل.س على كل 50000 ل.س.\n\n"
        "تابع العملية أو ألغِ الطلب.",
        reply_markup=telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add(
            "✔️ تأكيد حوالة الفؤاد", "❌ إلغاء"
        )
    )
    user_state[msg.from_user.id]['step'] = "alfouad_start"

@bot.message_handler(func=lambda msg: msg.text == "شركة شخاشير")
def handle_shakhashir(msg):
    bot.send_message(
        msg.chat.id,
        "💸 هذه الخدمة تخولك إلى استلام حوالتك المالية عبر **شركة شخاشير**.\n"
        "يتم إضافة مبلغ 1500 ل.س على كل 50000 ل.س.\n"
        "\n"
        "تابع العملية أو ألغِ الطلب.",
        reply_markup=telebot.types.ReplyKeyboardMarkup(resize_keyboard=True).add(
            "✔️ تأكيد حوالة شخاشير", "❌ إلغاء"
        )
    )
    user_state[msg.from_user.id]['step'] = "shakhashir_start"

# ---------------------------------------------------------
# تشغيل نظام الطابور (QUEUE)
# ---------------------------------------------------------
try:
    from services.queue_service import process_queue
except Exception:
    def process_queue(*args, **kwargs):
        return None

threading.Thread(target=process_queue, args=(bot,), daemon=True).start()

# ---------------------------------------------------------
# ✅ ربط معالجات لعبة الجوائز بعد إنشاء البوت وتسجيل الهاندلرز
# ---------------------------------------------------------
# ---------------------------------------------------------
# معالج إلغاء عام للنص "❌ إلغاء" (أمر /cancel مسجل في handlers/cancel)
# ---------------------------------------------------------
@bot.message_handler(func=lambda msg: msg.text in ["❌ إلغاء"])
def global_cancel_text(msg):
    try:
        from services.state_service import clear_state
        clear_state(msg.from_user.id)
    except Exception:
        pass
    try:
        from handlers import keyboards
        bot.send_message(msg.chat.id, "تم إلغاء كل العمليات والعودة للبداية.", reply_markup=keyboards.main_menu())
    except Exception:
        bot.send_message(msg.chat.id, "تم إلغاء كل العمليات.")

# ---------------------------------------------------------
# تشغيل البوت مع نظام إعادة المحاولة والتنبيه في حال الخطأ
# ---------------------------------------------------------
import time

def restart_bot():
    logging.warning("🔄 إعادة تشغيل البوت بعد 10 ثوانٍ…")
    time.sleep(10)
    os.execv(sys.executable, [sys.executable] + sys.argv)

def start_polling():
    print("🤖 البوت يعمل الآن…")
    while True:
        try:
            bot.infinity_polling(
                skip_pending=True,
                long_polling_timeout=40,
            )
        except telebot.apihelper.ApiTelegramException as e:
            if getattr(e, "error_code", None) == 409:
                logging.critical("❌ تم إيقاف هذه النسخة لأن نسخة أخرى من البوت متصلة بالفعل.")
                break
            else:
                logging.error(f"🚨 خطأ في Telegram API: {e}")
                time.sleep(5)
                continue
        except Exception as e:
            logging.error(f"⚠️ انقطاع مؤقت في الاتصال: {e} — إعادة المحاولة بعد 10 ثوانٍ")
            time.sleep(10)
            continue

start_polling()
