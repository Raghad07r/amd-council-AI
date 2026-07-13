# sandbox/tools.py — بيئة الأدوات الآمنة (Secure Sandbox) للوكلاء
# ══════════════════════════════════════════════════════════════════
# كل دالة هنا مُغلَّفة بمزخرف @tool من LangChain، ما يجعلها قابلة
# للربط المباشر مع نماذج Ollama الداعمة لـ Tool Calling (مثل llama3.1)
# عبر langchain_ollama.ChatOllama(...).bind_tools([...]) داخل server.py
# لاحقاً — الـ docstring في كل دالة هو نفسه "الوصف" الذي يراه النموذج
# ليقرر متى يستدعي كل أداة.
#
# مبدأ "الصندوق الرملي" (Sandbox) هنا معماري لا تنفيذي: كل أداة معزولة
# في هذا الملف، محدودة الصلاحية (Read-only / بيانات محاكاة)، ولا تصل
# لأي نظام حساس مباشرة — لا قواعد بيانات إنتاجية، لا عمليات كتابة.
#
# بوابة الأمان (Gateway Proxy):
# دالة _gateway_proxy_anonymize() أدناه تمثل الطبقة الوهمية (Middleware)
# المطلوبة في المخطط المعماري: أي استجابة قد تحتوي معرّفات شخصية
# (أرقام هوية، حسابات بنكية) تمر عبرها إلزامياً قبل مغادرة الأداة،
# تطبيقاً عملياً لمتطلبات حماية البيانات لدى سدايا وساما.
# ══════════════════════════════════════════════════════════════════

import re
import logging

from langchain_core.tools import tool

try:
    import yfinance as yf
except ImportError:
    yf = None  # الأداة ستُرجع رسالة واضحة إذا لم تُثبَّت المكتبة

logger = logging.getLogger("amd_council.sandbox")


# ══════════════════════════════════════════════════════════════════
# بوابة الأمان الذكية (Gateway Proxy) — تنقيح PII
# ══════════════════════════════════════════════════════════════════
def _gateway_proxy_anonymize(text: str) -> str:
    """
    طبقة Middleware تمثّل بوابة الحماية (Gateway Proxy) للخصوصية.
    تُطبَّق إلزامياً على أي نص يحتوي بيانات حساسة لحجب:
      • أرقام الهوية الوطنية / الإقامة السعودية (10 أرقام تبدأ بـ 1 أو 2، مع دعم المسافات أو الشرطات)
      • أرقام الآيبان السعودي (SA + 22 خانة، مع دعم المسافات أو الشرطات)
      • أرقام الهواتف المحمولة السعودية (تبدأ بـ 05 أو 9665 أو +9665)
      • أرقام الحسابات البنكية والبطاقات الائتمانية والمراجع الطويلة:
        - حجب غير مشروط للأرقام بطول 12–20 خانة.
        - حجب مشروط للأرقام بطول 8–11 خانة إذا كانت مسبوقة بكلمات مفتاحية تدل على الخصوصية.
    """
    sanitized = text

    # 1. حجب رقم الهوية الوطنية أو الإقامة (10 أرقام، يبدأ بـ 1 أو 2، مع دعم الفواصل/الشرطات الاختيارية)
    sanitized = re.sub(r"\b[12](?:[-\s]?\d){9}\b", "**********", sanitized)

    # 2. حجب رقم الآيبان السعودي: SA يتبعها 22 خانة رقمية/حرفية، مع دعم المسافات أو الشرطات
    sanitized = re.sub(r"\bSA(?:[-\s]?[a-z0-9]){22}\b", "SA**********************", sanitized, flags=re.IGNORECASE)

    # 3. حجب أرقام الجوال السعودية (جوال يبدأ بـ 05 أو 9665 أو +9665 أو 009665)
    sanitized = re.sub(r"\b(?:\+?966|00966|0)?5\d{8}\b", "05********", sanitized)

    # 4. حجب أرقام الحسابات والبطاقات الائتمانية الطويلة (12 إلى 20 رقماً) غير مشروط
    sanitized = re.sub(r"\b\d{12,20}\b", lambda m: "*" * len(m.group()), sanitized)

    # 5. حجب أرقام الحسابات المتوسطة (8 إلى 11 رقماً) مشروط بوجود كلمات دلالية للخصوصية قبلها بـ 25 حرفاً كحد أقصى
    def mask_conditional_account(match):
        start_idx = match.start()
        context_before = match.string[max(0, start_idx - 25):start_idx].lower()
        keywords = ["حساب", "رقم", "مرجع", "بطاقة", "عملية", "هوية", "account", "acc", "card", "ref", "no", "num", "id", "trans"]
        if any(kw in context_before for kw in keywords):
            return "*" * len(match.group(0))
        return match.group(0)

    sanitized = re.sub(r"\b\d{8,11}\b", mask_conditional_account, sanitized)

    return sanitized


