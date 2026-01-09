import os
import asyncio
import json
import logging
from typing import Dict, Optional
from random import choice
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


BOT_TOKEN = os.getenv("BOT_TOKEN")
WORDS_FILE = "words_dictionary.json"
STATS_FILE = "player_stats.json"

ROUND_TIME = 180
WARNING_TIME = 30

class PlayerStats:
    def __init__(self):
        self.words_explained = 0
        self.words_guessed = 0
        self.total_explain_time = 0.0
        self.total_guess_time = 0.0
        self.fastest_explain = None
        self.fastest_guess = None
    
    def avg_explain_time(self) -> float:
        """Средняя скорость объяснения"""
        if self.words_explained == 0:
            return 0.0
        return self.total_explain_time / self.words_explained
    
    def avg_guess_time(self) -> float:
        """Средняя скорость угадывания"""
        if self.words_guessed == 0:
            return 0.0
        return self.total_guess_time / self.words_guessed
    
    def to_dict(self):
        return {
            'words_explained': self.words_explained,
            'words_guessed': self.words_guessed,
            'total_explain_time': self.total_explain_time,
            'total_guess_time': self.total_guess_time,
            'fastest_explain': self.fastest_explain,
            'fastest_guess': self.fastest_guess
        }
    
    @staticmethod
    def from_dict(data):
        stats = PlayerStats()
        stats.words_explained = data.get('words_explained', 0)
        stats.words_guessed = data.get('words_guessed', 0)
        stats.total_explain_time = data.get('total_explain_time', 0.0)
        stats.total_guess_time = data.get('total_guess_time', 0.0)
        stats.fastest_explain = data.get('fastest_explain')
        stats.fastest_guess = data.get('fastest_guess')
        return stats

class GameState:
    def __init__(self):
        self.leader_id: Optional[int] = None
        self.current_word: Optional[str] = None
        self.previous_word: Optional[str] = None
        self.is_game_active: bool = False
        self.word_guessed: bool = False
        self.round_start_time: Optional[datetime] = None
        self.timer_task: Optional[asyncio.Task] = None
        self.warning_sent: bool = False

games: Dict[int, GameState] = {}
# Формат: {chat_id: {user_id: PlayerStats}}
player_stats: Dict[int, Dict[int, PlayerStats]] = {}
words_list = []

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

