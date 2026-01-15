import os
import asyncio
import json
import logging
from typing import Dict, Optional, List
from random import choice
from datetime import datetime
import asyncpg
import re
from difflib import SequenceMatcher

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)
ADMIN_USER_ID = 1630073668
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    pg_host = os.getenv("PGHOST")
    pg_port = os.getenv("PGPORT", "5432")
    pg_db = os.getenv("PGDATABASE")
    pg_user = os.getenv("PGUSER")
    pg_pass = os.getenv("PGPASSWORD")
    
    if all([pg_host, pg_db, pg_user, pg_pass]):
        DATABASE_URL = f"postgresql://{pg_user}:{pg_pass}@{pg_host}:{pg_port}/{pg_db}"
        logger.info("✅DATABASE_URL собран из отдельных переменных")

if not BOT_TOKEN:
    raise ValueError("❌BOT_TOKEN не найден! Добавьте в Variables сервиса бота.")
if not DATABASE_URL:
    raise ValueError("❌DATABASE_URL не найден! Добавьте DATABASE_URL в Variables сервиса бота.")

WORDS_FILE = "words_dictionary.json"
ROUND_TIME = 180
WARNING_TIME = 30
MAX_ATTEMPTS_PER_ROUND = 3
LEVEL_EXP_FACTOR = 7.5

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

db_pool = None

async def init_db():
    global db_pool
    
    logger.info(f"Подключение к БД...")
    
    try:
        db_pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=10,
            command_timeout=60
        )
        logger.info("✅Подключение к БД установлено")
        
        async with db_pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS player_stats (
                    chat_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    username TEXT,
                    
                    words_explained INTEGER DEFAULT 0,
                    words_guessed INTEGER DEFAULT 0,
                    total_explain_time FLOAT DEFAULT 0.0,
                    total_guess_time FLOAT DEFAULT 0.0,
                    fastest_explain FLOAT,
                    fastest_guess FLOAT,
                    
                    level INTEGER DEFAULT 1,
                    experience INTEGER DEFAULT 0,
                    elo_rating INTEGER DEFAULT 1000,
                    
                    violations INTEGER DEFAULT 0,
                    
                    PRIMARY KEY (chat_id, user_id)
                )
            ''')
            
            try:
                await conn.execute('ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS username TEXT')
                await conn.execute('ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS level INTEGER DEFAULT 1')
                await conn.execute('ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS experience INTEGER DEFAULT 0')
                await conn.execute('ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS elo_rating INTEGER DEFAULT 1000')
                await conn.execute('ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS violations INTEGER DEFAULT 0')
            except Exception as e:
                logger.info(f"Колонки уже существуют или ошибка при добавлении: {e}")
                
        logger.info("✅Таблица player_stats готова")
        
    except Exception as e:
        logger.error(f"❌Ошибка подключения к БД: {e}")
        logger.error(f"DATABASE_URL присутствует: {bool(DATABASE_URL)}")
        raise

async def load_player_stats(chat_id: int, user_id: int) -> Dict:
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
                'violations': 0
            }

async def save_player_stats(chat_id: int, user_id: int, stats: Dict):
    async with db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO player_stats 
                (chat_id, user_id, username, words_explained, words_guessed, 
                 total_explain_time, total_guess_time, fastest_explain, fastest_guess,
                 level, experience, elo_rating, violations)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
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
                violations = $13
        ''', chat_id, user_id, 
            stats.get('username'),
            stats['words_explained'], stats['words_guessed'],
            stats['total_explain_time'], stats['total_guess_time'],
            stats['fastest_explain'], stats['fastest_guess'],
            stats['level'], stats['experience'], stats['elo_rating'],
            stats['violations']
        )

async def get_chat_stats(chat_id: int) -> Dict[int, Dict]:
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
    title = "🌱Новичок"
    for lvl, t in sorted(LEVEL_TITLES.items()):
        if level >= lvl:
            title = t
        else:
            break
    return title

