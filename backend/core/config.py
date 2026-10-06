import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_CANDIDATES = (
    BASE_DIR / ".env",
    BASE_DIR.parent / ".env",
    BASE_DIR.parent / "rag-ocr-pipeline" / ".env",
)
for env_file in ENV_CANDIDATES:
    if env_file.exists():
        load_dotenv(env_file, override=False)


def _env_first(names: tuple[str, ...], default: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return default


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Settings(BaseModel):
    app_name: str = os.getenv("APP_NAME", "QBankCTU API")
    app_version: str = os.getenv("APP_VERSION", "0.1.0")
    app_env: str = os.getenv("APP_ENV", "production").strip().lower()
    demo_mode: bool = _env_bool(
        "DEMO_MODE",
        os.getenv("APP_ENV", "production").strip().lower() == "demo",
    )
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))

    cors_origins: list[str] = [
        origin.strip()
        for origin in os.getenv(
            "CORS_ORIGINS",
            os.getenv(
                "ALLOWED_ORIGINS",
                "http://localhost:5173,http://127.0.0.1:5173",
            ),
        ).split(",")
        if origin.strip()
    ]
    allowed_hosts: list[str] = [
        host.strip()
        for host in os.getenv(
            "ALLOWED_HOSTS",
            "localhost,127.0.0.1,testserver",
        ).split(",")
        if host.strip()
    ]
    api_prefix: str = os.getenv("API_PREFIX", "/api/v1")

    mongo_uri: str = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
    postgres_dsn: str = os.getenv("POSTGRES_DSN", "")
    postgres_pool_min_size: int = int(os.getenv("POSTGRES_POOL_MIN_SIZE", "1"))
    postgres_pool_max_size: int = int(os.getenv("POSTGRES_POOL_MAX_SIZE", "10"))
    # Business data lives in PostgreSQL. Setting a flag to "mongo" routes that
    # group back to MongoDB; kept only as a rollback path during the transition.
    user_store: str = os.getenv("USER_STORE", "postgres").strip().lower()
    ai_config_store: str = os.getenv("AI_CONFIG_STORE", "postgres").strip().lower()
    catalog_store: str = os.getenv("CATALOG_STORE", "postgres").strip().lower()
    notification_store: str = os.getenv("NOTIFICATION_STORE", "postgres").strip().lower()
    dictionary_store: str = os.getenv("DICTIONARY_STORE", "postgres").strip().lower()
    moodle_target_store: str = os.getenv("MOODLE_TARGET_STORE", "postgres").strip().lower()
    llm_slot_store: str = os.getenv("LLM_SLOT_STORE", "postgres").strip().lower()
    document_store: str = os.getenv("DOCUMENT_STORE", "postgres").strip().lower()
    audit_store: str = os.getenv("AUDIT_STORE", "postgres").strip().lower()
    review_policy_store: str = os.getenv("REVIEW_POLICY_STORE", "postgres").strip().lower()
    question_store: str = os.getenv("QUESTION_STORE", "postgres").strip().lower()
    generation_store: str = os.getenv("GENERATION_STORE", "postgres").strip().lower()
    exam_store: str = os.getenv("EXAM_STORE", "postgres").strip().lower()
    auth_db_name: str = os.getenv("AUTH_DB_NAME", "NCKH")
    rag_db_name: str = os.getenv(
        "RAG_DB_NAME",
        os.getenv("DB_NAME", "rag_database"),
    )
    mongo_connect_timeout_ms: int = int(os.getenv("MONGO_CONNECT_TIMEOUT_MS", "10000"))
    require_mongo_transactions: bool = _env_bool(
        "REQUIRE_MONGO_TRANSACTIONS",
        os.getenv("APP_ENV", "production").strip().lower() in {"production", "staging"},
    )
    job_recovery_timeout_minutes: int = int(os.getenv("JOB_RECOVERY_TIMEOUT_MINUTES", "120"))
    job_worker_poll_seconds: float = float(os.getenv("JOB_WORKER_POLL_SECONDS", "1"))
    job_lease_seconds: int = int(os.getenv("JOB_LEASE_SECONDS", "120"))
    job_heartbeat_seconds: int = int(os.getenv("JOB_HEARTBEAT_SECONDS", "30"))
    job_max_attempts: int = int(os.getenv("JOB_MAX_ATTEMPTS", "3"))
    job_retry_base_seconds: int = int(os.getenv("JOB_RETRY_BASE_SECONDS", "5"))
    job_retry_max_seconds: int = int(os.getenv("JOB_RETRY_MAX_SECONDS", "300"))
    max_active_generation_jobs_per_user: int = int(os.getenv("MAX_ACTIVE_GENERATION_JOBS_PER_USER", "10"))
    worker_shutdown_grace_seconds: int = int(os.getenv("WORKER_SHUTDOWN_GRACE_SECONDS", "30"))
    job_retention_days: int = int(os.getenv("JOB_RETENTION_DAYS", "90"))
    llm_slot_lease_seconds: int = int(os.getenv("LLM_SLOT_LEASE_SECONDS", "90"))
    llm_slot_heartbeat_seconds: int = int(os.getenv("LLM_SLOT_HEARTBEAT_SECONDS", "15"))
    llm_slot_wait_timeout_seconds: int = int(os.getenv("LLM_SLOT_WAIT_TIMEOUT_SECONDS", "600"))
    llm_slot_poll_seconds: float = float(os.getenv("LLM_SLOT_POLL_SECONDS", "0.5"))
    ollama_max_concurrency: int = int(os.getenv("OLLAMA_MAX_CONCURRENCY", "1"))
    generation_batch_size: int = int(os.getenv("GENERATION_BATCH_SIZE", "3"))
    ollama_generation_batch_size: int = int(os.getenv("OLLAMA_GENERATION_BATCH_SIZE", "1"))
    gemini_max_concurrency: int = int(os.getenv("GEMINI_MAX_CONCURRENCY", "5"))
    review_lock_timeout_minutes: int = int(os.getenv("REVIEW_LOCK_TIMEOUT_MINUTES", "30"))
    # Admin assignments reserve a question for the assigned reviewer much longer
    # than an interactive review lock; after this window other reviewers may claim.
    review_assignment_timeout_hours: int = int(os.getenv("REVIEW_ASSIGNMENT_TIMEOUT_HOURS", "72"))
    # A pending question is late once it has waited this long since submission.
    review_sla_hours: int = int(os.getenv("REVIEW_SLA_HOURS", "48"))
    review_sla_reminder_interval_seconds: float = float(
        os.getenv("REVIEW_SLA_REMINDER_INTERVAL_SECONDS", "900")
    )
    gpu_coordination_enabled: bool = _env_bool("GPU_COORDINATION_ENABLED", True)
    gpu_lock_path: str = os.getenv("GPU_LOCK_PATH", "./data/gpu-operation.lock")
    gpu_lock_timeout_seconds: float = float(os.getenv("GPU_LOCK_TIMEOUT_SECONDS", "1200"))
    gpu_lock_stale_seconds: float = float(os.getenv("GPU_LOCK_STALE_SECONDS", "120"))
    gpu_lock_heartbeat_seconds: float = float(os.getenv("GPU_LOCK_HEARTBEAT_SECONDS", "10"))
    gpu_lock_poll_seconds: float = float(os.getenv("GPU_LOCK_POLL_SECONDS", "0.25"))

    firebase_credentials_path: str = os.getenv(
        "FIREBASE_CREDENTIALS_PATH", str(BASE_DIR / "firebase-service-account.json")
    )
    firebase_project_id: str = os.getenv("FIREBASE_PROJECT_ID", "nckh-e6817").strip()
    demo_admin_email: str = os.getenv("DEMO_ADMIN_EMAIL", "admin@qbankctu.edu.vn")
    demo_admin_password: str = os.getenv("DEMO_ADMIN_PASSWORD", "")
    demo_reviewer_email: str = os.getenv("DEMO_REVIEWER_EMAIL", "reviewer@qbankctu.edu.vn")
    demo_reviewer_password: str = os.getenv("DEMO_REVIEWER_PASSWORD", "")
    demo_teacher_email: str = os.getenv("DEMO_TEACHER_EMAIL", "teacher@qbankctu.edu.vn")
    demo_teacher_password: str = os.getenv("DEMO_TEACHER_PASSWORD", "")
    demo_session_secret: str = os.getenv("DEMO_SESSION_SECRET", "")
    demo_session_ttl_hours: int = int(os.getenv("DEMO_SESSION_TTL_HOURS", "8"))

    # Provider LLM mặc định chạy local qua Ollama.
    model_provider: str = os.getenv("MODEL_PROVIDER", "qwen3-8b")
    code_generation_model_provider: str = os.getenv(
        "CODE_GENERATION_MODEL_PROVIDER", "qwen3-8b"
    ).strip()
    evaluation_model_provider: str = _env_first(
        ("EVALUATION_MODEL_PROVIDER", "EVALUATOR_MODEL_CODE"),
        "qwen3-8b",
    )
    generation_fallback_provider: str = os.getenv("GENERATION_FALLBACK_PROVIDER", "").strip()
    evaluation_fallback_provider: str = os.getenv("EVALUATION_FALLBACK_PROVIDER", "").strip()
    # Qwen3-8B benchmark: evaluation JSON with per-option checks took 600-970
    # output tokens, and 3 of 14 cases hit the old 900 limit on the first try.
    evaluation_num_predict: int = int(os.getenv("EVALUATION_NUM_PREDICT", "1400"))
    # Budget for the retry attempt after a truncated or malformed evaluation JSON.
    evaluation_retry_num_predict: int = int(os.getenv("EVALUATION_RETRY_NUM_PREDICT", "1800"))
    # Reasoning models (deepseek-r1 with thinking on) spend 1.1k-1.8k tokens
    # thinking before the JSON; 900 tokens left them with no answer at all.
    evaluation_thinking_num_predict: int = int(os.getenv("EVALUATION_THINKING_NUM_PREDICT", "4096"))
    # The evaluation prompt alone is ~3.2k-4k tokens, so a thinking budget needs
    # a larger context window than the default 8192.
    evaluation_thinking_num_ctx: int = int(os.getenv("EVALUATION_THINKING_NUM_CTX", "12288"))
    ollama_generate_url: str = _env_first(
        ("OLLAMA_GENERATE_URL", "OLLAMA_BASE_URL"),
        "http://localhost:11434/api/generate",
    )
    ollama_timeout_seconds: float = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "600"))
    ollama_num_ctx: int = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
    ollama_num_predict: int = int(os.getenv("OLLAMA_NUM_PREDICT", "4096"))
    ollama_temperature: float = float(os.getenv("OLLAMA_TEMPERATURE", "0"))
    # RTX 4050 6 GB shares VRAM with Docling; keep Ollama warm briefly, not indefinitely.
    ollama_keep_alive: str = os.getenv("OLLAMA_KEEP_ALIVE", "5m").strip()
    deepseek_model_name: str = _env_first(("DEEPSEEK_MODEL_NAME",), "deepseek-r1")
    deepseek_timeout_seconds: float = float(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "180"))
    deepseek_num_predict: int = int(os.getenv("DEEPSEEK_NUM_PREDICT", "900"))
    deepseek_temperature: float = float(os.getenv("DEEPSEEK_TEMPERATURE", "0"))

    chunk_size_default: int = int(os.getenv("CHUNK_SIZE_DEFAULT", "1000"))
    chunk_size_min: int = int(os.getenv("CHUNK_SIZE_MIN", "200"))
    chunk_size_max: int = int(os.getenv("CHUNK_SIZE_MAX", "4000"))

    chunk_overlap_default: int = int(os.getenv("CHUNK_OVERLAP_DEFAULT", "150"))
    chunk_overlap_min: int = int(os.getenv("CHUNK_OVERLAP_MIN", "0"))
    chunk_overlap_max: int = int(os.getenv("CHUNK_OVERLAP_MAX", "800"))

    chunk_buffer_max_pages: int = int(os.getenv("CHUNK_BUFFER_MAX_PAGES", "30"))
    chunk_buffer_max_chars: int = int(os.getenv("CHUNK_BUFFER_MAX_CHARS", "200000"))
    max_code_block_lines: int = int(os.getenv("MAX_CODE_BLOCK_LINES", "50"))

    chromadb_collection_name: str = os.getenv("CHROMADB_COLLECTION_NAME", "chunks")
    chromadb_batch_size: int = int(os.getenv("CHROMADB_BATCH_SIZE", "50"))
    embedding_model_name: str = os.getenv("EMBEDDING_MODEL_NAME", "BAAI/bge-m3")
    embedding_model_revision: str = os.getenv("EMBEDDING_MODEL_REVISION", "").strip()
    embedding_precision: str = os.getenv("EMBEDDING_PRECISION", "auto").strip().lower()
    embedding_batch_size: int = int(os.getenv("EMBEDDING_BATCH_SIZE", "16"))
    embedding_batch_max_tokens: int = int(os.getenv("EMBEDDING_BATCH_MAX_TOKENS", "8192"))
    embedding_max_tokens: int = int(os.getenv("EMBEDDING_MAX_TOKENS", "1024"))
    embedding_token_overlap: int = int(os.getenv("EMBEDDING_TOKEN_OVERLAP", "128"))
    embedding_cache_enabled: bool = _env_bool("EMBEDDING_CACHE_ENABLED", True)
    embedding_release_gpu_after_use: bool = _env_bool("EMBEDDING_RELEASE_GPU_AFTER_USE", True)
    lexical_fallback_max_chunks: int = int(os.getenv("LEXICAL_FALLBACK_MAX_CHUNKS", "1200"))
    lexical_fallback_distance_threshold: float = float(
        os.getenv("LEXICAL_FALLBACK_DISTANCE_THRESHOLD", "0.55")
    )

    # Docling is the default PDF OCR/layout engine; EASYOCR remains selectable explicitly.
    pdf_ocr_engine: str = os.getenv("PDF_OCR_ENGINE", "docling").strip().lower()
    docling_url: str = os.getenv("DOCLING_URL", "http://localhost:5001").rstrip("/")
    docling_timeout: int = int(os.getenv("DOCLING_TIMEOUT", "600"))
    docling_poll_seconds: float = float(os.getenv("DOCLING_POLL_SECONDS", "0.5"))
    docling_ocr_preset: str = os.getenv("DOCLING_OCR_PRESET", "tesseract").strip().lower()
    docling_ocr_backend: str = os.getenv("DOCLING_OCR_BACKEND", "onnxruntime").strip().lower()
    docling_ocr_languages: list[str] = [
        language.strip() for language in os.getenv("DOCLING_OCR_LANGUAGES", "vie").split(",")
        if language.strip()
    ]
    docling_images_scale: float = float(os.getenv("DOCLING_IMAGES_SCALE", "2.0"))
    docling_table_mode: str = os.getenv("DOCLING_TABLE_MODE", "accurate").strip().lower()
    docling_do_table_structure: bool = _env_bool("DOCLING_DO_TABLE_STRUCTURE", True)
    docling_include_images: bool = _env_bool("DOCLING_INCLUDE_IMAGES", True)

    # EasyOCR + PDFium retained for the existing setup and comparison tests.
    easyocr_languages: list[str] = [
        language.strip()
        for language in os.getenv("EASYOCR_LANGUAGES", "vi,en").split(",")
        if language.strip()
    ]
    easyocr_gpu: bool = _env_bool("EASYOCR_GPU", True)
    easyocr_batch_size: int = int(os.getenv("EASYOCR_BATCH_SIZE", "2"))
    easyocr_render_scale: float = float(os.getenv("EASYOCR_RENDER_SCALE", "2.0"))
    easyocr_min_confidence: float = float(os.getenv("EASYOCR_MIN_CONFIDENCE", "0.20"))
    easyocr_model_storage_directory: str = os.getenv(
        "EASYOCR_MODEL_STORAGE_DIRECTORY", "./data/easyocr_models"
    )
    easyocr_download_enabled: bool = _env_bool("EASYOCR_DOWNLOAD_ENABLED", True)
    easyocr_unload_after_use: bool = _env_bool("EASYOCR_UNLOAD_AFTER_USE", True)

    pdf_text_fast_path_enabled: bool = _env_bool("PDF_TEXT_FAST_PATH_ENABLED", True)
    pdf_text_fast_path_min_coverage: float = float(os.getenv("PDF_TEXT_FAST_PATH_MIN_COVERAGE", "0.98"))
    pdf_text_fast_path_min_chars_per_page: int = int(os.getenv("PDF_TEXT_FAST_PATH_MIN_CHARS_PER_PAGE", "150"))
    pdf_text_fast_path_max_image_page_ratio: float = float(
        os.getenv("PDF_TEXT_FAST_PATH_MAX_IMAGE_PAGE_RATIO", "0.0")
    )
    pdf_text_fast_path_max_replacement_ratio: float = float(
        os.getenv("PDF_TEXT_FAST_PATH_MAX_REPLACEMENT_RATIO", "0.005")
    )

    chromadb_path: str = os.getenv("CHROMADB_PATH", "./data/chroma_data")
    chroma_mode: str = os.getenv("CHROMA_MODE", "local").strip().lower()
    chroma_host: str = os.getenv("CHROMA_HOST", "localhost").strip()
    chroma_port: int = int(os.getenv("CHROMA_PORT", "8000"))
    chroma_ssl: bool = _env_bool("CHROMA_SSL", False)
    chroma_auth_token: str = os.getenv("CHROMA_AUTH_TOKEN", "")
    output_dir: str = os.getenv("OUTPUT_DIR", "./data/outputs")
    metadata_dir: str = os.getenv("METADATA_DIR", "./data/metadata")
    chunk_output_dir: str = os.getenv("CHUNK_OUTPUT_DIR", "./data/chunk_outputs")
    upload_dir: str = os.getenv("UPLOAD_DIR", "./data/uploads")
    ocr_output_dir: str = os.getenv("OCR_OUTPUT_DIR", "./data/ocr_outputs")
    raw_artifact_compression: str = os.getenv("RAW_ARTIFACT_COMPRESSION", "gzip").strip().lower()
    artifact_hot_retention_days: int = int(os.getenv("ARTIFACT_HOT_RETENTION_DAYS", "30"))
    artifact_cold_retention_days: int = int(os.getenv("ARTIFACT_COLD_RETENTION_DAYS", "365"))
    artifact_cold_dir: str = os.getenv("ARTIFACT_COLD_DIR", "./data/artifact_archive")
    artifact_blob_dir: str = os.getenv("ARTIFACT_BLOB_DIR", "./data/artifact_blobs")
    # New uploads, OCR artifacts and avatars go to this provider; existing
    # artifacts are always read from the provider recorded with them.
    storage_provider: str = os.getenv("STORAGE_PROVIDER", "local").strip().lower()
    s3_bucket: str = os.getenv("S3_BUCKET", "").strip()
    s3_prefix: str = os.getenv("S3_PREFIX", "").strip().strip("/")
    s3_endpoint_url: str = os.getenv("S3_ENDPOINT_URL", "").strip()
    s3_region: str = os.getenv("S3_REGION", "").strip()

    prompts_dir: str = os.getenv("PROMPTS_DIR", "./prompts")
    prompt_source: str = os.getenv("PROMPT_SOURCE", "file").strip().lower()

    # Brevo (Sendinblue) transactional email — used for contact/publication notifications.
    brevo_api_key: str = os.getenv("BREVO_API_KEY", "").strip()
    brevo_sender_email: str = os.getenv("BREVO_SENDER_EMAIL", "").strip()
    brevo_sender_name: str = os.getenv("BREVO_SENDER_NAME", "QBankCTU").strip()
    contact_admin_email: str = os.getenv("CONTACT_ADMIN_EMAIL", "").strip()
    email_notifications_enabled: bool = _env_bool("EMAIL_NOTIFICATIONS_ENABLED", True)
    brevo_api_url: str = os.getenv("BREVO_API_URL", "https://api.brevo.com/v3/smtp/email").strip()
    brevo_timeout_seconds: float = float(os.getenv("BREVO_TIMEOUT_SECONDS", "10"))

    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model_name: str = _env_first(
        ("GEMINI_MODEL_NAME", "DEFAULT_MODEL"),
        "gemini-3.6-flash",
    )
    gemini_max_output_tokens: int = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "8192"))


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


def resolve_path(relative: str | Path) -> Path:
    """Quy đổi 1 đường dẫn tương đối (trong Settings) thành đường dẫn tuyệt đối, luôn tính từ backend/ (BASE_DIR)."""
    return (BASE_DIR / relative).resolve()
