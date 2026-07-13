# ingest_data.py — سكريبت تغذية قاعدة المعرفة (RAG) — للمطور فقط
# ══════════════════════════════════════════════════════════════════
# ⚠️ هذا سكريبت Offline/Admin مستقل تماماً عن دورة حياة الدردشة.
#    المستخدم النهائي لا يرفع ملفات إطلاقاً؛ فقط المطور (Backend Admin)
#    يشغّل هذا الملف يدوياً كلما توفرت مستندات PDF جديدة يريد إضافتها
#    لمعرفة أحد الوكلاء.
#
# الخط الزمني للبيانات:
#   PDF محلي (data/<agent>_docs/*.pdf)
#        └─▶ تقسيم نصي (RecursiveCharacterTextSplitter)
#              └─▶ تضمين محلي (OllamaEmbeddings — nomic-embed-text)
#                    └─▶ حفظ في ChromaDB (مجموعة مستقلة لكل وكيل)
#                          └─▶ يقرأها rag_engine.py لاحقاً أثناء المحادثة
#
# طريقة التشغيل:
#   python ingest_data.py                     # يغذي كل المسارات الثلاثة
#   python ingest_data.py --agent risk         # يغذي مسار نورة فقط
#   python ingest_data.py --agent planner --reset   # يمسح المجموعة ثم يعيد تغذيتها
#
# ملاحظة تبعيات: يتطلب حزم إضافية غير موجودة في requirements.txt الأساسي:
#   pip install langchain-community pypdf langchain-ollama
# (سيتم توضيح ذلك عند تسليم requirements.txt النهائي)
# ══════════════════════════════════════════════════════════════════

import os
import glob
import time
import argparse
from pathlib import Path

# ── تقسيم النصوص (متوافق مع إصدارات LangChain الحديثة والقديمة) ──
try:
    from langchain_text_splitters import RecursiveCharacterTextSplitter
except ImportError:
    from langchain.text_splitter import RecursiveCharacterTextSplitter

# ── تحميل ملفات PDF ────────────────────────────────────────────
try:
    from langchain_community.document_loaders import PyPDFLoader
except ImportError:
    from langchain.document_loaders import PyPDFLoader

# ── التضمين المحلي عبر Ollama (nomic-embed-text) ─────────────────
try:
    from langchain_ollama import OllamaEmbeddings
except ImportError:
    from langchain_community.embeddings import OllamaEmbeddings

import chromadb


# ══════════════════════════════════════════════════════════════════
# إعدادات — يجب أن تُطابق تماماً القيم المستخدمة في rag_engine.py
# ══════════════════════════════════════════════════════════════════
CHROMA_DIR   = os.getenv("CHROMA_DIR", "chroma_store")
OLLAMA_HOST  = os.getenv("OLLAMA_HOST", "http://localhost:11434")
EMBED_MODEL  = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")

CHUNK_SIZE    = int(os.getenv("RAG_CHUNK_SIZE", "1000"))
CHUNK_OVERLAP = int(os.getenv("RAG_CHUNK_OVERLAP", "150"))
EMBED_BATCH   = 32  # عدد المقاطع المرسلة دفعة واحدة لـ Ollama لتفادي إبطاء الاستدلال

# مجلدات مصدر ملفات PDF الخام (يضع المطور الملفات هنا يدوياً)
AGENT_SOURCE_DIRS = {
    "planner":  "data/planner_docs",
    "risk":     "data/risk_docs",
    "behavior": "data/behavior_docs",
    "shared":   "data/shared_docs",
}

# أسماء مجموعات ChromaDB — مطابقة حرفياً لـ AGENT_COLLECTIONS في rag_engine.py
AGENT_COLLECTIONS = {
    "planner":  "planner_docs",
    "risk":     "risk_docs",
    "behavior": "behavior_docs",
    "shared":   "shared_docs",
}

AGENT_LABELS = {
    "planner":  "سلمان — المخطط المالي",
    "risk":     "نورة — محللة المخاطر",
    "behavior": "فهد — خبير السلوك",
    "shared":   "المستندات المشتركة للمجلس",
}


def log(msg: str):
    import sys
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        print(msg.encode(encoding, errors="replace").decode(encoding), flush=True)


# ── الخطوة 1: قراءة وتقسيم ملفات PDF لمسار وكيل واحد ─────────────
def load_and_split_pdfs(source_dir: str) -> list:
    pdf_paths = sorted(glob.glob(os.path.join(source_dir, "*.pdf")))

    if not pdf_paths:
        log(f"   ⚠️  لا توجد ملفات PDF في: {source_dir}")
        return []

    log(f"   🔎 تم العثور على {len(pdf_paths)} ملف PDF")

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", "۔", "؟", ".", "،", " ", ""],  # فواصل تراعي العربية أيضاً
    )

    all_chunks = []
    for pdf_path in pdf_paths:
        filename = os.path.basename(pdf_path)
        log(f"   📄 قراءة: {filename}")

        try:
            loader = PyPDFLoader(pdf_path)
            pages = loader.load()
        except Exception as e:
            log(f"      ❌ تعذّرت قراءة {filename}: {e}")
            continue

        if not pages:
            log(f"      ⚠️  الملف فارغ أو غير قابل للاستخراج: {filename}")
            continue

        chunks = splitter.split_documents(pages)
        for chunk in chunks:
            # نوحّد بيانات المصدر ليستخدمها rag_engine.py عند الاستشهاد
            chunk.metadata["source"] = filename
            chunk.metadata["page"] = chunk.metadata.get("page", 0)
        all_chunks.extend(chunks)

        log(f"      ✅ {len(chunks)} مقطعاً نصياً")

    return all_chunks


