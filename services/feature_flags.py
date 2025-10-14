# services/feature_flags.py
from __future__ import annotations
import logging
import re
import time
from typing import Dict, Any, List, Optional
from database.db import get_table

FEATURES_TABLE = "features"

# ==============================
# رسائل ونصوص مساعدة
# ==============================
UNAVAILABLE_MSG = "⛔ نعتذر، «{label}» غير متاح حاليًا بسبب الضغط/الصيانة. سنعمل على إعادته للعمل بأقرب وقت. شكرًا لتفهمك ونأسف على الإزعاج 🤍"

def slugify(s: str) -> str:
    """
    تبسيط نص للمفاتيح: حروف/أرقام عربية أو لاتينية + شرطات.
    لا نحاول تحويل العربية، فقط نحذف الرموز ونوحّد الفراغات لشرطات.
    """
    if not s:
        return ""
    s = str(s).strip()
    s = re.sub(r"[\s_]+", "-", s)  # فراغات/سطر سفلي -> واصلة
    s = re.sub(r"[^0-9A-Za-z\u0600-\u06FF\-]+", "", s)
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("-").lower()

# ==============================
# مفاتيح ديناميكية تفصيلية
# ==============================
def key_product(product_id: int, name: str) -> str:
    """مفتاح منتج مفرد (60 شدة/310 جوهرة/120000 توكنز..)."""
    return f"product:item:{int(product_id)}"

def key_units(carrier: str, qty_label: str) -> str:
    """مفتاح باقة وحدات لمشغّل معيّن (MTN/Syriatel) بوسم الكمية."""
    return f"units:{slugify(carrier)}:{slugify(qty_label)}"

def _tbl():
    return get_table(FEATURES_TABLE)

# ==============================
# (جديد) المفاتيح القياسية بأسلوب namespaces
# ==============================
# مفاتيح القائمة الرئيسية
MENU_KEYS: Dict[str, str] = {
    "menu:products": "القائمة: المنتجات",
    "menu:recharge": "القائمة: شحن محفظتي",
    "menu:wallet":   "القائمة: محفظتي",
    "menu:ads":      "القائمة: إعلاناتك",
    "menu:links":    "القائمة: صفحتنا/روابط",
    "menu:support":  "القائمة: الدعم الفني",
}

# مفاتيح تبويب المنتجات (قوائم عليا)
PRODUCTS_KEYS: Dict[str, str] = {
    "products:games":      "منتجات: شحن ألعاب وتطبيقات",
    "products:syr_units":  "منتجات: تحويل وحدات سوري",
    "products:internet":   "منتجات: مزودات ADSL",
    "products:university": "منتجات: رسوم جامعية",
    "products:transfers":  "منتجات: تحويلات/حوالات",
    "products:media":      "منتجات: خدمات إعلانية/تصميم",
    "products:home":       "منتجات: احتياجات منزلية",
}

# مفاتيح التحويلات
TRANSFERS_KEYS: Dict[str, str] = {
    "transfers:cash":      "تحويلات: رصيد كاش",
    "transfers:companies": "تحويلات: عبر شركات",
}

# مفاتيح الشحن بالطرق
RECHARGE_KEYS: Dict[str, str] = {
    "recharge:syr":    "شحن: سيرياتيل كاش",
    "recharge:mtn":    "شحن: أم تي إن كاش",
    "recharge:sham":   "شحن: شام كاش",
    "recharge:payeer": "شحن: Payeer",
}

# مفاتيح فئات الألعاب
GAMES_KEYS: Dict[str, str] = {
    "games:pubg":  "ألعاب: شدات ببجي",
    "games:ff":    "ألعاب: فري فاير",
    "games:jwkr":  "ألعاب: جواكر",
    "games:other": "ألعاب: أخرى",
}

# مفاتيح شركات الحوالات
COMPANIES_KEYS: Dict[str, str] = {
    "companies:alharam":    "شركة الهرم",
    "companies:alfouad":    "شركة الفؤاد",
    "companies:shakhashir": "شركة شخاشير",
}