def calculate_level_from_exp(exp: int) -> int:
    import math
    return max(1, int(math.sqrt(exp / LEVEL_EXP_FACTOR)) + 1)

def exp_for_next_level(current_level: int) -> int:
    return (current_level ** 2) * LEVEL_EXP_FACTOR

def word_similarity(word1: str, word2: str) -> float:
    return SequenceMatcher(None, word1.lower(), word2.lower()).ratio()

def contains_similar_word(text: str, target_word: str, threshold: float = 0.75) -> bool:
    words = re.findall(r'\b\w+\b', text.lower())
    target = target_word.lower()
    
    for word in words:
        if len(word) < 4 or len(target) < 4:
            continue
        if word_similarity(word, target) >= threshold:
            return True
    return False

def is_single_word_guess(text: str) -> bool:
    clean_text = re.sub(r'[^\w\s]', '', text)
    clean_text = clean_text.strip()
    
    words = clean_text.split()
    return len(words) == 1 and len(clean_text) > 0

def calculate_guess_exp(guess_time: float, position: int, total_competitors: int) -> int:
    base_exp = 40
    
    if guess_time < 10:
        speed_bonus = 80
    elif guess_time < 20:
        speed_bonus = 50
    elif guess_time < 40:
        speed_bonus = 30
    elif guess_time < 80:
        speed_bonus = 15
    else:
        speed_bonus = 0
    
    if position == 1 and total_competitors > 1:
        position_bonus = 60
    elif position == 1:
        position_bonus = 30
    else:
        position_bonus = 0
    
    total_exp = base_exp + speed_bonus + position_bonus
    return max(15, total_exp)

def calculate_leader_exp(round_time: float, total_words_in_explanation: int, was_guessed: bool) -> int:
    if not was_guessed:
        return 10
    
    base_exp = 100
    
    if total_words_in_explanation >= 15:
        quality_bonus = 50
    elif total_words_in_explanation >= 8:
        quality_bonus = 30
    elif total_words_in_explanation >= 4:
        quality_bonus = 10
    else:
        quality_bonus = 0
        
    total_exp = base_exp + quality_bonus
    return max(20, total_exp)

def calculate_elo_change(winner_elo: int, competitors_elos: List[int], guess_time: float) -> int:
    if not competitors_elos:
        return 10
    
    K = 32
    avg_competitor_elo = sum(competitors_elos) / len(competitors_elos)
    expected = 1 / (1 + 10 ** ((avg_competitor_elo - winner_elo) / 400))
    actual = 1
    competition_multiplier = 1 + (len(competitors_elos) * 0.1)

    if guess_time < 10:
        speed_multiplier = 1.15
    elif guess_time < 20:
        speed_multiplier = 1.08
    elif guess_time < 40:
        speed_multiplier = 1.03
    else:
        speed_multiplier = 1.0

    change = int(K * (actual - expected) * competition_multiplier * speed_multiplier)

    return max(5, change)

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
            self.violations = data.get('violations', 0)
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
            self.violations = 0
    
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
            'violations': self.violations
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
        
        self.leader_messages: List[str] = []
        self.leader_first_message_time: Optional[datetime] = None
        
        self.guessing_started: bool = False
        self.competitors: Dict[int, Dict] = {}
        
        self.banned_leaders: Dict[int, int] = {}

games: Dict[int, GameState] = {}
words_list = []

reset_requests: Dict[int, Dict] = {}

async def get_player_stats_obj(chat_id: int, user_id: int) -> PlayerStats:
    """Получить объект статистики игрока"""
    data = await load_player_stats(chat_id, user_id)
    return PlayerStats(data)

async def update_player_stats(chat_id: int, user_id: int, stats: PlayerStats):
    await save_player_stats(chat_id, user_id, stats.to_dict())

def format_time(seconds: float) -> str:
    if seconds is None:
        return "—"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}м {secs}с"

