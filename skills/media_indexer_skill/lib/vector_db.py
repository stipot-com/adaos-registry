"""
=============================================================================
Модуль векторного поиска на базе FAISS и мультимодальных нейросетей.

Тема ВКР: «Индексация медиаконтента и обогащение метаданных
           с использованием интеллектуального анализа данных»

Автор:  Феденко Никита Александрович
Группа: ИД 23.1/Б3-22
Год:    2026

Описание:
    Реализует двухканальный семантический поиск:
        - text-индекс  (Sentence-Transformer MiniLM, мультиязычный)
        - image-индекс (CLIP ViT-B/32 для визуального содержимого)

    Для текстового канала используется распределённая модель
    distiluse-base-multilingual-cased-v2, обеспечивающая корректную
    обработку русскоязычных и англоязычных запросов в едином
    эмбеддинг-пространстве.

    Для визуального канала используется CLIP с мультиязычным
    текстовым энкодером (clip-ViT-B-32-multilingual-v1), что
    позволяет искать изображения по русскоязычным запросам.
=============================================================================
"""

import logging
import numpy as np
from pathlib import Path
from PIL import Image
from typing import Dict, List, Any

import faiss
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)


class VectorDatabase:
    """Мультимодальная векторная база данных."""

    # Минимальные пороги для попадания результата в выдачу
    # (косинусное сходство, нормализованное в [0, 1] -> в проценты при выдаче)
    TEXT_MIN_SIMILARITY = 0.10   # 10% — text-канал
    IMAGE_MIN_SIMILARITY = 0.22  # 22% — image-канал (CLIP даёт высокие значения)

    def __init__(self):
        logger.info("Инициализация текстовой модели семантического поиска...")
        self.text_model = SentenceTransformer('distiluse-base-multilingual-cased-v2')

        logger.info("Инициализация визуальной модели (CLIP)...")
        self.clip_text = SentenceTransformer('clip-ViT-B-32-multilingual-v1')
        self.clip_vision = SentenceTransformer('clip-ViT-B-32')

        self.text_index = faiss.IndexFlatIP(512)
        self.image_index = faiss.IndexFlatIP(512)

        self.text_docs: List[Dict] = []
        self.image_docs: List[Dict] = []
        logger.info("Векторные модели (FAISS + CLIP) успешно загружены.")

    def add_text(self, text: str, payload: Dict):
        if not text.strip():
            return
        emb = self.text_model.encode(text, normalize_embeddings=True).astype('float32')
        self.text_index.add(np.array([emb]))
        self.text_docs.append({'text': text, 'payload': payload})

    def add_image(self, image_path: str, payload: Dict):
        try:
            img = Image.open(image_path)
            emb = self.clip_vision.encode(img, normalize_embeddings=True).astype('float32')
            self.image_index.add(np.array([emb]))
            self.image_docs.append({
                'text': f"[ВИЗУАЛ] {Path(image_path).name}",
                'payload': payload
            })
        except Exception as e:
            logger.warning(f"Ошибка CLIP при чтении {image_path}: {e}")

    def search(self, query: str, k: int = 5) -> List[Dict]:
        """
        Ищет файлы по смыслу запроса (сразу по тексту и по изображениям).

        Возвращает объединённый список результатов, отсортированный
        по убыванию сходства. Score выдаётся в процентах [0..100].
        """
        results = []

        # 1. Текстовый поиск
        if self.text_docs:
            q_text_emb = self.text_model.encode(query, normalize_embeddings=True).astype('float32')
            D_t, I_t = self.text_index.search(np.array([q_text_emb]), k)
            for i, similarity in zip(I_t[0], D_t[0]):
                if i == -1 or i >= len(self.text_docs):
                    continue
                if similarity >= self.TEXT_MIN_SIMILARITY:
                    score = round(float(similarity) * 100, 1)
                    res = self.text_docs[i].copy()
                    res['score'] = score
                    res['type'] = '🎬 [МЕДИА/ТЕКСТ]'
                    results.append(res)

        # 2. Визуальный поиск (CLIP по изображениям)
        # Score выдаётся честный (без искусственного растягивания) —
        # это позволяет text-каналу конкурировать с image-каналом.
        if self.image_docs:
            q_img_emb = self.clip_text.encode(query, normalize_embeddings=True).astype('float32')
            D_i, I_i = self.image_index.search(np.array([q_img_emb]), k)
            for i, similarity in zip(I_i[0], D_i[0]):
                if i == -1 or i >= len(self.image_docs):
                    continue
                raw_sim = float(similarity)
                if raw_sim >= self.IMAGE_MIN_SIMILARITY:
                    score = round(raw_sim * 100, 1)
                    res = self.image_docs[i].copy()
                    res['score'] = score
                    res['type'] = '📸 [ФОТО]'
                    results.append(res)

        # Сортируем все результаты по убыванию score
        results.sort(key=lambda x: x['score'], reverse=True)
        return results[:k]