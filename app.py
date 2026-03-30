import os
import re
import logging
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv

from llama_index.core import VectorStoreIndex, Settings, StorageContext, Document
from llama_index.core.node_parser import SentenceSplitter
from llama_index.llms.groq import Groq
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
import pymupdf4llm
import chromadb
from llama_index.vector_stores.chroma import ChromaVectorStore

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# PATHS
# ---------------------------------------------------------------------------
UPLOAD_FOLDER = "uploads"
CHROMA_PATH   = "chroma_db"
CACHE_FOLDER  = "parsed_cache"

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['CHROMA_PATH']   = CHROMA_PATH
app.config['CACHE_FOLDER']  = CACHE_FOLDER

# ---------------------------------------------------------------------------
# AI STACK
# ---------------------------------------------------------------------------

# llama-3.1-8b-instant: preferred over larger models for two reasons.
# First, structured extraction from tables doesn't require deep reasoning,
# so the 8B model is accurate enough. Second, Groq's free tier caps daily
# tokens; the 8B has a higher limit and each query only consumes ~2500
# tokens (4 chunks × 512 + response), keeping us well within budget.
Settings.llm = Groq(model="llama-3.1-8b-instant", temperature=0)

# Multilingual-e5-small handles queries in any language against technical
# text without explicit translation, at a fraction of larger models' cost.
Settings.embed_model = HuggingFaceEmbedding(model_name="intfloat/multilingual-e5-small")

# chunk_size=512 produces specific, focused embeddings. Larger chunks create
# embeddings too generic to distinguish a table row from a section header.
# chunk_overlap=64 prevents values from being split across chunk boundaries.
# include_metadata prepends page_number to each chunk's embedding input;
# include_prev_next_rel links adjacent chunks, preserving table continuity.
Settings.text_splitter = SentenceSplitter(
    chunk_size=512,
    chunk_overlap=64,
    include_metadata=True,
    include_prev_next_rel=True,
)

# ---------------------------------------------------------------------------
# DATABASE & ENGINE CACHE
# ---------------------------------------------------------------------------
db = chromadb.PersistentClient(path=CHROMA_PATH)
document_engines: dict = {}


# ---------------------------------------------------------------------------
# PRE-PROCESSING
# ---------------------------------------------------------------------------

# Generic noise patterns common across most PDF documents:
#   - Page counters in "N/TOT" format (e.g. "9/115", "3/42")
#   - Revision/version stamps (e.g. "Rev 20", "v2.1", "Version 3")
#   - Short repeated headers (3–6 word bold phrases repeated on every page)
# Used by is_noise_page() to detect pages with no meaningful content.
_NOISE_PATTERNS = re.compile(
    r'\b\d+/\d+\b|'                    # page counter: "9/115"
    r'\bRev(?:ision)?\s*[\d.]+\b|'     # revision stamp: "Rev 20", "Revision 2.1"
    r'\bv(?:er(?:sion)?)?\s*[\d.]+\b|' # version stamp: "v2.1", "Version 3"
    r'^\*\*[^*\n]{3,40}\*\*\s*$',      # short bold header on its own line
    re.MULTILINE | re.IGNORECASE
)

# Section titles that typically introduce the key specifications summary
# on the cover page of a technical document. Ordered from most to least common.
_FEATURES_TITLES = re.compile(
    r'(#{1,3}\s*\*{0,2}'
    r'(?:Features|Key\s+Features|Highlights|Product\s+Overview|'
    r'Specifications?\s+Summary|Key\s+Specifications?|Overview)'
    r'\*{0,2}.*?)(?=\n#{1,3}\s|\Z)',
    re.DOTALL | re.IGNORECASE,
)