def load_words():
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
    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(text="🔍Показать слово", callback_data="show_word"),
        InlineKeyboardButton(text="🔄Новое слово", callback_data="new_word"),
        InlineKeyboardButton(text="📤Поделиться словом", callback_data="share_word"),
        InlineKeyboardButton(text="✅Закончить раунд", callback_data="end_round")
    )
    builder.adjust(1)
    return builder.as_markup()

def get_join_keyboard():
    """Клавиатура для присоединения к игре"""
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="🎮Хочу быть ведущим", callback_data="join_game"))
    return builder.as_markup()

def get_word_keyboard():
    """Клавиатура для показа слова (начальная)"""
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="🔍Показать слово", callback_data="show_word"))
    return builder.as_markup()

def get_random_word() -> str:
    return choice(words_list)

def get_game_state(chat_id: int) -> GameState:
    if chat_id not in games:
        games[chat_id] = GameState()
    return games[chat_id]

def normalize_word(word: str) -> str:
    return word.lower().replace('ё', 'е')

def is_word_guessed(message_text: str, target_word: str) -> bool:
    if not target_word or not message_text:
        return False
    
    message_normalized = normalize_word(message_text.strip())
    target_normalized = normalize_word(target_word.strip())
    
    if message_normalized == target_normalized:
        return True
    
    words_in_message = re.findall(r'\b\w+\b', message_normalized)
    
    for word in words_in_message:
        if word == target_normalized:
            return True
    
    return False

def reduce_bans(game: GameState):
    for uid in list(game.banned_leaders):
        game.banned_leaders[uid] -= 1
        if game.banned_leaders[uid] <= 0:
            del game.banned_leaders[uid]


def finalize_round(game: GameState):
    reduce_bans(game)

async def cancel_timer(game: GameState):
    if game.timer_task and not game.timer_task.done():
        game.timer_task.cancel()
        try:
            await game.timer_task
        except asyncio.CancelledError:
            pass
    game.timer_task = None
    game.warning_sent = False

async def round_timer(chat_id: int):
    game = get_game_state(chat_id)
    
    try:
        await asyncio.sleep(ROUND_TIME - WARNING_TIME)
        
        if not game.is_game_active or game.word_guessed:
            return
        
        if not game.warning_sent:
            game.warning_sent = True
            await bot.send_message(
                chat_id,
                "⏰Внимание! Осталось 30 секунд!"
            )
            logger.info(f"Отправлено предупреждение о времени в чат {chat_id}")
        
        await asyncio.sleep(WARNING_TIME)
        
        if not game.is_game_active or game.word_guessed:
            return
        
        word_was = game.current_word
        round_time = (datetime.now() - game.round_start_time).total_seconds()
        
        if game.leader_id:
            leader_stats = await get_player_stats_obj(chat_id, game.leader_id)

            leader_stats.experience += 10
            leader_stats.level = calculate_level_from_exp(leader_stats.experience)
            
            leader_stats.elo_rating = max(800, leader_stats.elo_rating - 10)
            
            await update_player_stats(chat_id, game.leader_id, leader_stats)
        
        game.is_game_active = False
        game.word_guessed = False
        
        await bot.send_message(
            chat_id,
            f"⏱️Время вышло!\n\n"
            f"Слово было: {word_was}\n\n"
            f"Кто хочет быть следующим ведущим?",
            reply_markup=get_join_keyboard()
        )
        
        game.leader_id = None
        game.current_word = None
        game.round_start_time = None
        game.leader_messages = []
        game.leader_first_message_time = None
        game.guessing_started = False
        game.competitors = {}
        
        finalize_round(game)
        
        logger.info(f"Раунд завершен по таймауту в чате {chat_id}")
        
    except asyncio.CancelledError:
        logger.info(f"Таймер отменен для чата {chat_id}")
        raise

