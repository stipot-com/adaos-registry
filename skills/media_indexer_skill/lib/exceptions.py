"""
Модуль кастомных исключений системы.
Позволяет точно идентифицировать, на каком этапе обработки произошел сбой.
"""

class MediaSystemError(Exception):
    """Базовый класс исключений для всего приложения."""
    pass

class ConfigurationError(MediaSystemError):
    """Ошибка конфигурации или отсутствия переменных окружения."""
    pass

class DatabaseConnectionError(MediaSystemError):
    """Сбой подключения к SQLite."""
    pass

class MediaScannerError(MediaSystemError):
    """Ошибка при сканировании директорий или доступе к файловой системе."""
    pass

class MetadataExtractionError(MediaSystemError):
    """Сбой при извлечении технических данных через FFmpeg или Pillow."""
    pass

class EnrichmentServiceError(MediaSystemError):
    """Ошибка API внешних сервисов (Shazam, IMDb)."""
    pass

class NERPredictionError(MediaSystemError):
    """Сбой инференса нейросети или токенизатора."""
    pass

class VectorDBError(MediaSystemError):
    """Ошибка при работе с векторными индексами FAISS."""
    pass