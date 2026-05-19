"""
=============================================================================
Модуль семантического обогащения метаданных.

Тема ВКР: «Индексация медиаконтента и обогащение метаданных
           с использованием интеллектуального анализа данных»

Автор:  Феденко Никита Александрович
Группа: ИД 23.1/Б3-22
Год:    2026

Описание:
    Внешние сервисы обогащения метаданных по типу медиафайла:
        - video → Cinemagoer (IMDb) + локальный fallback-кэш
        - audio → Shazam (через shazamio)
        - image → EasyOCR (распознавание текста на картинке)

    Async-логика заточена под вызов из синхронного контекста
    (включая фоновый QThread в PyQt5) — для каждого вызова
    создаётся изолированный event loop, что гарантирует
    корректную работу в любом потоке и совместимость с
    Python 3.11 и выше.
=============================================================================
"""

import logging
import asyncio
from typing import Dict, Any

logger = logging.getLogger(__name__)


class EnrichmentService:
    def __init__(self):
        logger.info("Инициализация EnrichmentService...")

        try:
            from imdb import Cinemagoer
            self.ia = Cinemagoer()
        except ImportError:
            self.ia = None
            logger.warning("Библиотека Cinemagoer (IMDb) не установлена.")

        try:
            import easyocr
            self.reader = easyocr.Reader(['ru', 'en'])
        except ImportError:
            self.reader = None
            logger.warning("Библиотека EasyOCR не установлена.")

        try:
            from shazamio import Shazam
            self.shazam = Shazam()
            logger.info("Shazamio инициализирован успешно.")
        except ImportError:
            self.shazam = None
            logger.warning("Библиотека Shazamio не установлена.")

        self.local_cache = {
            "inter stellar": "Фильм про космос, черную дыру, гравитацию и спасение человечества.",
            "the dark knight": "Бэтмен противостоит Джокеру в Готэме. Криминальный триллер.",
            "the matrix": "Хакер Нео узнает, что наш мир - это матрица. Киберпанк и ИИ.",
            "terminator": "Робот-убийца из будущего охотится на человека. Научная фантастика.",
            "inception": "Команда проникает в сны людей чтобы украсть секреты. Триллер Нолана.",
            "interstellar": "Фильм про космос, черную дыру, гравитацию и спасение человечества.",
            "incredibles": "Семья супергероев скрывает свои способности. Мультфильм Pixar.",
        }

    def enrich(self, file_path: str, media_type: str) -> Dict[str, Any]:
        if media_type == 'video':
            return {}
        elif media_type == 'audio':
            return self._run_async(self.enrich_audio(file_path))
        elif media_type == 'image':
            return self.enrich_image(file_path)
        return {}

    def enrich_video(self, title: str) -> Dict[str, Any]:
        if self.ia:
            try:
                movies = self.ia.search_movie(title)
                if movies:
                    best_match = movies[0]
                    self.ia.update(best_match, info=['plot'])
                    plot = best_match.get('plot', [''])[0]
                    return {'imdb': {'plot': plot}}
            except Exception as e:
                logger.warning("IMDb недоступен. Активация Fallback-кэша...")

        clean_title_lower = title.lower().strip()
        for key, plot in self.local_cache.items():
            if key in clean_title_lower or clean_title_lower in key:
                logger.info(f"Сюжет из резервного кэша для: '{title}'")
                return {'imdb': {'plot': plot}}

        return {}

    def enrich_image(self, file_path: str) -> Dict[str, Any]:
        """Распознавание текста на изображении через EasyOCR."""
        if not self.reader:
            return {}
        try:
            # Читаем файл через numpy чтобы обойти проблему кириллицы в пути (Windows)
            import numpy as np
            img_array = np.fromfile(file_path, dtype=np.uint8)
            import cv2
            img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
            if img is None:
                logger.warning(f"Не удалось декодировать изображение: {file_path}")
                return {}
            results = self.reader.readtext(img, detail=0)
            text = " ".join(results)
            return {'ocr_text': text} if text else {}
        except Exception as e:
            logger.warning(f"Ошибка EasyOCR при обработке {file_path}: {e}")
            return {}

    async def enrich_audio(self, file_path: str) -> Dict[str, Any]:
        """
        Распознаёт аудиокомпозицию через Shazam.
        Возвращает словарь с ключами shazam_title, shazam_subtitle, shazam_genre.
        """
        if not self.shazam:
            logger.warning("Shazam не инициализирован — пропускаю аудио-обогащение.")
            return {}
        try:
            logger.info(f"Shazam: распознаю файл {file_path}")
            out = await self.shazam.recognize(file_path)

            if 'track' in out:
                track = out['track']
                result = {
                    'shazam_title': track.get('title'),
                    'shazam_subtitle': track.get('subtitle'),
                    'shazam_genre': (track.get('genres') or {}).get('primary'),
                }
                logger.info(
                    f"Shazam распознал: title='{result['shazam_title']}', "
                    f"artist='{result['shazam_subtitle']}', "
                    f"genre='{result['shazam_genre']}'"
                )
                return result
            else:
                logger.info(f"Shazam не нашёл совпадений для {file_path}")
                return {}
        except Exception as e:
            logger.warning(
                f"Ошибка Shazam при обработке {file_path}: "
                f"{type(e).__name__}: {e}"
            )
            return {}

    def _run_async(self, coro) -> Dict[str, Any]:
        """
        Универсальный запуск coroutine из синхронного контекста.

        Создаёт изолированный event loop для каждого вызова —
        это гарантирует работу из любого потока (в том числе из
        фонового QThread в PyQt5-приложении), и совместимо с
        Python 3.11+.

        Метод asyncio.get_event_loop() считается устаревшим
        начиная с Python 3.10 и в новых версиях может выдавать
        DeprecationWarning или некорректное поведение, поэтому
        используется явное создание нового loop.
        """
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()