import os
import asyncio
import json
import logging
from typing import Dict, Optional
from random import choice
from datetime import datetime
import asyncpg

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ID администратора (может менять статистику любого игрока)
ADMIN_USER_ID = 1630073668

# Получаем токен и DATABASE_URL из переменных окружения Railway
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# Если DATABASE_URL нет, собираем из отдельных переменных
if not DATABASE_URL:
    pg_host = os.getenv("PGHOST")
    pg_port = os.getenv("PGPORT", "5432")
    pg_db = os.getenv("PGDATABASE")
    pg_user = os.getenv("PGUSER")
    pg_pass = os.getenv("PGPASSWORD")
    
    if all([pg_host, pg_db, pg_user, pg_pass]):
        DATABASE_URL = f"postgresql://{pg_user}:{pg_pass}@{pg_host}:{pg_port}/{pg_db}"
        logger.info("✅ DATABASE_URL собран из отдельных переменных")

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден! Добавьте в Variables сервиса бота.")
if not DATABASE_URL:
    raise ValueError("❌ DATABASE_URL не найден! Добавьте DATABASE_URL в Variables сервиса бота.")

WORDS_FILE = "words_dictionary.json"
ROUND_TIME = 180
WARNING_TIME = 30

# Настройки системы прогресса
LEVEL_TITLES = {
    1: "🌱",
    5: "🎯", 
    10: "⚔️",
    20: "👑",
    35: "🔥",
    50: "⭐",
    75: "💎",
    100: "🏆"
}

# Глобальный пул подключений к БД
db_pool = None

async def init_db():
    """Инициализация базы данных"""
    global db_pool
    
    logger.info(f"Подключение к БД...")
    
    try:
        db_pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=10,
            command_timeout=60
        )
        logger.info("✅ Подключение к БД установлено")
        
        # Создаём таблицу статистики с новыми полями
        async with db_pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS player_stats (
                    chat_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    username TEXT,
                    
                    -- Основные метрики
                    words_explained INTEGER DEFAULT 0,
                    words_guessed INTEGER DEFAULT 0,
                    total_explain_time FLOAT DEFAULT 0.0,
                    total_guess_time FLOAT DEFAULT 0.0,
                    fastest_explain FLOAT,
                    fastest_guess FLOAT,
                    
                    -- Новая система прогресса
                    level INTEGER DEFAULT 1,
                    experience INTEGER DEFAULT 0,
                    elo_rating INTEGER DEFAULT 1000,
                    
                    -- Метрики качества игры
                    total_messages_sent INTEGER DEFAULT 0,
                    spam_attempts INTEGER DEFAULT 0,
                    
                    -- Метрики времени
                    sum_explain_times FLOAT DEFAULT 0.0,
                    sum_guess_times FLOAT DEFAULT 0.0,
                    
                    PRIMARY KEY (chat_id, user_id)
                )
            ''')
            
            # Добавляем новые колонки если таблица уже существует
            try:
                await conn.execute('ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS username TEXT')
                await conn.execute('ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS level INTEGER DEFAULT 1')
                await conn.execute('ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS experience INTEGER DEFAULT 0')
                await conn.execute('ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS elo_rating INTEGER DEFAULT 1000')
                await conn.execute('ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS total_messages_sent INTEGER DEFAULT 0')
                await conn.execute('ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS spam_attempts INTEGER DEFAULT 0')
                await conn.execute('ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS sum_explain_times FLOAT DEFAULT 0.0')
                await conn.execute('ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS sum_guess_times FLOAT DEFAULT 0.0')
            except Exception as e:
                logger.info(f"Колонки уже существуют или ошибка при добавлении: {e}")
                
        logger.info("✅ Таблица player_stats готова")
        
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к БД: {e}")
        logger.error(f"DATABASE_URL присутствует: {bool(DATABASE_URL)}")
        raise

async def load_player_stats(chat_id: int, user_id: int) -> Dict:
    """Загрузить статистику игрока из БД"""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT * FROM player_stats WHERE chat_id = $1 AND user_id = $2',
            chat_id, user_id
        )
        
        if row:
            return dict(row)
        else:
            return {
                'username': None,
                'words_explained': 0,
                'words_guessed': 0,
                'total_explain_time': 0.0,
                'total_guess_time': 0.0,
                'fastest_explain': None,
                'fastest_guess': None,
                'level': 1,
                'experience': 0,
                'elo_rating': 1000,
                'total_messages_sent': 0,
                'spam_attempts': 0,
                'sum_explain_times': 0.0,
                'sum_guess_times': 0.0
            }

async def save_player_stats(chat_id: int, user_id: int, stats: Dict):
    """Сохранить статистику игрока в БД"""
    async with db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO player_stats 
                (chat_id, user_id, username, words_explained, words_guessed, 
                 total_explain_time, total_guess_time, fastest_explain, fastest_guess,
                 level, experience, elo_rating, total_messages_sent, spam_attempts,
                 sum_explain_times, sum_guess_times)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
            ON CONFLICT (chat_id, user_id) 
            DO UPDATE SET
                username = $3,
                words_explained = $4,
                words_guessed = $5,
                total_explain_time = $6,
                total_guess_time = $7,
                fastest_explain = $8,
                fastest_guess = $9,
                level = $10,
                experience = $11,
                elo_rating = $12,
                total_messages_sent = $13,
                spam_attempts = $14,
                sum_explain_times = $15,
                sum_guess_times = $16
        ''', chat_id, user_id, 
            stats.get('username'),
            stats['words_explained'], stats['words_guessed'],
            stats['total_explain_time'], stats['total_guess_time'],
            stats['fastest_explain'], stats['fastest_guess'],
            stats['level'], stats['experience'], stats['elo_rating'],
            stats['total_messages_sent'], stats['spam_attempts'],
            stats['sum_explain_times'], stats['sum_guess_times']
        )