def remove_toc(text: str) -> str:
    """
    Strips Table of Contents entries from a page of PyMuPDF markdown output.

    PyMuPDF renders ToC entries as markdown table rows with dotted leaders:
        |**2**|**Description . . . . . . 9**|||
        ||2.2|Full compatibility . . . 13||
    If indexed, these rows produce embeddings similar to almost every
    technical query (they explicitly list section names like
    "Electrical Characteristics") causing systematic false positives.

    Three patterns are removed in order:
        1. Table rows containing dotted leaders followed by a page number.
        2. "List of tables" / "List of figures" section headers.
        3. Orphaned markdown separator rows (|---|---|) left after step 1.
    """
    cleaned = re.sub(r'^\|.*\.\s\.\s\..*\d+[^|]*\|.*$', '', text, flags=re.MULTILINE)
    cleaned = re.sub(r'(?:## )?\*\*List of (?:tables|figures)\*\*\s*\n', '', cleaned)
    cleaned = re.sub(r'^\|[-| :]+\|\s*\n', '', cleaned, flags=re.MULTILINE)
    return re.sub(r'\n{3,}', '\n\n', cleaned).strip()


def is_noise_page(text: str) -> bool:
    """
    Returns True if a page contains no meaningful technical content after
    ToC removal — i.e. only revision stamps, page counters, and headers.
    Threshold: fewer than 120 chars survive after stripping known noise patterns.
    """
    return len(_NOISE_PATTERNS.sub('', text).strip()) < 120


def extract_features_summary(page_text: str) -> str:
    """
    Extracts the key specifications section from the document cover page.

    Cover pages in technical documents are dense with critical specs but
    small (~500-800 tokens), so they may not appear in top-k retrieval for
    queries targeting specific sections deep in the document. Injecting the
    summary as metadata on every Document guarantees the LLM always has
    access to top-level specs regardless of which chunks are retrieved.

    Recognized section titles: Features, Key Features, Highlights,
    Product Overview, Specifications Summary, Key Specifications, Overview.

    Returns the first 1200 chars of the matched section, or empty string.
    """
    match = _FEATURES_TITLES.search(page_text)
    return match.group(1).strip()[:1200] if match else ""


def parse_md_to_documents(md_path: str, filename: str) -> list[Document]:
    """
    Converts a PyMuPDF-generated markdown file into a list of LlamaIndex
    Documents, one per physical PDF page.

    The .md uses '\n---\n' as the page separator (pymupdf4llm standard).
    For each page: apply remove_toc(), discard noise-only pages, then
    create a Document with page_number (1-based) and features_summary
    in the metadata.

    An additional "anchor" Document is prepended containing only the
    features summary with an explicit section tag. This ensures that
    general spec queries always retrieve it with a high similarity score,
    even when cover-page chunks aren't in top-k.
    """
    with open(md_path, "r", encoding="utf-8") as f:
        raw_content = f.read()

    raw_pages = raw_content.split('\n---\n')
    log.info(f"Parsing {md_path}: {len(raw_pages)} pages found.")

    features_summary = extract_features_summary(raw_pages[0] if raw_pages else "")
    if not features_summary:
        log.warning("Features summary not found on page 0.")

    documents = []

    if features_summary:
        documents.append(Document(
            text=f"DOCUMENT FEATURES SUMMARY — page 1 of {filename}:\n\n{features_summary}",
            metadata={
                "filename":    filename,
                "page_number": 1,
                "section":     "features_cover_page",
            },
        ))

    discarded = 0
    for idx, raw_page in enumerate(raw_pages):
        cleaned = remove_toc(raw_page)

        # The cover page (idx=0) is never discarded: it holds the key specs.
        if idx > 0 and is_noise_page(cleaned):
            discarded += 1
            continue

        documents.append(Document(
            text=cleaned,
            metadata={
                "filename":         filename,
                "page_number":      idx + 1,  # convert 0-based index → 1-based page number
                "features_summary": features_summary,
            },
            # Exclude features_summary from embedding: it's identical across
            # all chunks, so including it would flatten similarity scores.
            excluded_embed_metadata_keys=["features_summary"],
        ))

    log.info(f"Documents created: {len(documents)} ({discarded} noise pages discarded).")
    return documents


