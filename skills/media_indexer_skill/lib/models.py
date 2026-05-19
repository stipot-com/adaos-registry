"""
Модуль моделей данных (Data Models) системы Media Indexer.
Содержит DTO (Data Transfer Objects) для строгой типизации передаваемых данных
между слоями архитектуры приложения. Включает встроенную валидацию данных.
"""

import json
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, Optional
from datetime import datetime


@dataclass
class MediaFile:
    """
    Базовая структура найденного медиафайла на жестком диске.
    Описывает физические параметры файла до этапа извлечения глубоких метаданных.
    
    Attributes:
        full_path (str): Абсолютный путь к файлу в системе.
        name (str): Имя файла с расширением (например, 'movie.mp4').
        relative_path (str): Путь относительно корневой директории сканирования.
        size_mb (float): Размер файла в мегабайтах.
        extension (str): Расширение файла (в нижнем регистре, с точкой).
        media_type (str): Категория контента ('video', 'audio', 'image').
        discovered_at (str): Время обнаружения файла сканером (ISO формат).
    """
    full_path: str
    name: str
    relative_path: str
    size_mb: float
    extension: str
    media_type: str
    discovered_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def __post_init__(self):
        """
        Валидация данных после инициализации объекта.
        Гарантирует, что DTO не содержит логических ошибок.
        """
        if self.size_mb < 0:
            raise ValueError(f"Размер файла не может быть отрицательным: {self.size_mb}")
        if not self.extension.startswith('.'):
            self.extension = f".{self.extension}"
        self.extension = self.extension.lower()

    @property
    def is_large_file(self) -> bool:
        """Определяет, является ли файл тяжелым (более 1 ГБ)."""
        return self.size_mb > 1024.0

    def to_json(self) -> str:
        """Сериализует объект в JSON-строку."""
        return json.dumps(asdict(self), ensure_ascii=False)


@dataclass
class MediaMetadata:
    """
    Структура для хранения технических метаданных.
    Заполняется модулем TechnicalMetadataExtractor (FFmpeg / Pillow).
    
    Attributes:
        file_type (str): Тип медиафайла.
        file_path (str): Путь к анализируемому файлу.
        status (str): Статус извлечения ('success', 'error').
        error_msg (Optional[str]): Текст ошибки, если status == 'error'.
        size_bytes (int): Точный размер в байтах.
        duration_seconds (float): Длительность (для видео и аудио).
        bit_rate (int): Битрейт медиапотока.
        width (int): Ширина в пикселях (видео/изображения).
        height (int): Высота в пикселях (видео/изображения).
        video_codec (Optional[str]): Кодек видеопотока (например, h264).
        audio_codec (Optional[str]): Кодек аудиопотока (например, aac).
        sample_rate (int): Частота дискретизации аудио.
        image_format (Optional[str]): Формат изображения (JPEG, PNG).
    """
    file_type: str
    file_path: str
    status: str = "success"
    error_msg: Optional[str] = None
    
    # Общие технические параметры
    size_bytes: int = 0
    
    # Специфичные для мультимедиа (Видео / Аудио)
    duration_seconds: float = 0.0
    bit_rate: int = 0
    
    # Специфичные для визуального контента (Видео / Изображения)
    width: int = 0
    height: int = 0
    
    # Глубокие кодеки
    video_codec: Optional[str] = None
    audio_codec: Optional[str] = None
    sample_rate: int = 0
    image_format: Optional[str] = None

    def __post_init__(self):
        """Базовая очистка и валидация метаданных."""
        if self.duration_seconds < 0:
            self.duration_seconds = 0.0
        if self.status not in ["success", "error", "processing"]:
            self.status = "error"

    @property
    def resolution(self) -> str:
        """Возвращает разрешение в формате WxH, либо 'Unknown'."""
        if self.width > 0 and self.height > 0:
            return f"{self.width}x{self.height}"
        return "Unknown"

    def to_dict(self) -> Dict[str, Any]:
        """
        Умная сериализация в словарь.
        Удаляет все пустые (None) значения для экономии места в БД.
        
        Returns:
            Dict[str, Any]: Очищенный словарь метаданных.
        """
        return {k: v for k, v in asdict(self).items() if v is not None}