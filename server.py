# server.py — مجلس مستشار أمد للوعي المالي | FastAPI + Ollama + LangGraph Edition
# ══════════════════════════════════════════════════════════════════
# ملخص التحول المعماري (On-Premise 100%):
#   • الاستدلال (Inference): عبر Ollama محلياً (مثال: llama3.1) — لا مفاتيح API
#     خارجية، لا بيانات تغادر الجهاز/الشبكة الداخلية.
#   • التوجيه (Orchestration): عبر LangGraph — كل وكيل (سلمان/نورة/فهد) عقدة
#     (Node) مستقلة في StateGraph، تُنفَّذ بالتتابع (Sequential)، ويقرر كل
#     وكيل داخلياً هل السؤال ضمن تخصصه أم يتجاوزه للوكيل التالي.
#   • المعرفة (RAG): عبر rag_engine.py — كل وكيل يسترجع من مسار ChromaDB
#     خاص به فقط (planner_docs / risk_docs / behavior_docs) قبل الرد.
#   • البث (Streaming): نُبقي على SSE للواجهة الأمامية، لكن التوكنز الآن
#     تأتي من نموذج Ollama محلي بدل OpenRouter، وتُمرَّر عبر طابور
#     (asyncio.Queue) بين تنفيذ LangGraph والـ StreamingResponse.
#   • سياق المستخدم المالي: /api/council يستقبل حقل financial_context
#     {user_income, current_balance, fixed_expenses, savings_goal}
#     ويحقنه تلقائياً لكل الوكلاء الثلاثة — لا أحد يسأل المستخدم عن
#     دخله أو رصيده مجدداً.
#   • فهد وأزمات التمويل: عند كشف كلمات ضائقة/تمويل/قرض في سؤال
#     المستخدم، يستدعي الخادم تلقائياً (Deterministic Trigger) أداتي
#     get_simah_credit_report و get_available_bank_loans من
#     sandbox/tools.py قبل رد فهد، ليصمم خطة قسط آمنة بدل الاعتماد
#     على قرار النموذج الذاتي لاستدعاء الأداة.
# ══════════════════════════════════════════════════════════════════

import os
import re
import json
import asyncio
import logging
from typing import AsyncGenerator, TypedDict, Optional

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

import ollama
from langgraph.graph import StateGraph, END

load_dotenv()

logger = logging.getLogger("amd_council")
logging.basicConfig(level=logging.INFO)

# ── إعدادات Ollama المحلي ────────────────────────────────────────
OLLAMA_HOST  = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")   # نموذج الاستدلال الرئيسي المحلي
COUNCIL_MAX_ROUNDS = int(os.getenv("COUNCIL_MAX_ROUNDS", "1"))   # الحد الأقصى لجولات النقاش المشتركة للوكلاء

# عميل Ollama غير متزامن — كل الاتصال يبقى داخل الشبكة المحلية (localhost)
ollama_client = ollama.AsyncClient(host=OLLAMA_HOST)


# ── تعريف الوكلاء ────────────────────────────────────────────────
from agents.prompts import PLANNER_PROMPT, RISK_PROMPT, BEHAVIOR_PROMPT
from rag_engine import retrieve_context, collection_status
from sandbox.tools import (
    detect_financial_crisis,
    get_simah_credit_report,
    get_available_bank_loans,
    simulate_sama_bank_link,
    get_saudi_stock_price,
    get_real_estate_index,
    categorize_expenses,
    _SAUDI_STOCK_MAP,
    execute_stock_order,
)

AGENTS = [
    {
        "id":        "planner",
        "shortName": "سلمان",
        "role":      "مخطط مالي",
        "color":     "#14213D",
        "prompt":    PLANNER_PROMPT,
        "keywords":  ["ميزانية", "ادخار", "خطة", "راتب", "دخل", "مصاريف", "إنفاق", "توفير", "طوارئ", "أولويات", "تخطيط", "نفقات", "قرض", "دين"],
    },
    {
        "id":        "risk",
        "shortName": "نورة",
        "role":      "محللة مخاطر",
        "color":     "#C1663B",
        "prompt":    RISK_PROMPT,
        "keywords":  ["استثمار", "أسهم", "عقار", "ذهب", "مخاطر", "صندوق", "عائد", "بورصة", "محفظة", "سوق", "ودائع", "تقييم", "خسارة", "ربح"],
    },
    {
        "id":        "behavior",
        "shortName": "فهد",
        "role":      "خبير سلوك",
        "color":     "#8A8171",
        "prompt":    BEHAVIOR_PROMPT,
        "keywords":  [
            "عادات", "سلوك", "إسراف", "تبذير", "اندفاع", "نفسي", "شراء", "تسوق",
            "إدمان", "تحفيز", "التزام", "تسويف", "خوف", "قلق", "عاطفي",
            # كلمات أزمة/تمويل تُفعّل مشاركة فهد فوراً عبر المسار السريع
            # (بدون انتظار تقييم Ollama) — تتطابق مع FINANCIAL_CRISIS_KEYWORDS
            # في sandbox/tools.py لضمان اتساق الكشف بين مكانين مختلفين.
            "ضائقة", "أزمة", "متعثر", "تعثر", "تمويل", "قرض", "مديون", "دين", "استدانة",
        ],
    },
]
AGENTS_BY_ID = {a["id"]: a for a in AGENTS}

