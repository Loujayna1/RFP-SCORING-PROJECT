"""
Milvus vector store — the correct RAG backbone.

Collections (one per logical scope):
  rfp_chunks:{rfp_id}     — all raw document chunks from the RFP package
  vendor_chunks:{rfp_id}  — all raw document chunks from all vendor responses
  requirements:{rfp_id}   — structured requirement descriptions
  claims:{rfp_id}         — structured vendor claims (one per response_id)
  cv_skills:{rfp_id}      — team member skills/certs extracted via OCR

Embedding model: sentence-transformers/all-MiniLM-L12-v2
  - 384 dimensions
  - Trained on 1B+ sentence pairs — excellent for domain text
  - CPU-only, ~120MB, no GPU required
  - Significantly better than L6-v2 on retrieval benchmarks

Milvus runs in Docker (see docker-compose.yml).
Connection: MILVUS_HOST / MILVUS_PORT env vars.

Usage:
  from app.services.vector_store import VectorStore
  vs = VectorStore()
  await vs.connect()
  vs.upsert("requirements:rfp-123", "req-uuid", "data encryption at rest", {...})
  results = vs.search("claims:rfp-123:resp-456", "AES-256 encryption", top_k=5)
"""

import os
import json
import logging
import uuid
import threading
from typing import Optional, Callable

logger = logging.getLogger(__name__)

# ── Embedding model (loaded once at startup) ───────────────────────────────────
_embed_model = None
_embed_lock = threading.Lock()

def _get_embed_model():
    global _embed_model
    if _embed_model is None:
        with _embed_lock:
            if _embed_model is None:
                from sentence_transformers import SentenceTransformer
                logger.info("Loading all-MiniLM-L12-v2 embedding model on demand (~120MB, cached on disk)...")
                _embed_model = SentenceTransformer("sentence-transformers/all-MiniLM-L12-v2")
                logger.info("Embedding model ready. Dimension: 384")
    return _embed_model

EMBED_DIM = 384

def embed_one(text: str) -> list[float]:
    model = _get_embed_model()
    vec = model.encode(text, normalize_embeddings=True)
    return vec.tolist()

def embed_batch(texts: list[str]) -> list[list[float]]:
    """Batch embed — much faster than embed_one in a loop."""
    model = _get_embed_model()
    vecs = model.encode(
        texts,
        normalize_embeddings=True,
        batch_size=64,
        show_progress_bar=False,
    )
    return [v.tolist() for v in vecs]


# ── BM25 Sparse Embeddings ───────────────────────────────────────────────────
_bm25_cache = {}
_bm25_lock = threading.Lock()

_BM25_DIR = os.path.join(os.path.dirname(__file__), "bm25_models")
_BM25_S3_PREFIX = "bm25_models"


def _bm25_s3_client():
    """S3/MinIO client + bucket for shared BM25 storage, or (None, None)."""
    try:
        import boto3
        from app.core.config import settings
        client = boto3.client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT,
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            region_name="us-east-1",
        )
        return client, settings.S3_BUCKET
    except Exception as e:
        logger.warning(f"BM25 S3 storage unavailable, using local disk only: {e}")
        return None, None


def _bm25_download_from_s3(collection_name: str, file_path: str) -> bool:
    """Fetch a shared BM25 model from S3 into file_path. False if absent/unavailable."""
    client, bucket = _bm25_s3_client()
    if not client:
        return False
    try:
        client.download_file(bucket, f"{_BM25_S3_PREFIX}/{collection_name}.json", file_path)
        return True
    except Exception:
        return False  # not yet in S3 (first fit) or transient error → caller falls back


def _bm25_upload_to_s3(collection_name: str, file_path: str) -> None:
    """Publish a freshly fitted BM25 model to shared S3 storage so every
    container (API + worker) reads the same model."""
    client, bucket = _bm25_s3_client()
    if not client:
        return
    try:
        try:
            client.head_bucket(Bucket=bucket)
        except Exception:
            client.create_bucket(Bucket=bucket)
        client.upload_file(file_path, bucket, f"{_BM25_S3_PREFIX}/{collection_name}.json")
    except Exception as e:
        logger.error(f"Failed to upload BM25 model {collection_name} to S3: {e}")


