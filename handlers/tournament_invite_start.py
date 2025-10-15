# handlers/tournament_invite_start.py
from __future__ import annotations
from telebot import types
from services.tournament_invite_service import attach_invite, mark_verified
from config import FORCE_SUB_CHANNEL_ID

def register(bot):
    # يلتقط /start t-<...>
    @bot.message_handler(func=lambda m: isinstance(m.text, str) and m.text.startswith("/start t-"))
    def start_tournament_invite(m):
        token = m.text.split(maxsplit=1)[0].replace("/start ","").strip().split(" ",1)[0][7:] if m.text.startswith("/start ") else m.text[7:]
        inviter = attach_invite(token, m.from_user.id)
        if not inviter:
            bot.reply_to(m, "رابط الدعوة غير صالح.")
            return
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔔 اشترك بالقناة", url=f"https://t.me/{str(FORCE_SUB_CHANNEL_ID).lstrip('@')}"))
        kb.add(types.InlineKeyboardButton("✅ تحققت", callback_data=f"ti:check:{inviter}"))
        bot.reply_to(m, "انضم إلى القناة ثم اضغط تحقّقت لتأكيد الدعوة.", reply_markup=kb)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("ti:check:"))
    def cb_check(c):
        inviter = int(c.data.split(":")[2])
        try:
            cm = bot.get_chat_member(FORCE_SUB_CHANNEL_ID, c.from_user.id)
            ok = cm.status in ("member", "administrator", "creator")
        except Exception:
            ok = False
        mark_verified(inviter, c.from_user.id, ok)
        bot.answer_callback_query(c.id, "تم التحقق" if ok else "لم يتم العثور على اشتراكك")