async def get_chat_stats(chat_id: int) -> Dict[int, Dict]:
    """Получить статистику всех игроков в чате"""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT * FROM player_stats WHERE chat_id = $1',
            chat_id
        )
        
        result = {}
        for row in rows:
            result[row['user_id']] = dict(row)
        return result

def get_level_title(level: int) -> str:
    """Получить титул по уровню"""
    title = "🌱 Новичок"
    for lvl, t in sorted(LEVEL_TITLES.items()):
        if level >= lvl:
            title = t
        else:
            break
    return title

def calculate_level_from_exp(exp: int) -> int:
    """Вычислить уровень по опыту (прогрессия усложняется)"""
    # Формула: level = sqrt(exp / 100)
    # Уровень 1 = 0 опыта
    # Уровень 2 = 100 опыта
    # Уровень 5 = 2500 опыта
    # Уровень 10 = 10000 опыта
    # Уровень 50 = 250000 опыта
    # Уровень 100 = 1000000 опыта
    import math
    return max(1, int(math.sqrt(exp / 100)) + 1)

def exp_for_next_level(current_level: int) -> int:
    """Сколько опыта нужно для следующего уровня"""
    return ((current_level) ** 2) * 100

def calculate_guess_exp(guess_time: float, messages_count: int) -> int:
    """
    Вычислить опыт за угадывание
    
    Параметры:
    - guess_time: время угадывания в секундах
    - messages_count: количество сообщений от игрока в этом раунде
    """
    base_exp = 50  # Базовый опыт за угадывание
    
    # Бонус за скорость (чем быстрее, тем больше)
    if guess_time < 10:
        speed_bonus = 100
    elif guess_time < 20:
        speed_bonus = 50
    elif guess_time < 30:
        speed_bonus = 30
    elif guess_time < 60:
        speed_bonus = 10
    else:
        speed_bonus = 0
    
    # Штраф за спам (много сообщений = перебор вариантов)
    if messages_count == 1:
        message_bonus = 50  # Угадал с первого раза!
    elif messages_count <= 3:
        message_bonus = 20
    elif messages_count <= 5:
        message_bonus = 0
    elif messages_count <= 10:
        message_bonus = -20
    else:
        message_bonus = -50  # Явный спам
    
    total_exp = base_exp + speed_bonus + message_bonus
    return max(10, total_exp)  # Минимум 10 опыта

