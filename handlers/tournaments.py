# -*- coding: utf-8 -*-
# handlers/tournaments.py
from __future__ import annotations
from telebot import types
from services.state_adapter import UserStateDictLike
from services.feature_flags import ensure_feature, is_feature_enabled, require_feature_or_alert
from services.tournament_service import (
    get_or_create_open_tournament, count_verified_invites, numbers_available,
    reserve_slot, get_join_code, save_player_info, finalize_and_charge, cancel_and_cleanup
)
from services.wallet_service import get_available_balance
from config import FORCE_SUB_CHANNEL_USERNAME

user_states = UserStateDictLike()

BTN_TOUR = "🏆 البطولة"
CB = lambda s: f"tour:{s}"

def _kb_ok_cancel(ok="▶️ متابعة", cancel="❌ إلغاء"):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton(ok, callback_data=CB("go")),
           types.InlineKeyboardButton(cancel, callback_data=CB("cancel")))
    return kb

def _kb_types():
    kb = types.InlineKeyboardMarkup(row_width=1)
    if is_feature_enabled("tournaments:solo", True):
        kb.add(types.InlineKeyboardButton("1) بطولة سولو 1vs100", callback_data=CB("type:solo")))
    if is_feature_enabled("tournaments:duo", True):
        kb.add(types.InlineKeyboardButton("2) بطولة دو 2vs100",   callback_data=CB("type:duo")))
    if is_feature_enabled("tournaments:squad", True):
        kb.add(types.InlineKeyboardButton("3) بطولة سكواد 4vs100", callback_data=CB("type:squad")))
    kb.add(types.InlineKeyboardButton("⬅️ رجوع", callback_data=CB("home")))
    return kb

def _msg_intro():
    return ("هنا تختبر مهارتك وتُظهر احترافك في ببجي.\n"
            "الفائزون لهم جوائز شدّات، والحد الأدنى 325 شدة.\n"
            "انضم الآن وجهّز فريقك أو العب سولو.")