# ══════════════════════════════════════════════════════════════════
# أدوات نورة (محللة المخاطر)
# ══════════════════════════════════════════════════════════════════
# قاموس مرجعي لترجمة أسماء الشركات السعودية الشائعة إلى رموزها الرقمية في تداول
_SAUDI_STOCK_MAP = {
    "أرامكو": "2222",
    "ارامكو": "2222",
    "الراجحي": "1120",
    "راجحي": "1120",
    "مصرف الراجحي": "1120",
    "سابك": "2010",
    "الاتصالات السعودية": "7010",
    "stc": "7010",
    "اس تي سي": "7010",
    "الإنماء": "1150",
    "الانماء": "1150",
    "مصرف الإنماء": "1150",
    "مصرف الانماء": "1150",
    "الأهلي": "1180",
    "الاهلي": "1180",
    "البنك الأهلي": "1180",
    "البنك الاهلي": "1180",
    "الرياض": "1010",
    "بنك الرياض": "1010",
    "الكهرباء": "5110",
    "كهرباء السعودية": "5110",
    "معادن": "1211",
    "جرير": "4190",
    "مكتبة جرير": "4190",
    "الحبيب": "4013",
    "سليمان الحبيب": "4013",
    "علم": "7203",
    "بترورابغ": "2380",
    "بترو رابغ": "2380",
}
# أسعار أسهم تداول تجريبية (Mock) لشركات السوق المالية السعودية
_MOCK_STOCK_PRICES = {
    "1150": {"name": "مصرف الإنماء", "price": 31.85, "change": +0.45},
    "1120": {"name": "مصرف الراجحي", "price": 84.20, "change": -0.10},
    "2222": {"name": "أرامكو السعودية", "price": 29.30, "change": +0.15},
    "2010": {"name": "سابك", "price": 76.50, "change": -0.80},
    "7010": {"name": "stc (الاتصالات السعودية)", "price": 38.60, "change": +0.20},
    "1180": {"name": "البنك الأهلي السعودي", "price": 35.40, "change": -0.30},
    "1010": {"name": "بنك الرياض", "price": 25.15, "change": +0.05},
    "5110": {"name": "الكهرباء السعودية", "price": 18.90, "change": +0.10},
    "1211": {"name": "معادن", "price": 45.20, "change": +1.15},
    "4190": {"name": "جرير", "price": 14.80, "change": -0.05},
    "4013": {"name": "مجموعة الدكتور سليمان الحبيب", "price": 282.40, "change": +4.60},
    "7203": {"name": "علم", "price": 795.00, "change": +12.00},
    "2380": {"name": "بترورابغ", "price": 7.45, "change": +0.02},
}