def calculate_elo_change(winner_elo: int, loser_elo: int, guess_time: float) -> tuple:
    """
    Вычислить изменение Elo-рейтинга
    
    winner_elo: рейтинг угадавшего
    loser_elo: средний рейтинг остальных игроков (или ведущего)
    guess_time: время угадывания
    
    Возвращает: (изменение для победителя, базовое изменение)
    """
    K = 32  # Коэффициент изменения рейтинга
    
    # Ожидаемый результат
    expected = 1 / (1 + 10 ** ((loser_elo - winner_elo) / 400))
    
    # Фактический результат (победа = 1)
    actual = 1
    
    # Бонус за скорость
    if guess_time < 15:
        speed_multiplier = 1.5
    elif guess_time < 30:
        speed_multiplier = 1.2
    elif guess_time < 60:
        speed_multiplier = 1.0
    else:
        speed_multiplier = 0.8
    
    # Изменение рейтинга
    change = int(K * (actual - expected) * speed_multiplier)
    
    return (change, -change // 2)  # Победитель получает, остальные теряют

class PlayerStats:
    def __init__(self, data: Dict = None):
        if data:
            self.username = data.get('username')
            self.words_explained = data.get('words_explained', 0)
            self.words_guessed = data.get('words_guessed', 0)
            self.total_explain_time = data.get('total_explain_time', 0.0)
            self.total_guess_time = data.get('total_guess_time', 0.0)
            self.fastest_explain = data.get('fastest_explain')
            self.fastest_guess = data.get('fastest_guess')
            self.level = data.get('level', 1)
            self.experience = data.get('experience', 0)
            self.elo_rating = data.get('elo_rating', 1000)
            self.total_messages_sent = data.get('total_messages_sent', 0)
            self.spam_attempts = data.get('spam_attempts', 0)
            self.sum_explain_times = data.get('sum_explain_times', 0.0)
            self.sum_guess_times = data.get('sum_guess_times', 0.0)
        else:
            self.username = None
            self.words_explained = 0
            self.words_guessed = 0
            self.total_explain_time = 0.0
            self.total_guess_time = 0.0
            self.fastest_explain = None
            self.fastest_guess = None
            self.level = 1
            self.experience = 0
            self.elo_rating = 1000
            self.total_messages_sent = 0
            self.spam_attempts = 0
            self.sum_explain_times = 0.0
            self.sum_guess_times = 0.0
    
    def avg_explain_time(self) -> float:
        if self.words_explained == 0:
            return 0.0
        return self.total_explain_time / self.words_explained
    
    def avg_guess_time(self) -> float:
        if self.words_guessed == 0:
            return 0.0
        return self.total_guess_time / self.words_guessed
    
    def to_dict(self):
        return {
            'username': self.username,
            'words_explained': self.words_explained,
            'words_guessed': self.words_guessed,
            'total_explain_time': self.total_explain_time,
            'total_guess_time': self.total_guess_time,
            'fastest_explain': self.fastest_explain,
            'fastest_guess': self.fastest_guess,
            'level': self.level,
            'experience': self.experience,
            'elo_rating': self.elo_rating,
            'total_messages_sent': self.total_messages_sent,
            'spam_attempts': self.spam_attempts,
            'sum_explain_times': self.sum_explain_times,
            'sum_guess_times': self.sum_guess_times
        }

class GameState:
    def __init__(self):
        self.leader_id: Optional[int] = None
        self.current_word: Optional[str] = None
        self.is_game_active: bool = False
        self.word_guessed: bool = False
        self.round_start_time: Optional[datetime] = None
        self.timer_task: Optional[asyncio.Task] = None
        self.warning_sent: bool = False
        # Для отслеживания сообщений в раунде
        self.round_messages: Dict[int, int] = {}  # user_id: количество сообщений
        self.first_message_time: Optional[datetime] = None

games: Dict[int, GameState] = {}
words_list = []

async def get_player_stats_obj(chat_id: int, user_id: int) -> PlayerStats:
    """Получить объект статистики игрока"""
    data = await load_player_stats(chat_id, user_id)
    return PlayerStats(data)

async def update_player_stats(chat_id: int, user_id: int, stats: PlayerStats):
    """Обновить статистику игрока"""
    await save_player_stats(chat_id, user_id, stats.to_dict())

def format_time(seconds: float) -> str:
    """Форматировать время в читаемый вид"""
    if seconds is None:
        return "—"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}м {secs}с"