# مفاتيح ثابتة قائمة لديك مسبقًا (لا يوجد بديل جديد لها)
LEGACY_UNIQUE: Dict[str, str] = {
    # المحفظة والسجل
    "wallet": "المحفظة",
    "wallet_purchases": "مشترياتي",
    "wallet_transfers": "سجل التحويلات",
    "wallet_p2p": "تحويل بين المحافظ",
    # الفواتير والوحدات العامة
    "mtn_unit": "وحدات MTN",
    "syr_unit": "وحدات Syriatel",
    "mtn_bill": "فواتير MTN",
    "syr_bill": "فواتير Syriatel",
    # الإنترنت العام + مزوّدون (تبقى كما هي)
    "internet": "إنترنت",
    "internet_provider_tarassul": "مزود — تراسل",
    "internet_provider_mtn": "مزود — أم تي إن",
    "internet_provider_syriatel": "مزود — سيرياتيل",
    "internet_provider_aya": "مزود — آية",
    "internet_provider_sawa": "مزود — سوا",
    "internet_provider_rannet": "مزود — رن نت",
    "internet_provider_samanet": "مزود — سما نت",
    "internet_provider_omnia": "مزود — أمنية",
    "internet_provider_hypernet": "مزود — هايبر نت",
    "internet_provider_mts": "مزود — MTS",
    "internet_provider_yara": "مزود — يارا",
    "internet_provider_dunia": "مزود — دنيا",
    "internet_provider_aint": "مزود — آينت",
    "internet_provider_cards":          "مزود — مزود بطاقات",
    "internet_provider_scs":            "مزود — الجمعية SCS",
    "internet_provider_view":           "مزود — فيو",
    "internet_provider_haifi":          "مزود — هايفي",
    "internet_provider_syrian_telecom": "مزود — السورية للاتصالات",

    # الإعلانات / الرسوم / خدمات الميديا التفصيلية
    "ads": "إعلانات",
    "university_fees": "رسوم جامعية",
    "media_services": "خدمات سوشيال/ميديا",
    "media_logo": "خدمة — تصميم لوغو احترافي",
    "media_sm_daily": "خدمة — إدارة ونشر يومي",
    "media_ads_launch": "خدمة — إطلاق حملة إعلانية",
    "media_video_edit": "خدمة — مونتاج فيديو قصير",
    "media_twitter_threads": "خدمة — خيوط تويتر جاهزة",
    "media_voiceover": "خدمة — تعليق صوتي احترافي",
    "media_copywriting": "خدمة — كتابة محتوى تسويقي",
    # شراء جملة
    "wholesale": "شراء جملة",
}
# بطولات
TOURNAMENTS_KEYS: Dict[str, str] = {
    "menu:tournaments": "القائمة: البطولة",
    "tournaments:solo":  "بطولة سولو 1vs100",
    "tournaments:duo":   "بطولة دو 2vs100",
    "tournaments:squad": "بطولة سكواد 4vs100",
}

# ========== بذرة موحّدة ==========
FEATURES_SEED: Dict[str, str] = {
    **MENU_KEYS,
    **PRODUCTS_KEYS,
    **TRANSFERS_KEYS,
    **RECHARGE_KEYS,
    **GAMES_KEYS,
    **COMPANIES_KEYS,
    **TOURNAMENTS_KEYS,
    **LEGACY_UNIQUE,
}

# ==============================
# ترحيل مفاتيح قديمة لمفاتيح حديثة (حذف المكرر فقط)
# ==============================
LEGACY_ALIASES: Dict[str, str] = {
    # قوائم عليا
    "products_menu": "menu:products",
    "games_menu":    "products:games",
    # عناصر ألعاب قديمة
    "product_pubg":     "games:pubg",
    "product_freefire": "games:ff",
    "product_jawaker":  "games:jwkr",
    # زر شحن المحفظة في القائمة
    "wallet_recharge": "menu:recharge",
    # التحويلات
    "cash_transfer":      "transfers:cash",
    "companies_transfer": "transfers:companies",
    # مزودات الشحن
    "recharge_syriatel": "recharge:syr",
    "recharge_mtn":      "recharge:mtn",
    "recharge_sham":     "recharge:sham",
    "recharge_payeer":   "recharge:payeer",
    # الشركات
    "company_alharam":    "companies:alharam",
    "company_alfouad":    "companies:alfouad",
    "company_shakhashir": "companies:shakhashir",
}

