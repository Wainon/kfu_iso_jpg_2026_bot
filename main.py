import logging
import re
import json
import os
from datetime import datetime, timedelta
from io import BytesIO

import requests
from PIL import Image, ImageDraw, ImageFont

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------- #
# Настройки
# --------------------------------------------------------------------------- #

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is not set!")
BOT_USERNAME = "@kfu_iso_jpg_2026_bot"

os.makedirs("data", exist_ok=True)
USER_DATA_FILE = "data/user_data.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Загрузка шрифтов (максимально упрощенно)
# --------------------------------------------------------------------------- #

# Пути к системным шрифтам
FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]

def _load_fonts():
    """Загружает шрифты из системы."""
    regular_path = None
    bold_path = None
    
    for path in FONT_PATHS:
        if os.path.exists(path):
            if "Bold" in path:
                bold_path = path
            else:
                regular_path = path
    
    # Если нашли шрифты - используем их
    if regular_path and bold_path:
        try:
            logger.info(f"Загружаем шрифты: regular={regular_path}, bold={bold_path}")
            return {
                "title": ImageFont.truetype(bold_path, 30),
                "day": ImageFont.truetype(bold_path, 18),
                "date": ImageFont.truetype(regular_path, 14),
                "period": ImageFont.truetype(bold_path, 16),
                "time": ImageFont.truetype(regular_path, 13),
                "subject": ImageFont.truetype(bold_path, 14),
                "small": ImageFont.truetype(regular_path, 12),
                "footer": ImageFont.truetype(regular_path, 13),
            }
        except Exception as e:
            logger.error(f"Ошибка загрузки шрифтов: {e}")
    
    # Если шрифтов нет - используем дефолтный
    logger.warning("Шрифты не найдены, используем дефолтный")
    default_font = ImageFont.load_default()
    return {
        "title": default_font,
        "day": default_font,
        "date": default_font,
        "period": default_font,
        "time": default_font,
        "subject": default_font,
        "small": default_font,
        "footer": default_font,
    }


# Проверяем FreeType
try:
    from PIL import features
    if features.check("freetype2"):
        logger.info("✅ Pillow поддерживает FreeType2")
    else:
        logger.error("❌ Pillow НЕ поддерживает FreeType2")
except:
    logger.warning("Не удалось проверить FreeType")

# ... (остальной код бота такой же, как был)