# ── FastAPI App ──────────────────────────────────────────────────
app = FastAPI(title="مجلس مستشار أمد للوعي المالي")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── التوجيه الذكي: هل يجب أن يرد هذا المستشار؟ ─────────────────
async def should_agent_respond(agent: dict, query: str, council_context: str) -> bool:
    """
    فحص سريع (كلمات مفتاحية) ثم فحص دلالي عبر Ollama إن لزم.
    هذا هو أساس "التوجيه الديناميكي المتسلسل": كل وكيل يقيّم بنفسه
    - وبتكلفة استدلال منخفضة (num_predict قليلة) - هل يتدخل أم يمرر الدور.
    """
    query_lower = query.lower()

    keyword_match = any(kw in query_lower for kw in agent["keywords"])
    if keyword_match:
        return True

    eval_prompt = f"""أنت {agent['shortName']} ({agent['role']}).
السؤال التالي: "{query}"
هل هذا السؤال يتعلق بتخصصك مباشرة أو بشكل ذي صلة؟
أجب فقط بـ: نعم أو لا"""

    try:
        response = await ollama_client.chat(
            model=OLLAMA_MODEL,
            messages=[{"role": "user", "content": eval_prompt}],
            options={"temperature": 0.0, "num_predict": 5},
            stream=False,
        )
        answer = response["message"]["content"].strip()
        return "نعم" in answer
    except Exception as e:
        logger.error(f"⚠️ فشل تقييم التوجيه عبر Ollama، سنفترض المشاركة افتراضياً: {e}")
        return True


# ── كلمات تدل على تفكير داخلي يجب تصفيته ───────────────────────
THINKING_TRIGGERS = [
    "okay", "let me", "let's", "first,", "looking back", "i need to",
    "the user", "hmm,", "now,", "important:", "must not", "must avoid",
    "key points", "phrasing", "self-check", "drafting", "my response",
    "per sama", "per sdaia", "critical context", "behavioral pattern",
    "what they really", "their",
]

def _is_thinking_line(text: str) -> bool:
    lower = text.strip().lower()
    if not lower:
        return False
    english_chars = sum(1 for c in lower if c.isascii() and c.isalpha())
    total_chars = sum(1 for c in lower if c.isalpha())
    if total_chars > 0 and english_chars / total_chars > 0.6:
        return True
    for trigger in THINKING_TRIGGERS:
        if lower.startswith(trigger):
            return True
    return False


# ── تنسيق سياق المستخدم المالي (يصل عبر جسم الطلب /api/council) ──
def format_user_financial_context(fc: Optional[dict]) -> str:
    """
    يحوّل كائن السياق المالي الذي يرسله العميل مع كل طلب —
    {user_income, current_balance, fixed_expenses, savings_goal} —
    إلى نص عربي مقروء يُحقن في System Prompt لكل الوكلاء الثلاثة
    (عبر stream_agent_response). هذا يُغني الوكلاء عن سؤال المستخدم
    عن دخله أو رصيده في كل مرة — البيانات تصل جاهزة مع الرسالة نفسها.
    """
    if not fc or not isinstance(fc, dict):
        return ""

    parts = []
    if fc.get("user_income") is not None:
        parts.append(f"الدخل الشهري: {fc['user_income']} ريال")
    if fc.get("current_balance") is not None:
        parts.append(f"الرصيد الحالي: {fc['current_balance']} ريال")
    if fc.get("fixed_expenses") is not None:
        parts.append(f"المصاريف الثابتة الشهرية: {fc['fixed_expenses']} ريال")
    if fc.get("savings_goal") is not None:
        parts.append(f"هدف الادخار: {fc['savings_goal']} ريال")

    if not parts:
        return ""

    return "\n".join(f"- {p}" for p in parts)