def _migrate_legacy_duplicates() -> int:
    """
    يرحّل المفاتيح القديمة إلى الحديثة ويزيل المكرر فقط.
    يحافظ على حالة التفعيل والملصق عند الإمكان.
    """
    migrated = 0
    try:
        keys = list(LEGACY_ALIASES.keys())
        if not keys:
            return 0
        # اسحب كل المفاتيح القديمة الموجودة فعليًا
        rows = _tbl().select("key,label,active").execute()
        data = getattr(rows, "data", []) or []
        legacy_present = {r["key"]: r for r in data if r["key"] in LEGACY_ALIASES}

        for old_key, row in legacy_present.items():
            new_key = LEGACY_ALIASES[old_key]
            old_active = bool(row.get("active", True))
            old_label = str(row.get("label") or "")

            # هل الجديد موجود؟
            rnew = _tbl().select("key,label,active").eq("key", new_key).limit(1).execute()
            new_exists = bool(getattr(rnew, "data", []) or [])
            if not new_exists:
                # استخدم label من FEATURES_SEED إن توفر، وإلا الملصق القديم
                new_label = FEATURES_SEED.get(new_key, old_label or new_key)
                _tbl().insert({"key": new_key, "label": new_label, "active": old_active}).execute()
            else:
                # لو موجود مسبقًا: لا نغيّر تفعيله، لكن نحدّث الملصق من البذرة إن وُجد
                new_label = FEATURES_SEED.get(new_key)
                if new_label:
                    _tbl().update({"label": new_label}).eq("key", new_key).execute()

            # احذف القديم (هذا هو حذف "المكرر" فقط)
            _tbl().delete().eq("key", old_key).execute()
            migrated += 1
    except Exception as e:
        logging.exception("[features] legacy migration failed: %s", e)
    return migrated

# ============================================
# عناصر تفصيلية تُزرع تلقائيًا (منتجات/وحدات)
# ============================================
KNOWN_PRODUCTS: List[Dict[str, Any]] = [
    # PUBG
    {"id": 1, "label": "PUBG — 60 شدة"},
    {"id": 2, "label": "PUBG — 325 شدة"},
    {"id": 3, "label": "PUBG — 660 شدة"},
    {"id": 4, "label": "PUBG — 1800 شدة"},
    {"id": 5, "label": "PUBG — 3850 شدة"},
    {"id": 6, "label": "PUBG — 8100 شدة"},
    # FreeFire
    {"id": 7, "label": "FreeFire — 100 جوهرة"},
    {"id": 8, "label": "FreeFire — 310 جوهرة"},
    {"id": 9, "label": "FreeFire — 520 جوهرة"},
    {"id": 10, "label": "FreeFire — 1060 جوهرة"},
    {"id": 11, "label": "FreeFire — 2180 جوهرة"},
    # Jawaker
    {"id": 12, "label": "Jawaker — 10000 توكنز"},
    {"id": 13, "label": "Jawaker — 15000 توكنز"},
    {"id": 14, "label": "Jawaker — 20000 توكنز"},
    {"id": 15, "label": "Jawaker — 30000 توكنز"},
    {"id": 16, "label": "Jawaker — 60000 توكنز"},
    {"id": 17, "label": "Jawaker — 120000 توكنز"},
]

SYRIATEL_UNIT_PACKS = [
    "1000 وحدة", "1500 وحدة", "2013 وحدة", "3068 وحدة", "4506 وحدة",
    "5273 وحدة", "7190 وحدة", "9587 وحدة", "13039 وحدة",
]
MTN_UNIT_PACKS = [
    "1000 وحدة", "5000 وحدة", "7000 وحدة", "10000 وحدة", "15000 وحدة",
    "20000 وحدة", "23000 وحدة", "30000 وحدة", "36000 وحدة",
]

def _seed_known_details() -> int:
    """زرع مفاتيح عناصر المنتجات وباقات الوحدات."""
    created = 0
    try:
        # منتجات الألعاب
        for item in KNOWN_PRODUCTS:
            k = key_product(item["id"], item["label"])
            if ensure_feature(k, item["label"], default_active=True):
                created += 1
        # وحدات سيرياتيل
        for pack in SYRIATEL_UNIT_PACKS:
            k = key_units("syriatel", pack)
            if ensure_feature(k, f"وحدات Syriatel — {pack}", default_active=True):
                created += 1
        # وحدات MTN
        for pack in MTN_UNIT_PACKS:
            k = key_units("mtn", pack)
            if ensure_feature(k, f"وحدات MTN — {pack}", default_active=True):
                created += 1
    except Exception as e:
        logging.exception("[features] _seed_known_details failed: %s", e)
    return created