# Collections whose passages are searched with requirement texts as queries.
# They share ONE BM25 model per RFP, so that the passages and the queries are
# always encoded with the same vocabulary and term statistics.
_BM25_SHARED_PREFIXES = ("requirements_", "vendor_chunks_", "claims_")
_bm25_placeholders: set = set()


def _bm25_key(collection_name: str) -> str:
    """Name under which the BM25 model of a collection is stored.

    requirements_<rfp>, vendor_chunks_<rfp> and claims_<rfp> all map to
    rfp_shared_<rfp>; any other collection keeps its own model.
    """
    for prefix in _BM25_SHARED_PREFIXES:
        if collection_name.startswith(prefix):
            return "rfp_shared_" + collection_name[len(prefix):]
    return collection_name


def _get_bm25_model(collection_name: str, fit_texts: list[str] = None):
    """Get the BM25 model of a collection, fitting it once if it does not exist.

    A BM25 model assigns each word a position in the sparse vector and an IDF
    weight computed on the texts it was fitted on. Passages and queries must be
    encoded with the SAME fitted model, otherwise their sparse vectors do not
    line up. The model was previously refitted on every indexed file and
    replaced the previous one, so only the last file indexed matched the model
    used for queries.

    Now a model is fitted ONCE and then frozen:
      * the requirements, vendor passages and claims of an RFP share one model,
        fitted on the RFP's requirements, which are indexed in a single batch
        before any vendor response is uploaded. Since the evaluation queries are
        the requirement texts, every query word is in this vocabulary; words that
        appear only in vendor documents could not match a query anyway;
      * any other collection (e.g. RFP document chunks) keeps the first model
        fitted for it, so later files are encoded consistently with it.
    Later calls with ``fit_texts`` reuse the frozen model instead of refitting.

    Models are shared across containers via S3/MinIO (the source of truth), with
    local disk used as a per-process cache. If S3 is unavailable the function
    degrades to local disk.
    """
    global _bm25_cache
    key = _bm25_key(collection_name)
    with _bm25_lock:
        if key in _bm25_cache and key not in _bm25_placeholders:
            return _bm25_cache[key]

        from pymilvus.model.sparse import BM25EmbeddingFunction
        from pymilvus.model.sparse.bm25.tokenizers import build_default_analyzer

        analyzer = build_default_analyzer(language="en")
        bm25 = BM25EmbeddingFunction(analyzer)

        os.makedirs(_BM25_DIR, exist_ok=True)
        file_path = os.path.join(_BM25_DIR, f"{key}.json")

        # 1. An existing model is reused, never refitted.
        loaded = False
        if _bm25_download_from_s3(key, file_path):
            try:
                bm25.load(file_path)
                loaded = True
            except Exception as e:
                logger.error(f"Failed to load BM25 (S3) for {key}: {e}")
        if not loaded and os.path.exists(file_path):
            try:
                bm25.load(file_path)
                loaded = True
            except Exception as e:
                logger.error(f"Failed to load BM25 (local) for {key}: {e}")
        if loaded:
            _bm25_cache[key] = bm25
            _bm25_placeholders.discard(key)
            return bm25

        # 2. No model yet: fit it once on these texts and publish it.
        if fit_texts:
            logger.info(f"Fitting BM25 model {key} once on {len(fit_texts)} texts (frozen afterwards)")
            bm25.fit(fit_texts)
            try:
                bm25.save(file_path)
                _bm25_upload_to_s3(key, file_path)
            except Exception as e:
                logger.error(f"Failed to save BM25 model {key}: {e}")
            _bm25_cache[key] = bm25
            _bm25_placeholders.discard(key)
            return bm25

        # 3. Queried before anything was indexed: temporary placeholder, not
        #    saved, replaced by the first real fit.
        logger.warning(f"Initializing temporary BM25 placeholder for {key}")
        bm25.fit(["placeholder default text to initialize bm25 model"])
        _bm25_cache[key] = bm25
        _bm25_placeholders.add(key)
        return bm25