# ── بث رد الوكيل (Ollama Streaming) ────────────────────────────
async def stream_agent_response(
    agent: dict,
    messages: list,
    council_context: str,
    rag_context: str = "",
    financial_context: str = "",
    tool_results: str = "",
) -> AsyncGenerator[str, None]:
    """
    يبث توكنز الوكيل من Ollama محلياً مع تصفية التفكير الداخلي.
    - rag_context: مسترجع من مسار المعرفة العام الخاص بالوكيل (RAG).
    - financial_context: الوضع المالي الحالي للمستخدم (دخل/رصيد/
      مصاريف ثابتة/هدف ادخار) — يصل مع كل رسالة عبر جسم الطلب،
      ويُحقن لكل الوكلاء الثلاثة بلا استثناء (انظر format_user_financial_context).
    - tool_results: نتائج أدوات استُدعيت تلقائياً من الخادم قبل رد
      هذا الوكيل تحديداً (مثل تقرير سمة وعروض التمويل لفهد عند كشف
      ضائقة مالية) — مختلف عن RAG لأنها بيانات حية آنية لا معرفة
      مخزَّنة، ومختلف عن financial_context لأنها نتيجة استدعاء أداة
      لا بيانات وصفها المستخدم مباشرة.
    """
    MAX_HISTORY = 4
    trimmed_messages = messages[-MAX_HISTORY:] if len(messages) > MAX_HISTORY else messages

    system = agent["prompt"]

    if financial_context:
        system += f"""

الوضع المالي الحالي للمستخدم:
هذه بيانات فعلية لوضع المستخدم الحالي، وليست معرفة عامة. استخدمها
مباشرة في ردك ولا تسأل المستخدم عنها مجدداً:
{financial_context}"""

    if tool_results:
        system += f"""
 
نتائج الأدوات التي استُدعيت تلقائياً لهذه الرسالة (اعتمد عليها كمصدر أساسي وموثق للبيانات الحية، مثل أسعار الأسهم، مؤشرات العقار، أرصدة الحسابات البنكية، أو عروض التمويل، ولا تختلق أرقاماً بديلة):
{tool_results}"""

    if rag_context:
        system += f"""

سياق مرجعي مسترجع من قاعدة معرفتك الخاصة (RAG) — اعتمد عليه كمصدر أساسي
لأي أرقام أو نسب أو حقائق قبل معرفتك العامة، واذكر أنك تستند لمصدر داخلي
موثّق إن كان ذلك مناسباً:
{rag_context}"""

    if council_context:
        system += f"\n\nردود زملائك في المجلس حتى الآن:\n{council_context}\nيمكنك التعليق على آرائهم، الاتفاق، الاعتراض، أو إضافة زاوية جديدة."

    system += """

تعليمات صارمة — يجب الالتزام بها تماماً:
- ابدأ ردك مباشرة بالمحتوى العربي. لا مقدمات.
- لا تكتب أي كلمة بالإنجليزية إطلاقاً.
- لا تكتب تحليلك أو خطوات تفكيرك. فقط الجواب النهائي.
- لا تكتب جملاً مثل: "سأقوم"، "دعني"، "أولاً"، "بناءً على"، "كما قال".
- الرد: 2-4 جمل عربية مباشرة فقط."""

    ollama_messages = [
        {"role": "system", "content": system},
        *[{"role": m["role"], "content": m["content"]} for m in trimmed_messages],
    ]

    stream = await ollama_client.chat(
        model=OLLAMA_MODEL,
        messages=ollama_messages,
        options={
            "temperature": 0.7,
            "num_predict": 100,
            "num_ctx": 4096,
            "keep_alive": "10m"
        },
        stream=True,
    )

    buffer = ""
    found_arabic_start = False

    async for chunk in stream:
        token = chunk.get("message", {}).get("content", "") or ""
        if not token:
            continue

        if found_arabic_start:
            yield token
        else:
            buffer += token
            # Check if buffer contains Arabic
            if bool(re.search(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]", buffer)):
                found_arabic_start = True
                yield buffer
                buffer = ""
            else:
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if _is_thinking_line(line):
                        continue
                    else:
                        found_arabic_start = True
                        yield line + "\n"
                        if buffer:
                            yield buffer
                            buffer = ""
                        break

    if not found_arabic_start and buffer.strip():
        if not _is_thinking_line(buffer):
            yield buffer


# ── SSE helper ──────────────────────────────────────────────────
def sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


# ══════════════════════════════════════════════════════════════════
# LangGraph: تعريف حالة المجلس والعقد (Nodes) المتسلسلة
# ══════════════════════════════════════════════════════════════════
class CouncilState(TypedDict):
    messages: list          # سجل المحادثة الكامل
    query: str              # آخر سؤال من المستخدم
    council_context: str    # نص تراكمي بردود الوكلاء حتى الآن (للسياق التبادلي)
    responses: list         # الردود الكاملة للوكلاء الذين شاركوا فعلياً
    skipped: list           # الوكلاء الذين تجاوزوا السؤال (خارج تخصصهم)
    event_queue: asyncio.Queue    # قناة تمرير أحداث SSE من داخل العقدة إلى الـ endpoint
    financial_context: str  # نص جاهز (بعد التنسيق) لوضع المستخدم المالي الحالي —
                             # دخل/رصيد/مصاريف ثابتة/هدف ادخار — يُحقن لكل الوكلاء
    active_agents: list     # معرّفات الوكلاء النشطين الموجهين دلالياً
    discussion_round: int   # الجولة النقاشية الحالية (1 أو 2)
    current_agent_index: int # مؤشر الوكيل النشط الحالي
    rag_contexts: Optional[dict]  # سياقات الـ RAG المجلوبة مسبقاً بالتوازي لتخفيض زمن الاستجابة


def extract_loan_amount(query: str) -> float:
    """
    يستخلص مبلغ التمويل المطلوب من سؤال المستخدم باستخدام أنماط البحث الشائعة،
    ويرجع القيمة كـ float، أو 10000.0 كقيمة افتراضية إذا لم يحدد المستخدم مبلغاً.
    """
    text = query.lower()

    # 1. البحث عن الأرقام المقترنة بكلمات تدل على الآلاف مثل "ألف" أو "الف" أو "k"
    # مثال: "50 ألف" أو "50 الف" أو "50k"
    k_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:ألف|الف|k|ك)\b", text)
    if k_match:
        try:
            return float(k_match.group(1)) * 1000
        except ValueError:
            pass

    # 2. البحث عن الأرقام الكبيرة المباشرة (مثل 50000 أو 50,000) التي تظهر بالقرب من كلمات القرض/التمويل
    text_clean = text.replace(",", "")
    numbers = re.findall(r"\b(\d{3,8}(?:\.\d+)?)\b", text_clean)
    if numbers:
        try:
            amounts = [float(n) for n in numbers]
            # نستثني السنوات الشائعة
            filtered_amounts = [a for a in amounts if a not in [2024, 2025, 2026, 2027]]
            if filtered_amounts:
                return max(filtered_amounts)
        except ValueError:
            pass

    # 3. معالجة الأرقام المكتوبة نصاً باللغة العربية
    text_arabic_mapping = {
        "خمسين ألف": 50000.0,
        "خمسين الف": 50000.0,
        "مئة ألف": 100000.0,
        "مئة الف": 100000.0,
        "مائة ألف": 100000.0,
        "مائة الف": 100000.0,
        "عشرين ألف": 20000.0,
        "عشرين الف": 20000.0,
        "ثلاثين ألف": 30000.0,
        "ثلاثين الف": 30000.0,
        "أربعين ألف": 40000.0,
        "أربعين الف": 40000.0,
    }
    for key, val in text_arabic_mapping.items():
        if key in text:
            return val

    return 10000.0