# ==============================
# إنشاء/تحديث المفاتيح
# ==============================
def ensure_feature(key: str, label: str, default_active: bool = True) -> bool:
    """
    يضمن وجود مفتاح مخصّص (منتج مفرد/باقة وحدات..). يرجّع True لو تم الإنشاء.
    """
    try:
        r = _tbl().select("key").eq("key", key).limit(1).execute()
        if not getattr(r, "data", None):
            _tbl().insert({"key": key, "label": label, "active": bool(default_active)}).execute()
            _cache_clear()
            return True
        else:
            # حدّث الاسم إن تغيّر
            _tbl().update({"label": label}).eq("key", key).execute()
            _cache_clear()
            return False
    except Exception as e:
        logging.exception("[features] ensure_feature failed (%s): %s", key, e)
        return False

def ensure_bulk(items: List[Dict[str, Any]]) -> int:
    """
    زرع جماعي: items = [{key, label, active?}, ...] — يرجّع عدد الجديد المُنشأ.
    """
    created = 0
    for it in items:
        k = it.get("key")
        lbl = it.get("label", k)
        act = it.get("active", True)
        if ensure_feature(k, lbl, act):
            created += 1
    return created

def ensure_seed() -> int:
    """
    يزرع المزايا الافتراضية + يرحّل المكرّر + العناصر التفصيلية.
    يرجّع عدد المفاتيح الجديدة المُنشأة (لا يشمل عدد المهاجرة).
    """
    created = 0
    try:
        # 1) زرع البذرة القياسية (الموحدة)
        for k, label in FEATURES_SEED.items():
            r = _tbl().select("key").eq("key", k).limit(1).execute()
            if not getattr(r, "data", None):
                _tbl().insert({"key": k, "label": label, "active": True}).execute()
                created += 1
            else:
                _tbl().update({"label": label}).eq("key", k).execute()

        # 2) نقل المفاتيح القديمة إلى الحديثة وحذف المكرر فقط
        migrated = _migrate_legacy_duplicates()
        if migrated:
            logging.info("[features] migrated legacy duplicates: %s", migrated)

        # 3) العناصر التفصيلية
        created += _seed_known_details()

        _cache_clear()
    except Exception as e:
        logging.exception("[features] ensure_seed failed: %s", e)
    return created

# ==============================
# كاش خفيف لقراءات الحالة
# ==============================
__CACHE_TTL = 3.0  # ثوانٍ
__cache_map: Dict[str, bool] = {}
__cache_ts: float = 0.0

def _cache_ok() -> bool:
    return (time.time() - __cache_ts) <= __CACHE_TTL

def _cache_get(key: str, default: bool) -> bool:
    if _cache_ok() and key in __cache_map:
        return __cache_map[key]
    return default

def _cache_put(key: str, value: bool):
    global __cache_ts
    if not _cache_ok():
        __cache_map.clear()
    __cache_map[key] = bool(value)
    __cache_ts = time.time()

def _cache_clear():
    __cache_map.clear()

# ==============================
# استعلامات الحالة
# ==============================
def list_features() -> List[Dict[str, Any]]:
    try:
        r = _tbl().select("key,label,active").order("label", desc=False).execute()
        return getattr(r, "data", []) or []
    except Exception as e:
        logging.exception("[features] list_features failed: %s", e)
        return []

def set_feature_active(key: str, active: bool) -> bool:
    try:
        _tbl().update({"active": bool(active)}).eq("key", key).execute()
        _cache_clear()
        return True
    except Exception as e:
        logging.exception("[features] set_feature_active failed: %s", e)
        return False

def is_feature_enabled(key: str, default: bool = True) -> bool:
    # جرّب الكاش أولاً
    v = _cache_get(key, None)
    if isinstance(v, bool):
        return v
    try:
        r = _tbl().select("active").eq("key", key).limit(1).execute()
        data = getattr(r, "data", None)
        if not data:
            _cache_put(key, default)
            return default
        val = bool(data[0].get("active", default))
        _cache_put(key, val)
        return val
    except Exception:
        return default

