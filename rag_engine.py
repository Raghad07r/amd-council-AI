# rag_engine.py — محرك الاسترجاع (Retrieval-Only)
# ══════════════════════════════════════════════════════════════════
# ملاحظة معمارية مهمة:
# هذا الملف "للقراءة فقط" — لا يقوم بأي تغذية أو تضمين جديد للبيانات.
# مهمة التغذية (Ingestion) منفصلة بالكامل في ملف ingest_data.py الذي
# يُشغّله المطور (Backend Admin) يدوياً على ملفات PDF محلية.
#
# أثناء المحادثة الفعلية مع المستخدم، هذا الملف فقط يقوم بـ:
#   1) تحويل سؤال المستخدم إلى Embedding عبر نموذج Ollama محلي.
#   2) البحث في مجموعة ChromaDB الخاصة بالوكيل المعني (مسار مستقل).
#   3) إرجاع أقرب المقاطع النصية كسياق (Context) يُغذّى لاحقاً لبرومبت
#      الوكيل في server.py قبل توليد الرد.
#
# لماذا "مسارات مجزأة" (Segregated Collections)؟
# لتجنّب تسرّب سياق مخطط الميزانية إلى إجابات محلل المخاطر مثلاً —
# كل وكيل "يرى" فقط قاعدة معرفته الخاصة، تماماً كما لو كان لديه
# مكتبة مرجعية منفصلة على مكتبه.
# ══════════════════════════════════════════════════════════════════

import os
import logging
from typing import Optional

import chromadb
import ollama

logger = logging.getLogger("rag_engine")

# ── إعدادات محلية بالكامل (لا اتصال إنترنت) ─────────────────────
CHROMA_DIR   = os.getenv("CHROMA_DIR", "chroma_store")          # مجلد تخزين ChromaDB على القرص
OLLAMA_HOST  = os.getenv("OLLAMA_HOST", "http://localhost:11434")
EMBED_MODEL  = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")  # يجب أن يطابق ما استُخدم في ingest_data.py
TOP_K        = int(os.getenv("RAG_TOP_K", "4"))                 # عدد المقاطع المسترجعة لكل سؤال

# ── خريطة: معرّف الوكيل → اسم مجموعة ChromaDB الخاصة به ─────────
# يجب أن تطابق تماماً الأسماء المستخدمة في ingest_data.py
AGENT_COLLECTIONS = {
    "planner":  "planner_docs",   # سلمان — المخطط المالي   → data/planner_docs
    "risk":     "risk_docs",      # نورة  — محللة المخاطر    → data/risk_docs
    "behavior": "behavior_docs",  # فهد   — خبير السلوك       → data/behavior_docs
    "shared":   "shared_docs",    # المستندات المشتركة لجميع الوكلاء → data/shared_docs
}

# ── عميل Ollama للتضمين (Embeddings) — يعمل محلياً فقط ──────────
_embed_client = ollama.AsyncClient(host=OLLAMA_HOST)

# ── عميل ChromaDB دائم (Persistent) على القرص المحلي ─────────────
_chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)

# كاش داخلي بسيط لتفادي إعادة فتح نفس المجموعة في كل استعلام
_collection_cache: dict = {}

# كاش لتضمين الأسئلة لتفادي إعادة حساب المتجهات لنفس السؤال في نفس الطلب
_query_embed_cache: dict = {}


def _get_collection(agent_id: str):
    """
    يرجع كائن مجموعة ChromaDB الخاصة بالوكيل، أو None إذا لم تُغذَّ
    بعد (أي أن المطور لم يشغّل ingest_data.py لهذا المسار بعد).
    هذا يضمن أن النظام لا ينهار إذا كانت إحدى القواعد فارغة مؤقتاً.
    """
    if agent_id in _collection_cache:
        return _collection_cache[agent_id]

    collection_name = AGENT_COLLECTIONS.get(agent_id)
    if not collection_name:
        return None

    try:
        collection = _chroma_client.get_collection(name=collection_name)
    except Exception:
        # المجموعة غير موجودة بعد — طبيعي قبل أول تشغيل لـ ingest_data.py
        logger.warning(
            f"⚠️  مجموعة RAG '{collection_name}' غير موجودة بعد. "
            f"شغّل ingest_data.py لتغذيتها بملفات PDF."
        )
        return None

    _collection_cache[agent_id] = collection
    return collection