def _clamp_sparse(rows: list[dict[int, float]]) -> list[dict[int, float]]:
    """
    Drop non-positive weights from each sparse row.

    BM25 IDF is log((N - df + 0.5)/(df + 0.5)), which is negative for any term
    occurring in more than ~half of a small corpus. Milvus' sparse index rejects
    negative values outright ("Invalid sparse row: negative value"), failing the
    whole search. A term with <= 0 weight carries no useful ranking signal, so we
    drop it. An empty row falls back to {0: 0.0} so the search still runs.
    """
    cleaned = []
    for row in rows:
        kept = {int(k): float(v) for k, v in row.items() if float(v) > 0.0}
        cleaned.append(kept or {0: 0.0})
    return cleaned


def _sparse_query_data(encoded_query) -> list[dict[int, float]]:
    """Normalize BM25 query output for Milvus sparse-vector search."""
    sparse_vectors = getattr(encoded_query, "sparse_vectors", None)
    if sparse_vectors is not None:
        return _clamp_sparse(sparse_vectors)

    if hasattr(encoded_query, "tocsr"):
        csr = encoded_query.tocsr()
        if csr.shape[0] == 0:
            return [{0: 0.0}]
        start, end = csr.indptr[0], csr.indptr[1]
        return _clamp_sparse([
            {
                int(col): float(val)
                for col, val in zip(csr.indices[start:end], csr.data[start:end])
            }
        ])

    if isinstance(encoded_query, list):
        return _clamp_sparse(encoded_query)

    raise TypeError(f"Unsupported BM25 query sparse output: {type(encoded_query)!r}")


# ── Milvus helpers ─────────────────────────────────────────────────────────────
def _collection_schema():
    """Build the standard schema used for every collection."""
    from pymilvus import CollectionSchema, FieldSchema, DataType
    fields = [
        FieldSchema(name="pk",            dtype=DataType.VARCHAR,       max_length=64, is_primary=True),
        FieldSchema(name="text",          dtype=DataType.VARCHAR,       max_length=32768),
        FieldSchema(name="rfp_id",        dtype=DataType.VARCHAR,       max_length=64),
        FieldSchema(name="response_id",   dtype=DataType.VARCHAR,       max_length=64),
        FieldSchema(name="vendor_name",   dtype=DataType.VARCHAR,       max_length=256),
        FieldSchema(name="file_name",     dtype=DataType.VARCHAR,       max_length=512),
        FieldSchema(name="page_number",   dtype=DataType.INT64),
        FieldSchema(name="doc_role",      dtype=DataType.VARCHAR,       max_length=64),
        FieldSchema(name="section",       dtype=DataType.VARCHAR,       max_length=512),
        FieldSchema(name="deep_link",     dtype=DataType.VARCHAR,       max_length=1024),
        FieldSchema(name="extra",         dtype=DataType.VARCHAR,       max_length=8192),  # JSON blob for extra metadata
        FieldSchema(name="embedding",     dtype=DataType.FLOAT_VECTOR,  dim=EMBED_DIM),
        FieldSchema(name="sparse_vector",  dtype=DataType.SPARSE_FLOAT_VECTOR),
        FieldSchema(name="parent_id",     dtype=DataType.VARCHAR,       max_length=64),
        FieldSchema(name="chunk_type",    dtype=DataType.VARCHAR,       max_length=64),
    ]
    return CollectionSchema(fields, description="RFP Evaluator chunk store")

INDEX_PARAMS = {
    "metric_type": "COSINE",
    "index_type":  "IVF_FLAT",
    "params":      {"nlist": 128},
}
SPARSE_INDEX_PARAMS = {
    "metric_type": "IP",
    "index_type": "SPARSE_INVERTED_INDEX",
    "params": {"drop_ratio_build": 0.2}
}
SEARCH_PARAMS = {"metric_type": "COSINE", "params": {"nprobe": 16}}


def _truncate_utf8(value: str, max_bytes: int) -> str:
    """Trim text to a Milvus VARCHAR byte limit without splitting UTF-8."""
    raw = str(value).encode("utf-8")
    if len(raw) <= max_bytes:
        return str(value)
    return raw[:max_bytes].decode("utf-8", errors="ignore")