# ── الخطوة 2: التضمين المحلي والحفظ في ChromaDB ──────────────────
def embed_and_store(
    agent_id: str,
    chunks: list,
    embedder: "OllamaEmbeddings",
    chroma_client: "chromadb.PersistentClient",
    reset: bool,
) -> int:
    collection_name = AGENT_COLLECTIONS[agent_id]

    if reset:
        try:
            chroma_client.delete_collection(collection_name)
            log(f"   🗑️  تم مسح المجموعة القديمة: {collection_name}")
        except Exception:
            pass  # المجموعة لم تكن موجودة أصلاً — لا مشكلة

    collection = chroma_client.get_or_create_collection(
        name=collection_name,
        metadata={"hnsw:space": "cosine"}
    )

    if not chunks:
        log(f"   ⏭️  تخطي التضمين — لا توجد مقاطع جديدة لمجموعة '{collection_name}'")
        return 0

    import hashlib

    texts = [c.page_content for c in chunks]
    metadatas = [
        {"source": c.metadata.get("source", "غير معروف"), "page": c.metadata.get("page", 0)}
        for c in chunks
    ]

    # توليد بصمة فريدة (Hash) لكل مقطع لمنع تكرار البيانات (Deduplication)
    ids = []
    for text, meta in zip(texts, metadatas):
        hasher = hashlib.sha256()
        hasher.update(text.encode('utf-8'))
        hasher.update(meta["source"].encode('utf-8'))
        hasher.update(str(meta["page"]).encode('utf-8'))
        ids.append(f"chunk_{hasher.hexdigest()}")

    total = len(texts)
    for start in range(0, total, EMBED_BATCH):
        end = min(start + EMBED_BATCH, total)
        batch_texts = texts[start:end]

        log(f"   🧠 تضمين المقاطع {start + 1}–{end} من {total} عبر Ollama ({EMBED_MODEL}) ...")
        try:
            batch_embeddings = embedder.embed_documents(batch_texts)
        except Exception as e:
            log(f"      ❌ فشل التضمين عبر Ollama: {e}")
            log(f"      تأكد أن Ollama يعمل وأن النموذج منزّل: ollama pull {EMBED_MODEL}")
            raise

        # نستخدم upsert بدلاً من add لضمان تحديث أو تخطي المقاطع مكررة المعرف دون التسبب بأخطاء
        collection.upsert(
            ids=ids[start:end],
            documents=batch_texts,
            embeddings=batch_embeddings,
            metadatas=metadatas[start:end],
        )

    return total


# ── نقطة الدخول الرئيسية ─────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="سكريبت تغذية قاعدة معرفة RAG لمجلس مستشار أمد للوعي المالي"
    )
    parser.add_argument(
        "--agent",
        choices=["planner", "risk", "behavior", "shared", "all"],
        default="all",
        help="حدد مسار وكيل واحد فقط لتغذيته، أو اترك all لتغذية الجميع",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="امسح المجموعة الحالية قبل إعادة التغذية (بدل الإضافة التراكمية)",
    )
    args = parser.parse_args()

    targets = list(AGENT_SOURCE_DIRS.keys()) if args.agent == "all" else [args.agent]

    log("=" * 62)
    log("   سكريبت تغذية قاعدة المعرفة (RAG) — مجلس مستشار أمد للوعي المالي")
    log("   ⚠️  سكريبت مطوّر (Offline/Admin) — لا علاقة له بواجهة الدردشة")
    log("=" * 62)
    log(f"   Ollama Host    : {OLLAMA_HOST}")
    log(f"   نموذج التضمين  : {EMBED_MODEL}")
    log(f"   مسار التخزين   : {CHROMA_DIR}")
    log(f"   حجم المقطع     : {CHUNK_SIZE} حرف (تداخل {CHUNK_OVERLAP})")
    log(f"   الوضع          : {'مسح وإعادة تغذية (--reset)' if args.reset else 'إضافة تراكمية'}")
    log("=" * 62)

    # تأكد من وجود مجلدات المصدر حتى لو فارغة (لسهولة استخدام المطور)
    for source_dir in AGENT_SOURCE_DIRS.values():
        Path(source_dir).mkdir(parents=True, exist_ok=True)

    embedder = OllamaEmbeddings(model=EMBED_MODEL, base_url=OLLAMA_HOST)
    chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)

    start_time = time.time()
    grand_total = 0
    per_agent_summary = {}

    for agent_id in targets:
        label = AGENT_LABELS[agent_id]
        source_dir = AGENT_SOURCE_DIRS[agent_id]

        log(f"\n▶ معالجة مسار: {label}")
        log(f"  المجلد المصدر: {source_dir}")

        try:
            chunks = load_and_split_pdfs(source_dir)
            count = embed_and_store(agent_id, chunks, embedder, chroma_client, reset=args.reset)
        except Exception as e:
            log(f"  ❌ توقفت معالجة مسار {label} بسبب خطأ: {e}")
            per_agent_summary[agent_id] = 0
            continue

        grand_total += count
        per_agent_summary[agent_id] = count
        log(f"  ✅ تم حفظ {count} مقطعاً جديداً في مجموعة '{AGENT_COLLECTIONS[agent_id]}'")

    elapsed = time.time() - start_time

    log("\n" + "=" * 62)
    log("   ملخص التغذية النهائي")
    log("=" * 62)
    for agent_id, count in per_agent_summary.items():
        log(f"   • {AGENT_LABELS[agent_id]:<28}: {count} مقطعاً")
    log(f"   الإجمالي: {grand_total} مقطعاً خلال {elapsed:.1f} ثانية")
    log("=" * 62)
    log("   🎉 يمكنك الآن تشغيل server.py — سيسترجع الوكلاء من هذه البيانات تلقائياً.")
    log("=" * 62)


if __name__ == "__main__":
    main()
