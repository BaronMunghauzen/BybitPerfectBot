from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv
import os
import asyncio
import aiosqlite
from datetime import datetime
from pybit.unified_trading import HTTP
import pandas as pd
import time
import aiocron

# Загрузка переменных окружения
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_ADMIN_ID = int(os.getenv("TELEGRAM_ADMIN_ID"))  # Ваш user_id
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

# Константы
TIMEFRAME = 15  # Таймфрейм в минутах
CONTRACTS = ['WIFUSDT', 'ARKUSDT', 'SLERFUSDT', 'ATAUSDT', 'ADAUSDT', 'DEGENUSDT', 'ARUSDT',
             'MOVRUSDT', 'SSVUSDT', 'AUCTIONUSDT', 'ZETAUSDT', 'RAREUSDT', 'SKLUSDT', 'AXLUSDT',
             'SANDUSDT', 'AXSUSDT', 'SNXUSDT']  # Список контрактов
MA_PERIOD = 200  # Период для Moving Average
PRICE_CHANGE_THRESHOLD = 0.1  # Порог изменения цены в процентах 0.5
VOLUME_THRESHOLD = 1.01  # Порог увеличения объема (например, 1.2 = на 20% больше) 1.2
RISK_PERCENTAGE = 0.1  # Риск на сделку (10% от капитала)
STOP_LOSS_PERCENT = 0.5  # Стоп-лосс в процентах
TAKE_PROFIT_PERCENT = 10.0  # Тейк-профит в процентах

# Инициализация Bybit (демо-режим)
session = HTTP(
    testnet=True,  # Демо-режим
    api_key=BYBIT_API_KEY,
    api_secret=BYBIT_API_SECRET
)

# Инициализация Telegram-бота
bot = Bot(
    token=TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN)  # Установка parse_mode
)
dp = Dispatcher()

# Инициализация базы данных
DB_NAME = "trades.db"

async def init_db():
    """Инициализация базы данных."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT,
                contract TEXT,
                side TEXT,
                quantity REAL,
                entry_price REAL,
                stop_loss REAL,
                take_profit REAL,
                exit_price REAL,
                profit_loss REAL,
                volume REAL
            )"""
        )
        await db.commit()

async def save_trade_to_db(trade_data):
    """Сохранение информации о сделке в базу данных."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """INSERT INTO trades (
                date, contract, side, quantity, entry_price, stop_loss, take_profit, exit_price, profit_loss, volume
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trade_data["date"], trade_data["contract"], trade_data["side"], trade_data["quantity"],
                trade_data["entry_price"], trade_data["stop_loss"], trade_data["take_profit"],
                trade_data["exit_price"], trade_data["profit_loss"], trade_data["volume"]
            )
        )
        await db.commit()

async def fetch_stats(filter_by=None, filter_value=None):
    """Получение статистики из базы данных."""
    async with aiosqlite.connect(DB_NAME) as db:
        query = "SELECT * FROM trades"
        if filter_by and filter_value:
            query += f" WHERE {filter_by} = ?"
            cursor = await db.execute(query, (filter_value,))
        else:
            cursor = await db.execute(query)
        rows = await cursor.fetchall()
        return rows

async def send_telegram_message(chat_id, message):
    """Отправка сообщения в Telegram."""
    await bot.send_message(chat_id=chat_id, text=message)


def fetch_ohlcv(symbol, timeframe, limit):
    """Получение исторических данных по контракту."""
    try:
        response = session.get_kline(
            category="linear",  # Для USDT-фьючерсов
            symbol=symbol,
            interval=str(timeframe),
            limit=limit
        )

        # Проверяем, что данные есть в ответе
        if 'result' not in response or 'list' not in response['result']:
            print(f"Ошибка: Нет данных в ответе для {symbol}")
            return pd.DataFrame()  # Возвращаем пустой DataFrame

        # Создаем DataFrame
        df = pd.DataFrame(
            response['result']['list'],
            columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover']
        )

        # Преобразуем типы данных
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df[['open', 'high', 'low', 'close', 'volume']] = df[['open', 'high', 'low', 'close', 'volume']].astype(float)

        return df

    except Exception as e:
        print(f"Ошибка при получении данных для {symbol}: {e}")
        return pd.DataFrame()  # Возвращаем пустой DataFrame в случае ошибки

