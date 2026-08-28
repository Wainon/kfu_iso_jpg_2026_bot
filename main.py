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

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------
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
logger = logging.getLogger(__name__)  # ИСПРАВЛЕНО: было logging.getLogger(name)

WAITING_GROUP, CHOOSE_PARITY, CHOOSE_SUBGROUP, WAITING_ACTION = range(4)

# ИСПРАВЛЕНО: убраны лишние пробелы в концах строк
DAY_NAMES = {1: "Пн", 2: "Вт", 3: "Ср", 4: "Чт", 5: "Пт", 6: "Сб"}
PARITY_LABEL = {"ch": "чётная", "nch": "нечётная"}
PARITY_LESSON_VALUE = {"ch": "чёт", "nch": "нечёт"}

MAIN_KEYBOARD = ReplyKeyboardMarkup([
    [KeyboardButton("/start"), KeyboardButton("/my")]
], resize_keyboard=True)

# ---------------------------------------------------------------------------
# Сохранение и загрузка данных пользователей
# ---------------------------------------------------------------------------
def load_user_data() -> dict:
    if os.path.exists(USER_DATA_FILE):
        try:
            with open(USER_DATA_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as exc:
            logger.error("Ошибка загрузки данных пользователей: %s", exc)
            return {}
    return {}

def save_user_data(user_data: dict):
    try:
        with open(USER_DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(user_data, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.error("Ошибка сохранения данных пользователей: %s", exc)

# ---------------------------------------------------------------------------
# Утилиты и парсинг
# ---------------------------------------------------------------------------
def parse_group_input(user_input: str) -> tuple:
    """Разбирает ввод пользователя: 'ИВТ-б-о-242(2)' -> ('ИВТ-б-о-242', '2').
    Если подгруппа не указана, возвращает '1' по умолчанию."""
    user_input = user_input.strip()
    # ИСПРАВЛЕНО: регулярное выражение для скобок
    match = re.match(r'^(.+?)\s*\((\d+)\)\s*$', user_input)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return user_input, "1"  # По умолчанию подгруппа 1

def clean_subgroup(sg_value):
    """Извлекает номер подгруппы из строки API: '(п/гр 2)' -> '2'."""
    if not sg_value:
        return None
    sg_str = str(sg_value).strip()
    match = re.search(r'\d+', sg_str)
    if match:
        return match.group()
    return sg_str

# ---------------------------------------------------------------------------
# Запросы к API КФУ
# ---------------------------------------------------------------------------
def get_schedule(group_code: str):
    url = f"https://cfuv.ru/wp-json/cfu/v1/sched/group?code={group_code}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        logger.error("Ошибка загрузки расписания: %s", exc)
        return None

def get_index():
    try:
        response = requests.get("https://cfuv.ru/wp-json/cfu/v1/sched/index", timeout=10)
        return response.json() if response.ok else {}
    except Exception as exc:
        logger.error("Ошибка загрузки индекса: %s", exc)
        return {}

# ---------------------------------------------------------------------------
# Логика недель, чётности и подгрупп
# ---------------------------------------------------------------------------
def find_week_monday(index_data: dict, parity: str) -> datetime:
    weeks = index_data.get("weeks", {})
    target_dates = set(weeks.get(parity, []))
    today = datetime.now()
    monday = today - timedelta(days=today.weekday())
    for _ in range(10):
        week_dates = [(monday + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6)]
        if any(d in target_dates for d in week_dates):
            return monday
        monday += timedelta(weeks=1)
    return today - timedelta(days=today.weekday())

def filter_lessons(data: dict, monday: datetime, parity: str, subgroup: str):
    """Отбирает занятия для конкретной недели, чётности и подгруппы.
    subgroup="all" - служебный режим для получения всех занятий.
    subgroup="1" или "2" - показывает только выбранную подгруппу."""
    lessons = data.get("занятия", [])
    week_dates = {(monday + timedelta(days=i)).strftime("%Y-%m-%d"): i + 1 for i in range(6)}
    
    # ИСПРАВЛЕНО: теперь ключ "ch" или "nch" найдется без ошибки KeyError
    parity_value = PARITY_LESSON_VALUE[parity]
    result = []
    
    for lesson in lessons:
        lesson_date = lesson.get("дата")
        if lesson_date:
            if lesson_date not in week_dates:
                continue
        else:
            lesson_parity = lesson.get("чётность", "обе")
            if lesson_parity not in ("обе", parity_value):
                continue
            if lesson.get("день") not in DAY_NAMES:
                continue
                
        lesson_subgroup_raw = lesson.get("подгруппа")
        lesson_subgroup = clean_subgroup(lesson_subgroup_raw)
        
        if subgroup != "all":
            if lesson_subgroup is None:
                pass  # Общее занятие - показываем всегда
            elif str(lesson_subgroup) == str(subgroup):
                pass  # Совпадает с выбранной подгруппой
            else:
                continue  # Другая подгруппа - пропускаем
        result.append(lesson)
        
    logger.info(f"Отфильтровано занятий: {len(result)} для подгруппы {subgroup}")
    return result

def get_available_subgroups(lessons: list) -> list:
    """Возвращает отсортированный список номеров подгрупп, встречающихся в занятиях."""
    subgroups = set()
    for lesson in lessons:
        sg = lesson.get("подгруппа")
        if sg:
            clean_sg = clean_subgroup(sg)
            if clean_sg is not None:
                subgroups.add(clean_sg)
    
    def sort_key(x):
        return int(x) if str(x).isdigit() else 0
    return sorted(list(subgroups), key=sort_key)

# ---------------------------------------------------------------------------
# Отрисовка изображения расписания
# ---------------------------------------------------------------------------
def _load_fonts():
    candidates_bold = ["DejaVuSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf"]
    candidates_regular = ["DejaVuSans.ttf", "Arial.ttf", "arial.ttf"]
    
    def _load(names, size):
        for name in names:
            try:
                return ImageFont.truetype(name, size)
            except Exception:
                continue
        return ImageFont.load_default()
        
    return {
        "title": _load(candidates_bold, 30),
        "day": _load(candidates_bold, 18),
        "date": _load(candidates_regular, 14),
        "period": _load(candidates_bold, 16),
        "time": _load(candidates_regular, 13),
        "subject": _load(candidates_bold, 14),
        "small": _load(candidates_regular, 12),
        "footer": _load(candidates_regular, 13),
    }

# ИСПРАВЛЕНО: убраны пробелы в концах строк ключей
LESSON_COLORS = {
    "ЛК": ("#cdeecd", "#8fcf8f"),
    "ПЗ": ("#f4ddc0", "#d8ab72"),
    "ЛР": ("#f4ddc0", "#d8ab72"),
    "ЭЛЕКТИВНАЯ": ("#f0eec0", "#cdc774"),
}
EMPTY_COLOR = "#e8e8e8"
EMPTY_BORDER = "#d3d3d3"
HEADER_BG = "#bdbdbd"

def _cell_colors(lesson: dict):
    subject = (lesson.get("предмет") or " ").upper()
    lesson_type = (lesson.get("вид") or " ").upper()
    if "ЭЛЕКТИВ" in subject or "ЭЛЕКТИВ" in lesson_type:
        return LESSON_COLORS["ЭЛЕКТИВНАЯ"]
    return LESSON_COLORS.get(lesson_type, ("#faf7f0", "#c5a253"))

def create_schedule_image(group_code: str, lessons: list, index_data: dict, monday: datetime, parity: str, subgroup: str) -> BytesIO:
    fonts = _load_fonts()
    bells = {bell["пара"]: bell for bell in index_data.get("bells", [])}
    group_info = index_data.get("groups", {}).get(group_code, {})
    days_data = {day: {} for day in range(1, 7)}
    
    for lesson in lessons:
        day = lesson.get("день")
        para = lesson.get("пара")
        if day in days_data and para:
            days_data[day][para] = lesson
            
    para_list = list(range(1, 8))
    today_str = datetime.now().strftime("%Y-%m-%d")
    PERIOD_COL_WIDTH = 130
    DAY_COL_WIDTH = 230
    HEADER_HEIGHT = 90
    DAY_HEADER_HEIGHT = 70
    CELL_HEIGHT = 105
    FOOTER_HEIGHT = 45
    MARGIN = 20
    
    width = MARGIN * 2 + PERIOD_COL_WIDTH + DAY_COL_WIDTH * 6
    height = HEADER_HEIGHT + DAY_HEADER_HEIGHT + CELL_HEIGHT * len(para_list) + FOOTER_HEIGHT + MARGIN
    img = Image.new("RGB", (width, height), color="#ffffff")
    draw = ImageDraw.Draw(img)
    
    x0 = MARGIN
    y = MARGIN
    subgroup_suffix = f"({subgroup})" if subgroup != "all" else ""
    title = f"{group_code}{subgroup_suffix} | {group_info.get('course', '')} Курс | {PARITY_LABEL[parity]} неделя"
    bbox = draw.textbbox((0, 0), title, font=fonts["title"])
    title_w = bbox[2] - bbox[0]
    draw.text(((width - title_w) / 2, y), title, fill="#111111", font=fonts["title"])
    y += HEADER_HEIGHT - 30
    draw.line([(MARGIN, y), (width - MARGIN, y)], fill="#111111", width=3)
    y += 20
    
    for i, day in enumerate(range(1, 7)):
        x = x0 + PERIOD_COL_WIDTH + i * DAY_COL_WIDTH
        day_date = monday + timedelta(days=day - 1)
        date_str = day_date.strftime("%d.%m.%Y")
        is_today = day_date.strftime("%Y-%m-%d") == today_str
        draw.rectangle([x, y, x + DAY_COL_WIDTH - 6, y + DAY_HEADER_HEIGHT], fill=HEADER_BG)
        day_label = DAY_NAMES[day] + (" •" if is_today else "")
        bbox = draw.textbbox((0, 0), day_label, font=fonts["day"])
        dw = bbox[2] - bbox[0]
        draw.text((x + (DAY_COL_WIDTH - 6 - dw) / 2, y + 10), day_label, fill="#111111", font=fonts["day"])
        bbox = draw.textbbox((0, 0), date_str, font=fonts["date"])
        dw = bbox[2] - bbox[0]
        draw.text((x + (DAY_COL_WIDTH - 6 - dw) / 2, y + 38), date_str, fill="#333333", font=fonts["date"])
        
    draw.rectangle([x0, y, x0 + PERIOD_COL_WIDTH - 6, y + DAY_HEADER_HEIGHT], fill=HEADER_BG)
    draw.text((x0 + 20, y + 25), "Время", fill="#111111", font=fonts["day"])
    y += DAY_HEADER_HEIGHT
    
    for para_num in para_list:
        bell = bells.get(para_num, {})
        start_time = (bell.get("начало") or "")[:5]
        end_time = (bell.get("конец") or "")[:5]
        draw.rectangle([x0, y, x0 + PERIOD_COL_WIDTH - 6, y + CELL_HEIGHT - 6], fill=HEADER_BG)
        draw.text((x0 + 20, y + 15), str(para_num), fill="#111111", font=fonts["period"])
        draw.text((x0 + 20, y + 45), start_time, fill="#333333", font=fonts["time"])
        draw.text((x0 + 20, y + 65), end_time, fill="#333333", font=fonts["time"])
        
        for i, day in enumerate(range(1, 7)):
            x = x0 + PERIOD_COL_WIDTH + i * DAY_COL_WIDTH
            lesson = days_data[day].get(para_num)
            if lesson:
                fill_color, border_color = _cell_colors(lesson)
            else:
                fill_color, border_color = EMPTY_COLOR, EMPTY_BORDER
            draw.rectangle([x, y, x + DAY_COL_WIDTH - 6, y + CELL_HEIGHT - 6], fill=fill_color, outline=border_color, width=1)
            
            if lesson:
                lesson_type = lesson.get("вид", "")
                subject = lesson.get("предмет", "")
                if lesson_type:
                    draw.text((x + 10, y + 8), lesson_type, fill="#555555", font=fonts["small"])
                max_chars = 26
                words = subject.split()
                lines, current = [], ""
                for word in words:
                    if len(current) + len(word) + 1 <= max_chars:
                        current = f"{current} {word}".strip()
                    else:
                        lines.append(current)
                        current = word
                if current:
                    lines.append(current)
                lines = lines[:2]
                text_y = y + 24
                for line in lines:
                    draw.text((x + 10, text_y), line, fill="#111111", font=fonts["subject"])
                    text_y += 18
                    
                teachers = ", ".join(lesson.get("преподаватели", []) or [])
                if teachers:
                    draw.text((x + 10, y + CELL_HEIGHT - 46), teachers[:32], fill="#6b6b6b", font=fonts["small"])
                room = lesson.get("аудитория", "") or ""
                if lesson.get("корпус"):
                    room = f"{room} {lesson['корпус']}".strip()
                if room:
                    draw.text((x + 10, y + CELL_HEIGHT - 28), room[:32], fill="#6b6b6b", font=fonts["small"])
        y += CELL_HEIGHT
        
    footer_text = f"Создано: {datetime.now().strftime('%A, %d %B %Y %H:%M:%S')} {BOT_USERNAME}"
    bbox = draw.textbbox((0, 0), footer_text, font=fonts["footer"])
    fw = bbox[2] - bbox[0]
    draw.text(((width - fw) / 2, y + 10), footer_text, fill="#999999", font=fonts["footer"])
    
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    buffer.name = f"{group_code}_schedule.png"
    return buffer

# ---------------------------------------------------------------------------
# Обработчики бота
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = str(update.effective_user.id)
    logger.info(f"Пользователь {user_id} вызвал /start")
    all_user_data = load_user_data()
    
    if user_id in all_user_data:
        logger.info(f"Найдены сохранённые данные для {user_id}: {all_user_data[user_id]}")
        context.user_data.update(all_user_data[user_id])
        await update.message.reply_text(
            f"Добро пожаловать! Ваша сохранённая группа: {context.user_data.get('group_code', 'неизвестно')}",
            reply_markup=MAIN_KEYBOARD
        )
        return await my_schedule(update, context)
    else:
        logger.info(f"Новый пользователь {user_id}, запрашиваем группу")
        context.user_data.clear()
        await update.message.reply_text(
            "Введите код группы (например: ИВТ-б-о-242(2)).\n\n"
            "Если не указать подгруппу, будет выбрана подгруппа 1.\n\n",
            reply_markup=MAIN_KEYBOARD
        )
        return WAITING_GROUP

async def my_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if "group_code" not in context.user_data:
        await update.message.reply_text(
            "У вас нет сохранённой группы. Введите /start, чтобы выбрать группу.",
            reply_markup=MAIN_KEYBOARD
        )
        return WAITING_GROUP
        
    await update.message.reply_text("🔄 Загружаю сохранённое расписание...")
    group_code = context.user_data["group_code"]
    data = get_schedule(group_code)
    
    if not data or not data.get("занятия"):
        await update.message.reply_text(
            "Не удалось загрузить расписание. Возможно, код группы устарел.\n"
            "Введите /start для выбора новой группы."
        )
        context.user_data.clear()
        return WAITING_GROUP
        
    context.user_data["data"] = data
    context.user_data["index_data"] = get_index()
    
    if "parity" not in context.user_data:
        context.user_data["parity"] = "ch"
        context.user_data["monday"] = find_week_monday(context.user_data["index_data"], "ch")
    else:
        context.user_data["monday"] = find_week_monday(context.user_data["index_data"], context.user_data["parity"])
        
    subgroup = context.user_data.get("subgroup", "1")
    monday = context.user_data["monday"]
    parity = context.user_data["parity"]
    
    all_lessons = filter_lessons(data, monday, parity, subgroup="all")
    available_subgroups = get_available_subgroups(all_lessons)
    
    if str(subgroup) not in available_subgroups:
        logger.warning(f"Подгруппа {subgroup} не найдена в расписании, сбрасываем на 1")
        context.user_data["subgroup"] = "1"
        subgroup = "1"
        user_id = str(update.effective_user.id)
        all_user_data = load_user_data()
        if user_id in all_user_data:
            all_user_data[user_id]["subgroup"] = "1"
            save_user_data(all_user_data)
            
    await send_schedule_image(update, context, subgroup=subgroup)
    return WAITING_ACTION

async def receive_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_input = update.message.text.strip()
    logger.info(f"Получен ввод от пользователя: {user_input}")
    
    if user_input.startswith('/'):
        await update.message.reply_text(
            "Пожалуйста, введите код группы, а не команду.",
            reply_markup=MAIN_KEYBOARD
        )
        return WAITING_GROUP
        
    group_code, subgroup = parse_group_input(user_input)
    logger.info(f"Распарсено: группа={group_code}, подгруппа={subgroup}")
    
    if not group_code or len(group_code) < 3:
        await update.message.reply_text(
            "Неверный формат группы. Введите код, например: ИВТ-б-о-242(2)",
            reply_markup=MAIN_KEYBOARD
        )
        return WAITING_GROUP
        
    context.user_data["subgroup"] = subgroup
    await update.message.reply_text("⏳ Загружаю расписание, подождите...")
    data = get_schedule(group_code)
    
    if not data or not data.get("занятия"):
        await update.message.reply_text(
            "Не удалось найти расписание для этой группы.\n"
            "Проверьте правильность кода и попробуйте снова.\n\n"
            "Пример: ИВТ-б-о-242(2)",
            reply_markup=MAIN_KEYBOARD
        )
        return WAITING_GROUP
        
    index_data = get_index()
    if not index_data:
        await update.message.reply_text(
            "Не удалось загрузить данные о чётности недель.\n"
            "Попробуйте позже или введите /start заново.",
            reply_markup=MAIN_KEYBOARD
        )
        return WAITING_GROUP
        
    context.user_data["group_code"] = group_code
    context.user_data["data"] = data
    context.user_data["index_data"] = index_data
    
    user_id = str(update.effective_user.id)
    all_user_data = load_user_data()
    all_user_data[user_id] = {
        "group_code": group_code,
        "subgroup": subgroup
    }
    save_user_data(all_user_data)
    logger.info(f"Данные сохранены для пользователя {user_id}")
    
    keyboard = [
        [
            InlineKeyboardButton("Чётная неделя", callback_data="parity:ch"),
            InlineKeyboardButton("Нечётная неделя", callback_data="parity:nch"),
        ]
    ]
    await update.message.reply_text(
        "Выберите чётность недели:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CHOOSE_PARITY

async def choose_parity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    parity = query.data.split(":")[1]
    context.user_data["parity"] = parity
    data = context.user_data["data"]
    index_data = context.user_data["index_data"]
    group_code = context.user_data["group_code"]
    monday = find_week_monday(index_data, parity)
    context.user_data["monday"] = monday
    
    all_lessons = filter_lessons(data, monday, parity, subgroup="all")
    subgroups = get_available_subgroups(all_lessons)
    
    current_subgroup = context.user_data.get("subgroup", "1")
    
    if str(current_subgroup) in subgroups:
        await query.edit_message_text(f"Показываю расписание для подгруппы {current_subgroup}...")
        await send_schedule_image(update, context, subgroup=current_subgroup)
        return WAITING_ACTION
    elif len(subgroups) == 0:
        context.user_data["subgroup"] = "1"
        await query.edit_message_text("Формирую расписание...")
        await send_schedule_image(update, context, subgroup="all")
        return WAITING_ACTION
    else:
        keyboard = [
            [InlineKeyboardButton(f"{group_code}({sg})", callback_data=f"subgroup:{sg}")]
            for sg in subgroups
        ]
        await query.edit_message_text(
            "В расписании есть деление на подгруппы. Выберите свою:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return CHOOSE_SUBGROUP

async def choose_subgroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    subgroup = query.data.split(":")[1]
    context.user_data["subgroup"] = subgroup
    
    user_id = str(update.effective_user.id)
    all_user_data = load_user_data()
    if user_id in all_user_data:
        all_user_data[user_id]["subgroup"] = subgroup
        save_user_data(all_user_data)
        
    await query.edit_message_text("Формирую расписание...")
    await send_schedule_image(update, context, subgroup=subgroup)
    return WAITING_ACTION

def get_schedule_keyboard(current_parity: str, current_subgroup: str, group_code: str, context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    data = context.user_data.get("data", {})
    monday = context.user_data.get("monday")
    parity = context.user_data.get("parity", "ch")
    
    all_lessons = filter_lessons(data, monday, parity, subgroup="all")
    available_subgroups = get_available_subgroups(all_lessons)
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Чётная" if current_parity == "ch" else "Чётная", callback_data="switch_parity:ch"),
            InlineKeyboardButton("✅ Нечётная" if current_parity == "nch" else "Нечётная", callback_data="switch_parity:nch")
        ]
    ]
    
    if len(available_subgroups) >= 2:
        subgroup_buttons = []
        for sg in available_subgroups:
            if current_subgroup == sg:
                subgroup_buttons.append(InlineKeyboardButton(f"✅ {group_code}({sg})", callback_data=f"switch_subgroup:{sg}"))
            else:
                subgroup_buttons.append(InlineKeyboardButton(f"{group_code}({sg})", callback_data=f"switch_subgroup:{sg}"))
        for i in range(0, len(subgroup_buttons), 2):
            keyboard.append(subgroup_buttons[i:i+2])
            
    keyboard.append([
        InlineKeyboardButton("🔄 Обновить", callback_data="action:refresh"),
        InlineKeyboardButton("🔄 Сменить группу", callback_data="action:change_group")
    ])
    return InlineKeyboardMarkup(keyboard)

async def send_schedule_image(update: Update, context: ContextTypes.DEFAULT_TYPE, subgroup: str):
    group_code = context.user_data["group_code"]
    data = context.user_data["data"]
    index_data = context.user_data["index_data"]
    parity = context.user_data["parity"]
    monday = context.user_data["monday"]
    lessons = filter_lessons(data, monday, parity, subgroup)
    
    if not lessons:
        chat_id = update.effective_chat.id
        await context.bot.send_message(
            chat_id, 
            f"На {PARITY_LABEL[parity]} неделе для подгруппы {subgroup} занятий нет.\n"
            f"Попробуйте выбрать другую подгруппу или обновить расписание."
        )
        return
        
    try:
        image_buffer = create_schedule_image(group_code, lessons, index_data, monday, parity, subgroup)
    except Exception as exc:
        logger.error("Ошибка при создании изображения: %s", exc)
        chat_id = update.effective_chat.id
        await context.bot.send_message(chat_id, "Произошла ошибка при создании расписания.")
        return
        
    reply_markup = get_schedule_keyboard(parity, subgroup, group_code, context)
    if update.callback_query:
        await update.callback_query.message.reply_photo(photo=image_buffer, reply_markup=reply_markup)
    else:
        await update.message.reply_photo(photo=image_buffer, reply_markup=reply_markup)

async def switch_parity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    new_parity = query.data.split(":")[1]
    context.user_data["parity"] = new_parity
    index_data = context.user_data.get("index_data", get_index())
    context.user_data["monday"] = find_week_monday(index_data, new_parity)
    await query.message.reply_text(f"🔄 Переключаю на {PARITY_LABEL[new_parity]} неделю...")
    await send_schedule_image(update, context, subgroup=context.user_data.get("subgroup", "1"))
    return WAITING_ACTION

async def switch_subgroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    new_subgroup = query.data.split(":")[1]
    context.user_data["subgroup"] = new_subgroup
    
    user_id = str(update.effective_user.id)
    all_user_data = load_user_data()
    if user_id in all_user_data:
        all_user_data[user_id]["subgroup"] = new_subgroup
        save_user_data(all_user_data)
        
    await query.message.reply_text(f"🔄 Переключаю на подгруппу {new_subgroup}...")
    await send_schedule_image(update, context, subgroup=new_subgroup)
    return WAITING_ACTION

async def action_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("Обновляю данные...")
    group_code = context.user_data.get("group_code")
    subgroup = context.user_data.get("subgroup", "1")
    await query.message.reply_text("🔄 Загружаю актуальное расписание...")
    data = get_schedule(group_code)
    
    if not data or not data.get("занятия"):
        await query.message.reply_text("Не удалось загрузить расписание. Попробуйте позже или смените группу.")
        return WAITING_ACTION
        
    context.user_data["data"] = data
    context.user_data["index_data"] = get_index()
    await send_schedule_image(update, context, subgroup=subgroup)
    return WAITING_ACTION

async def action_change_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = str(update.effective_user.id)
    all_user_data = load_user_data()
    if user_id in all_user_data:
        del all_user_data[user_id]
        save_user_data(all_user_data)
    context.user_data.clear()
    
    await query.message.reply_text(
        "🔄 Введите новый код группы (например: ИВТ-б-о-242(2)):",
        reply_markup=MAIN_KEYBOARD
    )
    return WAITING_GROUP

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Отменено. Чтобы начать заново, отправьте /start.", reply_markup=MAIN_KEYBOARD)
    return WAITING_GROUP

# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------
def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()
    
    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("my", my_schedule),
            CallbackQueryHandler(action_change_group, pattern="^action:change_group$"),
        ],
        states={
            WAITING_GROUP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_group),
                CommandHandler("start", start),
                CommandHandler("my", my_schedule),
            ],
            CHOOSE_PARITY: [
                CallbackQueryHandler(choose_parity, pattern="^parity:"),
                CommandHandler("start", start),
                CommandHandler("my", my_schedule),
            ],
            CHOOSE_SUBGROUP: [
                CallbackQueryHandler(choose_subgroup, pattern="^subgroup:"),
                CommandHandler("start", start),
                CommandHandler("my", my_schedule),
            ],
            WAITING_ACTION: [
                CallbackQueryHandler(switch_parity, pattern="^switch_parity:"),
                CallbackQueryHandler(switch_subgroup, pattern="^switch_subgroup:"),
                CallbackQueryHandler(action_refresh, pattern="^action:refresh$"),
                CallbackQueryHandler(action_change_group, pattern="^action:change_group$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_group),
                CommandHandler("start", start),
                CommandHandler("my", my_schedule),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
        ],
    )
    application.add_handler(conversation)
    logger.info("Бот запущен")
    application.run_polling()

# ИСПРАВЛЕНО: было if name == "main":
if __name__ == "__main__":
    main()