def register(bot, history):
    # ضمان وجود المفاتيح (تظهر في لوحة الأدمن)
    ensure_feature("menu:tournaments", "القائمة: البطولة", True)
    ensure_feature("tournaments:solo",  "بطولة سولو 1vs100", True)
    ensure_feature("tournaments:duo",   "بطولة دو 2vs100",   True)
    ensure_feature("tournaments:squad", "بطولة سكواد 4vs100",True)

    @bot.message_handler(func=lambda m: m.text == BTN_TOUR)
    def open_home(m):
        if require_feature_or_alert(bot, m.chat.id, "menu:tournaments", "القائمة: البطولة", True):
            return
        bot.send_message(m.chat.id, _msg_intro(), reply_markup=_kb_ok_cancel())

    @bot.callback_query_handler(func=lambda c: c.data == CB("home"))
    def cb_home(c):
        bot.edit_message_text(_msg_intro(), c.message.chat.id, c.message.message_id, reply_markup=_kb_ok_cancel())

    @bot.callback_query_handler(func=lambda c: c.data == CB("cancel"))
    def cb_cancel(c):
        cancel_and_cleanup(c.from_user.id)
        try:
            from handlers.keyboards import main_menu
            bot.edit_message_text("✅ تم الإلغاء والعودة للقائمة.", c.message.chat.id, c.message.message_id, reply_markup=main_menu())
        except Exception:
            bot.edit_message_text("✅ تم الإلغاء.", c.message.chat.id, c.message.message_id)

    @bot.callback_query_handler(func=lambda c: c.data == CB("go"))
    def cb_go(c):
        bot.edit_message_text("اختر نوع البطولة:", c.message.chat.id, c.message.message_id, reply_markup=_kb_types())

    @bot.callback_query_handler(func=lambda c: c.data.startswith(CB("type:")))
    def cb_type(c):
        type_key = c.data.split(":",2)[2]
        user_states[c.from_user.id] = {"step":"type", "type_key": type_key}
        t = get_or_create_open_tournament(type_key)
        # تحقق الدعوات
        cnt, req, ok = count_verified_invites(c.from_user.id, 2)
        if not ok:
            from services.tournament_invite_service import ensure_token
            token = ensure_token(c.from_user.id)
            invite_link = f"https://t.me/{bot.get_me().username}?start={token}"
            kb = types.InlineKeyboardMarkup(row_width=1)
            kb.add(types.InlineKeyboardButton(f"🔔 اشترك بالقناة {FORCE_SUB_CHANNEL_USERNAME}", url=f"https://t.me/{FORCE_SUB_CHANNEL_USERNAME.lstrip('@')}"))
            kb.add(types.InlineKeyboardButton("🔗 رابط دعوتك للبطولة", url=invite_link))
            kb.add(types.InlineKeyboardButton("🔁 تحقق مجددًا", callback_data=CB(f"type:{type_key}")))
            kb.add(types.InlineKeyboardButton("❌ إلغاء", callback_data=CB("cancel")))
            txt = f"شرط الانضمام:\n1) ادعُ صديقين واشتركوا في القناة عبر رابطك.\n2) 2000 ل.س اشتراك.\n\nالتقدّم: {cnt}/{req}"
            bot.edit_message_text(txt, c.message.chat.id, c.message.message_id, reply_markup=kb)
            return

        # تحقق الرصيد فقط (بدون حجز)
        need = int(t.get("entry_fee") or 2000)
        have = get_available_balance(c.from_user.id)
        if have < need:
            kb = types.InlineKeyboardMarkup(row_width=1)
            kb.add(types.InlineKeyboardButton("💳 اشحن محفظتك", callback_data=CB("home")))
            kb.add(types.InlineKeyboardButton("❌ إلغاء", callback_data=CB("cancel")))
            bot.edit_message_text(f"رصيدك غير كافٍ. المطلوب {need} ل.س والمتاح {have} ل.س.", c.message.chat.id, c.message.message_id, reply_markup=kb)
            return
        # إدخال PUBG ID
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(types.InlineKeyboardButton("❌ إلغاء", callback_data=CB("cancel")))
        bot.edit_message_text("أدخل PUBG ID الخاص بك كرسالة نصية.", c.message.chat.id, c.message.message_id, reply_markup=kb)
        user_states[c.from_user.id] = {"step":"ask_pubg", "type_key": type_key, "tournament_id": t["id"]}

    @bot.message_handler(func=lambda m: (user_states.get(m.from_user.id,{}).get("step")=="ask_pubg"))
    def got_pubg(m):
        st = user_states[m.from_user.id]
        st["pubg_id"] = (m.text or "").strip()
        user_states[m.from_user.id] = st
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(types.InlineKeyboardButton("❌ إلغاء", callback_data=CB("cancel")))
        m2 = bot.send_message(m.chat.id, "أدخل رقم موبايلك للتواصل عند الفوز.", reply_markup=kb)
        st["step"]="ask_phone"; user_states[m.from_user.id]=st

    @bot.message_handler(func=lambda m: (user_states.get(m.from_user.id,{}).get("step")=="ask_phone"))
    def got_phone(m):
        st = user_states[m.from_user.id]
        st["phone"] = (m.text or "").strip()
        user_states[m.from_user.id] = st

        # اختيار رقم الفريق
        t_id = st["tournament_id"]
        avail = numbers_available(t_id)
        # كيبورد أرقام متاحة (أول 30 زر لكل صفحة — تبسيط: عرض 1..)
        kb = types.InlineKeyboardMarkup(row_width=5)
        for row in avail[:50]:
            n = row["team_number"]
            kb.add(types.InlineKeyboardButton(str(n), callback_data=CB(f"pick:{n}")))
        kb.add(types.InlineKeyboardButton("❌ إلغاء", callback_data=CB("cancel")))
        bot.send_message(m.chat.id, "اختر رقم الفريق.", reply_markup=kb)
        st["step"]="pick_num"; user_states[m.from_user.id]=st

    @bot.callback_query_handler(func=lambda c: c.data.startswith(CB("pick:")))
    def cb_pick_num(c):
        st = user_states.get(c.from_user.id,{})
        if st.get("step")!="pick_num": return
        num = int(c.data.split(":",2)[2])
        st["team_number"]=num
        user_states[c.from_user.id]=st

        # في duo/squad نطلب رمز الانضمام لغير أول عضو
        type_key = st["type_key"]
        if type_key in ("duo","squad"):
            kb = types.InlineKeyboardMarkup(row_width=2)
            kb.add(types.InlineKeyboardButton("أنا أول عضو", callback_data=CB("first_in_team")))
            kb.add(types.InlineKeyboardButton("عندي رمز فريق", callback_data=CB("have_code")))
            kb.add(types.InlineKeyboardButton("❌ إلغاء", callback_data=CB("cancel")))
            bot.edit_message_text("هل أنت أول من يحجز هذا الرقم أم لديك رمز فريق؟", c.message.chat.id, c.message.message_id, reply_markup=kb)
        else:
            _reserve_and_confirm(c, join_code=None)

    @bot.callback_query_handler(func=lambda c: c.data==CB("first_in_team"))
    def cb_first(c):
        _reserve_and_confirm(c, join_code=None)

    @bot.callback_query_handler(func=lambda c: c.data==CB("have_code"))
    def cb_have_code(c):
        st = user_states[c.from_user.id]; st["step"]="ask_code"; user_states[c.from_user.id]=st
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton("❌ إلغاء", callback_data=CB("cancel")))
        bot.edit_message_text("أرسل رمز فريقك كرسالة.", c.message.chat.id, c.message.message_id, reply_markup=kb)

    @bot.message_handler(func=lambda m: (user_states.get(m.from_user.id,{}).get("step")=="ask_code"))
    def got_code(m):
        st = user_states[m.from_user.id]; st["join_code"]=(m.text or "").strip()
        user_states[m.from_user.id]=st
        class C: pass
        c=C(); c.from_user=m.from_user; c.message=m
        _reserve_and_confirm(c, join_code=st["join_code"])

    def _reserve_and_confirm(c, join_code: str|None):
        st = user_states[c.from_user.id]
        t_id = st["tournament_id"]; num = st["team_number"]
        out = reserve_slot(t_id, c.from_user.id, num, join_code)
        if not out.get("entry_id"):
            bot.send_message(c.message.chat.id, "الرقم محجوز أو الرمز غير صحيح. اختر رقمًا آخر.")
            return
        st["entry_id"]=out["entry_id"]; user_states[c.from_user.id]=st
        # لو كان أول عضو، اعرض له رمز الفريق ليشاركه
        code = get_join_code(t_id, num)
        if code:
            st["team_code"]=code; user_states[c.from_user.id]=st

        # شاشة ملخّص نهائي قبل الخصم
        save_player_info(st["entry_id"], st["pubg_id"], st["phone"])
        fee = 2000
        txt = (f"أهلًا بك في بطولة بوت المتجر العالمي\n"
               f"النوع: {st['type_key']} | فريق: {num}\n"
               f"PUBG ID: {st['pubg_id']}\n"
               f"الموبايل: {st['phone']}\n"
               f"سيتم خصم {fee} ل.س رسوم الاشتراك عند الضغط على متابعة.")
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(types.InlineKeyboardButton("▶️ متابعة", callback_data=CB("finalize")),
               types.InlineKeyboardButton("❌ إلغاء", callback_data=CB("cancel")))
        if st.get("team_code") and st["type_key"] in ("duo","squad"):
            kb.add(types.InlineKeyboardButton("📋 انسخ رمز الفريق", switch_inline_query=st["team_code"]))
        bot.send_message(c.message.chat.id, txt, reply_markup=kb)

    @bot.callback_query_handler(func=lambda c: c.data==CB("finalize"))
    def cb_finalize(c):
        st = user_states.get(c.from_user.id,{})
        ok = finalize_and_charge(c.from_user.id, st.get("entry_id"))
        if not ok:
            bot.answer_callback_query(c.id, "❌ الرصيد غير كافٍ الآن.")
            return
        bot.edit_message_text("✅ تم خصم 2000 ل.س وتثبيت مشاركتك. بالتوفيق!", c.message.chat.id, c.message.message_id)
