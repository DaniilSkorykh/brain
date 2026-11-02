"""Индексирование Markdown-заметок в ChromaDB через sentence-transformers (e5)."""
from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import frontmatter

from bot.config import settings
from rag.embedder import embed_passages, get_model

if TYPE_CHECKING:
    import chromadb as _chroma

logger = logging.getLogger(__name__)

_client: "_chroma.PersistentClient | None" = None
_collection: "_chroma.Collection | None" = None


def get_collection() -> "_chroma.Collection":
    """Лениво создаёт клиент и коллекцию ChromaDB."""
    global _client, _collection
    if _collection is None:
        import chromadb
        _client = chromadb.PersistentClient(path=settings.chroma_dir)
        _collection = _client.get_or_create_collection(
            name="brain",
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


def _chunk_text(text: str) -> list[str]:
    """Разбивает текст на токен-чанки с overlap через токенайзер модели."""
    tokenizer = get_model().tokenizer
    token_ids: list[int] = tokenizer.encode(text, add_special_tokens=False)

    chunk_size = settings.chunk_size
    overlap = settings.chunk_overlap
    step = chunk_size - overlap

    if step <= 0:
        step = chunk_size

    chunks: list[str] = []
    start = 0
    while start < len(token_ids):
        window = token_ids[start: start + chunk_size]
        decoded = tokenizer.decode(window, skip_special_tokens=True)
        if decoded.strip():
            chunks.append(decoded.strip())
        start += step

    return chunks


def _file_hash(raw: str) -> str:
    """MD5-хеш сырого содержимого файла."""
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


_lock = threading.RLock()


def _rel_path(path: Path) -> str:
    """Относительный путь в posix-формате относительно knowledge_dir."""
    try:
        return path.relative_to(Path(settings.knowledge_dir)).as_posix()
    except ValueError:
        return path.as_posix()


def _is_ignored(path: Path) -> bool:
    """Служебные файлы: скрытые (.stversions и т.п.), шаблоны, sync-conflict."""
    try:
        parts = path.relative_to(Path(settings.knowledge_dir)).parts
    except ValueError:
        parts = (path.name,)
    for part in parts:
        if part.startswith(".") or part in ("_templates", "Шаблоны"):
            return True
    return ".sync-conflict-" in path.name


def _resolve_domain(rel: str, meta: dict) -> str:
    """Домен: frontmatter → подпапка внутри bot_inbox_dir → первая папка пути → 'inbox'."""
    raw_domain = meta.get("domain", "")
    if raw_domain:
        return str(raw_domain)

    prefix = settings.bot_inbox_dir.strip("/") + "/"
    if rel.startswith(prefix):
        sub = rel[len(prefix):].split("/")
        if len(sub) > 1 and sub[0]:
            return sub[0]

    parts = Path(rel).parts
    return parts[0] if len(parts) > 1 else "inbox"


def index_file(path: Path) -> dict:
    """Индексирует один MD-файл. Возвращает dict со status и chunks."""
    if _is_ignored(path):
        return {"status": "ignored", "chunks": 0}
    with _lock:
        collection = get_collection()

        raw = path.read_text(encoding="utf-8")
        post = frontmatter.loads(raw)
        body: str = post.content
        meta: dict = post.metadata

        current_hash = _file_hash(raw)
        rel = _rel_path(path)

        # Инкрементальная проверка: если чанки уже есть с тем же hash — пропустить
        existing = collection.get(where={"source_file": rel}, include=["metadatas"])
        if existing["ids"]:
            stored_hash = existing["metadatas"][0].get("file_hash", "")
            if stored_hash == current_hash:
                return {"status": "skipped", "chunks": len(existing["ids"])}
            # Хеш изменился — удаляем старые чанки
            collection.delete(where={"source_file": rel})

        chunks = _chunk_text(body)
        if not chunks:
            return {"status": "empty", "chunks": 0}

        domain = _resolve_domain(rel, meta)

        date_val = meta.get("date", "")
        date_str = str(date_val) if date_val else ""

        title_val = meta.get("title", "")
        title_str = str(title_val) if title_val else path.stem

        type_val = meta.get("type", "")
        type_str = str(type_val) if type_val else "note"

        metadatas = [
            {
                "source_file": rel,
                "domain": domain,
                "date": date_str,
                "title": title_str,
                "type": type_str,
                "chunk_index": i,
                "file_hash": current_hash,
            }
            for i in range(len(chunks))
        ]
        ids = [f"{rel}::{i}" for i in range(len(chunks))]
        embeddings = embed_passages(chunks)

        collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=chunks,
            metadatas=metadatas,
        )

        logger.info("Indexed %s: %d chunks", rel, len(chunks))
        return {"status": "indexed", "chunks": len(chunks)}


def reindex_all() -> dict:
    """Обходит knowledge_dir рекурсивно, индексирует *.md (кроме служебных) и чистит удалённые."""
    knowledge_dir = Path(settings.knowledge_dir)
    summary = {"files": 0, "indexed": 0, "skipped": 0, "empty": 0, "chunks": 0, "removed": 0}

    with _lock:
        seen: set[str] = set()

        for md_file in sorted(knowledge_dir.rglob("*.md")):
            if _is_ignored(md_file):
                continue

            seen.add(_rel_path(md_file))
            summary["files"] += 1
            try:
                result = index_file(md_file)
            except Exception:
                logger.exception("Failed to index %s", md_file)
                continue

            status = result.get("status", "")
            if status == "indexed":
                summary["indexed"] += 1
            elif status == "skipped":
                summary["skipped"] += 1
            elif status == "empty":
                summary["empty"] += 1

            summary["chunks"] += result.get("chunks", 0)

        # Чистка: файлы, которых больше нет на диске (или ставших служебными).
        # Папка пропала/не смонтирована — не трогаем индекс, иначе он сотрётся целиком.
        if not seen:
            logger.warning("В %s не найдено ни одного файла — чистку индекса пропускаю", knowledge_dir)
            return summary
        collection = get_collection()
        metadatas = collection.get(include=["metadatas"])["metadatas"] or []
        stale = {m.get("source_file") for m in metadatas if m} - seen - {None}
        for rel in sorted(stale):
            collection.delete(where={"source_file": rel})
            summary["removed"] += 1
            logger.info("Removed from index (file gone): %s", rel)

    return summary