# Aliases للتوافق مع أي كود يستدعي أسماء مختلفة
def is_feature_active(key: str, default: bool = True) -> bool:
    return is_feature_enabled(key, default)

def is_active(key: str, default: bool = True) -> bool:
    return is_feature_enabled(key, default)

# ==============================
# حُرّاس للاستخدام داخل الهاندلرز
# ==============================
def block_if_disabled(bot, chat_id: int, feature_key: str, label: Optional[str] = None) -> bool:
    """إن كانت الميزة مقفلة يرسل تنويه عام ويرجع True (يعني: قِف)."""
    if is_feature_enabled(feature_key, default=True):
        return False
    lbl = label or FEATURES_SEED.get(feature_key, feature_key)
    try:
        bot.send_message(chat_id, f"⛔ ميزة «{lbl}» غير متاحة حاليًا. سنعيد تفعيلها قريبًا.")
    except Exception:
        pass
    return True

def require_feature_or_alert(bot, chat_id: int, key: str, label: str, default_active: bool = True) -> bool:
    """
    يضمن المفتاح + يفحص تفعيله.
    يرجّع True لو يجب إيقاف الإجراء (غير متاح) بعد إرسال رسالة الاعتذار.
    """
    ensure_feature(key, label, default_active=default_active)
    if is_feature_enabled(key, default=True):
        return False
    try:
        bot.send_message(chat_id, UNAVAILABLE_MSG.format(label=label))
    except Exception:
        pass
    return True

# ==============================
# تجميع/ترتيب للعرض الإداري
# ==============================
def _group_for(key: str, label: str) -> str:
    # namespaces الحديثة
    if key.startswith("menu:"):
        return "القائمة الرئيسية"
    if key.startswith("products:"):
        return "المنتجات — قوائم"
    if key.startswith("transfers:"):
        return "تحويلات"
    if key.startswith("recharge:"):
        return "الشحن — طرق"
    if key.startswith("games:"):
        return "ألعاب — فئات"
    if key.startswith("companies:"):
        return "حوالات شركات — الشركات"

    # مفاتيح ديناميكية
    if key.startswith("product:item:"):
        return "المنتجات — عناصر مفردة"
    if key.startswith("units:mtn:"):
        return "وحدات MTN — باقات"
    if key.startswith("units:syriatel:"):
        return "وحدات Syriatel — باقات"

    # مفاتيح ثابتة قديمة (تبقى كما هي)
    if key.startswith("internet_provider_"):
        return "الإنترنت — المزودون"
    if key.startswith("recharge_"):
        return "الشحن — طرق (قديم)"
    if key.startswith("cash_"):
        return "تحويل كاش — الأنواع (قديم)"
    if key.startswith("company_"):
        return "حوالات شركات — الشركات (قديم)"

    fixed_groups = {
        "wallet": "المحفظة",
        "wallet_purchases": "المحفظة",
        "wallet_transfers": "المحفظة",
        "wallet_p2p": "المحفظة",
        "mtn_unit": "الفواتير/الوحدات — عامة",
        "syr_unit": "الفواتير/الوحدات — عامة",
        "mtn_bill": "الفواتير/الوحدات — عامة",
        "syr_bill": "الفواتير/الوحدات — عامة",
        "internet": "الإنترنت — عام",
        "ads": "الإعلانات",
        "university_fees": "الرسوم الجامعية",
        "media_services": "خدمات الميديا",
        "media_logo": "خدمات الميديا",
        "media_sm_daily": "خدمات الميديا",
        "media_ads_launch": "خدمات الميديا",
        "media_video_edit": "خدمات الميديا",
        "media_twitter_threads": "خدمات الميديا",
        "media_voiceover": "خدمات الميديا",
        "media_copywriting": "خدمات الميديا",
        "wholesale": "شراء جملة",
    }
    return fixed_groups.get(key, "أخرى")

def list_features_grouped() -> Dict[str, List[Dict[str, Any]]]:
    """يرجّع {اسم المجموعة: [features...]} بترتيب أبجدي حسب label داخل كل مجموعة."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for row in list_features():
        grp = _group_for(row["key"], row["label"])
        out.setdefault(grp, []).append(row)
    # فرز داخلي
    for grp, items in out.items():
        items.sort(key=lambda r: (str(r.get("label") or ""), str(r.get("key"))))
    return out