@tool
def get_saudi_stock_price(ticker: str) -> str:
    """
    (بيانات محاكاة تجريبية حية — Mock) يرجع سعر سهم تقريبي لإغلاق آخر جلسة
    لسهم مدرج في السوق السعودية (تداول) لأغراض العرض والتوضيح محلياً دون الحاجة لاتصال بالإنترنت.
    """
    ticker_input = ticker.strip().lower()

    # ── فحص إذا كان المدخل اسماً لشركة سعودية معروفة وتحويله للرمز الرقمي ──
    clean_ticker = None
    for name, code in _SAUDI_STOCK_MAP.items():
        if name in ticker_input or ticker_input in name:
            clean_ticker = code
            break

    if not clean_ticker:
        # ابحث عن رمز 4 أرقام
        num_match = re.search(r"\b\d{4}\b", ticker_input)
        if num_match:
            clean_ticker = num_match.group(0)
        else:
            clean_ticker = ticker_input.upper().replace(".SR", "")

    if not clean_ticker:
        return "⚠️ الرجاء تزويدي برمز سهم أو اسم شركة صالح (مثال: 1150 أو مصرف الإنماء)."

    # البحث في قائمة الأسعار التجريبية
    if clean_ticker in _MOCK_STOCK_PRICES:
        stock_info = _MOCK_STOCK_PRICES[clean_ticker]
        name = stock_info["name"]
        price = stock_info["price"]
        change = stock_info["change"]
    else:
        # توليد سعر افتراضي بناءً على الرمز
        try:
            val = sum(ord(c) for c in clean_ticker)
            price = round((val % 150) + 10.5, 2)
            change = round((val % 4) - 2.0 + 0.25, 2)
            name = f"شركة مساهمة (رمز: {clean_ticker})"
        except Exception:
            price = 50.0
            change = 0.0
            name = f"رمز {clean_ticker}"

    change_str = f"+{change:.2f}" if change >= 0 else f"{change:.2f}"
    status_emoji = "🟢" if change >= 0 else "🔴"

    return (
        f"سعر سهم {name} ({clean_ticker}.SR) في آخر جلسة تداول تجريبية هو: "
        f"{price:.2f} ريال سعودي {status_emoji} (التغير اليومي: {change_str}%) | "
        f"[بيانات محاكاة للإنماء للعرض والمراجعة]."
    )


# بيانات مؤشر عقاري تجريبية (Mock) — لغرض العرض التوضيحي في الهاكاثون فقط
_MOCK_REAL_ESTATE_INDEX = {
    "الرياض":           {"index_change_yoy": 4.2, "avg_price_sqm_residential": 3450, "trend": "تصاعدي معتدل"},
    "جدة":              {"index_change_yoy": 2.7, "avg_price_sqm_residential": 3100, "trend": "مستقر"},
    "الدمام":           {"index_change_yoy": 1.9, "avg_price_sqm_residential": 2600, "trend": "مستقر"},
    "مكة المكرمة":      {"index_change_yoy": 3.1, "avg_price_sqm_residential": 3800, "trend": "تصاعدي"},
    "المدينة المنورة":  {"index_change_yoy": 2.4, "avg_price_sqm_residential": 2900, "trend": "مستقر"},
}


@tool
def get_real_estate_index(city: str) -> str:
    """
    (بيانات هيكلية تجريبية — Mock) يرجع مؤشر أسعار العقار السكني لمدينة
    سعودية محددة، شاملاً نسبة التغير السنوي، متوسط سعر المتر المربع،
    والاتجاه العام للسوق. هذه بيانات توضيحية ثابتة للعرض فقط وليست
    بيانات سوقية حية — يجب استبدالها لاحقاً بربط فعلي مع مصدر رسمي
    (مثل الهيئة العامة للعقار) في مرحلة الإنتاج.
    """
    city_clean = city.strip()
    data = _MOCK_REAL_ESTATE_INDEX.get(city_clean)

    if not data:
        available = "، ".join(_MOCK_REAL_ESTATE_INDEX.keys())
        return f"⚠️ لا تتوفر بيانات مؤشر عقاري تجريبية للمدينة '{city_clean}'. المدن المتاحة حالياً: {available}."

    return (
        f"مؤشر العقار السكني في {city_clean} [بيانات تجريبية Mock]: "
        f"التغير السنوي {data['index_change_yoy']}%، "
        f"متوسط سعر المتر المربع {data['avg_price_sqm_residential']} ريال، "
        f"الاتجاه العام: {data['trend']}."
    )


