"""
=============================================================================
Модуль интеллектуального сканирования файловой системы (Media Indexer).

Тема ВКР: «Индексация медиаконтента и обогащение метаданных
           с использованием интеллектуального анализа данных»

Автор:  Феденко Никита Александрович
Группа: ИД 23.1/Б3-22
Год:    2026

Описание:
    Отвечает за рекурсивный обход директорий, глубокую классификацию
    медиафайлов на основе расширений и MIME-типов, вычисление
    контрольных сумм (для поиска дубликатов) и фильтрацию системного
    мусора (.git, __pycache__, $RECYCLE.BIN и т.п.).
=============================================================================
"""

import os
import hashlib
import logging
import mimetypes
from pathlib import Path
from typing import Dict, List, Set, Optional
from collections import defaultdict

from lib.models import MediaFile
from lib.exceptions import MediaScannerError

logger = logging.getLogger(__name__)

class DirectoryScanner:
    """
    Класс для продвинутого сканирования директорий.
    В отличие от простых обходов, использует проверку MIME-типов для защиты
    от подмены расширений файлов (например, когда .exe переименован в .mp4).
    """
    
    # Расширенная конфигурация поддерживаемых форматов
    SUPPORTED_FORMATS: Dict[str, Set[str]] = {
        'video': {'.mp4', '.mkv', '.avi', '.mov', '.flv', '.wmv', '.webm', '.m4v', '.ts'},
        'audio': {'.mp3', '.flac', '.wav', '.m4a', '.aac', '.ogg', '.wma', '.opus', '.alac'},
        'image': {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tiff', '.heic'}
    }

    # Системные папки и файлы, которые сканер должен жестко игнорировать
    IGNORED_DIRECTORIES: Set[str] = {'$RECYCLE.BIN', 'System Volume Information', '.git', '.venv', '__pycache__'}
    IGNORED_PREFIXES: tuple = ('.', '~', '$')

    def __init__(self, root_path: str, compute_hashes: bool = False):
        """
        Инициализация сканера.
        
        Args:
            root_path (str): Путь к директории для сканирования.
            compute_hashes (bool): Включает тяжелую проверку MD5 для поиска дубликатов.
        """
        self.root_path = Path(root_path).resolve()
        self.compute_hashes = compute_hashes
        self.media_inventory: Dict[str, List[MediaFile]] = defaultdict(list)
        
        # Инициализируем системную базу MIME-типов
        mimetypes.init()

    def validate_path(self) -> None:
        """
        Проверка доступности директории.
        
        Raises:
            MediaScannerError: Если путь не существует, не является папкой или нет прав доступа.
        """
        if not self.root_path.exists():
            raise MediaScannerError(str(self.root_path), "Указанный путь не существует в файловой системе.")
        if not self.root_path.is_dir():
            raise MediaScannerError(str(self.root_path), "Указанный путь не является директорией.")
        if not os.access(self.root_path, os.R_OK):
            raise MediaScannerError(str(self.root_path), "Нет прав на чтение директории (Access Denied).")

    def _compute_file_hash(self, file_path: Path, chunk_size: int = 8192) -> Optional[str]:
        """
        Вычисляет MD5 хэш файла по кускам (чтобы не забить оперативную память видеофайлами по 50 ГБ).
        
        Args:
            file_path (Path): Объект пути к файлу.
            chunk_size (int): Размер блока чтения в байтах.
            
        Returns:
            Optional[str]: MD5 хэш в виде hex-строки или None при ошибке чтения.
        """
        if not self.compute_hashes:
            return None
            
        md5_hash = hashlib.md5()
        try:
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(chunk_size), b""):
                    md5_hash.update(chunk)
            return md5_hash.hexdigest()
        except OSError as e:
            logger.warning(f"Не удалось вычислить хэш для {file_path.name}: {e}")
            return None

    def _verify_mime_type(self, file_path: Path, expected_category: str) -> bool:
        """
        Проверяет, соответствует ли реальный MIME-тип файла его расширению.
        Защита от битых файлов и вирусов.
        """
        mime_type, _ = mimetypes.guess_type(str(file_path))
        if not mime_type:
            # Если система не знает формат, доверяем расширению (fallback)
            return True 
            
        primary_type = mime_type.split('/')[0]
        return primary_type == expected_category

    def scan(self) -> Dict[str, List[MediaFile]]:
        """
        Рекурсивный обход и интеллектуальный сбор информации о файлах.
        
        Returns:
            Dict[str, List[MediaFile]]: Словарь с распределенными медиафайлами.
        """
        logger.info(f"Запуск глубокого сканирования директории: {self.root_path}")
        self.media_inventory.clear()
        self.validate_path()

        total_files_found = 0
        skipped_files = 0

        # os.walk используется вместо rglob для более тонкого контроля над игнорированием папок
        for root, dirs, files in os.walk(self.root_path):
            # Модифицируем список dirs in-place, чтобы os.walk не заходил в системные папки
            dirs[:] = [d for d in dirs if d not in self.IGNORED_DIRECTORIES and not d.startswith(self.IGNORED_PREFIXES)]
            
            current_root = Path(root)
            
            for file_name in files:
                if file_name.startswith(self.IGNORED_PREFIXES):
                    continue
                    
                file_path = current_root / file_name
                ext = file_path.suffix.lower()
                
                # Ищем категорию по расширению
                detected_category = None
                for category, extensions in self.SUPPORTED_FORMATS.items():
                    if ext in extensions:
                        detected_category = category
                        break
                        
                if not detected_category:
                    skipped_files += 1
                    continue
                    
                # Дополнительная проверка MIME
                if not self._verify_mime_type(file_path, detected_category):
                    logger.warning(f"Обнаружена подмена формата: {file_name}. Файл пропущен.")
                    skipped_files += 1
                    continue

                try:
                    size_mb = round(file_path.stat().st_size / (1024 * 1024), 3)
                    
                    media_obj = MediaFile(
                        full_path=str(file_path),
                        name=file_name,
                        relative_path=str(file_path.relative_to(self.root_path)),
                        size_mb=size_mb,
                        extension=ext,
                        media_type=detected_category
                    )
                    
                    self.media_inventory[detected_category].append(media_obj)
                    total_files_found += 1
                    
                except OSError as e:
                    logger.error(f"Системная ошибка доступа к файлу {file_name}: {e}")

        logger.info(f"Сканирование завершено. Успешно: {total_files_found}. Пропущено мусора: {skipped_files}")
        return dict(self.media_inventory)