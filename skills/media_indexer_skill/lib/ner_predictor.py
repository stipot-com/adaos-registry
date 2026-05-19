"""
=============================================================================
ФАЙЛ МОДЕЛИ — Инференс DistilBERT NER

Тема ВКР: «Индексация медиаконтента и обогащение метаданных
           с использованием интеллектуального анализа данных»

Автор:  Феденко Никита Александрович
Группа: ИД 23.1/Б3-22
Год:    2026

Описание:
    Модуль загружает веса предобученной DistilBERT-модели (model2.pt,
    версия v2 — multilingual) и применяет её для извлечения
    именованных сущностей из имён медиафайлов. Распознаваемые сущности:
        - TITLE   (название произведения)
        - YEAR    (год выпуска)
        - QUALITY (качество видео/аудио)
        - ARTIST  (исполнитель / автор)

    Базовая модель distilbert-base-multilingual-cased выбрана по итогам
    Этапа 3 как наилучшая на реалистичном тесте (см. RESEARCH_LOG,
    Запись №4): F1 = 0.9232, Token Accuracy = 0.9258 на out-of-distribution
    наборе из 52 размеченных вручную имён медиафайлов.

    Логика сборки сущностей:
        - инференс модели на subword-токенах WordPiece
        - сборка subword-токенов в исходные слова через is_continuation
          (для multilingual-токенизатора с префиксами ##)
        - для каждого слова берётся метка ПЕРВОГО subtoken'a
        - регекс-постобработка для нормализации технических токенов
          (1080p, BluRay, x264 и т.д.)
=============================================================================
"""

import os
import re
import logging
import torch
from typing import Dict, List
from transformers import DistilBertTokenizerFast, DistilBertForTokenClassification

logger = logging.getLogger(__name__)

LABELS = ["O", "B-TITLE", "I-TITLE", "B-YEAR", "I-YEAR", "B-QUALITY", "I-QUALITY", "B-ARTIST", "I-ARTIST"]
ID2LABEL = {i: label for i, label in enumerate(LABELS)}

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_WEIGHTS_PATH = os.path.join(BASE_DIR, 'ml', 'weights', 'model2.pt')
GDRIVE_MODEL_ID = "19YBXzTYLoizbm8RF8gigUQ0fApZVmpoZ"
GDRIVE_MODEL_URL = f"https://drive.google.com/uc?id={GDRIVE_MODEL_ID}"
BASE_MODEL_NAME = 'distilbert-base-multilingual-cased'



def _ensure_weights_downloaded(weights_path):
    """
    Если файл весов отсутствует — скачивает его с Google Drive
    в указанную локацию через gdown.
    """
    import os
    weights_path = str(weights_path)
    if os.path.exists(weights_path):
        return

    import gdown
    import pathlib

    target_dir = pathlib.Path(weights_path).parent
    target_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Веса модели не найдены локально. Скачиваем с Google Drive в {weights_path}")
    gdown.download(GDRIVE_MODEL_URL, weights_path, quiet=False)
    logger.info("Скачивание модели завершено")