async def _embed_query(text: str) -> list[float]:
    """يحوّل نص السؤال إلى متجه (Embedding) عبر نموذج Ollama محلي."""
    global _query_embed_cache
    if text in _query_embed_cache:
        logger.info(f"⚡ [RAG Cache] تم استخدام التضمين المخزن مسبقاً للسؤال: '{text[:40]}...'")
        return _query_embed_cache[text]

    logger.info(f"🧠 [Ollama Embeddings] جاري توليد متجه التضمين للسؤال: '{text[:40]}...'")
    response = await _embed_client.embeddings(model=EMBED_MODEL, prompt=text)
    embedding = response["embedding"]

    # حد أقصى لحجم الكاش لمنع استهلاك الذاكرة
    if len(_query_embed_cache) >= 50:
        _query_embed_cache.clear()

    _query_embed_cache[text] = embedding
    return embedding


async def retrieve_context(agent_id: str, query: str, top_k: int = TOP_K) -> Optional[str]:
    """
    نقطة الدخول الرئيسية التي يستدعيها server.py قبل توليد رد كل وكيل.

    الخطوات:
      1) تضمين سؤال المستخدم عبر Ollama محلياً.
      2) استرجاع أقرب المقاطع التخصصية من مجموعة الوكيل الخاصة.
      3) استرجاع أقرب المقاطع العامة من المجموعة المشتركة (shared_docs).
      4) دمج وتنسيق السياق الموحد.
    """
    try:
        query_embedding = await _embed_query(query)
    except Exception as e:
        logger.error(f"❌ فشل تضمين السؤال عبر Ollama ({EMBED_MODEL}): {e}")
        return ""

    formatted_chunks = []

    # 1. استرجاع من مجموعة الوكيل التخصصية
    collection = _get_collection(agent_id)
    if collection is not None:
        try:
            if collection.count() > 0:
                results = collection.query(
                    query_embeddings=[query_embedding],
                    n_results=min(top_k, collection.count()),
                )
                documents = results.get("documents", [[]])[0]
                metadatas = results.get("metadatas", [[]])[0]
                for doc, meta in zip(documents, metadatas):
                    source = (meta or {}).get("source", "مصدر داخلي غير محدد")
                    page = (meta or {}).get("page")
                    label = f"{source}" + (f" — صفحة {page}" if page is not None else "")
                    formatted_chunks.append(f"[المصدر التخصصي: {label}]\n{doc.strip()}")
        except Exception as e:
            logger.error(f"❌ فشل الاستعلام في مجموعة الوكيل التخصصية '{agent_id}': {e}")

    # 2. استرجاع من مجموعة المستندات المشتركة للمجلس
    shared_collection = _get_collection("shared")
    if shared_collection is not None:
        try:
            if shared_collection.count() > 0:
                # نأخذ عدداً مناسباً من المقاطع المشتركة (مثلاً نصف عدد المقاطع التخصصية، بحد أدنى 2)
                shared_top_k = max(top_k // 2, 2)
                shared_results = shared_collection.query(
                    query_embeddings=[query_embedding],
                    n_results=min(shared_top_k, shared_collection.count()),
                )
                shared_documents = shared_results.get("documents", [[]])[0]
                shared_metadatas = shared_results.get("metadatas", [[]])[0]
                for doc, meta in zip(shared_documents, shared_metadatas):
                    source = (meta or {}).get("source", "مصدر مشترك")
                    page = (meta or {}).get("page")
                    label = f"{source}" + (f" — صفحة {page}" if page is not None else "")
                    formatted_chunks.append(f"[المصدر المشترك: {label}]\n{doc.strip()}")
        except Exception as e:
            logger.error(f"❌ فشل الاستعلام في المجموعة المشتركة 'shared': {e}")

    if not formatted_chunks:
        return ""

    return "\n\n---\n\n".join(formatted_chunks)


def collection_status() -> dict:
    """
    تقرير سريع بحالة كل قاعدة معرفة (فارغة/مُغذاة) — يُستخدم في
    /api/health لمساعدة المطور على التأكد من نجاح عملية التغذية.
    """
    status = {}
    for agent_id, collection_name in AGENT_COLLECTIONS.items():
        try:
            collection = _chroma_client.get_collection(name=collection_name)
            status[agent_id] = {
                "collection": collection_name,
                "chunks": collection.count(),
                "ready": collection.count() > 0,
            }
        except Exception:
            status[agent_id] = {
                "collection": collection_name,
                "chunks": 0,
                "ready": False,
            }
    return status