def parse_pdf_to_documents(pdf_path: str, filename: str) -> list[Document]:
    """
    Fallback path: parses directly from the PDF when no .md cache exists.
    Persists the result to parsed_cache/ as '\n---\n'-separated markdown,
    then delegates to parse_md_to_documents to keep a single processing path.
    """
    log.info(f"Parsing PDF with PyMuPDF: {pdf_path}")
    pages = pymupdf4llm.to_markdown(pdf_path, page_chunks=True)

    md_cache_path = os.path.join(app.config['CACHE_FOLDER'], filename + ".md")
    with open(md_cache_path, "w", encoding="utf-8") as f:
        f.write("\n---\n".join(p.get("text", "") for p in pages))
    log.info(f"Markdown cache saved: {md_cache_path}")

    return parse_md_to_documents(md_cache_path, filename)


# ---------------------------------------------------------------------------
# CHAT ENGINE
# ---------------------------------------------------------------------------

def get_chat_engine(filename: str):
    if filename in document_engines:
        return document_engines[filename]

    log.info(f"Building chat engine for: {filename}")
    chroma_collection = db.get_or_create_collection(filename.replace(".", "_"))
    vector_store      = ChromaVectorStore(chroma_collection=chroma_collection)
    index             = VectorStoreIndex.from_vector_store(vector_store)

    # top_k=4 retrieves ~2048 tokens of context per query. Raising it beyond
    # this risks hitting Groq's daily token limit with diminishing returns,
    # since ToC removal and granular chunking already ensure high-precision hits.
    chat_engine = index.as_chat_engine(
        chat_mode="context",
        similarity_top_k=4,
        system_prompt=(
            "You are a technical assistant specialized in analyzing complex PDF documents "
            "such as datasheets, manuals, and technical specifications.\n"
            "Your goal is to extract data and specifications with precision and accuracy.\n\n"
            "Each retrieved chunk includes:\n"
            "  - Page text, which may contain tables in markdown format.\n"
            "  - 'page_number' metadata: the page number in the original PDF.\n"
            "  - 'features_summary' metadata: a summary of the document's key specifications, "
            "    extracted from the cover page.\n\n"
            "Rules to follow:\n"
            "1. CITE THE PAGE: Always indicate the page number for every value you report "
            "   (e.g. 'Table 13, page 40').\n"
            "2. TABLES FIRST: Look for values in markdown tables and report them exactly "
            "   as written, including units of measure.\n"
            "3. USE FEATURES SUMMARY: For general top-level specifications, use the "
            "   'features_summary' metadata if the retrieved chunk doesn't contain the data.\n"
            "4. DISTINGUISH OPERATING REGIMES when present:\n"
            "   - 'Absolute Maximum Ratings': hard limits — never to be exceeded.\n"
            "   - 'Operating Conditions': guaranteed values under normal operation.\n"
            "   - 'Typical values': measured under specific conditions — not guaranteed.\n"
            "5. MATCH EXACT CONDITIONS: If the query specifies a condition (frequency, "
            "   temperature, load, mode), find the table row that matches exactly.\n"
            "6. HONESTY: If data is absent or ambiguous, say so clearly. Never fabricate values."
        ),
    )

    document_engines[filename] = chat_engine
    return chat_engine


# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------

@app.route('/')
def home():
    existing_files = [c.name.replace("_", ".") for c in db.list_collections()]
    return render_template('index.html', existing_files=existing_files)


@app.route('/health')
def health():
    return jsonify({"status": "ok"})