def make_agent_node(agent: dict):
    """
    مصنع عقد LangGraph: كل وكيل يصبح عقدة مستقلة ضمن StateGraph.
    منطق العقدة:
      1) استرجاع RAG الخاص بمساره فقط (مع التخزين المسبق للسرعة).
      2) تشغيل الأدوات التلقائية عند الأزمات (لفهد).
      3) البدء بالبث الحي للرد.
    """
    async def node(state: CouncilState) -> dict:
        queue = state["event_queue"]

        # ── استرجاع من مسار RAG المخصص لهذا الوكيل فقط (مع الكاش المسبق للسرعة) ──
        rag_contexts = state.get("rag_contexts", {}) or {}
        if agent["id"] in rag_contexts:
            rag_context = rag_contexts[agent["id"]]
        else:
            rag_context = await retrieve_context(agent["id"], state["query"])

        # ── استخراج أسماء مستندات المصادر من سياق الـ RAG وبثها كحدث للمستخدم ──
        if rag_context:
            sources = re.findall(r"\[المصدر (?:التخصصي|المشترك): (.*?)\]", rag_context)
            unique_sources = list(dict.fromkeys(sources))
            if unique_sources:
                await queue.put(sse({
                    "type": "citations",
                    "agentId": agent["id"],
                    "sources": unique_sources
                }))

        # ── تشغيل الأدوات التلقائية بناءً على سياق سؤال المستخدم ونوع الوكيل ──
        tool_results = ""
        query_lower = state["query"].lower()

        # 1. أدوات سلمان (مخطط مالي)
        if agent["id"] == "planner":
            if any(kw in query_lower for kw in ["رصيد", "حسابي", "بياناتي البنكية", "ربط البنك", "البنك المفتوح", "اسحب"]):
                await queue.put(sse({"type": "tool_call", "agentId": agent["id"], "tool": "simulate_sama_bank_link"}))
                bank_result = simulate_sama_bank_link.invoke({"account_data": state["query"][:80]})
                tool_results += f"[نتيجة simulate_sama_bank_link]\n{bank_result}\n\n"

            # أتمتة وشراء أسهم
            if any(kw in query_lower for kw in ["شراء", "اشتري", "بيع", "أتمتة", "اتمتة"]):
                found_stock = "مصرف الإنماء (1150)"
                for name, code in _SAUDI_STOCK_MAP.items():
                    if name in query_lower:
                        found_stock = f"{name} ({code})"
                        break
                action = "أتمتة استثمار شهري" if any(kw in query_lower for kw in ["أتمتة", "اتمتة"]) else "شراء فوري"
                await queue.put(sse({"type": "tool_call", "agentId": agent["id"], "tool": "execute_stock_order"}))
                order_result = execute_stock_order.invoke({"ticker": found_stock, "action": action, "quantity": 100})
                tool_results += f"[نتيجة execute_stock_order]\n{order_result}\n\n"

        # 2. أدوات نورة (تحليل مخاطر)
        elif agent["id"] == "risk":
            # أتمتة وشراء أسهم
            if any(kw in query_lower for kw in ["شراء", "اشتري", "بيع", "أتمتة", "اتمتة"]):
                found_stock = "مصرف الإنماء (1150)"
                for name, code in _SAUDI_STOCK_MAP.items():
                    if name in query_lower:
                        found_stock = f"{name} ({code})"
                        break
                action = "أتمتة استثمار شهري" if any(kw in query_lower for kw in ["أتمتة", "اتمتة"]) else "شراء فوري"
                await queue.put(sse({"type": "tool_call", "agentId": agent["id"], "tool": "execute_stock_order"}))
                order_result = execute_stock_order.invoke({"ticker": found_stock, "action": action, "quantity": 100})
                tool_results += f"[نتيجة execute_stock_order]\n{order_result}\n\n"

            # جلب سعر سهم
            elif any(kw in query_lower for kw in ["سهم", "أسهم", "سعر", "تداول"]) or any(kw in query_lower for kw in _SAUDI_STOCK_MAP.keys()):
                found_ticker = None
                num_match = re.search(r"\b\d{4}\b", query_lower)
                if num_match:
                    found_ticker = num_match.group(0)
                else:
                    for name in _SAUDI_STOCK_MAP.keys():
                        if name in query_lower:
                            found_ticker = name
                            break
                if found_ticker:
                    await queue.put(sse({"type": "tool_call", "agentId": agent["id"], "tool": "get_saudi_stock_price"}))
                    stock_result = get_saudi_stock_price.invoke({"ticker": found_ticker})
                    tool_results += f"[نتيجة get_saudi_stock_price]\n{stock_result}\n\n"

            # جلب مؤشر العقار
            if any(kw in query_lower for kw in ["عقار", "عقاري", "أسعار العقار", "شراء بيت", "سعر المتر"]):
                found_city = "الرياض"
                for city in ["الرياض", "جدة", "الدمام", "مكة", "المدينة"]:
                    if city in query_lower:
                        found_city = city
                        if found_city == "مكة": found_city = "مكة المكرمة"
                        if found_city == "المدينة": found_city = "المدينة المنورة"
                        break
                await queue.put(sse({"type": "tool_call", "agentId": agent["id"], "tool": "get_real_estate_index"}))
                real_estate_result = get_real_estate_index.invoke({"city": found_city})
                tool_results += f"[نتيجة get_real_estate_index]\n{real_estate_result}\n\n"

        # 3. أدوات فهد (خبير السلوك ومهندس تمويل)
        elif agent["id"] == "behavior":
            # تصنيف مصاريف
            if any(kw in query_lower for kw in ["صنف", "تصنيف", "مصاريفي", "مشتريات", "صرفيات"]) or len(state["query"].splitlines()) > 2:
                await queue.put(sse({"type": "tool_call", "agentId": agent["id"], "tool": "categorize_expenses"}))
                expense_result = categorize_expenses.invoke({"expenses_text": state["query"]})
                tool_results += f"[نتيجة categorize_expenses]\n{expense_result}\n\n"

            # أدوات الأزمة والتمويل
            if detect_financial_crisis(state["query"]):
                loan_amount = extract_loan_amount(state["query"])

                await queue.put(sse({"type": "tool_call", "agentId": agent["id"], "tool": "get_simah_credit_report"}))
                simah_result = get_simah_credit_report.invoke({"note": state["query"][:80]})

                await queue.put(sse({"type": "tool_call", "agentId": agent["id"], "tool": "get_available_bank_loans"}))
                loans_result = get_available_bank_loans.invoke({"loan_amount_sar": loan_amount})

                tool_results += (
                    f"[نتيجة get_simah_credit_report]\n{simah_result}\n\n"
                    f"[نتيجة get_available_bank_loans لمبلغ {loan_amount:.0f} ريال]\n{loans_result}"
                )

        await queue.put(sse({
            "type":      "agent_start",
            "agentId":   agent["id"],
            "agentName": agent["shortName"],
            "role":      agent["role"],
            "color":     agent["color"],
            "round":     state.get("discussion_round", 1),
        }))

        full_text = ""
        async for token in stream_agent_response(
            agent,
            state["messages"],
            state["council_context"],
            rag_context,
            financial_context=state.get("financial_context", ""),
            tool_results=tool_results,
        ):
            full_text += token
            await queue.put(sse({"type": "token", "agentId": agent["id"], "token": token}))

        await queue.put(sse({"type": "agent_done", "agentId": agent["id"]}))

        new_context = state["council_context"] + f"[{agent['shortName']} - {agent['role']}]: {full_text}\n"
        return {
            "responses": state["responses"] + [{**agent, "text": full_text}],
            "council_context": new_context,
            "current_agent_index": state.get("current_agent_index", 0) + 1,
        }

    return node