def load_words():
    """Загрузить слова из файла"""
    global words_list
    try:
        with open(WORDS_FILE, 'r', encoding='utf-8') as f:
            words_dict = json.load(f)
            words_list = list(words_dict.keys())
        logger.info(f"Загружено {len(words_list)} слов")
        if len(words_list) == 0:
            raise ValueError("Файл слов пустой")
    except Exception as e:
        logger.error(f"Ошибка загрузки слов: {e}")
        words_list = ["кот", "стол", "машина", "книга", "телефон", "окно", "солнце", "река"]
        logger.info(f"Используется резервный список: {len(words_list)} слов")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def get_leader_keyboard():
    """Клавиатура для ведущего"""
    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(text="🔍 Показать слово", callback_data="show_word"),
        InlineKeyboardButton(text="🔄 Новое слово", callback_data="new_word"),
        InlineKeyboardButton(text="📤 Поделиться словом", callback_data="share_word"),
        InlineKeyboardButton(text="✅ Закончить раунд", callback_data="end_round")
    )
    builder.adjust(1)
    return builder.as_markup()

def get_join_keyboard():
    """Клавиатура для присоединения к игре"""
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="🎮 Хочу быть ведущим", callback_data="join_game"))
    return builder.as_markup()

def get_word_keyboard():
    """Клавиатура для показа слова (начальная)"""
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="🔍 Показать слово", callback_data="show_word"))
    return builder.as_markup()

def get_random_word() -> str:
    """Получить случайное слово"""
    return choice(words_list)

def get_game_state(chat_id: int) -> GameState:
    """Получить состояние игры для чата"""
    if chat_id not in games:
        games[chat_id] = GameState()
    return games[chat_id]

def normalize_word(word: str) -> str:
    """Нормализация слова для сравнения"""
    return word.lower().replace('ё', 'е')

def is_word_guessed(message_text: str, target_word: str) -> bool:
    """Проверка: содержится ли загаданное слово в тексте сообщения"""
    if not target_word or not message_text:
        return False
    
    message_normalized = normalize_word(message_text.strip())
    target_normalized = normalize_word(target_word.strip())
    
    if message_normalized == target_normalized:
        return True
    
    import re
    words_in_message = re.findall(r'\b\w+\b', message_normalized)
    
    for word in words_in_message:
        if word == target_normalized:
            return True
    
    return False

async def cancel_timer(game: GameState):
    """Отменить таймер раунда"""
    if game.timer_task and not game.timer_task.done():
        game.timer_task.cancel()
        try:
            await game.timer_task
        except asyncio.CancelledError:
            pass
    game.timer_task = None
    game.warning_sent = False

async def round_timer(chat_id: int):
    """Таймер раунда с предупреждением"""
    game = get_game_state(chat_id)
    
    try:
        await asyncio.sleep(ROUND_TIME - WARNING_TIME)
        
        if not game.is_game_active or game.word_guessed:
            return
        
        if not game.warning_sent:
            game.warning_sent = True
            await bot.send_message(
                chat_id,
                "⏰ Внимание! Осталось 30 секунд!"
            )
            logger.info(f"Отправлено предупреждение о времени в чат {chat_id}")
        
        await asyncio.sleep(WARNING_TIME)
        
        if not game.is_game_active or game.word_guessed:
            return
        
        word_was = game.current_word
        round_time = (datetime.now() - game.round_start_time).total_seconds()
        
        # Обновляем статистику ведущего (слово не отгадано)
        if game.leader_id:
            leader_stats = await get_player_stats_obj(chat_id, game.leader_id)
            leader_stats.words_explained += 1
            leader_stats.total_explain_time += round_time
            leader_stats.sum_explain_times += round_time
            
            if leader_stats.fastest_explain is None or round_time < leader_stats.fastest_explain:
                leader_stats.fastest_explain = round_time
            
            # Небольшой штраф к Elo за неотгаданное слово
            leader_stats.elo_rating = max(0, leader_stats.elo_rating - 5)
            
            await update_player_stats(chat_id, game.leader_id, leader_stats)
        
        game.is_game_active = False
        game.word_guessed = False
        
        await bot.send_message(
            chat_id,
            f"⏱️ Время вышло!\n\n"
            f"Слово было: {word_was}\n\n"
            f"Кто хочет быть следующим ведущим?",
            reply_markup=get_join_keyboard()
        )
        
        game.leader_id = None
        game.current_word = None
        game.round_start_time = None
        game.round_messages = {}
        game.first_message_time = None
        
        logger.info(f"Раунд завершен по таймауту в чате {chat_id}")
        
    except asyncio.CancelledError:
        logger.info(f"Таймер отменен для чата {chat_id}")
        raise