@app.route('/upload', methods=['POST'])
def upload():
    file = request.files.get('file')
    if not file or file.filename == '':
        return jsonify({"error": "Invalid file."}), 400

    filename        = file.filename
    pdf_path        = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    md_cache_path   = os.path.join(app.config['CACHE_FOLDER'], filename + ".md")
    collection_name = filename.replace(".", "_")

    file.save(pdf_path)

    try:
        # Level 1 — already in ChromaDB: return immediately.
        if collection_name in [c.name for c in db.list_collections()]:
            return jsonify({
                "status":  "success",
                "message": "Document already indexed. Upload skipped.",
                "files":   [c.name.replace("_", ".") for c in db.list_collections()],
            })

        # Level 2 — .md cache exists: skip PDF parsing.
        documents = None
        if os.path.exists(md_cache_path):
            with open(md_cache_path, "r", encoding="utf-8") as f:
                cache_size = len(f.read())
            if cache_size > 5000:
                log.info(f"Valid .md cache found ({cache_size} chars).")
                documents = parse_md_to_documents(md_cache_path, filename)
            else:
                log.warning(f".md cache too short ({cache_size} chars), ignored.")

        # Level 3 — parse from the original PDF.
        if documents is None:
            documents = parse_pdf_to_documents(pdf_path, filename)

        if not documents:
            return jsonify({"error": "No text could be extracted from the document."}), 500

        chroma_collection = db.get_or_create_collection(collection_name)
        vector_store      = ChromaVectorStore(chroma_collection=chroma_collection)
        storage_context   = StorageContext.from_defaults(vector_store=vector_store)
        VectorStoreIndex.from_documents(documents, storage_context=storage_context)
        log.info(f"Indexed {len(documents)} documents for '{filename}'.")

        document_engines.pop(filename, None)

        return jsonify({
            "status": "success",
            "files":  [c.name.replace("_", ".") for c in db.list_collections()],
        })

    except Exception as e:
        log.exception("Error during upload/indexing.")
        return jsonify({"error": str(e)}), 500


@app.route('/reindex', methods=['POST'])
def reindex():
    """
    Forces re-indexing of an already-indexed document.

    Required after changes to chunk_size, chunk_overlap, embed_model,
    remove_toc(), or parse_md_to_documents(). Not required for changes
    to LLM, temperature, top_k, or system_prompt.

    Payload: { "filename": "file.pdf", "invalidate_cache": false }
    If invalidate_cache is true, the .md cache is also deleted,
    forcing a full re-parse from the original PDF on the next upload.
    """
    data     = request.json or {}
    filename = data.get("filename", "").strip()

    if not filename:
        return jsonify({"error": "'filename' field is required."}), 400

    collection_name = filename.replace(".", "_")

    try:
        db.delete_collection(collection_name)
        log.info(f"Collection '{collection_name}' deleted.")
    except Exception as e:
        log.warning(f"Could not delete collection '{collection_name}': {e}")

    document_engines.pop(filename, None)

    if data.get("invalidate_cache", False):
        md_path = os.path.join(app.config['CACHE_FOLDER'], filename + ".md")
        if os.path.exists(md_path):
            os.remove(md_path)
            log.info(f"Cache deleted: {md_path}")

    return jsonify({
        "status":  "ok",
        "message": f"'{filename}' removed from index. Re-upload to re-index.",
    })


@app.route('/ask', methods=['POST'])
def ask():
    data         = request.json or {}
    user_query   = data.get("message", "").strip()
    selected_doc = data.get("document", "").strip()

    if not user_query:
        return jsonify({"error": "'message' field is required."}), 400
    if not selected_doc:
        return jsonify({"error": "'document' field is required."}), 400

    try:
        response = get_chat_engine(selected_doc).chat(user_query)
        return jsonify({"response": str(response)})
    except Exception as e:
        log.exception("Error during query.")
        return jsonify({"response": f"Error: {str(e)}"}), 500


@app.route('/documents', methods=['GET'])
def list_documents():
    return jsonify({"documents": [c.name.replace("_", ".") for c in db.list_collections()]})


# ---------------------------------------------------------------------------
# ENTRYPOINT
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(CACHE_FOLDER,  exist_ok=True)
    app.run(host="0.0.0.0", port=5000, debug=False)