async def router_node(state: CouncilState) -> dict:
    """
    عقدة الموجه الدلالي الذكي:
    تقيم السؤال أولاً بالكلمات المفتاحية كمسار سريع لتجنب استدعاء Ollama،
    وإذا لم يتطابق شيء، تقيم دلالياً عبر Ollama لتحديد أي الوكلاء يجب أن يشاركوا.
    """
    query = state["query"]
    queue = state["event_queue"]
    query_lower = query.lower()

    # 1. فحص سريع بالكلمات المفتاحية كمسار سريع (Fast-path Keyword Routing)
    decision = {}
    for agent in AGENTS:
        decision[agent["id"]] = any(kw in query_lower for kw in agent["keywords"])

    # إذا تم العثور على أي تطابق للكلمات المفتاحية، نتخطى الاستدعاء الدلالي المباشر لتوفير الوقت
    keywords_matched = any(decision.values())
    if keywords_matched:
        logger.info(f"⚡ تم تفعيل المسار السريع بناءً على الكلمات المفتاحية: {decision}")
    else:
        logger.info("🧠 لم يتم العثور على كلمات مفتاحية واضحة، جاري التوجيه الدلالي عبر Ollama...")
        # 2. إعداد موجه الاستعلام الدلالي
        eval_prompt = f"""أنت الموجه الذكي لمجلس أمد المالي. مهمتك هي تحليل سؤال المستخدم وتحديد أي من المستشارين الثلاثة يجب أن يشارك في الإجابة.
المستشارون هم:
1. planner (سلمان - مخطط مالي): متخصص في الميزانيات، الادخار، موازنة الراتب، وتقليل النفقات والديون.
2. risk (نورة - محللة مخاطر): متخصصة في الاستثمار، الأسهم، العقارات، الذهب، وتقييم المخاطر والأرباح والخسائر.
3. behavior (فهد - خبير سلوك): متخصص في العادات المالية، الإسراف، الدوافع النفسية للشراء، والحلول التمويلية عند الأزمات والتعثر وتقرير سمة.

حلل السؤال التالي: "{query}"

أجب بتنسيق JSON فقط بالشكل التالي:
{{
  "planner": true/false,
  "risk": true/false,
  "behavior": true/false
}}
أجب بالـ JSON فقط دون أي مقدمات أو تعليقات أو علامات كود أخرى."""

        # 3. الاستدعاء الدلالي عبر Ollama مع تحديد عدد التوكنز لسرعة الاستجابة
        try:
            response = await ollama_client.chat(
                model=OLLAMA_MODEL,
                messages=[{"role": "user", "content": eval_prompt}],
                options={"temperature": 0.0, "num_predict": 30},
                stream=False,
            )
            content = response["message"]["content"].strip()
            
            # استخراج JSON باستخدام تعبير نمطي
            json_match = re.search(r"\{.*?\}", content, re.DOTALL)
            if json_match:
                decision = json.loads(json_match.group(0))
        except Exception as e:
            logger.error(f"⚠️ فشل التقييم الدلالي للموجه عبر Ollama: {e}")

    # 4. التحقق الاحتياطي بالكلمات المفتاحية إذا فشل الـ LLM أو أعطى نتيجة فارغة (ولم نكن قد قمنا بالفحص الفعلي مسبقاً)
    if not decision or not any(isinstance(v, bool) for v in decision.values()):
        decision = {}
        for agent in AGENTS:
            decision[agent["id"]] = any(kw in query_lower for kw in agent["keywords"])

    # 5. إذا لم يتم تفعيل أي وكيل، نقوم بتفعيل سلمان افتراضياً كخيار احتياطي
    if not any(decision.values()):
        decision["planner"] = True

    active_agents = []
    skipped = []
    
    for agent in AGENTS:
        if decision.get(agent["id"], False):
            active_agents.append(agent["id"])
            # إرسال حدث جاري التقييم لتنشيط مؤشر الانتظار في الواجهة
            await queue.put(sse({"type": "evaluating", "agentId": agent["id"]}))
        else:
            skipped.append(agent)

    # جلب سياقات RAG للوكلاء النشطين بالتوازي لتقليص زمن الاستجابة الإجمالي (RAG Parallel Prefetching)
    rag_contexts = {}
    if active_agents:
        async def prefetch_rag(agent_id, query_text):
            ctx = await retrieve_context(agent_id, query_text)
            return agent_id, ctx
        tasks = [prefetch_rag(aid, query) for aid in active_agents]
        results = await asyncio.gather(*tasks)
        rag_contexts = dict(results)

    return {
        "active_agents": active_agents,
        "skipped": skipped,
        "discussion_round": 0,
        "current_agent_index": 0,
        "rag_contexts": rag_contexts,
    }