# ══════════════════════════════════════════════════════════════════
# أداة سلمان (المخطط المالي) — الربط البنكي (محاكاة Open Banking / ساما)
# ══════════════════════════════════════════════════════════════════
# ملاحظة معمارية مهمة:
# الربط البنكي نوعان من الاستخدام في هذا المشروع، وكلاهما يعتمدان على
# نفس مولّد البيانات (build_mock_financial_profile) لتفادي ازدواجية
# المنطق:
#   1) ربط صريح بموافقة المستخدم (زر "ربط حسابي" في الواجهة) → يُستدعى
#      مرة واحدة عبر endpoint منفصل في server.py (/api/link-account)،
#      والنتيجة الهيكلية الكاملة تُخزَّن في حالة الجلسة (session-only,
#      لا تُكتب على القرص) لتُستخدم في كل رسالة لاحقة يرد فيها سلمان.
#   2) استعلام حواري لحظي أثناء الدردشة (مثلاً "شو رصيدي الآن؟") →
#      عبر simulate_sama_bank_link() كأداة LLM تقليدية (@tool) ترجع
#      ملخصاً نصياً مختصراً بدل الكائن الهيكلي الكامل.
# ══════════════════════════════════════════════════════════════════

def build_mock_financial_profile(account_data: str) -> dict:
    """
    (محاكاة Mock) يبني "ملف تعريف مالي" هيكلي كامل لحساب المستخدم،
    كما لو جاء فعلياً من واجهة Open Banking لدى ساما. هذا الكائن —
    وليس نصاً مختصراً — هو ما يُخزَّن في حالة الجلسة عند الربط الصريح،
    لأن سلمان يحتاج تفاصيل كافية (دخل، التزامات، فواتير متكررة، نمط
    إنفاق) ليقدّم تخطيطاً حقيقياً بدل رد عام.

    ⚠️ بيانات محاكاة بالكامل — لا يوجد اتصال فعلي بأي بنك. عند الربط
    الفعلي لاحقاً مع مزوّد Open Banking مرخّص من ساما، هذه الدالة هي
    نقطة الاستبدال الوحيدة المطلوبة (نفس الشكل الهيكلي للمخرجات).
    """
    raw_note = f"بيانات مستلمة من المستخدم عند الربط: {account_data.strip()[:80]}"

    profile = {
        "note":                    raw_note,
        "internal_reference_id":   "4200123456789012",  # رقم مرجعي داخلي — يُحجب لاحقاً
        "current_balance_sar":     18342.50,
        "available_credit_sar":    5000.00,
        "monthly_income_sar":      12000.00,
        "recurring_bills": [
            {"name": "اتصالات (STC)",     "amount_sar": 250.00},
            {"name": "كهرباء",             "amount_sar": 420.00},
            {"name": "اشتراك نتفليكس",     "amount_sar": 45.00},
        ],
        "outstanding_debts": [
            {"type": "قرض شخصي", "remaining_sar": 15000.00, "monthly_installment_sar": 800.00},
        ],
        "last_30_days_spending_by_category_sar": {
            "بقالة وسوبرماركت": 1450.00,
            "مطاعم ومقاهي":     1200.00,
            "مواصلات":           600.00,
            "تسوق وملابس":       900.00,
        },
        "last_transaction": "مشتريات - سوبرماركت - 245.00 ريال",
    }
    return profile


def anonymize_profile_for_display(profile: dict) -> dict:
    """
    تطبّق بوابة الأمان (Gateway Proxy) نفسها المستخدمة في الأداة
    النصية، لكن على مستوى الحقول الهيكلية بدل نص واحد طويل — تحجب
    فقط الحقول التعريفية الحساسة (note, internal_reference_id) وتُبقي
    الأرقام المالية الوظيفية (الرصيد، الفواتير، الديون) ظاهرة، لأنها
    ليست بيانات تعريف شخصية (PII) بل بيانات لازمة لعمل المستشار.
    """
    sanitized = dict(profile)  # نسخة سطحية — لا نعدّل الأصل
    if "note" in sanitized:
        sanitized["note"] = _gateway_proxy_anonymize(sanitized["note"])
    if "internal_reference_id" in sanitized:
        sanitized["internal_reference_id"] = _gateway_proxy_anonymize(
            sanitized["internal_reference_id"]
        )
    logger.info("🛡️ تم تمرير الملف المالي الهيكلي عبر Gateway Proxy للتنقيح")
    return sanitized