async def start_round_timer(chat_id: int):
    """Запустить таймер раунда"""
    game = get_game_state(chat_id)

    await cancel_timer(game)

    game.round_start_time = datetime.now()
    game.word_guessed = False
    game.warning_sent = False
    game.leader_messages = []
    game.leader_first_message_time = None
    game.guessing_started = False
    game.competitors = {}
    game.timer_task = asyncio.create_task(round_timer(chat_id))
    logger.info(f"Запущен таймер на {ROUND_TIME} секунд для чата {chat_id}")

async def handle_correct_guess(chat_id: int, winner_id: int, winner_name: str, guessed_word: str):
    game = get_game_state(chat_id)
    
    if game.word_guessed:
        return
    
    game.word_guessed = True
    await cancel_timer(game)
    
    round_time = (datetime.now() - game.round_start_time).total_seconds()
    
    total_explanation_words = sum(len(re.findall(r'\b\w+\b', msg)) for msg in game.leader_messages)
    
    violation_detected = False
    for msg in game.leader_messages:
        if contains_similar_word(msg, game.current_word):
            violation_detected = True
            break
    
    # ЕСЛИ НАРУШЕНИЕ - НЕ ЗАСЧИТЫВАЕМ ПОБЕДУ
    if violation_detected and game.leader_id:
        leader_id_temp = game.leader_id
        leader_stats = await get_player_stats_obj(chat_id, leader_id_temp)
        leader_stats.violations += 1
        
        if leader_stats.violations >= 2:
            game.banned_leaders[leader_id_temp] = 5
        
        await update_player_stats(chat_id, leader_id_temp, leader_stats)
        
        finalize_round(game)
        
        game.is_game_active = False
        game.current_word = None
        game.word_guessed = False
        game.round_start_time = None
        game.leader_messages = []
        game.leader_first_message_time = None
        game.guessing_started = False
        game.competitors = {}
        
        ban_text = ""
        if leader_id_temp in game.banned_leaders:
            ban_text = f"\n\n🚫ВЕДУЩИЙ ОТСТРАНЕН НА 5 ИГР!"
        
        game.leader_id = None
        
        await bot.send_message(
            chat_id,
            f"⚠️РАУНД НЕ ЗАСЧИТАН!\n\n"
            f"Ведущий использовал похожее слово (нарушение правил)\n"
            f"Слово было: {guessed_word.upper()}"
            f"{ban_text}\n\n"
            f"Кто хочет быть следующим ведущим?",
            reply_markup=get_join_keyboard()
        )
        return
    
    winner_stats = await get_player_stats_obj(chat_id, winner_id)
    
    # Время угадывания: от момента, когда началась конкуренция (первое сообщение ведущего)
    # Если неизвестно, то возвращаемся к старту раунда
    start_time = game.leader_first_message_time or game.round_start_time
    if start_time is None:
        start_time = datetime.now()

    winner_guess_time = (datetime.now() - start_time).total_seconds()
    
    position = 1
    for user_id, data in game.competitors.items():
        if user_id != winner_id:
            if data['first_attempt_time'] < game.competitors[winner_id]['first_attempt_time']:
                position += 1
    
    competitor_elos = []
    
    for user_id, data in game.competitors.items():
        if user_id != winner_id:
            competitor_elos.append((await get_player_stats_obj(chat_id, user_id)).elo_rating)
    
    exp_gained = calculate_guess_exp(winner_guess_time, position, len(competitor_elos) + 1)
    elo_change = calculate_elo_change(winner_stats.elo_rating, competitor_elos, winner_guess_time)
    
    old_level = winner_stats.level
    winner_stats.words_guessed += 1
    winner_stats.total_guess_time += winner_guess_time
    
    if winner_stats.fastest_guess is None or winner_guess_time < winner_stats.fastest_guess:
        winner_stats.fastest_guess = winner_guess_time
    
    winner_stats.experience += exp_gained
    winner_stats.level = calculate_level_from_exp(winner_stats.experience)
    winner_stats.elo_rating += elo_change
    
    await update_player_stats(chat_id, winner_id, winner_stats)
    
    leader_exp = 0
    if game.leader_id:
        leader_stats = await get_player_stats_obj(chat_id, game.leader_id)
        leader_stats.words_explained += 1
        leader_stats.total_explain_time += round_time
        
        if leader_stats.fastest_explain is None or round_time < leader_stats.fastest_explain:
            leader_stats.fastest_explain = round_time
        
        leader_exp = calculate_leader_exp(round_time, total_explanation_words, True)
        
        leader_stats.experience += leader_exp
        leader_stats.level = calculate_level_from_exp(leader_stats.experience)
        
        await update_player_stats(chat_id, game.leader_id, leader_stats)
        
    game.is_game_active = False
    
    finalize_round(game)
    
    level_up_msg = ""
    if winner_stats.level > old_level:
        level_up_msg = f"\n\n🎊 УРОВЕНЬ ПОВЫШЕН! {old_level} → {winner_stats.level}\n{get_level_title(winner_stats.level)}"
    
    exp_to_next = exp_for_next_level(winner_stats.level)
    exp_progress = stats.experience - ((stats.level - 1) ** 2) * LEVEL_EXP_FACTOR
    
    elo_sign = "+" if elo_change >= 0 else ""
    
    competition_text = ""
    if len(competitor_elos) > 0:
        competition_text = f"\n🏁Конкуренция: победил {len(competitor_elos) + 1} игроков!"
    
    leader_reward_text = ""
    if leader_exp > 0:
        leader_reward_text = f"\n📢 Ведущий получил: +{leader_exp} опыта"
    
    await bot.send_message(
        chat_id,
        f"🎉ПОБЕДА!🎉\n\n"
        f"🏆{winner_name} угадал: {guessed_word.upper()}\n"
        f"⏱️Время: {format_time(winner_guess_time)}"
        f"\n\n"
        f"{winner_name} становится новым ведущим!"
    )

    if winner_id in game.banned_leaders:
        await bot.send_message(
            chat_id,
            f"🚫 {winner_name} угадал слово, но отстранён от роли ведущего ещё на {game.banned_leaders[winner_id]} игр.\n\nКто хочет быть следующим ведущим?",
            reply_markup=get_join_keyboard()
        )

        game.is_game_active = False
        game.leader_id = None
        game.current_word = None
        game.word_guessed = False
        game.round_start_time = None
        game.leader_messages = []
        game.leader_first_message_time = None
        game.guessing_started = False
        game.competitors = {}

        return

    await send_leader_instructions(chat_id, winner_id, winner_name)
    await start_round_timer(chat_id)