async def coordinator_node(state: CouncilState) -> dict:
    """
    منسق الحوار: يزيد رقم الجولة ويهيئ مؤشر الوكيل النشط الحالي للجولة القادمة.
    """
    current_round = state.get("discussion_round", 0)
    new_round = current_round + 1

    # بث زمني لبدء الجولة النقاشية الجديدة في الواجهة
    queue = state["event_queue"]
    await queue.put(sse({"type": "discussion_round_start", "round": new_round}))

    return {
        "discussion_round": new_round,
        "current_agent_index": 0
    }


def route_from_coordinator(state: CouncilState):
    """
    تحديد المسار التالي من المنسق:
    إما التوجه للوكيل النشط الأول، أو الإنهاء إذا تجاوزنا جولات النقاش المحددة.
    إذا كان هناك مستشار واحد فقط نشط، نكتفي بجولة واحدة لتسريع الإجابة وتجنب التكرار.
    """
    discussion_round = state.get("discussion_round", 1)
    active_agents = state.get("active_agents", [])

    max_rounds = COUNCIL_MAX_ROUNDS if len(active_agents) > 1 else 1
    if discussion_round > max_rounds:
        return END

    if not active_agents:
        return END

    return active_agents[0]


def route_after_agent(state: CouncilState):
    """
    تحديد الوكيل النشط التالي في هذه الجولة، أو العودة للمنسق إذا انتهى الجميع.
    """
    active_agents = state.get("active_agents", [])
    current_idx = state.get("current_agent_index", 0)

    if current_idx < len(active_agents):
        return active_agents[current_idx]

    return "coordinator"