# ── VectorStore class ─────────────────────────────────────────────────────────
class VectorStore:
    """
    Thin async-friendly wrapper around pymilvus.

    All heavy IO (connect, collection creation) is done in `connect()`.
    Call `await vs.connect()` once at FastAPI startup.
    """

    def __init__(self, host: str = "milvus", port: int = 19530):
        self.host = host
        self.port = port
        self._collections: dict[str, object] = {}  # collection_name -> Collection
        self._collections_lock = threading.RLock()
        self._connected = False

    async def connect(self):
        from pymilvus import connections
        try:
            connections.connect("default", host=self.host, port=self.port)
            self._connected = True
            logger.info(f"Connected to Milvus at {self.host}:{self.port}")
        except Exception as e:
            logger.error(f"Could not connect to Milvus: {e}. "
                         "Make sure milvus is running: docker compose up milvus -d")
            raise

    def _get_or_create_collection(self, name: str):
        """Return cached collection or create it if it doesn't exist."""
        from pymilvus import Collection, utility

        with self._collections_lock:
            cached = self._collections.get(name)
            if cached is not None:
                if utility.has_collection(name):
                    try:
                        has_sparse = any(f.name == "sparse_vector" for f in cached.schema.fields)
                        if has_sparse:
                            return cached
                    except Exception:
                        pass
                self._collections.pop(name, None)

            if utility.has_collection(name):
                col = Collection(name)
                has_sparse = any(f.name == "sparse_vector" for f in col.schema.fields)
                if not has_sparse:
                    logger.warning("Milvus collection %s exists with old schema; dropping and recreating", name)
                    col.drop()
                    schema = _collection_schema()
                    col = Collection(name, schema)
                    col.create_index("embedding", INDEX_PARAMS)
                    col.create_index("sparse_vector", SPARSE_INDEX_PARAMS)
                    col.load()
                else:
                    has_dense_idx = any(idx.field_name == "embedding" for idx in col.indexes)
                    has_sparse_idx = any(idx.field_name == "sparse_vector" for idx in col.indexes)
                    if not has_dense_idx:
                        col.create_index("embedding", INDEX_PARAMS)
                    if not has_sparse_idx:
                        col.create_index("sparse_vector", SPARSE_INDEX_PARAMS)
                    try:
                        col.load()
                    except Exception as exc:
                        logger.warning("Repairing Milvus indexes for collection %s: %s", name, exc)
                        col.create_index("embedding", INDEX_PARAMS)
                        col.create_index("sparse_vector", SPARSE_INDEX_PARAMS)
                        col.load()
            else:
                schema = _collection_schema()
                col = Collection(name, schema)
                col.create_index("embedding", INDEX_PARAMS)
                col.create_index("sparse_vector", SPARSE_INDEX_PARAMS)
                col.load()
                logger.info(f"Created Milvus collection: {name}")

            self._collections[name] = col
            return col

    # ── Write ──────────────────────────────────────────────────────────────────
    def upsert(
        self,
        collection_name: str,
        doc_id: str,
        text: str,
        metadata: Optional[dict] = None,
    ) -> None:
        """Embed one text and insert into the collection."""
        if not text or not text.strip():
            return
        self.upsert_batch(collection_name, [{"id": doc_id, "text": text, "metadata": metadata or {}}])

    def upsert_batch(self, collection_name: str, items: list[dict]) -> None:
        """
        Batch upsert — embed all texts in one shot then insert.
        items: [{id, text, metadata}]
        This is the recommended path for ingestion pipelines.
        """
        if not items:
            return
        import json

        # Determine if we should perform parent-child chunking
        is_chunk_collection = collection_name.startswith("rfp_chunks_") or collection_name.startswith("vendor_chunks_")
        
        expanded_items = []
        if is_chunk_collection:
            parent_size = 2000
            parent_overlap = 200
            child_size = 400
            child_overlap = 50
            
            for item in items:
                orig_id = item.get("id", str(uuid.uuid4()))
                text = item["text"]
                metadata = item.get("metadata", {})
                
                # Split into parents
                parent_texts = []
                if len(text) <= parent_size:
                    parent_texts = [text]
                else:
                    start = 0
                    while start < len(text):
                        end = start + parent_size
                        parent_texts.append(text[start:end])
                        start += (parent_size - parent_overlap)
                        if start >= len(text):
                            break
                
                for p_idx, p_text in enumerate(parent_texts):
                    p_id = f"{orig_id}_p{p_idx}"
                    
                    p_meta = metadata.copy()
                    p_meta["parent_id"] = ""
                    p_meta["chunk_type"] = "parent"
                    expanded_items.append({
                        "id": p_id,
                        "text": p_text,
                        "metadata": p_meta,
                        "is_parent": True,
                    })
                    
                    # Split parent into children
                    if len(p_text) <= child_size:
                        c_id = f"{orig_id}_p{p_idx}_c0"
                        c_meta = metadata.copy()
                        c_meta["parent_id"] = p_id
                        c_meta["chunk_type"] = "child"
                        expanded_items.append({
                            "id": c_id,
                            "text": p_text,
                            "metadata": c_meta,
                            "is_parent": False,
                        })
                    else:
                        start = 0
                        c_idx = 0
                        while start < len(p_text):
                            end = start + child_size
                            c_text = p_text[start:end]
                            c_id = f"{orig_id}_p{p_idx}_c{c_idx}"
                            c_meta = metadata.copy()
                            c_meta["parent_id"] = p_id
                            c_meta["chunk_type"] = "child"
                            expanded_items.append({
                                "id": c_id,
                                "text": c_text,
                                "metadata": c_meta,
                                "is_parent": False,
                            })
                            start += (child_size - child_overlap)
                            c_idx += 1
                            if start >= len(p_text):
                                break
        else:
            for item in items:
                metadata = item.get("metadata", {}).copy()
                metadata["parent_id"] = ""
                metadata["chunk_type"] = "child"
                expanded_items.append({
                    "id": item.get("id", str(uuid.uuid4())),
                    "text": item["text"],
                    "metadata": metadata,
                    "is_parent": False,
                })

        child_items = [x for x in expanded_items if not x.get("is_parent", False)]
        child_texts = [x["text"] for x in child_items]
        
        bm25 = _get_bm25_model(collection_name, fit_texts=child_texts if child_texts else None)
        child_dense_vectors = embed_batch(child_texts) if child_texts else []
        child_sparse_matrix = bm25.encode_documents(child_texts) if child_texts else None
        
        child_sparse_vectors = []
        if child_sparse_matrix is not None:
            csr = child_sparse_matrix.tocsr()
            indptr = csr.indptr
            indices = csr.indices
            data = csr.data
            for i in range(csr.shape[0]):
                start, end = indptr[i], indptr[i+1]
                row = {int(col): float(val) for col, val in zip(indices[start:end], data[start:end])}
                # A chunk whose tokens are all out-of-vocabulary (e.g. only
                # digits/punctuation) encodes to an empty sparse row, which Milvus
                # rejects on insert ("input must be a sparse matrix in supported
                # format") and fails the whole batch. Fall back to the same
                # no-signal placeholder used for parent chunks and in _clamp_sparse.
                child_sparse_vectors.append(row or {0: 0.0})

        def _s(meta, key, default="", maxlen=None):
            val = str(meta.get(key, default))
            return val[:maxlen] if maxlen else val

        def _i(meta, key, default=0):
            try:
                return int(float(meta.get(key) or default))
            except (TypeError, ValueError):
                return default

        col = self._get_or_create_collection(collection_name)
        
        pks = []
        texts = []
        rfp_ids = []
        response_ids = []
        vendor_names = []
        file_names = []
        page_numbers = []
        doc_roles = []
        sections = []
        deep_links = []
        extras = []
        embeddings = []
        sparse_vectors = []
        parent_ids = []
        chunk_types = []
        
        child_ptr = 0
        missing_response_id_count = 0
        empty_response_id_count = 0
        for idx, x in enumerate(expanded_items):
            meta = x["metadata"]

            rfp_id_val = _s(meta, "rfp_id", "", 64)
            response_id_raw = meta.get("response_id", "")
            response_id_val = _s(meta, "response_id", "", 64)

            # Defensive guard: claims/vendor chunks must always have response_id,
            # otherwise evaluation searches with response_id will return 0 hits.
            if is_chunk_collection and not response_id_raw:
                missing_response_id_count += 1
            if is_chunk_collection and isinstance(response_id_raw, str) and not response_id_raw.strip():
                empty_response_id_count += 1

            pks.append(x["id"])
            texts.append(_truncate_utf8(x["text"], 32768))
            rfp_ids.append(rfp_id_val)
            response_ids.append(response_id_val)
            vendor_names.append(_s(meta, "vendor_name", "", 256))
            file_names.append(_s(meta, "file_name", "", 512))
            page_numbers.append(_i(meta, "page_number"))
            doc_roles.append(_s(meta, "doc_role", "", 64))
            sections.append(_s(meta, "section", "", 512))
            deep_links.append(_s(meta, "deep_link", "", 1024))
            
            extra_keys = ("rfp_id","response_id","vendor_name","file_name",
                          "page_number","doc_role","section","deep_link",
                          "parent_id", "chunk_type")
            extra_data = {k: v for k, v in meta.items() if k not in extra_keys}
            extras.append(json.dumps(extra_data)[:8192])
            
            parent_ids.append(meta.get("parent_id", ""))
            chunk_types.append(meta.get("chunk_type", "child"))
            
            if x.get("is_parent", False):
                embeddings.append([0.0] * EMBED_DIM)
                sparse_vectors.append({0: 0.0})
            else:
                embeddings.append(child_dense_vectors[child_ptr])
                sparse_vectors.append(child_sparse_vectors[child_ptr])
                child_ptr += 1

        if is_chunk_collection and (missing_response_id_count or empty_response_id_count):
            logger.warning(
                "VectorStore.upsert_batch: %s has missing/empty response_id metadata: missing=%d empty=%d (collection=%s)",
                "vendor/claims chunk collection",
                missing_response_id_count,
                empty_response_id_count,
                collection_name,
            )

        data = [
            pks, texts, rfp_ids, response_ids, vendor_names, file_names,
            page_numbers, doc_roles, sections, deep_links, extras,
            embeddings, sparse_vectors, parent_ids, chunk_types
        ]
        col.insert(data)
        col.flush()

    # ── Read ───────────────────────────────────────────────────────────────────
    def search(
        self,
        collection_name: str,
        query: str,
        top_k: int = 5,
        filter_expr: Optional[str] = None,
    ) -> list[dict]:
        """
        Semantic hybrid search — returns top_k most similar chunks.
        filter_expr: Milvus boolean expression, e.g. 'rfp_id == "abc"'
        Returns: [{id, text, metadata, score}]
        """
        col = self._get_or_create_collection(collection_name)
        if col.num_entities == 0:
            return []

        from app.core.config import settings as _settings
        mode = str(getattr(_settings, "RETRIEVAL_MODE", "hybrid")).lower()
        dense_only = mode == "dense"
        sparse_only = mode == "sparse"
        query_dense = embed_one(query)
        if not dense_only:
            bm25 = _get_bm25_model(collection_name)
            query_sparse = _sparse_query_data(bm25.encode_queries([query]))

        has_chunk_type = any(f.name == "chunk_type" for f in col.schema.fields)

        if has_chunk_type:
            child_filter = 'chunk_type == "child"'
            if filter_expr:
                filter_expr = f"({filter_expr}) and ({child_filter})"
            else:
                filter_expr = child_filter

        output_fields = ["pk", "text", "rfp_id", "response_id", "vendor_name",
                         "file_name", "page_number", "doc_role", "section",
                         "deep_link", "extra", "parent_id", "chunk_type"]

        from pymilvus import AnnSearchRequest, RRFRanker
        
        req_dense = AnnSearchRequest(
            data=[query_dense],
            anns_field="embedding",
            param=SEARCH_PARAMS,
            limit=max(top_k * 2, 20),
            expr=filter_expr
        )
        
        if not dense_only:
            sparse_search_params = {
                "metric_type": "IP",
                "index_type": "SPARSE_INVERTED_INDEX",
                "params": {"drop_ratio_build": 0.2}
            }
            req_sparse = AnnSearchRequest(
                data=query_sparse,
                anns_field="sparse_vector",
                param=sparse_search_params,
                limit=max(top_k * 2, 20),
                expr=filter_expr
            )

        if sparse_only:
            # Ablation: BM25 search alone, same filter, same number of results.
            results = col.search(
                data=query_sparse,
                anns_field="sparse_vector",
                param=sparse_search_params,
                limit=top_k,
                expr=filter_expr,
                output_fields=output_fields,
            )
        elif dense_only:
            # Ablation: dense search alone, same filter, same number of results.
            results = col.search(
                data=[query_dense],
                anns_field="embedding",
                param=SEARCH_PARAMS,
                limit=top_k,
                expr=filter_expr,
                output_fields=output_fields,
            )
        else:
            results = col.hybrid_search(
                reqs=[req_dense, req_sparse],
                rerank=RRFRanker(),
                limit=top_k,
                output_fields=output_fields,
            )

        parent_ids = {hit.entity.get("parent_id") for hit in results[0] if hit.entity.get("parent_id") and hit.entity.get("chunk_type") == "child"}
        parent_texts = {}
        if parent_ids:
            try:
                pk_list_str = ", ".join(f'"{pid}"' for pid in parent_ids)
                parent_results = col.query(
                    expr=f"pk in [{pk_list_str}]",
                    output_fields=["pk", "text"],
                )
                parent_texts = {p["pk"]: p["text"] for p in parent_results}
            except Exception as e:
                logger.error(f"Error fetching parent chunks batch {parent_ids}: {e}")

        hits = []
        for hit in results[0]:
            import json
            extra = {}
            try:
                extra = json.loads(hit.entity.get("extra") or "{}")
            except Exception:
                pass
            
            parent_id = hit.entity.get("parent_id")
            chunk_type = hit.entity.get("chunk_type")
            text_to_use = hit.entity.get("text")
            if parent_id and chunk_type == "child" and parent_id in parent_texts:
                text_to_use = parent_texts[parent_id]

            meta = {
                "rfp_id":      hit.entity.get("rfp_id"),
                "response_id": hit.entity.get("response_id"),
                "vendor_name": hit.entity.get("vendor_name"),
                "file_name":   hit.entity.get("file_name"),
                "page_number": hit.entity.get("page_number"),
                "doc_role":    hit.entity.get("doc_role"),
                "section":     hit.entity.get("section"),
                "deep_link":   hit.entity.get("deep_link"),
                "parent_id":   parent_id,
                "chunk_type":  chunk_type,
                **extra,
            }
            hits.append({
                "id":       hit.entity.get("pk"),
                "text":     text_to_use,
                "metadata": meta,
                "score":    float(hit.score if getattr(hit, "score", None) is not None else hit.distance),
            })

        return hits

    def search_multi(
        self,
        collection_names: list[str],
        query: str,
        top_k: int = 6,
        rfp_id: Optional[str] = None,
    ) -> list[dict]:
        """
        Search across multiple collections simultaneously using hybrid search.
        """
        query_vec = embed_one(query)
        filter_expr = f'rfp_id == "{rfp_id}"' if rfp_id else None

        all_hits: list[dict] = []
        per_col = max(top_k, 4)

        for col_name in collection_names:
            col = self._get_or_create_collection(col_name)
            if col.num_entities == 0:
                continue

            bm25 = _get_bm25_model(col_name)
            query_sparse = _sparse_query_data(bm25.encode_queries([query]))


            has_chunk_type = any(f.name == "chunk_type" for f in col.schema.fields)
            if has_chunk_type:
                child_filter = 'chunk_type == "child"'
                col_filter_expr = f"({filter_expr}) and ({child_filter})" if filter_expr else child_filter
            else:
                col_filter_expr = filter_expr

            output_fields = ["pk", "text", "rfp_id", "response_id", "vendor_name",
                             "file_name", "page_number", "doc_role", "section",
                             "deep_link", "extra", "parent_id", "chunk_type"]

            from pymilvus import AnnSearchRequest, RRFRanker
            
            req_dense = AnnSearchRequest(
                data=[query_vec],
                anns_field="embedding",
                param=SEARCH_PARAMS,
                limit=per_col,
                expr=col_filter_expr
            )
            
            sparse_search_params = {
                "metric_type": "IP",
                "index_type": "SPARSE_INVERTED_INDEX",
                "params": {"drop_ratio_build": 0.2}
            }
            req_sparse = AnnSearchRequest(
                data=query_sparse,
                anns_field="sparse_vector",
                param=sparse_search_params,
                limit=per_col,
                expr=col_filter_expr
            )

            results = col.hybrid_search(
                reqs=[req_dense, req_sparse],
                rerank=RRFRanker(),
                limit=per_col,
                output_fields=output_fields,
            )

            parent_ids = {hit.entity.get("parent_id") for hit in results[0] if hit.entity.get("parent_id") and hit.entity.get("chunk_type") == "child"}
            parent_texts = {}
            if parent_ids:
                try:
                    pk_list_str = ", ".join(f'"{pid}"' for pid in parent_ids)
                    parent_results = col.query(
                        expr=f"pk in [{pk_list_str}]",
                        output_fields=["pk", "text"],
                    )
                    parent_texts = {p["pk"]: p["text"] for p in parent_results}
                except Exception as e:
                    logger.error(f"Error fetching parent chunks batch {parent_ids}: {e}")

            import json
            for hit in results[0]:
                extra = {}
                try:
                    extra = json.loads(hit.entity.get("extra") or "{}")
                except Exception:
                    pass
                
                parent_id = hit.entity.get("parent_id")
                chunk_type = hit.entity.get("chunk_type")
                text_to_use = hit.entity.get("text")
                if parent_id and chunk_type == "child" and parent_id in parent_texts:
                    text_to_use = parent_texts[parent_id]

                meta = {
                    "rfp_id":      hit.entity.get("rfp_id"),
                    "response_id": hit.entity.get("response_id"),
                    "vendor_name": hit.entity.get("vendor_name"),
                    "file_name":   hit.entity.get("file_name"),
                    "page_number": hit.entity.get("page_number"),
                    "doc_role":    hit.entity.get("doc_role"),
                    "section":     hit.entity.get("section"),
                    "deep_link":   hit.entity.get("deep_link"),
                    "parent_id":   parent_id,
                    "chunk_type":  chunk_type,
                    **extra,
                }
                all_hits.append({
                    "id":         hit.entity.get("pk"),
                    "text":       text_to_use,
                    "metadata":   meta,
                    "score":      float(hit.score),
                    "collection": col_name,
                })

        seen: dict[str, dict] = {}
        for h in all_hits:
            if h["id"] not in seen or h["score"] > seen[h["id"]]["score"]:
                seen[h["id"]] = h

        return sorted(seen.values(), key=lambda x: x["score"], reverse=True)[:top_k]

    def drop_collection(self, collection_name: str) -> None:
        from pymilvus import utility
        if utility.has_collection(collection_name):
            from pymilvus import Collection
            Collection(collection_name).drop()
            logger.info(f"Dropped collection: {collection_name}")
        self._collections.pop(collection_name, None)

    def delete_by_expression(self, collection_name: str, expr: str) -> None:
        """Delete records from a collection matching an expression (e.g. 'response_id == "..."')."""
        from pymilvus import utility
        if utility.has_collection(collection_name):
            col = self._get_or_create_collection(collection_name)
            col.delete(expr)
            col.flush()
            logger.info(f"Deleted records from {collection_name} matching: {expr}")

    def collection_count(self, collection_name: str) -> int:
        col = self._get_or_create_collection(collection_name)
        return col.num_entities


# ── Collection name helpers ───────────────────────────────────────────────────
# Call these everywhere so names are consistent and never typo'd.

def col_rfp_chunks(rfp_id: str)    -> str: return f"rfp_chunks_{rfp_id.replace('-','_')}"
def col_vendor_chunks(rfp_id: str) -> str: return f"vendor_chunks_{rfp_id.replace('-','_')}"
def col_requirements(rfp_id: str)  -> str: return f"requirements_{rfp_id.replace('-','_')}"
def col_claims(rfp_id: str)        -> str: return f"claims_{rfp_id.replace('-','_')}"
def col_cv_skills(rfp_id: str)     -> str: return f"cv_skills_{rfp_id.replace('-','_')}"


# ── Singleton ─────────────────────────────────────────────────────────────────
_vs: Optional[VectorStore] = None

def get_vector_store() -> VectorStore:
    global _vs
    if _vs is None:
        from app.core.config import settings
        _vs = VectorStore(host=settings.MILVUS_HOST, port=settings.MILVUS_PORT)
    return _vs

