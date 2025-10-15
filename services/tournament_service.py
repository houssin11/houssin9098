# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import Dict, Any, List, Tuple
from database.db import get_table, client, try_deduct_rpc
from services.referral_service import get_or_create_today_goal
from postgrest.exceptions import APIError

TTBL = "tournaments"
ETBL = "tournament_entries"
CTBL = "tournament_team_codes"

def get_or_create_open_tournament(type_key: str) -> Dict[str, Any]:
    r = get_table(TTBL).select("*").eq("type_key", type_key).eq("status","open").limit(1).execute()
    rows = getattr(r,"data",[]) or []
    if rows: return rows[0]
    ins = {
        "type_key": type_key, "status":"open",
        "entry_fee": 2000, "prize_min": 325
    }
    return get_table(TTBL).insert(ins).execute().data[0]

def count_verified_invites(referrer_id: int, required_count: int = 2) -> Tuple[int,int,bool]:
    g = get_or_create_today_goal(referrer_id, required_count=required_count)
    gid = g["id"]
    # حاول قراءة فيو التقدم إن وجد
    try:
        v = client().table("referral_goals_progress_v").select("*").eq("goal_id", gid).limit(1).execute()
        rows = getattr(v,"data",[]) or []
        if rows:
            cnt = int(rows[0].get("verified_count") or 0)
            req = int(rows[0].get("required_count") or required_count)
            return cnt, req, cnt >= req
    except Exception:
        pass
    # fallback: عدّ من referral_joins
    q = client().table("referral_joins").select("id,verified_at,still_member").eq("goal_id", gid).execute()
    cnt = sum(1 for r in (getattr(q,"data",[]) or []) if r.get("verified_at") and r.get("still_member"))
    return cnt, required_count, cnt >= required_count

def numbers_available(tournament_id: str) -> List[Dict[str,int]]:
    try:
        r = client().rpc("available_team_numbers", {"p_tournament": tournament_id}).execute()
        return getattr(r,"data",[]) or []
    except APIError:
        # fallback بسيط: عرض 1..100 بلا تحقق السعة (لن يُستخدم إذا الـ RPC موجود)
        return [{"team_number": i, "slots_left": 1} for i in range(1, 101)]

def reserve_slot(tournament_id: str, user_id: int, num: int, join_code: str | None) -> Dict[str, Any]:
    r = client().rpc("reserve_team_slot", {
        "p_tournament": tournament_id, "p_user": user_id, "p_num": int(num),
        "p_join_code": join_code
    }).execute()
    return {"entry_id": (getattr(r,"data", None))}

def get_join_code(tournament_id: str, num: int) -> str | None:
    r = client().rpc("get_team_join_code", {"p_tournament": tournament_id, "p_num": int(num)}).execute()
    rows = getattr(r,"data", None)
    return rows

def save_player_info(entry_id: str, pubg_id: str, phone: str):
    client().table(ETBL).update({"pubg_id": pubg_id, "phone": phone}).eq("id", entry_id).execute()

def finalize_and_charge(user_id: int, entry_id: str, fee: int = 2000) -> bool:
    """خصم ذَرّي بعد التأكيد النهائي. لا holds إطلاقًا."""
    ok = bool(try_deduct_rpc(user_id, fee).data)
    if not ok:
        return False
    client().table(ETBL).update({"payment_captured": True}).eq("id", entry_id).execute()
    return True

def cancel_and_cleanup(user_id: int):
    # احذف أي مشاركات غير مُسدّدة لهذا المستخدم
    client().table(ETBL).delete().eq("user_id", user_id).eq("payment_captured", False).execute()