def load_stats():
    """Загрузить статистику игроков"""
    global player_stats
    try:
        with open(STATS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            for chat_id_str, users in data.items():
                chat_id = int(chat_id_str)
                player_stats[chat_id] = {}
                for user_id_str, stats_dict in users.items():
                    user_id = int(user_id_str)
                    player_stats[chat_id][user_id] = PlayerStats.from_dict(stats_dict)
        logger.info(f"Загружена статистика для {len(player_stats)} чатов")
    except FileNotFoundError:
        logger.info("Файл статистики не найден, создается новый")
        player_stats = {}
    except Exception as e:
        logger.error(f"Ошибка загрузки статистики: {e}")
        player_stats = {}

def save_stats():
    """Сохранить статистику игроков"""
    try:
        data = {}
        for chat_id, users in player_stats.items():
            data[str(chat_id)] = {}
            for user_id, stats in users.items():
                data[str(chat_id)][str(user_id)] = stats.to_dict()
        
        with open(STATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("Статистика сохранена")
    except Exception as e:
        logger.error(f"Ошибка сохранения статистики: {e}")

def get_player_stats(chat_id: int, user_id: int) -> PlayerStats:
    """Получить статистику игрока"""
    if chat_id not in player_stats:
        player_stats[chat_id] = {}
    if user_id not in player_stats[chat_id]:
        player_stats[chat_id][user_id] = PlayerStats()
    return player_stats[chat_id][user_id]

def format_time(seconds: float) -> str:
    """Форматировать время в читаемый вид"""
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes} минут {secs} секунд"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def get_leader_keyboard():
    """Клавиатура для ведущего"""
    builder = InlineKeyboardBuilder()
    builder.add(
        InlineKeyboardButton(text="🔍 Показать слово", callback_data="show_word"),
        InlineKeyboardButton(text="🔄 Новое слово", callback_data="new_word"),
        InlineKeyboardButton(text="⏮️ Предыдущее слово", callback_data="prev_word"),
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
    """
    Нормализация слова для сравнения.
    Приводит к нижнему регистру и заменяет 'ё' на 'е'.
    """
    return word.lower().replace('ё', 'е')

def is_word_guessed(message_text: str, target_word: str) -> bool:
    """
    ЛОКАЛЬНАЯ проверка: содержится ли загаданное слово в тексте сообщения.
    Все происходит внутри бота, никуда ничего не отправляется.
    
    Буква 'ё' в загаданном слове может быть заменена на 'е' в ответе.
    Пример: слово "трёхмерный" засчитывается как "трехмерный" или "трёхмерный"
    """
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
        
        # Вычисляем время раунда для неотгаданного слова
        round_time = (datetime.now() - game.round_start_time).total_seconds()
        
        # Обновляем статистику ведущего (слово не отгадано, но время учитывается)
        if game.leader_id:
            leader_stats = get_player_stats(chat_id, game.leader_id)
            leader_stats.words_explained += 1
            leader_stats.total_explain_time += round_time
            if leader_stats.fastest_explain is None or round_time < leader_stats.fastest_explain:
                leader_stats.fastest_explain = round_time
            save_stats()
        
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
        game.previous_word = None
        game.round_start_time = None
        
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
    game.timer_task = asyncio.create_task(round_timer(chat_id))
    logger.info(f"Запущен таймер на {ROUND_TIME} секунд для чата {chat_id}")

async def handle_correct_guess(chat_id: int, winner_id: int, winner_name: str, guessed_word: str):
    """Обработка правильного ответа"""
    game = get_game_state(chat_id)
    
    if game.word_guessed:
        return
    
    game.word_guessed = True
    
    await cancel_timer(game)
    
    # Вычисляем время раунда
    round_time = (datetime.now() - game.round_start_time).total_seconds()
    
    # Обновляем статистику угадавшего
    winner_stats = get_player_stats(chat_id, winner_id)
    winner_stats.words_guessed += 1
    winner_stats.total_guess_time += round_time
    if winner_stats.fastest_guess is None or round_time < winner_stats.fastest_guess:
        winner_stats.fastest_guess = round_time
    
    # Обновляем статистику ведущего
    if game.leader_id:
        leader_stats = get_player_stats(chat_id, game.leader_id)
        leader_stats.words_explained += 1
        leader_stats.total_explain_time += round_time
        if leader_stats.fastest_explain is None or round_time < leader_stats.fastest_explain:
            leader_stats.fastest_explain = round_time
    
    # Сохраняем статистику
    save_stats()
    
    game.is_game_active = False
    
    logger.info(f"Обрабатываем победу: {winner_name} угадал '{guessed_word}' за {format_time(round_time)}")
    
    try:
        await bot.send_message(
            chat_id,
            f"🎉 ПОБЕДА! 🎉\n\n"
            f"🏆 {winner_name} угадал слово: {guessed_word.upper()}\n"
            f"⏱️ Время: {format_time(round_time)}\n\n"
            f"Теперь {winner_name} становится новым ведущим!",
            reply_markup=get_join_keyboard()
        )
        logger.info(f"Сообщение о победе отправлено в чат {chat_id}")
    except Exception as e:
        logger.error(f"Ошибка при отправке сообщения о победе: {e}")
    
    game.leader_id = None
    game.current_word = None
    game.previous_word = None
    game.word_guessed = False
    game.round_start_time = None

async def send_leader_instructions(chat_id: int, leader_id: int, leader_name: str):
    """Отправить инструкции для ведущего"""
    game = get_game_state(chat_id)
    game.leader_id = leader_id
    game.is_game_active = True
    game.word_guessed = False
    game.current_word = get_random_word()
    game.previous_word = None
    
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
            "Правила:\n"
            "• Ведущий принимает дозу, игроки угадывают галлюцинации\n"
            "• Как только кто-то напишет загаданное слово - он становится новым ведущим!\n"
            "• На раунд дается 3 минуты ⏱️",
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
        game.previous_word = None
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
        "3. Объясните слово жестами\n"
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
    
    stats = get_player_stats(chat_id, user_id)
    
    text = f"📊 Статистика игрока: {user_name}\n\n"
    
    if stats.words_explained == 0 and stats.words_guessed == 0:
        text += "Пока нет данных. Сыграй хотя бы один раунд!"
    else:
        if stats.words_explained > 0:
            text += f"🎭 ОБЪЯСНЕНИЕ СЛОВ:\n"
            text += f"   • Всего объяснено: {stats.words_explained} слов\n"
            text += f"   • Среднее время: {format_time(stats.avg_explain_time())}\n"
            text += f"   • Лучшее время: {format_time(stats.fastest_explain)}\n\n"
        
        if stats.words_guessed > 0:
            text += f"🎯 УГАДЫВАНИЕ СЛОВ:\n"
            text += f"   • Всего угадано: {stats.words_guessed} слов\n"
            text += f"   • Среднее время: {format_time(stats.avg_guess_time())}\n"
            text += f"   • Лучшее время: {format_time(stats.fastest_guess)}\n"
        
        if stats.words_explained == 0:
            text += f"🎭 ОБЪЯСНЕНИЕ СЛОВ:\n"
            text += f"   Еще не был ведущим\n"
    
    await message.answer(text)

@dp.message(Command("rating"))
async def cmd_rating(message: Message):
    """Показать рейтинг игроков"""
    chat_id = message.chat.id
    
    if chat_id not in player_stats or not player_stats[chat_id]:
        await message.answer("📊 Пока нет статистики. Сыграйте хотя бы один раунд!")
        return
    
    # Собираем данные игроков
    players_data = []
    for user_id, stats in player_stats[chat_id].items():
        if stats.words_explained > 0 or stats.words_guessed > 0:
            try:
                user = await bot.get_chat(user_id)
                name = user.first_name
            except:
                name = f"Игрок {user_id}"
            
            players_data.append({
                'name': name,
                'explained': stats.words_explained,
                'guessed': stats.words_guessed,
                'avg_explain': stats.avg_explain_time(),
                'avg_guess': stats.avg_guess_time(),
                'fastest_explain': stats.fastest_explain,
                'fastest_guess': stats.fastest_guess
            })
    
    if not players_data:
        await message.answer("📊 Пока нет статистики. Сыграйте хотя бы один раунд!")
        return
    
    # Топ по объяснениям
    text = "🏆 РЕЙТИНГ ИГРОКОВ\n\n"
    text += "━━━━━━━━━━━━━━━━━━━━\n"
    text += "🎭 ТОП ВЕДУЩИХ\n"
    text += "━━━━━━━━━━━━━━━━━━━━\n"
    top_explainers = sorted(players_data, key=lambda x: x['explained'], reverse=True)[:10]
    for i, player in enumerate(top_explainers, 1):
        if player['explained'] > 0:
            text += f"{i}. {player['name']}\n"
            text += f"   Объяснено: {player['explained']} слов\n"
            text += f"   Среднее: {format_time(player['avg_explain'])}\n"
            text += f"   Лучшее: {format_time(player['fastest_explain'])}\n\n"
    
    text += "━━━━━━━━━━━━━━━━━━━━\n"
    text += "🎯 ТОП УГАДЫВАЮЩИХ\n"
    text += "━━━━━━━━━━━━━━━━━━━━\n"
    top_guessers = sorted(players_data, key=lambda x: x['guessed'], reverse=True)[:10]
    for i, player in enumerate(top_guessers, 1):
        if player['guessed'] > 0:
            text += f"{i}. {player['name']}\n"
            text += f"   Угадано: {player['guessed']} слов\n"
            text += f"   Среднее: {format_time(player['avg_guess'])}\n"
            text += f"   Лучшее: {format_time(player['fastest_guess'])}\n\n"
    
    # Самые быстрые
    text += "━━━━━━━━━━━━━━━━━━━━\n"
    text += "⚡ РЕКОРДЫ СКОРОСТИ\n"
    text += "━━━━━━━━━━━━━━━━━━━━\n"
    
    fastest_explainers = [p for p in players_data if p['explained'] > 0]
    if fastest_explainers:
        fastest = min(fastest_explainers, key=lambda x: x['avg_explain'])
        text += f"🎭 Среднее объяснение:\n"
        text += f"   {fastest['name']}\n"
        text += f"   {format_time(fastest['avg_explain'])}\n\n"
        
        fastest_single = min(fastest_explainers, key=lambda x: x['fastest_explain'])
        text += f"🎭 Быстрейшее объяснение:\n"
        text += f"   {fastest_single['name']}\n"
        text += f"   {format_time(fastest_single['fastest_explain'])}\n\n"
    
    fastest_guessers = [p for p in players_data if p['guessed'] > 0]
    if fastest_guessers:
        fastest = min(fastest_guessers, key=lambda x: x['avg_guess'])
        text += f"🎯 Среднее угадывание:\n"
        text += f"   {fastest['name']}\n"
        text += f"   {format_time(fastest['avg_guess'])}\n\n"
        
        fastest_single = min(fastest_guessers, key=lambda x: x['fastest_guess'])
        text += f"🎯 Быстрейшее угадывание:\n"
        text += f"   {fastest_single['name']}\n"
        text += f"   {format_time(fastest_single['fastest_guess'])}\n"
    
    await message.answer(text)

@dp.callback_query(F.data == "join_game")
async def join_game(callback: CallbackQuery):
    """Обработчик присоединения к игре"""
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    user_name = callback.from_user.first_name
    game = get_game_state(chat_id)
    
    if game.is_game_active:
        await callback.answer("❌ Игра уже идет! Дождитесь окончания раунда.", show_alert=True)
        return
    
    await send_leader_instructions(chat_id, user_id, user_name)
    await callback.answer()

@dp.callback_query(F.data == "show_word")
async def show_word(callback: CallbackQuery):
    """Показать слово ведущему"""
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    game = get_game_state(chat_id)
    
    if not game.is_game_active:
        await callback.answer("❌ Игра не активна!", show_alert=True)
        return
    
    if user_id != game.leader_id:
        await callback.answer("❌ Ты не ведущий!", show_alert=True)
        return
    
    if not game.current_word:
        game.current_word = get_random_word()
    
    if game.round_start_time is None:
        await start_round_timer(chat_id)
    
    await callback.answer(
        f"🎯 Твоё слово: {game.current_word.upper()}",
        show_alert=True
    )
    
    try:
        await callback.message.edit_reply_markup(reply_markup=get_leader_keyboard())
    except Exception as e:
        if "message is not modified" not in str(e):
            logger.error(f"Ошибка при обновлении клавиатуры: {e}")

@dp.callback_query(F.data == "new_word")
async def new_word(callback: CallbackQuery):
    """Поменять слово ведущему"""
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    game = get_game_state(chat_id)
    
    if not game.is_game_active:
        await callback.answer("❌ Игра не активна!", show_alert=True)
        return
    
    if user_id != game.leader_id:
        await callback.answer("❌ Ты не ведущий!", show_alert=True)
        return
    
    game.previous_word = game.current_word
    game.current_word = get_random_word()
    
    logger.info(f"Смена слова: '{game.previous_word}' -> '{game.current_word}'")
    
    await start_round_timer(chat_id)
    
    await callback.answer(
        f"🔄 Новое слово: {game.current_word.upper()}\n⏱️ Таймер перезапущен!",
        show_alert=True
    )

@dp.callback_query(F.data == "prev_word")
async def prev_word(callback: CallbackQuery):
    """Показать предыдущее слово ведущему"""
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    game = get_game_state(chat_id)
    
    if not game.is_game_active:
        await callback.answer("❌ Игра не активна!", show_alert=True)
        return
    
    if user_id != game.leader_id:
        await callback.answer("❌ Ты не ведущий!", show_alert=True)
        return
    
    if game.previous_word is None:
        await callback.answer(
            f"⏮️ Текущее слово: {game.current_word.upper()}\n\n"
            f"(Слово еще не менялось)",
            show_alert=True
        )
        logger.info(f"Показано текущее слово (смены не было): {game.current_word}")
    else:
        await callback.answer(
            f"⏮️ Предыдущее слово: {game.previous_word.upper()}",
            show_alert=True
        )
        logger.info(f"Показано предыдущее слово: {game.previous_word}")

@dp.callback_query(F.data == "share_word")
async def share_word(callback: CallbackQuery):
    """Поделиться словом в чате и получить новое"""
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    game = get_game_state(chat_id)
    
    if not game.is_game_active:
        await callback.answer("❌ Игра не активна!", show_alert=True)
        return
    
    if user_id != game.leader_id:
        await callback.answer("❌ Ты не ведущий!", show_alert=True)
        return
    
    old_word = game.current_word
    game.previous_word = None
    game.current_word = get_random_word()
    
    await start_round_timer(chat_id)
    
    await bot.send_message(
        chat_id,
        f"📤Чекайте слово: {old_word.upper()}\n"
    )
    
    await callback.answer(
        f"📤 Слово {old_word.upper()} опубликовано в чате\n"
        f"🔄 Новое слово: {game.current_word.upper()}",
        show_alert=True
    )

@dp.callback_query(F.data == "end_round")
async def end_round(callback: CallbackQuery):
    """Завершить раунд"""
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    game = get_game_state(chat_id)
    
    if not game.is_game_active:
        await callback.answer("❌ Игра не активна!", show_alert=True)
        return
    
    if user_id != game.leader_id:
        await callback.answer("❌ Ты не ведущий!", show_alert=True)
        return
    
    await cancel_timer(game)
    
    game.is_game_active = False
    old_leader_name = callback.from_user.first_name
    word_was = game.current_word
    
    await callback.message.edit_text(
        f"✅ Раунд завершён!\n\n"
        f"Слово было: {word_was}\n"
        f"Ведущий: {old_leader_name}\n\n"
        f"Желающие быть следующим ведущим - жмите кнопку:",
        reply_markup=get_join_keyboard()
    )
    
    game.leader_id = None
    game.current_word = None
    game.previous_word = None
    game.word_guessed = False
    game.round_start_time = None

@dp.message(F.text)
async def check_word_guess(message: Message):
    """
    ЛОКАЛЬНАЯ проверка сообщений на угаданное слово.
    Все происходит в памяти бота, ничего никуда не отправляется.
    """
    try:
        chat_id = message.chat.id
        game = get_game_state(chat_id)
        
        logger.info(f"=" * 20)
        logger.info(f"   Текст: '{message.text}'")
        logger.info(f"-" * 20)
        logger.info(f"   Игра активна: {game.is_game_active}")
        logger.info(f"   Загаданное слово: {game.current_word}")
        logger.info(f"   ID ведущего: {game.leader_id}")
        logger.info(f"-" * 20)
        
        if not game.is_game_active:
            logger.info(f"⏸️  Игра не активна - игнорируем")
            return
            
        if not game.current_word:
            logger.info(f"❌ Нет загаданного слова - игнорируем")
            return
            
        if game.word_guessed:
            logger.info(f"✅ Слово уже угадано - игнорируем")
            return
        
        if message.from_user.id == game.leader_id:
            logger.info(f"🎭 Это ведущий - игнорируем его сообщение")
            return
        
        logger.info(f"🔍 ПРОВЕРЯЕМ СЛОВО:")
        logger.info(f"   Ищем: '{game.current_word}'")
        logger.info(f"   В сообщении: '{message.text}'")
        
        if is_word_guessed(message.text, game.current_word):
            winner_name = message.from_user.first_name
            winner_id = message.from_user.id
            
            logger.info(f"🎉 СЛОВО УГАДАНО!")
            logger.info(f"   Победитель: {winner_name}")
            logger.info(f"=" * 20)
            
            await handle_correct_guess(chat_id, winner_id, winner_name, game.current_word)
        else:
            logger.info(f"❌ Слово не совпало")
            logger.info(f"=" * 20)
            
    except Exception as e:
        logger.error(f"❌ КРИТИЧЕСКАЯ ОШИБКА: {e}", exc_info=True)

async def main():
    logger.info("Загрузка слов...")
    load_words()
    
    logger.info("Загрузка статистики...")
    load_stats()
    
    logger.info("Запуск бота...")
    await dp.start_polling(bot)
    
from aiohttp import web

async def health_check(request):
    """Простой health check для Render"""
    return web.Response(text="Bot is running!")

async def run_web_server():
    """Запуск веб-сервера"""
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get('PORT', 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"Web server started on port {port}")
    
if __name__ == "__main__":
    asyncio.run(main())

