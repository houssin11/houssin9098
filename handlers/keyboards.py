from telebot import types
import logging
from services.feature_flags import is_feature_enabled

def main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)

    # الصف 1: المنتجات فقط
    markup.row("🛒 المنتجات", "➕ إضافة خصم")
if is_feature_enabled("menu:tournaments", True):
    markup.row("🏆 البطولة")

    # باقي الصفوف كما كانت
    markup.row("💳 شحن محفظتي", "💰 محفظتي")
    markup.row("📢 إعلاناتك", "🌐 صفحتنا")
    markup.row("🛠️ الدعم الفني")
    return markup

def products_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.row("🎮 شحن ألعاب و تطبيقات", "💳 تحويل وحدات فاتورة سوري")
    markup.row("🌐 دفع مزودات الإنترنت ADSL", "🎓 دفع رسوم جامعية")
    markup.row("تحويلات كاش و حوالات", "🖼️ خدمات إعلانية وتصميم")
    markup.row("📦 طلب احتياجات منزلية او تجارية")
    markup.row("⬅️ رجوع")
    return markup

# قائمة التحويلات المدمجة
def transfers_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.row("💵 تحويل الى رصيد كاش", "حوالة مالية عبر شركات")
    markup.row("⬅️ رجوع")
    return markup

def game_categories():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.row("🎯 شحن شدات ببجي العالمية", "🔥 شحن جواهر فري فاير")
    markup.row("🏏 تطبيق جواكر", "🎮 شحن العاب و تطبيقات مختلفة")
    markup.row("⬅️ رجوع")
    return markup

def recharge_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.row("📲 سيرياتيل كاش", "📲 أم تي إن كاش")
    markup.row("📲 شام كاش", "💳 Payeer")
    markup.row("⬅️ رجوع")
    return markup

def cash_transfer_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.row("تحويل إلى سيرياتيل كاش", "تحويل إلى أم تي إن كاش")
    markup.row("تحويل إلى شام كاش")
    markup.row("⬅️ رجوع")
    return markup

def companies_transfer_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.row("شركة الهرم", "شركة الفؤاد")
    markup.row("شركة شخاشير")
    markup.row("⬅️ رجوع")
    return markup

def syrian_balance_menu():
    from handlers.syr_units import SYRIATEL_PRODUCTS
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    buttons = [types.KeyboardButton(f"{p.name} - {p.price:,} ل.س") for p in SYRIATEL_PRODUCTS]
    buttons.append(types.KeyboardButton("⬅️ رجوع"))
    markup.add(*buttons)
    return markup

def wallet_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.row("💰 محفظتي", "🛍️ مشترياتي")
    markup.row("📑 سجل التحويلات")
    markup.row("🔁 تحويل من محفظتك إلى محفظة عميل آخر")
    markup.row("⬅️ رجوع")
    return markup

def support_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.row("🛠️ الدعم الفني")
    markup.row("⬅️ رجوع")
    return markup

def links_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.row("🌐 موقعنا", "📘 فيس بوك")
    markup.row("📸 إنستغرام")
    markup.row("⬅️ رجوع")
    return markup

def media_services_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.row("🖼️ تصميم لوغو احترافي", "📱 إدارة ونشر يومي")
    markup.row("📢 إطلاق حملة إعلانية", "🧾 باقة متكاملة شهرية")
    markup.row("✏️ طلب مخصص")
    markup.row("⬅️ رجوع")
    return markup

def hide_keyboard():
    return types.ReplyKeyboardRemove()

# زر القائمة Menu الثابت
def menu_button():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.row("Menu")
    return markup
