import asyncio
import datetime
import os
import urllib.parse

from dotenv import load_dotenv

import aiosqlite
import aiohttp
from aiohttp import ClientTimeout

from bs4 import BeautifulSoup

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InputMediaPhoto,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

load_dotenv()

bot = Bot(token=os.environ["BOT_TOKEN"])
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
BASE_URL = os.environ["BASE_URL"]
ZONA_SEARCH_URL = BASE_URL + "/search/"
FILMS_TO_SHOW = int(os.environ.get("FILMS_TO_SHOW", 10))

user_data = {}
DB_FILE = "cinema_bot.db"


async def safe_delete(chat_id: int, msg_id: int | None):
    if not msg_id:
        return
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        pass


async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS history (
                user_id INTEGER,
                query TEXT,
                timestamp TEXT
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS stats (
                user_id INTEGER,
                film_title TEXT,
                show_count INTEGER,
                PRIMARY KEY (user_id, film_title)
            )"""
        )
        await db.commit()


class ChooseFilm(StatesGroup):
    waiting_for_number = State()
    confirming_choice = State()


class ConfirmCallback(CallbackData, prefix="confirm"):
    action: str
    index: int


class NavCallback(CallbackData, prefix="nav"):
    action: str


async def parse_zona_results(query: str):
    url = ZONA_SEARCH_URL + urllib.parse.quote(query)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 \
        (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                return []
            html = await resp.text()

    soup = BeautifulSoup(html, "lxml")
    if not soup.find("div", class_="results-title"):
        return []

    results = []
    for card in soup.find_all("li", class_="results-item-wrap"):
        link_elem = card.find("a", class_="results-item")
        if not link_elem:
            continue

        href = link_elem.get("href", "")
        watch_link = BASE_URL + href if href else None

        title = link_elem.find("div", class_="results-item-title")
        title = title.get_text(strip=True) if title else "Без названия"

        year = link_elem.find("span", class_="results-item-year")
        year = year.get_text(strip=True) if year else ""

        rating_elem = link_elem.find("span", class_="results-item-rating")
        rating = (
            rating_elem.find("span").get_text(strip=True)
            if rating_elem and rating_elem.find("span")
            else "N/A"
        )

        poster_url = None
        meta_img = card.find("meta", itemprop="image")
        if meta_img:
            poster_url = meta_img.get("content")

        if not poster_url:
            preview = link_elem.find("div", class_="result-item-preview")
            if preview and preview.has_attr("style"):
                import re

                match = re.search(
                    r"background-image:\s*url\(([^)]+)\)", preview["style"]
                )
                if match:
                    url = match.group(1).strip("'\" ,")
                    if url and url != "/img/nocover.png":
                        poster_url = url

        if poster_url and poster_url.startswith("//"):
            poster_url = "https:" + poster_url

        results.append(
            {
                "title": title,
                "year": year,
                "rating": rating,
                "poster_url": poster_url,
                "watch_link": watch_link,
            }
        )

    return results


def build_keyboard(user_id: int, total: int, show_list: bool = True):
    builder = InlineKeyboardBuilder()
    current = user_data[user_id]["current_index"]
    result = user_data[user_id]["results"][current]

    if current > 0:
        builder.button(text="⬅️ Назад", callback_data=NavCallback(action="prev").pack())

    builder.button(text="🎬 Смотреть онлайн", url=result["watch_link"])

    if current < total - 1:
        builder.button(text="➡️ Далее", callback_data=NavCallback(action="next").pack())

    if total > 3 and show_list:
        builder.row(
            InlineKeyboardButton(
                text="📋 Все варианты", callback_data=NavCallback(action="list").pack()
            )
        )

    return builder.as_markup()


async def get_film_ratings(watch_link: str):
    """
    Docstring for get_film_ratings

    Parse film ratings Kinpoint and IMDb from `watch link`

    :param watch_link: Description
    :type watch_link: str
    """
    if not watch_link:
        return "—", "—"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 \
            (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        try:
            async with session.get(watch_link, timeout=ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return "—", "—"
                html = await resp.text()
        except Exception:
            return "—", "—"

    soup = BeautifulSoup(html, "lxml")
    rating_dd = soup.find("dd", class_="entity-desc-value is-rating")

    if not rating_dd:
        return "—", "—"

    kp_span = rating_dd.find("span", class_="entity-rating-kp")
    imdb_span = rating_dd.find("span", class_="entity-rating-imdb")

    return (
        kp_span.get_text(strip=True) if kp_span else "—",
        imdb_span.get_text(strip=True) if imdb_span else "—",
    )


async def show_current_film(message: Message | CallbackQuery, user_id: int):
    """
    Docstring for show_current_film

    Show poster of current film\\
    Photo\\
    Title (year)\\
    Ratings\\
    Postion\\
    Query

    :param message: Description
    :type message: Message | CallbackQuery
    :param user_id: Description
    :type user_id: int
    """
    data = user_data[user_id]
    result = data["results"][data["current_index"]]
    current = data["current_index"] + 1
    total = len(data["results"])

    if "kp" not in result:
        result["kp"], result["imdb"] = await get_film_ratings(result["watch_link"])

    full_title = result["title"] + (f" ({result['year']})" if result["year"] else "")

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            """INSERT INTO stats (user_id, film_title, show_count)
               VALUES (?, ?, 1)
               ON CONFLICT(user_id, film_title) DO UPDATE SET show_count = show_count + 1""",
            (user_id, full_title),
        )
        await db.commit()

    caption = (
        f"🎥 <b>{result['title']}</b> {f'({result['year']})' if result['year'] else ''}\n\n"
        f"⭐ <b>Рейтинги:</b>\n"
        f"   🇷🇺 Кинопоиск: <b>{result['kp']}</b>\n"
        f"   🌍 IMDb: <b>{result['imdb']}</b>\n"
        f"   🏠 Zona: <b>{result['rating']}</b>\n\n"
        f"📊 Позиция: {current} из {total}\n"
        f"🔍 Запрос: <i>{data['query']}</i>"
    )

    keyboard = build_keyboard(user_id, total)

    if isinstance(message, CallbackQuery):
        if not message.message or not message.message.chat:
            return
        chat_id = message.message.chat.id
    else:
        if not message.chat:
            return
        chat_id = message.chat.id

    msg_id = data.get("msg_id")

    media = InputMediaPhoto(
        media=result["poster_url"], caption=caption, parse_mode="HTML"
    )

    try:
        if msg_id:
            await bot.edit_message_media(
                chat_id=chat_id,
                message_id=msg_id,
                media=media,
                reply_markup=keyboard,
            )
        else:
            raise Exception("First show")
    except Exception:
        await safe_delete(chat_id, msg_id)
        data["msg_id"] = None

        sent = await bot.send_photo(
            chat_id=chat_id,
            photo=result["poster_url"],
            caption=caption,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        data["msg_id"] = sent.message_id


async def show_list_variants(message: CallbackQuery, user_id: int, state: FSMContext):
    if not message.message or not message.message.chat:
        return

    data = user_data[user_id]

    text = "📋 <b>Все найденные варианты:</b>\n\n"
    for i, res in enumerate(data["results"][:FILMS_TO_SHOW], 1):
        title = res["title"]
        if res["year"]:
            title += f" ({res['year']})"
        if len(title) > 80:
            title = title[:77] + "..."
        text += f"<b>{i}.</b> {title} — ⭐ {res['rating']}\n"

    if len(data["results"]) > FILMS_TO_SHOW:
        text += f"\n... и ещё {len(data['results']) - FILMS_TO_SHOW} вариантов"

    text += "\n\nНапиши номер нужного фильма или нажми «Закрыть»"

    builder = InlineKeyboardBuilder()
    builder.button(
        text="❌ Закрыть", callback_data=NavCallback(action="close_list").pack()
    )

    await safe_delete(message.message.chat.id, data.get("list_msg_id"))

    sent = await message.message.answer(
        text, parse_mode="HTML", reply_markup=builder.as_markup()
    )
    data["list_msg_id"] = sent.message_id

    await state.set_state(ChooseFilm.waiting_for_number)
    await state.set_data({"user_id": user_id})


async def close_list(user_id: int, chat_id: int):
    data = user_data.get(user_id)
    if data and data.get("list_msg_id"):
        try:
            await bot.delete_message(chat_id, data["list_msg_id"])
        except Exception:
            pass
        data["list_msg_id"] = None


@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    if not message.from_user or not message.chat:
        return
    await close_list(message.from_user.id, message.chat.id)

    greeting = "🎬 <b>Привет"
    if message.from_user.first_name:
        greeting += f", {message.from_user.first_name}"
    greeting += "!</b>\n\n"

    greeting += (
        "Я бот для поиска фильмов на zona.plus\n\n"
        "Просто напиши название фильма — покажу варианты с постерами, рейтингами и ссылками на просмотр.\n"
        "Кнопки: ← Назад | Далее → | Все варианты | Смотреть онлайн"
    )

    await message.answer(
        greeting,
        parse_mode="HTML",
    )


@dp.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext):
    if not message.from_user or not message.chat:
        return
    await close_list(message.from_user.id, message.chat.id)

    await message.answer(
        "ℹ️ <b>Как я работаю:</b>\n\n"
        "🔍 Просто напиши название фильма\n"
        "◀️▶️ Листай кнопками «Назад» / «Далее»\n"
        "📋 «Все варианты» — выбери номер из списка\n"
        "🎬 «Смотреть онлайн» — сразу к просмотру\n\n"
        "📊 <b>Команды:</b>\n"
        "/history — последние запросы\n"
        "/stats — твои самые просматриваемые фильмы\n"
        "/clear_data — очистить всё\n\n"
        "Приятного просмотра! 🍿",
        parse_mode="HTML",
    )


@dp.message(Command("history"))
async def cmd_history(message: Message, state: FSMContext):
    if not message.from_user or not message.chat:
        return

    user_id = message.from_user.id
    await close_list(user_id, message.chat.id)

    async with aiosqlite.connect(DB_FILE) as db:
        rows = await db.execute_fetchall(
            "SELECT query, timestamp FROM history WHERE user_id = ? ORDER BY timestamp DESC LIMIT 20",
            (user_id,),
        )

    if not rows:
        await message.answer("📜 История запросов пуста.")
        return

    text = "🕒 <b>История поисков:</b>\n\n"
    for query, timestamp in rows:
        text += f"🎬 <i>{query}</i>\n   <code>{timestamp}</code>\n\n"

    await message.answer(text, parse_mode="HTML")


@dp.message(Command("stats"))
async def cmd_stats(message: Message, state: FSMContext):
    if not message.from_user or not message.chat:
        return

    user_id = message.from_user.id
    await close_list(user_id, message.chat.id)

    async with aiosqlite.connect(DB_FILE) as db:
        rows = await db.execute_fetchall(
            "SELECT film_title, show_count FROM stats WHERE user_id = ? ORDER BY show_count DESC LIMIT 15",
            (user_id,),
        )

    if not rows:
        await message.answer(
            "📊 Статистика пока пуста...\n\nНачните листать фильмы — я запомню ваши предпочтения! ✨"
        )
        return

    text = "🔥 <b>Твои самые просматриваемые фильмы:</b>\n\n"
    for i, (title, count) in enumerate(rows, 1):
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        text += f"{medal} <b>{title}</b> — {count} раз(а)\n"

    await message.answer(text, parse_mode="HTML")


@dp.message(Command("clear_data"))
async def cmd_clear_data(message: Message, state: FSMContext):
    if not message.from_user or not message.chat:
        return

    user_id = message.from_user.id
    await close_list(user_id, message.chat.id)

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM history WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM stats WHERE user_id = ?", (user_id,))
        await db.commit()
    await message.answer("Данные очищены: история и статистика удалены. 🗑️")


@dp.message(F.text, StateFilter(None))
async def handle_search(message: Message, state: FSMContext):
    if not message.from_user or not message.text:
        return

    await state.clear()
    user_id = message.from_user.id
    query = message.text.strip()

    if user_id in user_data:
        old = user_data[user_id]

        await safe_delete(message.chat.id, old.get("list_msg_id"))

        if old.get("msg_id"):
            try:
                old_result = old["results"][old["current_index"]]
                old_caption = (
                    f"<b>{old_result['title']}</b> {f'({old_result['year']})' if old_result['year'] else ''}\n"
                    f"⭐ <b>Рейтинг:</b> {old_result['rating']}\n\n"
                    f"<i>Запрос: {old['query']} (старый поиск)</i>"
                )

                frozen_keyboard = InlineKeyboardBuilder()
                frozen_keyboard.row(
                    InlineKeyboardButton(
                        text="🎬 Смотреть онлайн", url=old_result["watch_link"]
                    )
                )

                await bot.edit_message_media(
                    chat_id=message.chat.id,
                    message_id=old["msg_id"],
                    media=InputMediaPhoto(
                        media=old_result["poster_url"],
                        caption=old_caption,
                        parse_mode="HTML",
                    ),
                    reply_markup=frozen_keyboard.as_markup(),
                )
            except Exception:
                await safe_delete(message.chat.id, old["msg_id"])

        del user_data[user_id]

    async with aiosqlite.connect(DB_FILE) as db:
        timestamp = datetime.datetime.now().strftime("%d-%m-%Y %H:%M:%S")
        await db.execute(
            "INSERT INTO history (user_id, query, timestamp) VALUES (?, ?, ?)",
            (user_id, query, timestamp),
        )
        await db.commit()

    searching = await message.answer("🔍 Ищу...")
    results = await parse_zona_results(query)
    await searching.delete()

    if not results:
        await message.answer(
            f"😔 Ничего не найдено по запросу:\n<code>{query}</code>", parse_mode="HTML"
        )
        return

    user_data[user_id] = {
        "results": results,
        "current_index": 0,
        "query": query,
        "msg_id": None,
        "list_msg_id": None,
    }

    await show_current_film(message, user_id)


@dp.callback_query(NavCallback.filter())
async def handle_navigation(
    callback: CallbackQuery, callback_data: NavCallback, state: FSMContext
):
    if not callback.message or not callback.message.chat:
        return

    user_id = callback.from_user.id
    if user_id not in user_data:
        await callback.answer("Сделай новый поиск!", show_alert=True)
        return

    data = user_data[user_id]

    match callback_data.action:
        case "prev":
            data["current_index"] -= 1

        case "next":
            data["current_index"] += 1

        case "list":
            await callback.answer()
            await show_list_variants(callback, user_id, state)
            return

        case "close_list":
            await callback.answer()

            if data.get("list_msg_id"):
                await safe_delete(callback.message.chat.id, data["list_msg_id"])
                data["list_msg_id"] = None

            await state.clear()
            return

    await show_current_film(callback, user_id)


@dp.callback_query(ConfirmCallback.filter(F.action == "yes"))
async def confirm_yes(
    callback: CallbackQuery, callback_data: ConfirmCallback, state: FSMContext
):
    if not callback.message or not callback.message.chat:
        return

    state_data = await state.get_data()
    user_id = callback.from_user.id
    if user_id not in user_data:
        await callback.answer("Сессия устарела", show_alert=True)
        await state.clear()
        return

    data = user_data[user_id]
    chosen_index = callback_data.index

    if data.get("list_msg_id"):
        await safe_delete(callback.message.chat.id, data["list_msg_id"])
        data["list_msg_id"] = None

    confirm_msg_id = state_data.get("confirm_msg_id")
    await safe_delete(callback.message.chat.id, confirm_msg_id)

    data["current_index"] = chosen_index
    await state.clear()

    await show_current_film(callback, user_id)
    await callback.answer()


@dp.callback_query(ConfirmCallback.filter(F.action == "no"))
async def confirm_no(callback: CallbackQuery, state: FSMContext):
    if not callback.message or not callback.message.chat:
        return

    confirm_msg_id = (await state.get_data()).get("confirm_msg_id")
    await safe_delete(callback.message.chat.id, confirm_msg_id)

    await callback.answer("Выберите другой номер")
    await state.set_state(ChooseFilm.waiting_for_number)


@dp.message(ChooseFilm.waiting_for_number, F.text.regexp(r"^\d+$"))
async def handle_number_choice(message: Message, state: FSMContext):
    if not message.text:
        return

    state_data = await state.get_data()
    user_id = state_data.get("user_id")
    if user_id not in user_data:
        await message.answer("Поиск устарел. Сделай новый запрос.")
        await state.clear()
        return

    data = user_data[user_id]

    try:
        num = int(message.text) - 1
        if 0 <= num < len(data["results"]):
            selected = data["results"][num]

            caption = (
                f"🔍 <b>Это тот фильм?</b>\n\n"
                f"<b>{selected['title']}</b> {f'({selected['year']})' if selected['year'] else ''}\n"
                f"⭐ <b>Рейтинг Zona:</b> {selected['rating']}"
            )

            builder = InlineKeyboardBuilder()
            builder.row(
                InlineKeyboardButton(
                    text="✅ Да, тот!",
                    callback_data=ConfirmCallback(action="yes", index=num).pack(),
                ),
                InlineKeyboardButton(
                    text="❌ Нет, другой",
                    callback_data=ConfirmCallback(action="no", index=num).pack(),
                ),
            )

            confirm_msg = await message.answer_photo(
                photo=selected["poster_url"],
                caption=caption,
                parse_mode="HTML",
                reply_markup=builder.as_markup(),
            )
            await state.set_state(ChooseFilm.confirming_choice)
            await state.update_data(
                confirm_msg_id=confirm_msg.message_id, chosen_index=num
            )

        else:
            await message.answer(f"Введите номер от 1 до {num}")

    except ValueError:
        await message.answer("Введите только число")


@dp.message(ChooseFilm.waiting_for_number)
async def handle_invalid(message: Message, state: FSMContext):
    await message.answer("Пожалуйста, напиши только номер фильма из списка.")


async def set_bot_commands():
    commands = [
        ("start", "Перезапустить бота"),
        ("help", "Помощь и инструкции"),
        ("history", "История ваших поисков"),
        ("stats", "Статистика выбранных фильмов"),
        ("clear_data", "Очистить историю и статистику"),
    ]

    await bot.set_my_commands(
        [BotCommand(command=cmd, description=desc) for cmd, desc in commands]
    )


async def main():
    await init_db()
    await set_bot_commands()
    print("CinemaBot запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