async def start_round_timer(chat_id: int):
    """Запустить таймер раунда"""
    game = get_game_state(chat_id)
    
    await cancel_timer(game)
    
    game.round_start_time = datetime.now()
    game.warning_sent = False
    game.round_messages = {}
    game.first_message_time = None
    game.timer_task = asyncio.create_task(round_timer(chat_id))
    logger.info(f"Запущен таймер на {ROUND_TIME} секунд для чата {chat_id}")

async def handle_correct_guess(chat_id: int, winner_id: int, winner_name: str, guessed_word: str, messages_count: int):
    """Обработка правильного ответа"""
    game = get_game_state(chat_id)
    
    if game.word_guessed:
        return
    
    game.word_guessed = True
    await cancel_timer(game)
    
    round_time = (datetime.now() - game.round_start_time).total_seconds()
    
    # Получаем статистику победителя
    winner_stats = await get_player_stats_obj(chat_id, winner_id)
    
    # Вычисляем опыт и изменение Elo
    exp_gained = calculate_guess_exp(round_time, messages_count)
    
    # Для Elo используем средний рейтинг всех игроков в чате
    all_stats = await get_chat_stats(chat_id)
    avg_elo = 1000
    if len(all_stats) > 1:
        elos = [s['elo_rating'] for s in all_stats.values() if s['user_id'] != winner_id]
        avg_elo = sum(elos) // len(elos) if elos else 1000
    
    elo_change, _ = calculate_elo_change(winner_stats.elo_rating, avg_elo, round_time)
    
    # Обновляем статистику угадавшего
    winner_stats.words_guessed += 1
    winner_stats.total_guess_time += round_time
    winner_stats.sum_guess_times += round_time
    winner_stats.total_messages_sent += messages_count
    
    if messages_count > 10:
        winner_stats.spam_attempts += 1
    
    if winner_stats.fastest_guess is None or round_time < winner_stats.fastest_guess:
        winner_stats.fastest_guess = round_time
    
    # Добавляем опыт и обновляем уровень
    old_level = winner_stats.level
    winner_stats.experience += exp_gained
    winner_stats.level = calculate_level_from_exp(winner_stats.experience)
    
    # Обновляем Elo
    winner_stats.elo_rating += elo_change
    
    # Антиабьюз: если средняя скорость угадывания < 15 сек - штраф к опыту
    avg_guess = winner_stats.avg_guess_time()
    if avg_guess > 0 and avg_guess < 15 and winner_stats.words_guessed > 5:
        exp_penalty = exp_gained // 3
        winner_stats.experience = max(0, winner_stats.experience - exp_penalty)
        winner_stats.level = calculate_level_from_exp(winner_stats.experience)
        abuse_warning = f"\n⚠️ Антиабьюз: -{exp_penalty} опыта (слишком быстрая средняя скорость)"
    else:
        abuse_warning = ""
    
    await update_player_stats(chat_id, winner_id, winner_stats)
    
    # Обновляем статистику ведущего
    if game.leader_id:
        leader_stats = await get_player_stats_obj(chat_id, game.leader_id)
        leader_stats.words_explained += 1
        leader_stats.total_explain_time += round_time
        leader_stats.sum_explain_times += round_time
        
        if leader_stats.fastest_explain is None or round_time < leader_stats.fastest_explain:
            leader_stats.fastest_explain = round_time
        
        # Ведущий НЕ получает бонус за быстрое объяснение, но получает базовый опыт
        leader_stats.experience += 25  # Фиксированный опыт за роль ведущего
        leader_stats.level = calculate_level_from_exp(leader_stats.experience)
        
        await update_player_stats(chat_id, game.leader_id, leader_stats)
    
    game.is_game_active = False
    
    # Формируем сообщение о победе
    level_up_msg = ""
    if winner_stats.level > old_level:
        level_up_msg = f"\n\n🎊 УРОВЕНЬ ПОВЫШЕН! {old_level} → {winner_stats.level}\n{get_level_title(winner_stats.level)}"
    
    exp_to_next = exp_for_next_level(winner_stats.level)
    exp_progress = winner_stats.experience - ((winner_stats.level - 1) ** 2) * 100
    
    elo_sign = "+" if elo_change >= 0 else ""
    
    await bot.send_message(
        chat_id,
        f"🎉 ПОБЕДА! 🎉\n\n"
        f"🏆 {winner_name} угадал слово: {guessed_word.upper()}\n"
        f"⏱️ Время: {format_time(round_time)}\n"
        f"💬 Сообщений: {messages_count}\n\n"
        f"📊 НАГРАДА:\n"
        f"   +{exp_gained} опыта\n"
        f"   {elo_sign}{elo_change} Elo (теперь: {winner_stats.elo_rating})\n"
        f"   Прогресс: {exp_progress}/{exp_to_next} до уровня {winner_stats.level + 1}"
        f"{level_up_msg}"
        f"{abuse_warning}\n\n"
        f"Теперь {winner_name} становится новым ведущим!",
        reply_markup=get_join_keyboard()
    )
    
    game.leader_id = None
    game.current_word = None
    game.word_guessed = False
    game.round_start_time = None
    game.round_messages = {}
    game.first_message_time = None