def calculate_aggregated_price_and_volume(contracts, timeframe, limit):
    """Расчет общей цены и объема на основе всех контрактов."""
    aggregated_price = 0
    aggregated_volume = 0
    prices = {}
    for contract in contracts:
        df = fetch_ohlcv(contract, timeframe, limit)
        if df.empty:
            print(f"Нет данных для контракта {contract}")
            continue  # Пропускаем этот контракт
        last_close = df['close'].iloc[-1]
        last_volume = df['volume'].iloc[-1]
        prices[contract] = last_close
        aggregated_price += last_close
        aggregated_volume += last_volume
    return aggregated_price / len(contracts), aggregated_volume / len(contracts), prices

def calculate_moving_average(df, period):
    """Расчет Moving Average."""
    return df['close'].rolling(window=period).mean()

def check_entry_conditions(price_change, volume_change, ma, current_price):
    """Проверка условий для входа в сделку."""
    if current_price > ma:
        position_flag = 'Buy'  # Длинная позиция
    else:
        position_flag = 'Sell'  # Короткая позиция

    if abs(price_change) > PRICE_CHANGE_THRESHOLD and volume_change > VOLUME_THRESHOLD:
        return position_flag
    return None

def place_order(contract, side, qty, entry_price):
    """Размещение ордера с установкой стоп-лосса и тейк-профита."""
    try:
        stop_loss = entry_price * (1 - STOP_LOSS_PERCENT / 100) if side == "Buy" else entry_price * (1 + STOP_LOSS_PERCENT / 100)
        take_profit = entry_price * (1 + TAKE_PROFIT_PERCENT / 100) if side == "Buy" else entry_price * (1 - TAKE_PROFIT_PERCENT / 100)

        order = session.place_active_order(
            symbol=contract,
            side=side,
            order_type="Market",
            qty=str(qty),
            time_in_force="GTC",
            stop_loss=str(stop_loss),
            take_profit=str(take_profit)
        )
        return order, stop_loss, take_profit
    except Exception as e:
        raise e

async def trading_logic():
    """Основная логика торговли."""
    try:
        # Получаем общую цену и объем
        aggregated_price, aggregated_volume, prices = calculate_aggregated_price_and_volume(CONTRACTS, TIMEFRAME, MA_PERIOD)

        # Получаем данные для расчета MA
        df = fetch_ohlcv(CONTRACTS[0], TIMEFRAME, MA_PERIOD)  # Используем первый контракт для расчета MA
        if df.empty:
            print(f"Нет данных для контракта {contract}")
            return  # Пропускаем этот контракт
        ma = calculate_moving_average(df, MA_PERIOD).iloc[-1]

        # Проверяем условия для входа
        price_change = (df['close'].iloc[-1] - df['open'].iloc[-1]) / df['open'].iloc[-1] * 100
        volume_change = df['volume'].iloc[-1] / df['volume'].iloc[-2]

        position_flag = check_entry_conditions(price_change, volume_change, ma, aggregated_price)

        if position_flag:
            message = f"*Условия для входа в {position_flag} позицию выполнены!*"
            await send_telegram_message(TELEGRAM_ADMIN_ID, message)

            # Получаем текущий баланс
            balance = session.get_wallet_balance(coin="USDT")['result']['USDT']['available_balance']
            risk_amount = float(balance) * RISK_PERCENTAGE

            # Рассчитываем размер позиции для каждого контракта
            for contract in CONTRACTS:
                contract_price = prices[contract]
                weight = contract_price / aggregated_price
                qty = (risk_amount * weight) / contract_price
                qty = round(qty, 3)  # Округляем до 3 знаков

                # Размещаем ордер
                try:
                    order, stop_loss, take_profit = place_order(contract, position_flag, qty, contract_price)
                    message = (
                        f"Ордер размещен:\n"
                        f"*Контракт*: {contract}\n"
                        f"*Сторона*: {position_flag}\n"
                        f"*Количество*: {qty}\n"
                        f"*Цена входа*: {contract_price}\n"
                        f"*Стоп-лосс*: {stop_loss}\n"
                        f"*Тейк-профит*: {take_profit}"
                    )
                    await send_telegram_message(TELEGRAM_ADMIN_ID, message)

                    # Сохраняем сделку в базу данных
                    trade_data = {
                        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "contract": contract,
                        "side": position_flag,
                        "quantity": qty,
                        "entry_price": contract_price,
                        "stop_loss": stop_loss,
                        "take_profit": take_profit,
                        "exit_price": None,  # Будет заполнено при закрытии сделки
                        "profit_loss": None,  # Будет заполнено при закрытии сделки
                        "volume": df['volume'].iloc[-1]
                    }
                    await save_trade_to_db(trade_data)

                except Exception as e:
                    message = f"*Ошибка при размещении ордера: {e}*"
                    await send_telegram_message(TELEGRAM_ADMIN_ID, message)

        else:
            message = "Условия для входа не выполнены."
            await send_telegram_message(TELEGRAM_ADMIN_ID, message)

    except Exception as e:
        message = f"*Произошла ошибка: {e}*"
        await send_telegram_message(TELEGRAM_ADMIN_ID, message)

