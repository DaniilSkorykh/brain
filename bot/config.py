from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Telegram
    telegram_bot_token: str
    bot_mode: str = "polling"
    telegram_webhook_url: str = ""
    telegram_allowed_user_id: int

    # Self-hosted Bot API server (для файлов >20 МБ; иначе облачный API)
    local_bot_api: bool = False
    bot_api_base_url: str = "http://telegram-bot-api:8081"
    telegram_api_id: int = 0
    telegram_api_hash: str = ""

    # LLM
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_model: str = "anthropic/claude-sonnet-4-5"
    # Дешёвая модель для парсинга/классификации сообщений
    llm_fast_model: str = "anthropic/claude-haiku-5.5"

    # Paths
    knowledge_dir: str = "/app/knowledge"
    chroma_dir: str = "/app/chroma_db"
    sqlite_path: str = "/app/db/brain.sqlite"
    # Куда бот складывает свои файлы (относительно knowledge_dir); синхронизируется на ПК
    bot_inbox_dir: str = "ministry/ВХОДЯЩИЕ-ИЗ-БОТА"

    # RAG settings
    embedding_model: str = "intfloat/multilingual-e5-base"
    chunk_size: int = 400
    chunk_overlap: int = 50
    top_k_results: int = 5

    # Планировщик / напоминания
    timezone: str = "Europe/Moscow"
    morning_time: str = "07:00"          # утренний дайджест задач
    evening_time: str = "23:00"          # вечерняя сводка
    weekly_review_day: str = "sun"       # день недели итогов (cron: mon..sun)
    weekly_review_time: str = "20:00"    # время итогов недели
    meeting_reminder_min: int = 60       # за сколько минут напоминать о встрече
    index_poll_min: int = 2              # как часто подхватывать изменения файлов (Syncthing)

    # Голос
    whisper_model: str = "openai/whisper-1"
    long_audio_min_sec: int = 300        # с какой длительности включать режим лекции (конспект)
    whisper_chunk_sec: int = 600         # длина сегмента при нарезке длинного аудио


settings = Settings()