async def send_leader_instructions(chat_id: int, leader_id: int, leader_name: str):
    """Отправить инструкции для ведущего"""
    game = get_game_state(chat_id)
    game.leader_id = leader_id
    game.is_game_active = True
    game.current_word = get_random_word()
    logger.info(f"Новый ведущий: {leader_name}, слово: {game.current_word}")

    await bot.send_message(
        chat_id,
        f"{leader_name} теперь ведущий!\n\n"
        f"Ищи норм слово\n\n"
        f"⏱️У тебя 3 минуты!\n\n"
        f"Нажми кнопку ниже, чтобы начать:",
        reply_markup=get_leader_keyboard()
    )

@dp.message(Command("start"))
async def cmd_start(message: Message):
    chat_id = message.chat.id
    game = get_game_state(chat_id)
    if game.is_game_active:
        await message.answer(
            "🎭Игра уже идет!\n"
            "Если ты ведущий - нажми кнопку 'Показать слово'",
            reply_markup=get_word_keyboard()
        )
    else:
        await message.answer(
            "🎭Крокодил!\n\n"
            "Ведущий принимает дозу\n"
            "Остальные угадывают галлюцинации\n",
            reply_markup=get_join_keyboard()
        )

@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    chat_id = message.chat.id
    game = get_game_state(chat_id)
    
    if game.is_game_active:
        await cancel_timer(game)
        
        game.is_game_active = False
        game.leader_id = None
        game.current_word = None
        game.word_guessed = False
        game.round_start_time = None
        game.leader_messages = []
        game.guessing_started = False
        game.competitors = {}
        await message.answer("🛑Игра остановлена. Для начала новой игры нажмите /start")
    else:
        await message.answer("❌Игра не активна. Для начала игры нажмите /start")

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "🎭Игра 'Крокодил' - Помощь\n\n"
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
        "3. Объясните слово (избегайте похожих слов!)\n"
        "4. Кто первый напишет слово - становится новым ведущим!\n\n"
        "⏱️ На раунд дается 3 минуты\n"
        "⚠️ За 30 секунд до конца - предупреждение\n\n"
        "🎯 ПРАВИЛА УГАДЫВАНИЯ:\n"
        "• Считаются только сообщения из ОДНОГО слова\n"
        "• Конкурируют только те, кто писал после первого объяснения ведущего\n\n"
        "📢 ПРАВИЛА ДЛЯ ВЕДУЩЕГО:\n"
        "• НЕ используйте похожие слова (>60% схожести)\n"
        "• Быть ведущим выгоднее, чем угадывать!\n\n"
        "Удачи!"
    )