# Клавиатура для статистики
stats_keyboard = ReplyKeyboardBuilder()
stats_keyboard.add(KeyboardButton(text="Статистика по дням"))
stats_keyboard.add(KeyboardButton(text="Статистика по контрактам"))
stats_keyboard.add(KeyboardButton(text="Общая статистика"))

@dp.message(Command("start"))
async def send_welcome(message: types.Message):
    """Обработка команды /start."""
    if message.from_user.id == TELEGRAM_ADMIN_ID:
        await message.reply("Привет! Я торговый бот. Используй кнопки для получения статистики.", reply_markup=stats_keyboard.as_markup())
    else:
        await message.reply("У вас нет доступа к этому боту.")

@dp.message(lambda message: message.text == "Статистика по дням")
async def stats_by_day(message: types.Message):
    """Статистика по дням."""
    if message.from_user.id == TELEGRAM_ADMIN_ID:
        stats = await fetch_stats()
        stats_by_day = {}
        for trade in stats:
            date = trade[1][:10]  # Берем только дату
            if date not in stats_by_day:
                stats_by_day[date] = []
            stats_by_day[date].append(trade)
        response = "Статистика по дням:\n"
        for date, trades in stats_by_day.items():
            response += f"📅 {date}:\n"
            for trade in trades:
                response += f"  - {trade[2]} ({trade[3]}): {trade[4]} шт.\n"
        await message.reply(response)
    else:
        await message.reply("У вас нет доступа к этой команде.")

@dp.message(lambda message: message.text == "Статистика по контрактам")
async def stats_by_contract(message: types.Message):
    """Статистика по контрактам."""
    if message.from_user.id == TELEGRAM_ADMIN_ID:
        stats = await fetch_stats()
        stats_by_contract = {}
        for trade in stats:
            contract = trade[2]
            if contract not in stats_by_contract:
                stats_by_contract[contract] = []
            stats_by_contract[contract].append(trade)
        response = "Статистика по контрактам:\n"
        for contract, trades in stats_by_contract.items():
            response += f"📊 {contract}:\n"
            for trade in trades:
                response += f"  - {trade[1]} ({trade[3]}): {trade[4]} шт.\n"
        await message.reply(response)
    else:
        await message.reply("У вас нет доступа к этой команде.")

@dp.message(lambda message: message.text == "Общая статистика")
async def overall_stats(message: types.Message):
    """Общая статистика."""
    if message.from_user.id == TELEGRAM_ADMIN_ID:
        stats = await fetch_stats()
        total_trades = len(stats)
        total_profit = sum(trade[9] or 0 for trade in stats)
        response = (
            "Общая статистика:\n"
            f"*📈 Всего сделок: {total_trades}\n*"
            f"*💰 Общая прибыль/убыток: {total_profit:.2f} USDT*"
        )
        await message.reply(response)
    else:
        await message.reply("У вас нет доступа к этой команде.")

async def on_shutdown():
    """Закрытие сессий при завершении работы."""
    await bot.session.close()

async def main():
    await init_db()
    # Запуск шедулера каждые 15 минут
    aiocron.crontab('*/15 * * * *', func=trading_logic)

    # Запуск бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    # Запуск бота
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен.")
    finally:
        asyncio.run(on_shutdown())