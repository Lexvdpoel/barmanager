import os
from dataclasses import dataclass


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "ja", "on")


@dataclass
class Settings:
    db_path: str = "data/barmanager.db"
    image_dir: str = "data/images"
    host: str = "0.0.0.0"
    port: int = 8080
    # Bootstrap admin, only used when the database has no users yet.
    admin_username: str = "admin"
    admin_password: str | None = None
    session_days: float = 30.0
    # Internet lookups for unknown barcodes.
    lookup_timeout: float = 8.0
    use_upcitemdb: bool = True
    opengtindb_id: str = ""          # free query id from opengtindb.org (optional)
    brave_api_key: str = ""          # optional: web search for unknown barcodes + photo search
    searxng_url: str = ""            # optional self-hosted SearXNG (JSON enabled), e.g. http://searxng:8080
    max_images: int = 3
    # Optional local AI (Ollama) that fills in missing details, e.g. http://ollama:11434
    ollama_url: str = ""
    ollama_model: str = "qwen3:4b"
    ollama_timeout: float = 600.0
    max_image_bytes: int = 8_000_000
    # Tests run lookups inline instead of in the background thread.
    lookup_inline: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ
        return cls(
            db_path=e.get("BM_DB_PATH", cls.db_path),
            image_dir=e.get("BM_IMAGE_DIR", cls.image_dir),
            host=e.get("BM_HOST", cls.host),
            port=int(e.get("BM_PORT", cls.port)),
            admin_username=e.get("BM_ADMIN_USERNAME", cls.admin_username),
            admin_password=e.get("BM_ADMIN_PASSWORD") or None,
            session_days=float(e.get("BM_SESSION_DAYS", cls.session_days)),
            lookup_timeout=float(e.get("BM_LOOKUP_TIMEOUT", cls.lookup_timeout)),
            use_upcitemdb=_bool("BM_UPCITEMDB", cls.use_upcitemdb),
            opengtindb_id=e.get("BM_OPENGTINDB_ID", "").strip(),
            brave_api_key=e.get("BM_BRAVE_API_KEY", "").strip(),
            searxng_url=e.get("BM_SEARXNG_URL", "").strip(),
            max_images=int(e.get("BM_MAX_IMAGES", cls.max_images)),
            ollama_url=e.get("BM_OLLAMA_URL", "").strip(),
            ollama_model=e.get("BM_OLLAMA_MODEL", "").strip() or cls.ollama_model,
            ollama_timeout=float(e.get("BM_OLLAMA_TIMEOUT", cls.ollama_timeout)),
        )