@dp.message(Command("word_count"))
async def cmd_word_count(message: Message):
    await message.answer(f"📚 В базе бота доступно {len(words_list)} слов")

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    user_name = message.from_user.first_name
    stats = await get_player_stats_obj(chat_id, user_id)

    if stats.username != message.from_user.username:
        stats.username = message.from_user.username
        await update_player_stats(chat_id, user_id, stats)

    level_title = get_level_title(stats.level)
    exp_to_next = exp_for_next_level(stats.level)
    exp_progress = stats.experience - ((stats.level - 1) ** 2) * LEVEL_EXP_FACTOR

    text = f"📊Статистика: {user_name}\n\n"

    text += f"УРОВЕНЬ: {stats.level} {level_title}\n"
    text += f"Опыт: {stats.experience} ({exp_progress}/{exp_to_next} до следующего)\n"
    text += f"Elo-рейтинг: {stats.elo_rating}\n\n"

    text += f"🎯ОСНОВНОЕ:\n"
    text += f"Слов объяснено: {stats.words_explained}\n"
    text += f"Слов угадано: {stats.words_guessed}\n"

    if stats.words_explained > 0:
        text += f"📢ОБЪЯСНЕНИЕ:\n"
        text += f"Среднее время: {format_time(stats.avg_explain_time())}\n"
        text += f"Самое быстрое: {format_time(stats.fastest_explain)}\n"
        text += f"Нарушений: {stats.violations}\n\n"

    if stats.words_guessed > 0:
        text += f"🎪УГАДЫВАНИЕ:\n"
        text += f"Среднее время: {format_time(stats.avg_guess_time())}\n"
        text += f"Самое быстрое: {format_time(stats.fastest_guess)}\n\n"
    
    await message.answer(text)

@dp.message(Command("rating"))
async def cmd_rating(message: Message):
    chat_id = message.chat.id
    all_stats = await get_chat_stats(chat_id)
    
    if not all_stats:
        await message.answer("❌ В этом чате еще нет игроков")
        return
    
    sorted_stats = sorted(all_stats.items(), key=lambda x: x[1].get('experience', 0), reverse=True)

    text = "🏆РЕЙТИНГ ИГРОКОВ\n\n"

    for i, (user_id, stats) in enumerate(sorted_stats[:10], 1):
        level = stats.get('level', 1)
        exp = stats.get('experience', 0)
        elo = stats.get('elo_rating', 1000)
        title = get_level_title(level)

        display_name = None
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            u = member.user
            name_parts = []
            if getattr(u, 'first_name', None):
                name_parts.append(u.first_name)
            if getattr(u, 'last_name', None):
                name_parts.append(u.last_name)
            display_name = " ".join(name_parts).strip()
        except Exception:
            display_name = None

        if not display_name:
            db_name = stats.get('username')
            display_name = db_name or f"User_{user_id}"

        text += f"{i}. {display_name} — {title} | Уровень {level} | Elo: {elo}\n"
        text += f"   Опыт: {exp} | Угадано: {stats.get('words_guessed', 0)} | Объяснено: {stats.get('words_explained', 0)}\n\n"

    await message.answer(text)