async def send_leader_instructions(chat_id: int, leader_id: int, leader_name: str):
    """Отправить инструкции для ведущего"""
    game = get_game_state(chat_id)
    game.leader_id = leader_id
    game.is_game_active = True
    game.word_guessed = False
    game.current_word = get_random_word()
    logger.info(f"Новый ведущий: {leader_name}, слово: {game.current_word}")

    await bot.send_message(
        chat_id,
        f"🎭 {leader_name} теперь ведущий!\n\n"
        f"Ищи норм слово\n\n"
        f"⏱️ У тебя 3 минуты!\n\n"
        f"Нажми кнопку ниже, чтобы увидеть своё слово:",
        reply_markup=get_word_keyboard()
    )

@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Обработчик команды /start"""
    chat_id = message.chat.id
    game = get_game_state(chat_id)
    if game.is_game_active:
        await message.answer(
            "🎭 Игра 'Крокодил' уже идет!\n"
            "Если ты ведущий - нажми кнопку 'Показать слово'",
            reply_markup=get_word_keyboard()
        )
    else:
        await message.answer(
            "🎭 Крокодил!\n\n"
            "• Ведущий принимает дозу\n"
            "• Остальные угадывают галлюцинации\n"
            "• \n",
            reply_markup=get_join_keyboard()
        )

@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    """Обработчик команды /stop"""
    chat_id = message.chat.id
    game = get_game_state(chat_id)
    
    if game.is_game_active:
        await cancel_timer(game)
        
        game.is_game_active = False
        game.leader_id = None
        game.current_word = None
        game.word_guessed = False
        game.round_start_time = None
        await message.answer("🛑 Игра остановлена. Для начала новой игры нажмите /start")
    else:
        await message.answer("❌ Игра не активна. Для начала игры нажмите /start")

@dp.message(Command("help"))
async def cmd_help(message: Message):
    """Обработчик команды /help"""
    await message.answer(
        "🎭 Игра 'Крокодил' - Помощь\n\n"
        "Команды:\n"
        "/start - Начать игру\n"
        "/stop - Остановить игру\n"
        "/stats - Твоя статистика\n"
        "/rating - Рейтинг игроков\n"
        "/word_count - Количество слов\n"
        "/help - Эта справка\n\n"
        "Как играть:\n"
        "1. Нажмите 'Хочу быть ведущим'\n"
        "2. Нажмите 'Показать слово'\n"
        "3. Объясните слово\n"
        "4. Кто первый напишет слово - становится новым ведущим!\n\n"
        "⏱️ На раунд дается 3 минуты\n"
        "⚠️ За 30 секунд до конца - предупреждение\n\n"
        "Удачи!"
    )

@dp.message(Command("word_count"))
async def cmd_word_count(message: Message):
    """Показать количество доступных слов"""
    await message.answer(f"📚 В базе бота доступно {len(words_list)} слов")

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    """Показать статистику игрока"""
    chat_id = message.chat.id
    user_id = message.from_user.id
    user_name = message.from_user.first_name
    stats = await get_player_stats_obj(chat_id, user_id)

    # Обновляем username если изменился
    if stats.username != message.from_user.username:
        stats.username = message.from_user.username
        await update_player_stats(chat_id, user_id, stats)

    level_title = get_level_title(stats.level)
    exp_to_next = exp_for_next_level(stats.level)
    exp_progress = stats.experience - ((stats.level - 1) ** 2) * 100

    text = f"📊 Статистика: {user_name}\n\n"

    text += f"⭐ УРОВЕНЬ: {stats.level} {level_title}\n"
    text += f"   Опыт: {stats.experience} ({exp_progress}/{exp_to_next} до следующего)\n"
    text += f"   Elo-рейтинг: {stats.elo_rating}\n\n"

    text += f"🎯 ОСНОВНОЕ:\n"
    text += f"   Слов объяснено: {stats.words_explained}\n"
    text += f"   Слов угадано: {stats.words_guessed}\n"
    text += f"   Всего раундов: {stats.words_explained + stats.words_guessed}\n\n"

    if stats.words_explained > 0:
        text += f"📢 ОБЪЯСНЕНИЕ:\n"
        text += f"   Среднее время: {format_time(stats.avg_explain_time())}\n"
        text += f"   Самое быстрое: {format_time(stats.fastest_explain)}\n\n"

    if stats.words_guessed > 0:
        text += f"🎪 УГАДЫВАНИЕ:\n"
        text += f"   Среднее время: {format_time(stats.avg_guess_time())}\n"
        text += f"   Самое быстрое: {format_time(stats.fastest_guess)}\n"
        text += f"   Среднее сообщений: {stats.total_messages_sent / stats.words_guessed:.1f}\n"
        text += f"   Спам-попыток: {stats.spam_attempts}\n\n"

    text += f"⚠️ АНТИАБЬЮЗ:\n"
    text += f"   Сообщений отправлено: {stats.total_messages_sent}\n"
    text += f"   Попыток спама: {stats.spam_attempts}\n"

    await message.answer(text)

@dp.message(Command("rating"))
async def cmd_rating(message: Message):
    """Показать рейтинг игроков"""
    chat_id = message.chat.id
    all_stats = await get_chat_stats(chat_id)
    
    if not all_stats:
        await message.answer("❌ В этом чате еще нет игроков")
        return
    
    # Сортируем по опыту (уровню)
    sorted_stats = sorted(all_stats.items(), key=lambda x: x[1]['experience'], reverse=True)
    
    text = "🏆 РЕЙТИНГ ИГРОКОВ\n\n"
    
    for i, (user_id, stats) in enumerate(sorted_stats[:10], 1):
        level = stats['level']
        exp = stats['experience']
        elo = stats['elo_rating']
        title = get_level_title(level)
        
        text += f"{i}. {title} | Уровень {level} | Elo: {elo}\n"
        text += f"   Опыт: {exp} | Угадано: {stats['words_guessed']} | Объяснено: {stats['words_explained']}\n\n"
    
    await message.answer(text)

@dp.callback_query(F.data == "join_game")
async def callback_join_game(query: CallbackQuery):
    """Обработчик присоединения к игре"""
    chat_id = query.message.chat.id
    user_id = query.from_user.id
    user_name = query.from_user.first_name
    
    game = get_game_state(chat_id)
    
    if game.is_game_active and game.leader_id != user_id:
        await query.answer("❌ Игра уже идет. Ждите следующего раунда!", show_alert=False)
        return
    
    await query.answer()
    
    # Становится ведущим
    await send_leader_instructions(chat_id, user_id, user_name)
    await start_round_timer(chat_id)

@dp.callback_query(F.data == "show_word")
async def callback_show_word(query: CallbackQuery):
    """Показать слово ведущему"""
    chat_id = query.message.chat.id
    user_id = query.from_user.id
    
    game = get_game_state(chat_id)
    
    if game.leader_id != user_id or not game.is_game_active:
        await query.answer("❌ Ты не ведущий!", show_alert=True)
        return
    
    await query.answer()
    
    # Отправляем слово в личное сообщение
    await bot.send_message(
        user_id,
        f"🎭 Твое слово: <b>{game.current_word.upper()}</b>\n\n"
        f"⏱️ Объясняй! У тебя осталось {ROUND_TIME} секунд!",
        parse_mode="HTML"
    )
    
    await query.message.edit_text(
        f"🎭 {query.from_user.first_name} увидел слово!\n\n"
        f"⏱️ Игра началась! Объясняй!\n\n"
        f"Остается: 3 минуты",
        reply_markup=None
    )

@dp.callback_query(F.data == "new_word")
async def callback_new_word(query: CallbackQuery):
    """Выбрать новое слово"""
    chat_id = query.message.chat.id
    user_id = query.from_user.id
    
    game = get_game_state(chat_id)
    
    if game.leader_id != user_id:
        await query.answer("❌ Ты не ведущий!", show_alert=True)
        return
    
    game.current_word = get_random_word()
    
    await query.answer(f"Новое слово: {game.current_word}", show_alert=True)
    
    # Отправляем новое слово
    await bot.send_message(
        user_id,
        f"🎭 Твое новое слово: <b>{game.current_word.upper()}</b>",
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "share_word")
async def callback_share_word(query: CallbackQuery):
    """Поделиться словом (для проверки)"""
    chat_id = query.message.chat.id
    user_id = query.from_user.id
    
    game = get_game_state(chat_id)
    
    if game.leader_id != user_id:
        await query.answer("❌ Ты не ведущий!", show_alert=True)
        return
    
    await query.answer()
    
    # Показываем слово всем (только для администратора или в тестовых целях)
    await query.message.edit_text(
        f"🎭 Слово для этого раунда: <b>{game.current_word.upper()}</b>\n\n"
        f"(Это видит только ведущий и администраторы)",
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "end_round")
async def callback_end_round(query: CallbackQuery):
    """Закончить раунд"""
    chat_id = query.message.chat.id
    user_id = query.from_user.id
    
    game = get_game_state(chat_id)
    
    if game.leader_id != user_id:
        await query.answer("❌ Ты не ведущий!", show_alert=True)
        return
    
    await query.answer()
    
    if not game.is_game_active:
        await query.message.edit_text("❌ Игра уже закончена")
        return
    
    word_was = game.current_word
    
    await cancel_timer(game)
    
    game.is_game_active = False
    game.leader_id = None
    game.current_word = None
    game.word_guessed = False
    game.round_start_time = None
    game.round_messages = {}
    game.first_message_time = None
    
    await bot.send_message(
        chat_id,
        f"🛑 Ведущий закончил раунд!\n\n"
        f"Слово было: {word_was}\n\n"
        f"Кто хочет быть следующим ведущим?",
        reply_markup=get_join_keyboard()
    )

@dp.message(F.text)
async def handle_message(message: Message):
    """Обработчик обычных сообщений"""
    chat_id = message.chat.id
    user_id = message.from_user.id
    user_name = message.from_user.first_name
    
    game = get_game_state(chat_id)
    
    # Если игра не активна или это ведущий - игнорируем
    if not game.is_game_active or game.leader_id == user_id:
        return
    
    # Отслеживаем сообщения в раунде
    if user_id not in game.round_messages:
        game.round_messages[user_id] = 0
        game.first_message_time = datetime.now()
    
    game.round_messages[user_id] += 1
    
    # Проверяем, угадано ли слово
    if is_word_guessed(message.text, game.current_word):
        messages_count = game.round_messages.get(user_id, 1)
        await handle_correct_guess(chat_id, user_id, user_name, game.current_word, messages_count)

async def main():
    """Главная функция"""
    await init_db()
    load_words()
    
    logger.info("🚀 Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