def build_council_graph():
    """
    يبني StateGraph يمثل حلقة نقاش تفاعلية ثنائية الجولات (WhatsApp-style Group Discussion Loop).
    """
    graph = StateGraph(CouncilState)

    graph.add_node("router", router_node)
    graph.add_node("coordinator", coordinator_node)

    for agent in AGENTS:
        graph.add_node(agent["id"], make_agent_node(agent))

    graph.set_entry_point("router")

    # من الموجه نذهب دائماً إلى منسق الجولات
    graph.add_edge("router", "coordinator")

    # من المنسق نوجه الوكلاء أو ننهي الجلسة
    graph.add_conditional_edges(
        "coordinator",
        route_from_coordinator,
        {
            "planner": "planner",
            "risk": "risk",
            "behavior": "behavior",
            END: END
        }
    )

    # من أي وكيل يكتمل رده، نحدد التالي أو نعود للمنسق
    for agent in AGENTS:
        graph.add_conditional_edges(
            agent["id"],
            route_after_agent,
            {
                "planner": "planner",
                "risk": "risk",
                "behavior": "behavior",
                "coordinator": "coordinator"
            }
        )

    return graph.compile()


council_graph = build_council_graph()


# ── المسار الرئيسي ──────────────────────────────────────────────
@app.post("/api/council")
async def council_endpoint(request: Request):
    body = await request.json()
    messages = body.get("messages", [])

    # ── الوضع المالي الحالي للمستخدم — يُرسَل مع كل طلب من العميل ──
    # {user_income, current_balance, fixed_expenses, savings_goal}
    # يُنسَّق هنا مرة واحدة ثم يُحقن لكل الوكلاء الثلاثة داخل الرسم
    # البياني، بدل أن يسأل كل وكيل المستخدم عن دخله ورصيده بنفسه.
    raw_financial_context = body.get("financial_context")
    financial_context_text = format_user_financial_context(raw_financial_context)

    if not messages:
        return StreamingResponse(
            iter([sse({"type": "error", "message": "messages array required"})]),
            media_type="text/event-stream",
        )

    last_user_msg = next(
        (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
    )

    async def event_generator():
        event_queue: asyncio.Queue = asyncio.Queue()

        initial_state: CouncilState = {
            "messages": messages,
            "query": last_user_msg,
            "council_context": "",
            "responses": [],
            "skipped": [],
            "event_queue": event_queue,
            "financial_context": financial_context_text,
            "active_agents": [],
            "discussion_round": 0,
            "current_agent_index": 0,
        }

        async def run_graph_and_finish():
            """
            يُشغَّل كمهمة خلفية منفصلة: ينفّذ LangGraph عقدة تلو الأخرى،
            وكل عقدة تدفع أحداثها إلى event_queue فور توفرها (بث حي)،
            بدل انتظار انتهاء الرسم البياني كاملاً.
            """
            try:
                final_state = await council_graph.ainvoke(initial_state)

                # ✅ إذا لم يشارك أي وكيل — نجبر المستشار الأنسب (سلمان) على رد عام
                if not final_state["responses"]:
                    fallback_agent = dict(AGENTS[0])
                    fallback_agent["prompt"] = AGENTS[0]["prompt"] + """

إذا كان السؤال ليس في صميم تخصصك تماماً، أجب بشكل عام مفيد من منظورك المالي،
وأشر للمستخدم بأن يوجه سؤالاً أكثر تحديداً إذا أراد رأياً متخصصاً."""

                    await event_queue.put(sse({
                        "type":      "agent_start",
                        "agentId":   fallback_agent["id"],
                        "agentName": fallback_agent["shortName"],
                        "role":      fallback_agent["role"],
                        "color":     fallback_agent["color"],
                    }))

                    full_text = ""
                    rag_contexts = final_state.get("rag_contexts", {}) or {}
                    if fallback_agent["id"] in rag_contexts:
                        rag_context = rag_contexts[fallback_agent["id"]]
                    else:
                        rag_context = await retrieve_context(fallback_agent["id"], last_user_msg)

                    if rag_context:
                        sources = re.findall(r"\[المصدر (?:التخصصي|المشترك): (.*?)\]", rag_context)
                        unique_sources = list(dict.fromkeys(sources))
                        if unique_sources:
                            await event_queue.put(sse({
                                "type": "citations",
                                "agentId": fallback_agent["id"],
                                "sources": unique_sources
                            }))

                    async for token in stream_agent_response(
                        fallback_agent, messages, "", rag_context,
                        financial_context=financial_context_text,
                    ):
                        full_text += token
                        await event_queue.put(sse({"type": "token", "agentId": fallback_agent["id"], "token": token}))

                    await event_queue.put(sse({"type": "agent_done", "agentId": fallback_agent["id"]}))

                    for skipped in final_state["skipped"]:
                        if skipped["id"] != fallback_agent["id"]:
                            await event_queue.put(sse({"type": "agent_skipped", "agentId": skipped["id"], "agentName": skipped["shortName"]}))
                else:
                    for skipped in final_state["skipped"]:
                        await event_queue.put(sse({"type": "agent_skipped", "agentId": skipped["id"], "agentName": skipped["shortName"]}))

                await event_queue.put(sse({"type": "council_done"}))

            except Exception as e:
                err = str(e)
                if "connection" in err.lower() or "connect" in err.lower():
                    msg = f"❌ تعذّر الاتصال بخدمة Ollama المحلية على {OLLAMA_HOST}. تأكد أن Ollama يعمل (ollama serve)."
                elif "model" in err.lower() and "not found" in err.lower():
                    msg = f"❌ النموذج '{OLLAMA_MODEL}' غير مُنزَّل محلياً. نفّذ: ollama pull {OLLAMA_MODEL}"
                else:
                    msg = f"حدث خطأ في الاستدلال المحلي عبر Ollama: {err}"
                logger.error(msg)
                await event_queue.put(sse({"type": "error", "message": msg}))
            finally:
                await event_queue.put(None)  # إشارة نهاية البث

        graph_task = asyncio.create_task(run_graph_and_finish())

        try:
            while True:
                event = await event_queue.get()
                if event is None:
                    break
                yield event
        finally:
            if not graph_task.done():
                graph_task.cancel()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Health Check ─────────────────────────────────────────────────
@app.get("/api/health")
async def health():
    ollama_reachable = True
    local_models = []
    try:
        tags = await ollama_client.list()
        local_models = [m["model"] for m in tags.get("models", [])]
    except Exception:
        ollama_reachable = False

    return {
        "status":         "ok",
        "project":        "AMD Financial Council",
        "provider":       "Ollama (On-Premise)",
        "ollama_host":    OLLAMA_HOST,
        "model":          OLLAMA_MODEL,
        "ollama_reachable": ollama_reachable,
        "local_models":   local_models,
        "routing":        "sequential-dynamic (LangGraph)",
        "rag_status":     collection_status(),
    }


# ── Static Files ─────────────────────────────────────────────────
app.mount("/", StaticFiles(directory="public", html=True), name="static")


# ── تشغيل مباشر ─────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 3000))
    banner = f"""
╔══════════════════════════════════════════════════════╗
║   مجلس مستشار أمد للوعي المالي                      ║
║   هاكاثون أمد 2026 — Ollama + LangGraph Edition      ║
╠══════════════════════════════════════════════════════╣
║   Server  : http://localhost:{port}                     ║
║   Provider: Ollama (محلي بالكامل — On-Premise)        ║
║   Model   : {OLLAMA_MODEL[:40].ljust(40)} ║
║   Ollama  : {OLLAMA_HOST[:40].ljust(40)} ║
║   Routing : Sequential Dynamic (LangGraph)            ║
╚══════════════════════════════════════════════════════╝
    """
    try:
        print(banner)
    except Exception:
        print("--------------------------------------------------")
        print("  AMD Financial Council - On-Premise Edition")
        print(f"  Server  : http://localhost:{port}")
        print(f"  Model   : {OLLAMA_MODEL}")
        print("--------------------------------------------------")
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=True)