def format_financial_profile_for_prompt(profile: dict) -> str:
    """
    يحوّل الملف المالي الهيكلي (بعد التنقيح) إلى نص مقروء يُحقن ضمن
    System Prompt الخاص بسلمان في server.py — هذا هو الجسر بين بيانات
    الربط البنكي ورسالة النموذج الفعلية.
    """
    lines = [
        f"الرصيد الحالي: {profile['current_balance_sar']:.2f} ريال",
        f"الدخل الشهري: {profile['monthly_income_sar']:.2f} ريال",
        f"الحد الائتماني المتاح: {profile['available_credit_sar']:.2f} ريال",
    ]

    if profile.get("recurring_bills"):
        bills = "، ".join(f"{b['name']} ({b['amount_sar']:.0f} ريال)" for b in profile["recurring_bills"])
        lines.append(f"الفواتير الشهرية المتكررة: {bills}")

    if profile.get("outstanding_debts"):
        debts = "، ".join(
            f"{d['type']} (متبقي {d['remaining_sar']:.0f} ريال، قسط شهري {d['monthly_installment_sar']:.0f} ريال)"
            for d in profile["outstanding_debts"]
        )
        lines.append(f"الالتزامات والديون: {debts}")

    if profile.get("last_30_days_spending_by_category_sar"):
        spending = "، ".join(
            f"{cat}: {amount:.0f} ريال" for cat, amount in profile["last_30_days_spending_by_category_sar"].items()
        )
        lines.append(f"الإنفاق خلال آخر 30 يوماً حسب الفئة: {spending}")

    return "\n".join(f"- {line}" for line in lines)


@tool
def simulate_sama_bank_link(account_data: str) -> str:
    """
    (محاكاة Mock) يحاكي الربط البنكي الآمن المتوافق مع بيئة الخدمات
    المصرفية المفتوحة لدى ساما (Open Banking) لجلب ملخص أرصدة وحركات
    حساب المستخدم. استخدم هذه الأداة لاستعلام لحظي أثناء المحادثة
    (مثل "شو رصيدي الآن؟") عندما لا يكون الحساب مربوطاً مسبقاً عبر
    زر الربط الصريح في الواجهة. لا يوجد اتصال فعلي بأي بنك — البيانات
    محاكاة بالكامل لأغراض العرض التوضيحي في الهاكاثون.

    الإخراج يمر إلزامياً عبر بوابة تنقيح البيانات (Gateway Proxy) التي
    تحجب أي معرّفات شخصية (أرقام هوية/حسابات) قبل إرجاع النتيجة للوكيل،
    امتثالاً لمتطلبات حماية البيانات لدى سدايا وساما.
    """
    profile = build_mock_financial_profile(account_data)
    sanitized = anonymize_profile_for_display(profile)

    summary_text = (
        f"{sanitized['note']} | "
        f"الرصيد الحالي: {sanitized['current_balance_sar']} ريال | "
        f"الحد الائتماني المتاح: {sanitized['available_credit_sar']} ريال | "
        f"آخر حركة: {sanitized['last_transaction']} | "
        f"المرجع الداخلي للعملية: {sanitized['internal_reference_id']}"
    )

    logger.info("🛡️ تم تمرير استجابة simulate_sama_bank_link عبر Gateway Proxy للتنقيح")
    return summary_text


# ══════════════════════════════════════════════════════════════════
# أداة فهد (خبير السلوك)
# ══════════════════════════════════════════════════════════════════
_EXPENSE_CATEGORIES = {
    "بقالة وسوبرماركت":  ["بقالة", "سوبرماركت", "بنده", "بندة", "كارفور", "لولو", "تموينات", "العثيم"],
    "مطاعم ومقاهي":      ["مطعم", "مقهى", "كافيه", "قهوة", "ستاربكس", "هنقرستيشن", "جاهز", "توصيل طلبات"],
    "مواصلات":            ["بنزين", "وقود", "أوبر", "كريم", "تاكسي", "صيانة سيارة", "مواقف", "أدنوك", "أرامكو"],
    "تسوق وملابس":        ["ملابس", "أزياء", "حذاء", "سنتربوينت", "زارا", "شي إن", "أمازون", "نون", "شي ان"],
    "فواتير واشتراكات":   ["فاتورة", "كهرباء", "اتصالات", "stc", "إنترنت", "اشتراك", "نتفليكس", "شاهد", "موبايلي"],
    "صحة":                ["صيدلية", "دواء", "مستشفى", "عيادة", "تأمين طبي", "النهدي"],
    "ترفيه وسفر":         ["سينما", "ترفيه", "رحلة", "سفر", "فندق", "تذكرة طيران"],
}


