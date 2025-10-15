# services/tournament_invite_service.py
from __future__ import annotations
from datetime import datetime, timezone
from typing import Tuple, Optional
from database.db import client
from postgrest.exceptions import APIError
import secrets

TOK = "tournament_invite_tokens"
INV = "tournament_invites"

def ensure_token(inviter_id: int) -> str:
    # أعِد نفس التوكن إن وُجد؛ وإلا أنشئ واحدًا
    c = client()
    q = c.table(TOK).select("token,created_at").eq("inviter_user_id", inviter_id)\
        .order("created_at", desc=True).limit(1).execute()
    rows = getattr(q, "data", []) or []
    if rows:
        return rows[0]["token"]
    token = f"t-{inviter_id}-{secrets.token_urlsafe(8)}"
    c.table(TOK).insert({"token": token, "inviter_user_id": inviter_id}).execute()
    return token

def attach_invite(token: str, invitee_id: int) -> Optional[int]:
    # تُستدعى عند /start t-...
    c = client()
    q = c.table(TOK).select("inviter_user_id").eq("token", token).limit(1).execute()
    rows = getattr(q, "data", []) or []
    if not rows:
        return None
    inviter = int(rows[0]["inviter_user_id"])
    try:
        c.table(INV).insert({"inviter_user_id": inviter, "invitee_user_id": invitee_id}).execute()
    except APIError:
        pass  # موجودة مسبقًا
    return inviter

def mark_verified(inviter_id: int, invitee_id: int, still_member: bool):
    client().table(INV).update({
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "still_member": bool(still_member),
    }).eq("inviter_user_id", inviter_id).eq("invitee_user_id", invitee_id).execute()

def count_verified(inviter_id: int) -> Tuple[int, int, bool]:
    req = 2
    q = client().table(INV).select("id,verified_at,still_member").eq("inviter_user_id", inviter_id).execute()
    rows = getattr(q, "data", []) or []
    cnt = sum(1 for r in rows if r.get("verified_at") and r.get("still_member"))
    return cnt, req, cnt >= req
