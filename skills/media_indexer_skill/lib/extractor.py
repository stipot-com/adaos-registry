"""
Модуль извлечения технических метаданных.
Отвечает за работу с бинарными файлами через FFmpeg (видео/аудио) и Pillow (изображения).
"""

import os
import logging
import ffmpeg
from PIL import Image
from typing import Optional, Any
from lib.models import MediaFile, MediaMetadata

# Подключаем встроенный FFmpeg
try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except ImportError:
    logging.warning("Модуль static_ffmpeg не найден. Убедитесь, что FFmpeg установлен в системе.")

logger = logging.getLogger(__name__)

class TechnicalMetadataExtractor:
    """
    Анализатор медиафайлов. Читает заголовки файлов и извлекает кодеки, 
    разрешение, битрейт и длительность, не загружая файл целиком в память.
    """

    def extract(self, media: Any, media_type: Optional[str] = None) -> MediaMetadata:
        """
        Единая точка входа для извлечения данных.
        Поддерживает как объект MediaFile, так и прямую передачу пути (для тестов).
        """
        if hasattr(media, 'media_type'):
            path = media.full_path
            m_type = media.media_type
        else:
            path = str(media)
            m_type = media_type or 'unknown'

        metadata = MediaMetadata(file_type=m_type, file_path=path)
        
        if m_type == 'image':
            return self._extract_image_data(path, metadata)
            
        return self._extract_av_data(path, m_type, metadata)

    def _extract_image_data(self, file_path: str, metadata: MediaMetadata) -> MediaMetadata:
        """Приватный метод обработки изображений через Pillow."""
        try:
            with Image.open(file_path) as img:
                metadata.width, metadata.height = img.size
                metadata.image_format = img.format
                metadata.status = 'success'
        except Exception as e:
            logger.error(f"Ошибка чтения изображения {file_path}: {e}")
            metadata.status = 'error'
            metadata.error_msg = str(e)
        return metadata

    def _extract_av_data(self, file_path: str, media_type: str, metadata: MediaMetadata) -> MediaMetadata:
        """Приватный метод обработки видео и аудио через FFmpeg Probe."""
        try:
            if os.path.getsize(file_path) == 0:
                raise ValueError("Empty file")

            probe = ffmpeg.probe(file_path)
            fmt = probe.get('format', {})
            
            metadata.duration_seconds = float(fmt.get('duration', 0))
            metadata.size_bytes = int(fmt.get('size', 0))
            metadata.bit_rate = int(fmt.get('bit_rate', 0))
            metadata.status = 'success'
            
            if media_type == 'video':
                video_stream = next((s for s in probe['streams'] if s['codec_type'] == 'video'), None)
                if video_stream:
                    metadata.video_codec = video_stream.get('codec_name')
                    metadata.width = int(video_stream.get('width', 0))
                    metadata.height = int(video_stream.get('height', 0))
                    
            elif media_type == 'audio':
                audio_stream = next((s for s in probe['streams'] if s['codec_type'] == 'audio'), None)
                if audio_stream:
                    metadata.audio_codec = audio_stream.get('codec_name')
                    metadata.sample_rate = int(audio_stream.get('sample_rate', 0))
                    
        except Exception as e:
            logger.debug(f"FFmpeg ошибка при обработке {file_path}: {e}")
            metadata.status = 'error'
            metadata.error_msg = str(e)
            
        return metadata