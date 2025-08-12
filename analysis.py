import aiohttp
import asyncio
import random
import logging
import requests
from config import BINANCE_API_URL, get_current_proxy, rotate_proxy, record_proxy_usage
from datetime import datetime

logger = logging.getLogger(__name__)


async def sleep_random():
    """Случайная задержка от 0.7 до 0.9 секунды"""
    await asyncio.sleep(random.uniform(0.7, 0.9))


async def update_progress_with_bot(bot, update, current_step, steps,
                                   progress_message_id):
    """Обновляет прогресс с использованием переданного бота"""
    total_steps = len(steps)
    percentage = int((current_step / total_steps) * 100)

    # Создаем прогресс-бар
    filled_squares = "🟦" * current_step
    empty_squares = "⬜" * (total_steps - current_step)
    progress_bar = f"{filled_squares}{empty_squares} {percentage}%"

    # Создаем список этапов
    steps_list = []
    for i, step in enumerate(steps):
        if i < current_step:
            steps_list.append(f"✅ {step}")
        elif i == current_step:
            steps_list.append(f"🔄 {step}")
        else:
            steps_list.append(f"⏳ {step}")

    message_text = f"{progress_bar}\n" + "\n".join(steps_list)

    # Определяем chat_id
    if hasattr(update, 'callback_query') and update.callback_query:
        chat_id = update.callback_query.message.chat_id
    else:
        chat_id = update.message.chat_id

    try:
        await bot.edit_message_text(chat_id=chat_id,
                                    message_id=progress_message_id,
                                    text=message_text)
        await sleep_random()  # Добавляем задержку для визуального эффекта
    except Exception as e:
        logger.error(f"Ошибка обновления прогресса: {e}")