@dp.callback_query(F.data == "join_game")
async def callback_join_game(query: CallbackQuery):
    """Обработчик присоединения к игре"""
    chat_id = query.message.chat.id
    user_id = query.from_user.id
    user_name = query.from_user.first_name
    
    game = get_game_state(chat_id)
    
    if user_id in game.banned_leaders:
        await query.answer(
            f"🚫Ты отстранён от роли ведущего ещё на {game.banned_leaders[user_id]} игр",
            show_alert=True
        )
        return
    
    if game.is_game_active and game.leader_id != user_id:
        await query.answer("❌ Игра уже идет. Ждите следующего раунда!", show_alert=False)
        return
    
    await query.answer()
    await send_leader_instructions(chat_id, user_id, user_name)
    await start_round_timer(chat_id)

@dp.callback_query(F.data == "show_word")
async def callback_show_word(query: CallbackQuery):
    chat_id = query.message.chat.id
    user_id = query.from_user.id
    
    game = get_game_state(chat_id)
    
    if game.leader_id != user_id or not game.is_game_active:
        await query.answer("❌Ты не ведущий!", show_alert=True)
        return
    
    if game.round_start_time is None:
        await start_round_timer(chat_id)
    
    await query.answer(
        f"🎯Твоё слово: {game.current_word.upper()}",
        show_alert=True
    )
    
    try:
        await query.message.edit_reply_markup(reply_markup=get_leader_keyboard())
    except Exception as e:
        if "message is not modified" not in str(e):
            logger.error(f"Ошибка при обновлении клавиатуры: {e}")

@dp.callback_query(F.data == "new_word")
async def callback_new_word(query: CallbackQuery):
    chat_id = query.message.chat.id
    user_id = query.from_user.id
    
    game = get_game_state(chat_id)
    
    if game.leader_id != user_id:
        await query.answer("❌Ты не ведущий!", show_alert=True)
        return
    
    game.current_word = get_random_word()
    
    await start_round_timer(chat_id)
    
    await query.answer(
        f"🔄Новое слово: {game.current_word.upper()}\n⏱️Таймер перезапущен!",
        show_alert=True
    )
    
    logger.info(f"Смена слова: новое слово '{game.current_word}'")

@dp.callback_query(F.data == "share_word")
async def callback_share_word(query: CallbackQuery):
    chat_id = query.message.chat.id
    user_id = query.from_user.id
    game = get_game_state(chat_id)
    
    if game.leader_id != user_id:
        await query.answer("❌ Ты не ведущий!", show_alert=True)
        return
    
    old_word = game.current_word
    game.current_word = get_random_word()
    
    await start_round_timer(chat_id)
    
    await bot.send_message(
        chat_id,
        f"📤Чекайте какое было слово: {old_word.upper()}\n"
    )
    
    await query.answer(
        f"📤Слово {old_word.upper()} опубликовано в чате\n"
        f"🔄Новое слово: {game.current_word.upper()}",
        show_alert=True
    )

@dp.callback_query(F.data == "end_round")
async def callback_end_round(query: CallbackQuery):
    chat_id = query.message.chat.id
    user_id = query.from_user.id
    
    game = get_game_state(chat_id)
    
    if game.leader_id != user_id:
        await query.answer("❌Ты не ведущий!", show_alert=True)
        return
    
    await query.answer()
    
    if not game.is_game_active:
        await query.message.edit_text("❌Игра уже закончена")
        return
    
    word_was = game.current_word
    
    await cancel_timer(game)
    
    game.is_game_active = False
    game.leader_id = None
    game.current_word = None
    game.word_guessed = False
    game.round_start_time = None
    game.leader_messages = []
    game.guessing_started = False
    game.competitors = {}
    
    await bot.send_message(
        chat_id,
        f"🛑Ведущий закончил раунд!\n\n"
        f"Слово было: {word_was}\n\n"
        f"Кто хочет быть следующим ведущим?",
        reply_markup=get_join_keyboard()
    )