class NERPredictor:
    def __init__(self):
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        logger.info(f"Инициализация NERPredictor на устройстве: {self.device}")

        try:
            self.tokenizer = DistilBertTokenizerFast.from_pretrained(BASE_MODEL_NAME)
            self.model = DistilBertForTokenClassification.from_pretrained(
                BASE_MODEL_NAME,
                num_labels=len(LABELS)
            )
            self._load_weights()
        except Exception as e:
            logger.error(f"Критическая ошибка при инициализации NER-модели: {e}")
            self.model = None

    def _load_weights(self):
        _ensure_weights_downloaded(MODEL_WEIGHTS_PATH)
        if os.path.exists(MODEL_WEIGHTS_PATH):
            state_dict = torch.load(MODEL_WEIGHTS_PATH, map_location=self.device, weights_only=True)
            self.model.load_state_dict(state_dict)
            self.model.to(self.device)
            self.model.eval()
            logger.info("Успех: Локальные веса NER-модели загружены.")
        else:
            logger.warning("Веса модели не найдены. Будет использована базовая модель.")
            self.model.to(self.device)

    def extract_entities(self, text: str) -> Dict[str, str]:
        if not self.model or not text.strip():
            return {}

        # ===== Препроцессинг =====
        # 1. Удаляем расширение файла
        clean_text = re.sub(r'\.[a-zA-Z0-9]{2,4}$', '', text)
        # 2. Убираем все виды скобок (круглые, квадратные, фигурные)
        clean_text = re.sub(r'[\(\)\[\]\{\}]', ' ', clean_text)
        # 3. Заменяем разделители имён файлов на пробелы
        clean_text = clean_text.replace('.', ' ').replace('_', ' ').replace('-', ' ')
        # 4. Схлопываем последовательные пробелы
        clean_text = re.sub(r'\s+', ' ', clean_text).strip()

        if not clean_text:
            return {}

        # ===== Инференс на уровне СЛОВ (а не subword'ов) =====
        # Разбиваем строку на слова и подаём как is_split_into_words=True.
        # Тогда tokenizer создаст word_ids() — отображение subtoken→word,
        # которое позволит правильно собрать предсказания на уровне слов.
        words = clean_text.split()
        if not words:
            return {}

        encoding = self.tokenizer(
            words,
            is_split_into_words=True,
            return_tensors="pt",
            truncation=True,
            max_length=128,
        )
        word_ids = encoding.word_ids()
        inputs = {k: v.to(self.device) for k, v in encoding.items()}

        with torch.no_grad():
            outputs = self.model(**inputs)
            logits = outputs.logits
            predictions = torch.argmax(logits, dim=2).squeeze().tolist()

        # Для каждого слова берём метку ПЕРВОГО subtoken'a
        word_to_label = {}
        for tok_idx, word_idx in enumerate(word_ids):
            if word_idx is None:
                continue
            if word_idx not in word_to_label:
                word_to_label[word_idx] = ID2LABEL[predictions[tok_idx]]

        # ===== Группировка слов по сущностям =====
        extracted_data = {"title": [], "year": [], "quality": [], "artist": []}

        for word_idx, word in enumerate(words):
            label = word_to_label.get(word_idx, "O")
            if label == "O":
                continue
            if "TITLE" in label:
                extracted_data["title"].append(word)
            elif "YEAR" in label:
                extracted_data["year"].append(word)
            elif "QUALITY" in label:
                extracted_data["quality"].append(word)
            elif "ARTIST" in label:
                extracted_data["artist"].append(word)

        return {
            "title":   self._clean_assembled_text(extracted_data["title"]),
            "year":    self._clean_assembled_text(extracted_data["year"]),
            "quality": self._clean_assembled_text(extracted_data["quality"]),
            "artist":  self._clean_assembled_text(extracted_data["artist"]),
        }

    def _clean_assembled_text(self, words: List[str]) -> str:
        """
        Постобработка сущности: соединение слов в строку и нормализация
        типичных технических токенов (форматы качества, кодеки и т.д.).
        """
        if not words:
            return ""

        assembled = " ".join(words)

        # Нормализация технических токенов:

        # BluRay (на случай если разделитель внутри попал)
        assembled = re.sub(r'\bBlu\s*-?\s*Ray\b', 'BluRay', assembled, flags=re.IGNORECASE)

        # WEB-DL, WEB-Rip
        assembled = re.sub(r'\bWEB\s+DL\b',  'WEB-DL',  assembled, flags=re.IGNORECASE)
        assembled = re.sub(r'\bWEB\s+Rip\b', 'WEB-Rip', assembled, flags=re.IGNORECASE)

        # HDRip, HDTV — на случай если попали раздельно
        assembled = re.sub(r'\bHD\s+(Rip|TV)\b', r'HD\1', assembled, flags=re.IGNORECASE)

        return assembled.strip()