async def make_request_with_retry(url, params, timeout=10, weight=1):
    """
    Делает асинхронный HTTP GET запрос с балансированными прокси и повторными попытками.
    """
    from config import get_current_proxy, record_proxy_usage, rotate_proxy

    attempts = 2  # Уменьшаем количество попыток для более быстрой ротации

    for attempt in range(attempts):
        proxy = get_current_proxy()

        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                'Accept': 'application/json',
                'Accept-Language': 'en-US,en;q=0.9'
            }

            connector = aiohttp.TCPConnector() if proxy else None

            if proxy:
                logger.info(f"🌐 Попытка {attempt + 1}: используем балансированный прокси")

            async with aiohttp.ClientSession(
                    connector=connector,
                    timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.get(
                        url,
                        params=params,
                        headers=headers,
                        proxy=proxy if proxy else None) as response:

                    if response.status == 200:
                        data = await response.json()

                        # Записываем успешное использование
                        if proxy:
                            record_proxy_usage(proxy, weight=weight, error=False)

                        logger.info(f"✅ Успешный запрос к {url} (weight: {weight})")
                        return {
                            "status_code": response.status,
                            "json": lambda: data
                        }
                    else:
                        logger.warning(f"⚠️ HTTP {response.status} на попытке {attempt + 1}")

                        # Записываем ошибку только для серьезных статусов
                        if proxy and response.status >= 400:
                            record_proxy_usage(proxy, weight=weight, error=True)

                        return {"status_code": response.status, "json": lambda: None}
        except Exception as e:
            logger.warning(f"❌ Ошибка на попытке {attempt + 1}: {e}")
            if proxy and attempt < attempts - 1:
                logger.info("🔄 Ротация прокси...")
                rotate_proxy()
            await asyncio.sleep(1)  # Пауза между попытками

    logger.error("❌ Все попытки исчерпаны")
    return None


async def validate_ticker(ticker):
    """Проверяет, существует ли торговая пара на Binance"""
    try:
        # Сначала проверяем через ticker/24hr статистику (более простой эндпоинт)
        url = f"{BINANCE_API_URL}/api/v3/ticker/24hr"
        params = {'symbol': f"{ticker}USDT"}
        response = await make_request_with_retry(url, params, timeout=10)

        if response is None:
            logger.error(
                f"Не удалось получить ответ для валидации тикера {ticker} после всех попыток."
            )
            # Возвращаем специальный код для ошибки подключения
            return "CONNECTION_ERROR"

        # Если получили 451 или другие блокировки, считаем тикер валидным для популярных монет
        if response["status_code"] == 451:
            logger.warning(
                f"Binance API заблокирован (451), принимаем {ticker} как валидный"
            )
            return ticker.upper() in [
                'BTC', 'ETH', 'BNB', 'ADA', 'SOL', 'XRP', 'DOGE', 'AVAX',
                'DOT', 'MATIC', 'SHIB', 'LTC', 'ATOM', 'UNI', 'LINK'
            ]

        if response["status_code"] == 200:
            data = response["json"]()
            return 'symbol' in data
        elif response["status_code"] == 400:
            # 400 обычно означает что символ не найден
            return False
        else:
            logger.warning(
                f"Код ответа {response['status_code']} для {ticker}, возможна ошибка подключения"
            )
            # При других ошибках считаем это проблемой подключения
            return "CONNECTION_ERROR"

    except Exception as e:
        logger.error(f"Ошибка валидации {ticker}: {e}")
        # При исключениях считаем это ошибкой подключения
        return "CONNECTION_ERROR"


async def get_klines(symbol, interval, limit=100):
    """Получает свечные данные с Binance"""
    try:
        url = f"{BINANCE_API_URL}/api/v3/klines"
        params = {'symbol': symbol, 'interval': interval, 'limit': limit}
        response = await make_request_with_retry(url, params, timeout=15)

        if response is None:
            logger.error(
                f"Не удалось получить ответ для получения данных klines для {symbol} после всех попыток."
            )
            return None

        if response["status_code"] == 451:
            logger.error(f"API заблокирован для {symbol} (451)")
            return None

        if response["status_code"] == 200:
            return response["json"]()
        else:
            logger.error(
                f"Ошибка получения данных для {symbol}: HTTP {response['status_code']}"
            )
            return None
    except requests.RequestException as e:
        logger.error(f"Ошибка получения данных для {symbol}: {e}")
        return None


def calculate_sma(data, period):
    """Вычисляет простую скользящую среднюю"""
    if not data or len(data) < period:
        return None
    closes = [float(candle[4]) for candle in data]
    return sum(closes[-period:]) / period


def calculate_rsi(data, period=14):
    """Вычисляет индекс относительной силы"""
    if not data or len(data) < period:
        return None
    closes = [float(candle[4]) for candle in data]
    gains = losses = 0
    for i in range(1, period):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    rs = avg_gain / avg_loss if avg_loss != 0 else 0
    return 100 - (100 / (1 + rs))


def get_support_resistance_levels(data_4h, data_1h):
    """Определяет уровни поддержки и сопротивления"""
    if not data_4h or not data_1h:
        return None, None

    # Берем последние данные
    recent_4h = data_4h[-30:]  # Последние 30 свечей 4h
    recent_1h = data_1h[-50:]  # Последние 50 свечей 1h

    # Уровни с разных таймфреймов
    lows_4h = [float(candle[3]) for candle in recent_4h]
    highs_4h = [float(candle[2]) for candle in recent_4h]
    lows_1h = [float(candle[3]) for candle in recent_1h]
    highs_1h = [float(candle[2]) for candle in recent_1h]

    # Находим ключевые уровни
    support = min(min(lows_4h), min(lows_1h[-20:]))
    resistance = max(max(highs_4h), max(highs_1h[-20:]))

    return support, resistance


def calculate_risk_reward(entry, stop_loss, take_profit):
    """Вычисляет соотношение риск/прибыль"""
    risk = abs(entry - stop_loss)
    reward = abs(take_profit - entry)
    return reward / risk if risk > 0 else 0


def format_price(price):
    """Динамическое округление цены в зависимости от её величины"""
    if price < 1:
        return f"${price:.6f}"
    elif price < 10:
        return f"${price:.4f}"
    elif price < 100:
        return f"${price:.3f}"
    elif price < 1000:
        return f"${price:.2f}"
    else:
        return f"${price:,.0f}"


def format_signal(symbol, current_price, direction, entry_price, stop_loss,
                  take_profit, stop_loss_pct, take_profit_pct, risk_reward,
                  cancel_price, warning, sma_50, sma_200, support, resistance):
    """Форматирует торговый сигнал"""

    direction_emoji = "📈" if direction == "Long" else "📉"
    level_price = support if direction == "Long" else resistance
    level_name = "поддержки" if direction == "Long" else "сопротивления"
    trend_name = "восходящий" if direction == "Long" else "нисходящий"
    explanation = f"{trend_name} тренд, вход от {level_name} {format_price(level_price)}"

    signal = f"""{direction_emoji} *{symbol}*
💲 Текущая цена: {format_price(current_price)}
📊 Направление: *{direction}*
🎯 Точка входа: {format_price(entry_price)} (лимитный ордер)
🛑 Стоп-лосс: {format_price(stop_loss)} ({stop_loss_pct:+.2f}%)
💎 Тейк-профит: {format_price(take_profit)} ({take_profit_pct:+.2f}%)
⚖️ Риск/Прибыль: 1:{risk_reward:.1f}
💡 Пояснение: {explanation}
❌ Условия отмены: Пробой {format_price(cancel_price)}"""

    if warning:
        signal += f"\n\n{warning}"

    return signal


def format_progress_bars(current_step, total_steps, square_type="🟦"):
    """Форматирует прогресс-бары"""
    filled = square_type * current_step
    empty = "⏳" * (total_steps - current_step)
    percentage = int((current_step / total_steps) * 100)
    return f"{filled}{empty} {percentage}%"


def format_steps_list(steps, current_step):
    """Форматирует список этапов"""
    result = []
    for i, step_text in enumerate(steps):
        if i < current_step - 1:
            result.append(f"✅ {step_text}")
        elif i == current_step - 1:
            result.append(f"🔄 {step_text}")
    return result


async def update_progress_message(update, current_step, steps,
                                  progress_message_id):
    """Обновляет сообщение с прогрессом"""
    total_steps = len(steps)
    percentage = int((current_step / total_steps) * 100)

    # Создаем прогресс-бар
    filled_squares = "🟦" * current_step
    empty_squares = "⬜" * (total_steps - current_step)
    progress_bar = f"{filled_squares}{empty_squares} {percentage}%"

    # Создаем список этапов
    steps_list = []
    for i, step in enumerate(steps):
        if i < current_step:
            steps_list.append(f"✅ {step}")
        elif i == current_step:
            steps_list.append(f"🔄 {step}")
        else:
            steps_list.append(f"⏳ {step}")

    message_text = f"{progress_bar}\n" + "\n".join(steps_list)

    # Определяем где взять chat_id и bot
    if hasattr(update, 'callback_query') and update.callback_query:
        chat_id = update.callback_query.message.chat_id
        bot = update.callback_query.bot if hasattr(update.callback_query,
                                                   'bot') else None
    else:
        chat_id = update.message.chat_id
        bot = update.message.bot if hasattr(update.message, 'bot') else None

    try:
        if bot:
            await bot.edit_message_text(chat_id=chat_id,
                                        message_id=progress_message_id,
                                        text=message_text)
    except Exception as e:
        logger.error(f"Ошибка обновления прогресса: {e}")


async def analyze_ticker(ticker, update, progress_message_id=None, bot=None):
    """Основная функция анализа тикера"""
    symbol = f"{ticker}USDT"

    steps = [
        "Подключение к Binance API...", "Загрузка исторических данных...",
        "Мультифакторный анализ...", "Определение уровней входа...",
        "Расчет риск/прибыль...", "✅ Сигнал готов!"
    ]

    current_step = 1
    if progress_message_id and bot:
        await update_progress_with_bot(bot, update, current_step, steps,
                                       progress_message_id)

    # Валидация тикера
    validation_result = await validate_ticker(ticker)
    if validation_result == "CONNECTION_ERROR":
        return f"❌ Ошибка подключения к Binance API. Попробуйте снова через несколько секунд."
    elif not validation_result:
        return f"❌ Ошибка: тикер {ticker} не найден на Binance. Попробуйте другой, например, BTC или ETH."

    # Этап 2: Получение данных
    current_step = 2
    if progress_message_id and bot:
        await update_progress_with_bot(bot, update, current_step, steps,
                                       progress_message_id)

    data_1d = await get_klines(symbol, '1d', 200)
    data_4h = await get_klines(symbol, '4h', 100)
    data_1h = await get_klines(symbol, '1h', 50)

    if not (data_1d and data_4h and data_1h):
        return f"❌ Ошибка подключения к Binance API или нет данных для {ticker}. Попробуйте снова."

    # Этап 3: Расчеты SMA
    current_step = 3
    if progress_message_id and bot:
        await update_progress_with_bot(bot, update, current_step, steps,
                                       progress_message_id)

    current_price = float(data_1h[-1][4])
    sma_50_1d = calculate_sma(data_1d, 50)
    sma_200_1d = calculate_sma(data_1d, 200)

    if sma_50_1d is None or sma_200_1d is None:
        return f"❌ Ошибка: недостаточно данных для расчета тренда для {ticker}."

    # Этап 4: Определение уровней
    current_step = 4
    if progress_message_id and bot:
        await update_progress_with_bot(bot, update, current_step, steps,
                                       progress_message_id)

    direction = 'Long' if sma_50_1d > sma_200_1d else 'Short'
    support, resistance = get_support_resistance_levels(data_4h, data_1h)

    if support is None or resistance is None:
        return f"❌ Ошибка: не удалось определить уровни для {ticker}."

    # Этап 5: Расчет риск/прибыль
    current_step = 5
    if progress_message_id and bot:
        await update_progress_with_bot(bot, update, current_step, steps,
                                       progress_message_id)

    entry_price = support * 1.005 if direction == 'Long' else resistance * 0.995
    stop_loss = support * 0.98 if direction == 'Long' else resistance * 1.02
    take_profit = resistance if direction == 'Long' else support
    risk_reward = calculate_risk_reward(entry_price, stop_loss, take_profit)

    # Расчет процентов (стоп-лосс всегда убыток, тейк-профит всегда прибыль)
    if direction == 'Long':
        stop_loss_pct = (
            (stop_loss - entry_price) / entry_price) * 100  # отрицательный
        take_profit_pct = (
            (take_profit - entry_price) / entry_price) * 100  # положительный
    else:  # Short
        stop_loss_pct = -((stop_loss - entry_price) /
                          entry_price) * 100  # отрицательный (убыток)
        take_profit_pct = -((take_profit - entry_price) /
                            entry_price) * 100  # положительный (прибыль)
    cancel_price = support * 0.99 if direction == 'Long' else resistance * 1.01

    # Предупреждение при низком соотношении риск/прибыль
    warning = ""
    if risk_reward < 2:
        warning = f"⚠️ Для {ticker} соотношение риск/прибыль слишком низкое ({risk_reward:.2f}). Рекомендуем пропустить сигнал."

    # Этап 6: Сигнал готов
    current_step = 6
    if progress_message_id and bot:
        await update_progress_with_bot(bot, update, current_step, steps,
                                       progress_message_id)
        await sleep_random(
        )  # Небольшая пауза чтобы пользователь увидел завершение

    return format_signal(symbol, current_price, direction, entry_price,
                         stop_loss, take_profit, stop_loss_pct,
                         take_profit_pct, risk_reward, cancel_price, warning,
                         sma_50_1d, sma_200_1d, support, resistance)