@tool
def categorize_expenses(expenses_text: str) -> str:
    """
    يصنّف قائمة مشتريات/مصاريف نصية (سطر واحد لكل عملية شراء) إلى
    فئات مالية قياسية (بقالة، مطاعم، مواصلات، تسوق، فواتير، صحة،
    ترفيه، أخرى) بالاعتماد على كلمات مفتاحية شائعة في السياق السعودي.
    يرجع ملخصاً نصياً بعدد العمليات وإجماليها التقريبي لكل فئة، ليستخدمه
    وكيل خبير السلوك في تحليل أنماط الإنفاق العاطفي أو الاندفاعي.
    """
    lines = [line.strip() for line in expenses_text.splitlines() if line.strip()]
    if not lines:
        return "لم يتم العثور على أي عمليات لتصنيفها. أرسل كل عملية في سطر مستقل."

    category_counts = {cat: 0 for cat in _EXPENSE_CATEGORIES}
    category_totals = {cat: 0.0 for cat in _EXPENSE_CATEGORIES}
    category_counts["أخرى"] = 0
    category_totals["أخرى"] = 0.0
    uncategorized_count = 0

    for line in lines:
        line_lower = line.lower()
        matched_category = None

        for category, keywords in _EXPENSE_CATEGORIES.items():
            if any(kw.lower() in line_lower for kw in keywords):
                matched_category = category
                break

        if not matched_category:
            matched_category = "أخرى"
            uncategorized_count += 1

        category_counts[matched_category] += 1

        # ── استخراج المبلغ المالي بذكاء وتجنب التواريخ والأوقات ──
        amount = 0.0
        # 1. إزالة الأوقات والتواريخ الشائعة من السطر النصي أولاً
        # إزالة التواريخ الكاملة YYYY-MM-DD أو DD-MM-YYYY
        no_date_line = re.sub(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b", "", line_lower)
        no_date_line = re.sub(r"\b\d{1,2}[-/]\d{1,2}[-/]\d{4}\b", "", no_date_line)
        # إزالة التواريخ القصيرة DD-MM أو DD/MM
        no_date_line = re.sub(r"\b\d{1,2}[-/]\d{1,2}\b", "", no_date_line)
        # إزالة الأوقات HH:MM ص/م أو AM/PM
        no_date_line = re.sub(r"\b\d{1,2}:\d{2}(?:\s*(?:ص|م|am|pm))?\b", "", no_date_line)

        # 2. البحث عن مبلغ مسبوق أو ملحق بالعملة (ريال، ريالاً، sar، sr)
        currency_pattern = re.compile(r"(\d+(?:\.\d+)?)\s*(?:ريال|sar|sr)\b")
        currency_match = currency_pattern.search(no_date_line)
        if currency_match:
            try:
                amount = float(currency_match.group(1))
            except ValueError:
                pass
        else:
            # 3. خطة بديلة: البحث عن أي أرقام متبقية وأخذ آخر رقم في السطر النظيف
            amount_pattern = re.compile(r"(\d+(?:\.\d+)?)")
            amounts_found = amount_pattern.findall(no_date_line)
            if amounts_found:
                try:
                    amount = float(amounts_found[-1])
                except ValueError:
                    pass

        category_totals[matched_category] += amount

    summary_lines = []
    for category, count in category_counts.items():
        if count == 0:
            continue
        total = category_totals[category]
        summary_lines.append(f"- {category}: {count} عملية، بإجمالي تقريبي {total:.2f} ريال")

    result = "تصنيف المشتريات:\n" + "\n".join(summary_lines)
    if uncategorized_count:
        result += f"\n\n(⚠️ {uncategorized_count} عملية لم تُصنَّف بدقة وتحتاج مراجعة يدوية.)"

    return result


# ══════════════════════════════════════════════════════════════════
# أدوات فهد الإضافية — هندسة التمويل الشخصي عند الأزمات
# ══════════════════════════════════════════════════════════════════
# هذا التوسّع يحوّل فهد من مجرد "خبير سلوك" إلى مهندس حلول تمويلية
# عند الضائقة المالية: يقيّم الوضع الائتماني الحالي (سمة)، يستعرض
# عروض تمويل متاحة، ثم يصمم خطة قسط آمنة لا تتجاوز نسبة استقطاع
# معقولة من دخل المستخدم — بدل تركه بلا حل عملي في لحظة أزمة.
# ══════════════════════════════════════════════════════════════════

# كلمات مفتاحية تدل على ضائقة مالية أو حاجة تمويل عاجلة — تُستخدم في
# server.py لتفعيل استدعاء أدوات سمة والتمويل تلقائياً (تفعيل حتمي
# من الخادم، وليس متروكاً لقرار النموذج نفسه، لضمان سلوك موثوق
# ومضمون أثناء العرض الحي).
FINANCIAL_CRISIS_KEYWORDS = [
    "ضائقة", "أزمة مالية", "أزمة", "متعثر", "تعثرت", "تعثّر",
    "احتاج تمويل", "أحتاج تمويل", "محتاج تمويل",
    "احتاج قرض", "أحتاج قرض", "محتاج قرض",
    "مديون", "غرقان بالديون", "غارق بالديون",
    "ما أقدر أسدد", "ما اقدر اسدد", "عاجز عن السداد",
    "أبي أستدين", "ابغى استدين", "استدانة",
]


def detect_financial_crisis(text: str) -> bool:
    """
    كشف بسيط قائم على كلمات مفتاحية لحالات الضائقة المالية أو طلب
    تمويل/قرض عاجل. يُستدعى من server.py قبل رد فهد لتفعيل تلقائي
    (Deterministic Trigger) لأداتي get_simah_credit_report و
    get_available_bank_loans، عوضاً عن الاعتماد فقط على قرار النموذج
    الذاتي — يضمن أن السيناريو الحرج يعمل دائماً في العرض الحي.
    """
    if not text:
        return False
    lower = text.strip()
    return any(kw in lower for kw in FINANCIAL_CRISIS_KEYWORDS)


def build_mock_simah_report() -> dict:
    """(محاكاة Mock) يبني تقريراً ائتمانياً مبسطاً شبيهاً بتقرير سمة."""
    return {
        "credit_score":                        682,   # نطاق سمة التقريبي: 300–900
        "rating_label":                         "جيد",
        "active_obligations_count":             2,
        "total_monthly_installments_sar":       950.00,
        "historical_default_ratio_percent":     3.5,
        "max_recommended_installment_percent":  33,   # الحد الأقصى المعتاد لنسبة الاستقطاع من الدخل
    }


@tool
def get_simah_credit_report(note: str = "") -> str:
    """
    (محاكاة Mock) يجلب تقريراً ائتمانياً مبسطاً شبيهاً بتقرير 'سمة'
    الفعلي، يشمل: التقييم الائتماني (300-900)، عدد الالتزامات النشطة،
    إجمالي الأقساط الشهرية الحالية، نسبة التعثر التاريخية، والحد
    الأقصى الموصى به لنسبة الاستقطاع الشهري من الدخل. استخدمه لتقييم
    قدرة المستخدم على تحمّل تمويل إضافي دون مخاطرة بالتعثر، قبل اقتراح
    أي خطة تمويلية. لا يوجد اتصال فعلي بشركة سمة — بيانات محاكاة
    بالكامل لأغراض العرض التوضيحي في الهاكاثون.
    """
    report = build_mock_simah_report()
    return (
        f"التقييم الائتماني: {report['credit_score']} من 900 ({report['rating_label']}) | "
        f"عدد الالتزامات النشطة: {report['active_obligations_count']} | "
        f"إجمالي الأقساط الشهرية الحالية: {report['total_monthly_installments_sar']:.2f} ريال | "
        f"نسبة التعثر التاريخية: {report['historical_default_ratio_percent']}% | "
        f"الحد الأقصى الموصى به لنسبة الاستقطاع الشهري: "
        f"{report['max_recommended_installment_percent']}% من الدخل."
    )


# عروض تمويل مصرف الإنماء الفعلية والتجريبية لأغراض المحاكاة والعرض التوضيحي
_MOCK_BANK_LOAN_OFFERS = [
    {"bank": "مصرف الإنماء", "product": "التمويل الشخصي المتوافق مع الأحكام الشرعية",  "profit_rate_percent": 3.99, "max_tenure_months": 60},
    {"bank": "مصرف الإنماء", "product": "تمويل الطوارئ العاجل",                        "profit_rate_percent": 4.25, "max_tenure_months": 36},
    {"bank": "مصرف الإنماء", "product": "برنامج إعادة هيكلة وتوحيد الديون (سداد المديونية)", "profit_rate_percent": 3.50, "max_tenure_months": 84},
]


def _estimate_flat_installment(amount: float, annual_rate_percent: float, months: int) -> float:
    """
    تقدير مبسط للقسط الشهري بطريقة الربح الثابت (Flat Rate) —
    تقريبي لأغراض العرض فقط، وليس حساباً بنكياً دقيقاً.
    """
    total_profit = amount * (annual_rate_percent / 100) * (months / 12)
    return (amount + total_profit) / months


@tool
def get_available_bank_loans(loan_amount_sar: float = 10000.0) -> str:
    """
    (بيانات هيكلية تجريبية — Mock) يرجع عروض تمويل/قروض متاحة حالياً
    من مصرف الإنماء، شاملة هامش الربح السنوي، الحد الأقصى لمدة
    السداد، وتقدير القسط الشهري بناءً على مبلغ التمويل المطلوب.
    استخدم هذه الأداة بعد الاطلاع على get_simah_credit_report لمقارنة
    خيارات التمويل المتاحة قبل اقتراح خطة معينة. هذه بيانات توضيحية
    ثابتة للعرض فقط وليست عروضاً بنكية حية.
    """
    lines = []
    for offer in _MOCK_BANK_LOAN_OFFERS:
        installment = _estimate_flat_installment(
            loan_amount_sar, offer["profit_rate_percent"], offer["max_tenure_months"]
        )
        lines.append(
            f"- {offer['bank']} ({offer['product']}): هامش ربح {offer['profit_rate_percent']}% سنوياً، "
            f"حتى {offer['max_tenure_months']} شهراً، "
            f"قسط شهري تقديري لمبلغ {loan_amount_sar:.0f} ريال ≈ {installment:.0f} ريال [تقديري]"
        )

    return (
        f"عروض التمويل المتاحة حالياً من مصرف الإنماء لمبلغ {loan_amount_sar:.0f} ريال:\n"
        + "\n".join(lines)
    )



@tool
def execute_stock_order(ticker: str, action: str = "شراء / أتمتة", quantity: int = 10) -> str:
    """
    (أداة محاكاة تنفيذ الأوامر — Mock) تقوم بتنفيذ عمليات شراء/بيع الأسهم أو تفعيل الأتمتة الاستثمارية 
    حسب طلب شريك الإنماء مباشرة عبر محفظة الإنماء الاستثمارية وتأكيد القبول.
    """
    return f"نجاح: تم قبول وتفعيل أمر {action} لـ {quantity} أسهم في {ticker} بنجاح عبر محفظتك الاستثمارية بمصرف الإنماء."


# ══════════════════════════════════════════════════════════════════
# تجميع الأدوات حسب الوكيل — يُستخدم في server.py مع bind_tools()
# ══════════════════════════════════════════════════════════════════
PLANNER_TOOLS  = [simulate_sama_bank_link, execute_stock_order]
RISK_TOOLS     = [get_saudi_stock_price, get_real_estate_index, execute_stock_order]
BEHAVIOR_TOOLS = [categorize_expenses, get_simah_credit_report, get_available_bank_loans]

AGENT_TOOLS = {
    "planner":  PLANNER_TOOLS,
    "risk":     RISK_TOOLS,
    "behavior": BEHAVIOR_TOOLS,
}