async def reset_stats_timeout(user_id: int, chat_id: int, confirmation_msg_id: int):
    try:
        await asyncio.sleep(15)
        
        if user_id in reset_requests:
            del reset_requests[user_id]
            
            await bot.send_message(
                chat_id,
                "❌Сброс отменен. Время ожидания подтверждения истекло."
            )
            logger.info(f"Сброс статистики отменен для пользователя {user_id} - истекло время")
    except asyncio.CancelledError:
        logger.info(f"Таймер сброса статистики отменен для пользователя {user_id}")
    except Exception as e:
        logger.error(f"Ошибка в таймере сброса статистики: {e}")

@dp.message(F.text)
async def handle_message(message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    user_name = message.from_user.first_name
    message_text = message.text
    
    RESET_COMMANDS = {
        "крокодил сбрось мой рейтинг",
        "крокодил сбрось мою статистику",
        "крокодил сбрось статистику",
        "крокодил сбрось рейтинг",
        "крокодил сброс статистики",
        "крокодил сброс рейтинга",
        "крокодил сброс моей статистики",
        "крокодил сброс моего рейтинга",
    }

    if message_text.lower().strip() in RESET_COMMANDS:
        if user_id in reset_requests:
            await message.answer("⚠️ У вас уже есть активный запрос на сброс статистики. Подтвердите его или ждите 15 секунд")
            return

        confirmation_msg = await message.answer(
            "🔄Твоя статистика будет сброшена. Напиши СБРОС (соблюдая регистр) для подтверждения\n"
            "⏰У тебя есть 15 секунд"
        )

        cancel_task = asyncio.create_task(reset_stats_timeout(user_id, chat_id, confirmation_msg.message_id))

        reset_requests[user_id] = {
            "chat_id": chat_id,
            "confirmation_time": datetime.now(),
            "cancel_task": cancel_task
        }

        logger.info(f"Запрос на сброс статистики для пользователя {user_id} ({user_name})")
        return
    
    if message_text == "СБРОС" and user_id in reset_requests:
        request = reset_requests[user_id]
        
        if request["chat_id"] != chat_id:
            await message.answer("❌Это не тот чат, в котором вы запросили сброс")
            return
        
        request["cancel_task"].cancel()
        del reset_requests[user_id]
        
        default_stats = PlayerStats()
        await update_player_stats(chat_id, user_id, default_stats)
        
        await message.answer(f"✅Статистика игрока {user_name} сброшена на начальные значения")
        logger.info(f"Статистика пользователя {user_id} ({user_name}) успешно сброшена")
        return
    
    game = get_game_state(chat_id)
    
    if not game.is_game_active:
        return
    
    if game.leader_id == user_id:
        game.leader_messages.append(message_text)
        
        if game.leader_first_message_time is None:
            game.leader_first_message_time = datetime.now()
            game.guessing_started = True
            logger.info(f"Начата конкуренция в чате {chat_id} после первого объяснения ведущего")
        
        return
    
    if not game.guessing_started:
        return
    
    if not is_single_word_guess(message_text):
        return
    
    if user_id not in game.competitors:
        game.competitors[user_id] = {
            'first_attempt_time': datetime.now(),
            'attempts_count': 1
        }
    else:
        attempts = game.competitors[user_id].get('attempts_count', 0)
        if attempts >= MAX_ATTEMPTS_PER_ROUND:
            return
        game.competitors[user_id]['attempts_count'] = attempts + 1
    
    if is_word_guessed(message_text, game.current_word):
        await handle_correct_guess(chat_id, user_id, user_name, game.current_word)

async def main():
    """Главная функция"""
    await init_db()
    load_words()
    
    logger.info("🚀 Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
