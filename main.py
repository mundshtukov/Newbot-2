import os
import asyncio
import logging
import threading
import signal
import sys
from contextlib import asynccontextmanager
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from fastapi import FastAPI, Request
import uvicorn
import json
from datetime import datetime, timedelta

from coingecko import get_top_coins, update_coins_cache
from analysis import analyze_ticker
from config import TELEGRAM_BOT_TOKEN, ENVIRONMENT, WEBHOOK_URL, PORT, HOST, BOT_DISABLED
from admin_users import is_admin_user, is_super_admin, get_access_denied_message, is_chat_member, can_view_proxy_stats, get_chat_access_denied_message, has_basic_access

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO)
logger = logging.getLogger(__name__)

# FastAPI приложение
app = FastAPI(lifespan=lifespan)

# Глобальные переменные
cached_coins = []
telegram_app = None
shutdown_event = asyncio.Event()

# Обработчик сигналов
def signal_handler(signum, frame):
    """Обработчик сигналов завершения"""
    logger.info(f"Получен сигнал {signum}. Начинаю graceful shutdown...")
    shutdown_event.set()

# Регистрируем обработчики сигналов
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# Контекст-менеджер для управления жизненным циклом
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    global telegram_app, cached_coins
    logger.info("Инициализация приложения...")

    # Проверяем, отключен ли бот
    if BOT_DISABLED:
        logger.info("🚫 Бот отключен (BOT_DISABLED=true)")
        yield
        return

    # Проверка на единственный экземпляр
    import psutil
    current_pid = os.getpid()
    python_processes = []

    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if proc.info['name'] and 'python' in proc.info['name'].lower():
                cmdline = proc.info['cmdline']
                if cmdline and 'main.py' in ' '.join(cmdline):
                    if proc.info['pid'] != current_pid:
                        python_processes.append(proc.info['pid'])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if python_processes:
        logger.warning(
            f"🚫 Обнаружены другие экземпляры main.py: {python_processes}")
        logger.warning("Завершаю работу во избежание конфликтов")
        yield
        return

    # Создаем Telegram Application
    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Добавляем обработчики
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CallbackQueryHandler(button_callback))
    telegram_app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Используем fallback монеты для быстрого старта
    cached_coins = [
        'BTC', 'ETH', 'ADA', 'SOL', 'XRP', 'DOGE', 'AVAX', 'DOT', 'MATIC',
        'SHIB', 'LTC', 'ATOM'
    ]
    logger.info(f"Загружено {len(cached_coins)} монет (fallback)")

    # Загружаем актуальные монеты в фоне после старта сервера
    async def load_coins_after_startup():
        """Загружает актуальные монеты после старта сервера"""
        try:
            await asyncio.sleep(5)  # Ждем 5 секунд после старта
            global cached_coins
            logger.info("🔄 Принудительное обновление списка монет после перезапуска...")
            fresh_coins = await update_coins_cache()  # Используем update_coins_cache для принудительного обновления
            if fresh_coins and len(fresh_coins) >= 10:
                cached_coins = fresh_coins
                logger.info(f"✅ После перезапуска загружены актуальные монеты: {len(fresh_coins)}")
                logger.info(f"Обновленный список: {', '.join(fresh_coins)}")
            else:
                logger.warning("⚠️ Не удалось загрузить актуальные монеты, используем fallback")
                # Пробуем загрузить из кэша как fallback
                cached_coins = await get_top_coins()
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки актуальных монет: {e}")
            # В случае ошибки пробуем загрузить из кэша
            try:
                cached_coins = await get_top_coins()
            except:
                pass

    # Запускаем автоматическое обновление каждые 6 часов
    async def auto_update_coins():
        """Автоматически обновляет список монет каждые 6 часов"""
        while not shutdown_event.is_set():
            try:
                await asyncio.sleep(6 * 60 * 60)  # 6 часов = 21600 секунд
                if shutdown_event.is_set():
                    break
                logger.info("🔄 Автоматическое обновление списка монет...")
                new_coins = await update_coins_cache()
                if new_coins and len(new_coins) >= 10:
                    global cached_coins
                    cached_coins = new_coins
                    logger.info(f"✅ Автоматически обновлено: {len(new_coins)} монет")
                else:
                    logger.warning("⚠️ Автоматическое обновление не дало результатов")
            except Exception as e:
                logger.error(f"❌ Ошибка автоматического обновления: {e}")

    # Keep-alive механизм для предотвращения завершения процесса
    async def keep_alive():
        """Внутренний keep-alive механизм"""
        while not shutdown_event.is_set():
            try:
                await asyncio.sleep(180)  # Каждые 3 минуты
                if not shutdown_event.is_set():
                    logger.info("🫀 Keep-alive ping - бот активен")
                    # Дополнительная активность - проверяем состояние бота
                    if telegram_app and telegram_app.running:
                        logger.info("📱 Telegram бот в рабочем состоянии")
            except Exception as e:
                logger.error(f"❌ Ошибка keep-alive: {e}")

    # Внешний keep-alive пинг (каждые 10 минут)
    async def external_keepalive():
        """Внешний keep-alive для поддержания активности"""
        await asyncio.sleep(60)  # Начинаем через минуту после старта
        while not shutdown_event.is_set():
            try:
                if WEBHOOK_URL:  # Только если есть внешний URL
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        url = f"{WEBHOOK_URL}/keepalive"
                        await session.get(url, timeout=10)
                        logger.info("🌐 Внешний keep-alive пинг отправлен")
                await asyncio.sleep(600)  # Каждые 10 минут
            except Exception as e:
                logger.error(f"❌ Ошибка внешнего keep-alive: {e}")
                await asyncio.sleep(600)

    # Запускаем фоновые задачи
    asyncio.create_task(load_coins_after_startup())
    asyncio.create_task(auto_update_coins())
    asyncio.create_task(keep_alive())
    asyncio.create_task(external_keepalive())

    # Инициализация бота
    await telegram_app.initialize()

    # Определяем режим работы четко и однозначно
    from config import IS_RENDER_DEPLOYMENT
    use_webhook = bool(WEBHOOK_URL and (ENVIRONMENT == 'production' or IS_RENDER_DEPLOYMENT))

    if use_webhook:
        logger.info("🚀 Продакшн режим (webhook)")
    else:
        logger.info("🔧 Режим разработки (polling)")

    # Запускаем в соответствующем режиме
    await telegram_app.start()

    if use_webhook:
        # Продакшн режим - ТОЛЬКО webhook
        try:
            logger.info("🚀 Настройка webhook режима...")

            # Принудительно удаляем webhook и очищаем состояние
            await telegram_app.bot.delete_webhook(drop_pending_updates=True)
            await asyncio.sleep(3)  # Больше времени для очистки

            # Устанавливаем новый webhook
            webhook_url = f"{WEBHOOK_URL}/webhook"
            result = await telegram_app.bot.set_webhook(
                webhook_url, drop_pending_updates=True, max_connections=40)

            if result:
                logger.info(f"✅ Webhook установлен: {webhook_url}")
            else:
                logger.error("❌ Не удалось установить webhook")

        except Exception as e:
            logger.error(f"❌ Ошибка настройки webhook: {e}")
    else:
        # Режим разработки - ТОЛЬКО polling
        try:
            logger.info("🔧 Настройка polling режима...")

            # Удаляем webhook полностью
            await telegram_app.bot.delete_webhook(drop_pending_updates=True)
            await asyncio.sleep(2)

            logger.info("✅ Webhook удален, готов к polling")

            # Запускаем polling в фоновой задаче
            async def run_polling():
                try:
                    await telegram_app.run_polling(drop_pending_updates=True)
                except Exception as e:
                    logger.error(f"❌ Ошибка polling: {e}")

            asyncio.create_task(run_polling())

    try:
        yield
    finally:
        # Shutdown
        logger.info("Завершение работы приложения...")
        shutdown_event.set()
        if telegram_app:
            await telegram_app.stop()
            await telegram_app.shutdown()
        logger.info("Приложение завершено")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    if not await has_basic_access(context.bot, update.effective_user.id):
        await update.message.reply_text(get_chat_access_denied_message())
        return

    keyboard = [
        [KeyboardButton("💰 К списку монет"), KeyboardButton("📋 Инструкция")],
    ]

    if is_super_admin(update.effective_user.id):
        keyboard.append([KeyboardButton("🔄 Обновить список"), KeyboardButton("📊 Статистика прокси")])
    elif is_admin_user(update.effective_user.id):
        keyboard.append([KeyboardButton("🔍 Анализ по тикеру")])

    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    welcome_message = """
🚀 Добро пожаловать в Crypto Signals Bot!

📊 Получайте мгновенные торговые сигналы на основе ИИ-анализа в реальном времени.

🎯 Выберите действие ниже:
"""
    await update.message.reply_text(welcome_message, reply_markup=reply_markup, parse_mode='Markdown')

async def show_coins_list(update, context, edit_message=True):
    """Показывает список топ-монет"""
    global cached_coins
    user_id = update.effective_user.id

    if not await has_basic_access(context.bot, user_id):
        await update.message.reply_text(get_chat_access_denied_message())
        return

    if not cached_coins:
        cached_coins = await get_top_coins()

    keyboard = []
    row = []
    for i, coin in enumerate(cached_coins):
        row.append(InlineKeyboardButton(coin, callback_data=f"coin_{coin}"))
        if (i + 1) % 3 == 0:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    additional_buttons = []
    if is_super_admin(user_id) or is_admin_user(user_id):
        additional_buttons.append([InlineKeyboardButton("🔍 Анализ по тикеру", callback_data="analyze_ticker")])
    if is_super_admin(user_id):
        additional_buttons.append([InlineKeyboardButton("🔄 Обновить список", callback_data="update_list")])
        additional_buttons.append([InlineKeyboardButton("📊 Статистика прокси", callback_data="proxy_stats")])
    
    keyboard.extend(additional_buttons)
    reply_markup = InlineKeyboardMarkup(keyboard)

    message_text = "💰 Выберите монету для анализа:"
    
    try:
        if edit_message and hasattr(update, 'callback_query'):
            await update.callback_query.edit_message_text(
                text=message_text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                text=message_text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
    except Exception as e:
        logger.error(f"Ошибка отображения списка монет: {e}")
        await update.message.reply_text("❌ Ошибка отображения списка. Попробуйте снова.")

async def delete_signal_message(context):
    """Удаляет предыдущее сообщение с сигналом"""
    if 'signal_message_id' in context.user_data:
        try:
            await context.bot.delete_message(
                chat_id=context.user_data['chat_id'],
                message_id=context.user_data['signal_message_id']
            )
        except Exception:
            pass
        finally:
            del context.user_data['signal_message_id']

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий на inline-кнопки"""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    if not await has_basic_access(context.bot, user_id):
        await query.message.reply_text(get_chat_access_denied_message())
        return

    if data == "update_list":
        if not is_super_admin(user_id):
            await query.message.reply_text(get_access_denied_message())
            return

        await query.message.edit_text("🔄 Обновление списка монет...")
        try:
            new_cached_coins = await update_coins_cache()
            if new_cached_coins:
                global cached_coins
                cached_coins = new_cached_coins
                await query.message.edit_text("✅ Список обновлен!")
                await show_coins_list(update, context, edit_message=False)
            else:
                await query.message.edit_text("⚠️ API временно недоступен. Используем кэшированные данные.")
                await show_coins_list(update, context, edit_message=False)
        except Exception as e:
            logger.error(f"Ошибка обновления списка: {e}")
            await query.message.edit_text("❌ Ошибка обновления списка. Попробуйте позже.")
            await show_coins_list(update, context, edit_message=False)

    elif data == "proxy_stats":
        if not can_view_proxy_stats(user_id):
            await query.message.reply_text(get_access_denied_message())
            return

        from config import get_proxy_stats
        from admin_users import format_proxy_stats_message
        
        stats = get_proxy_stats()
        message = format_proxy_stats_message(stats)
        
        await query.message.edit_text(message, parse_mode='Markdown')

    elif data == "analyze_ticker":
        if not is_super_admin(user_id) and not is_admin_user(user_id):
            await query.message.reply_text(get_access_denied_message())
            return

        await query.message.edit_text("🔍 Введите тикер монеты для анализа (например: BTC, ETH, ADA):")
        context.user_data['waiting_for_ticker'] = True
        context.user_data['chat_id'] = query.message.chat_id

    elif data.startswith("coin_"):
        ticker = data.replace("coin_", "")
        
        # Удаляем предыдущий сигнал если есть
        await delete_signal_message(context)
        
        context.user_data['chat_id'] = query.message.chat_id
        progress_msg = await query.message.edit_text("🔄 Анализирую...")

        try:
            signal = await analyze_ticker(ticker, update, progress_msg.message_id, context.bot)
            
            # Удаляем сообщение с прогрессом
            await context.bot.delete_message(
                chat_id=query.message.chat_id,
                message_id=progress_msg.message_id
            )

            # Отправляем новый сигнал
            signal_msg = await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=signal,
                parse_mode='Markdown'
            )
            
            context.user_data['signal_message_id'] = signal_msg.message_id

        except Exception as e:
            logger.error(f"Ошибка анализа {ticker}: {e}")

            # Удаляем сообщение с прогрессом
            await context.bot.delete_message(
                chat_id=query.message.chat_id,
                message_id=progress_msg.message_id
            )

            # Отправляем сообщение об ошибке
            error_msg = await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"❌ Ошибка при анализе {ticker.upper()}. Попробуйте позже.",
                parse_mode='Markdown'
            )
            context.user_data['signal_message_id'] = error_msg.message_id

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений"""
    global cached_coins

    # Проверяем, что есть сообщение
    if not update.message:
        return

    # Проверяем, что это личное сообщение, а не группа/канал
    if update.message.chat.type != 'private':
        # В группах/каналах бот не отвечает
        return

    text = update.message.text
    user_id = update.message.from_user.id

    # Проверяем базовый доступ: либо админ, либо участник чата
    if not is_admin_user(user_id) and not await is_chat_member(context.bot, user_id):
        await update.message.reply_text(get_chat_access_denied_message())
        return

    if text == "🔄 Обновить список":
        if not is_super_admin(user_id):
            await update.message.reply_text(get_access_denied_message())
            return

        status_msg = await update.message.reply_text("🔄 Обновление списка монет...")

        try:
            new_cached_coins = await update_coins_cache()
            if new_cached_coins:
                cached_coins = new_cached_coins
                await status_msg.edit_text("✅ Список обновлен!")
                # Показываем обновленный список
                await show_coins_list(update, context, edit_message=False)
            else:
                await status_msg.edit_text("⚠️ API временно недоступен. Используем кэшированные данные.")
                # Показываем текущий список
                await show_coins_list(update, context, edit_message=False)
        except Exception as e:
            logger.error(f"Ошибка обновления списка: {e}")
            await status_msg.edit_text("❌ Слишком частые обновления или API недоступен. Попробуйте позже (минимум через 10 минут).")
            # Показываем текущий список даже при ошибке
            await show_coins_list(update, context, edit_message=False)

    elif text == "📊 Статистика прокси":
        if not can_view_proxy_stats(user_id):
            await update.message.reply_text(get_access_denied_message())
            return

        from config import get_proxy_stats
        from admin_users import format_proxy_stats_message
        
        stats = get_proxy_stats()
        message = format_proxy_stats_message(stats)
        
        await update.message.reply_text(message, parse_mode='Markdown')

    elif text == "📋 Инструкция":
        help_text = f"""
📋 **Инструкция по использованию:**

🎯 **Как пользоваться:**
• Выберите любую монету из топ-12 криптовалют
• Получите мгновенный торговый сигнал
• "💰 К списку монет" - вернуться к выбору активов
{"• 🔍 Анализ по тикеру - расширенные возможности анализа" if is_super_admin(user_id) or is_admin_user(user_id) else ""}

🤖 **Технологии нового поколения:**
• Нейронные сети глубокого обучения
• Многофакторный ИИ-анализ в реальном времени
• Алгоритмы машинного обучения
• Квантовые методы обработки данных

📊 **Что вы получаете:**
• Точки входа с высокой вероятностью успеха
• Оптимальные уровни стоп-лосс и тейк-профит
• Соотношение риск/прибыль не менее 1:2
• Long/Short сигналы для любого рынка

⚡ **Преимущества:**
• Анализ за секунды вместо часов
• Исключение человеческих эмоций
• Постоянное самообучение системы
• Адаптация к изменениям рынка

⚠️ **Дисклеймер:**
Сигналы носят информационный характер. Управляйте рисками и принимайте взвешенные решения.
        """
        await update.message.reply_text(help_text, parse_mode='Markdown')

    elif text == "🔍 Анализ по тикеру":
        if not is_super_admin(user_id) and not is_admin_user(user_id):
            await update.message.reply_text(get_access_denied_message())
            return

        await update.message.reply_text(
            "🔍 Введите тикер монеты для анализа (например: BTC, ETH, ADA):")
        context.user_data['waiting_for_ticker'] = True
        context.user_data['chat_id'] = update.message.chat_id

    elif context.user_data.get('waiting_for_ticker'):
        if not is_super_admin(user_id) and not is_admin_user(user_id) and not await is_chat_member(context.bot, user_id):
            await update.message.reply_text(get_access_denied_message())
            context.user_data['waiting_for_ticker'] = False
            return

        # Удаляем предыдущий сигнал если есть
        await delete_signal_message(context)

        ticker = update.message.text.upper().strip()
        context.user_data['waiting_for_ticker'] = False

        progress_msg = await update.message.reply_text("🔄 Анализирую...")

        # Создаем временный объект для analyze_ticker
        class TempUpdate:
            def __init__(self, message):
                self.message = message

        temp_update = TempUpdate(update.message)

        try:
            signal = await analyze_ticker(ticker, temp_update, progress_msg.message_id, context.bot)

            # Удаляем сообщение с прогрессом
            await context.bot.delete_message(
                chat_id=update.message.chat_id,
                message_id=progress_msg.message_id)

            # Отправляем новый сигнал
            signal_msg = await update.message.reply_text(signal, parse_mode='Markdown')
            context.user_data['signal_message_id'] = signal_msg.message_id

        except Exception as e:
            logger.error(f"Ошибка анализа {ticker}: {e}")

            # Удаляем сообщение с прогрессом
            await context.bot.delete_message(
                chat_id=update.message.chat_id,
                message_id=progress_msg.message_id)

            # Отправляем сообщение об ошибке
            error_msg = await update.message.reply_text(
                f"❌ Ошибка при анализе {ticker}. Проверьте правильность тикера.",
                parse_mode='Markdown')
            context.user_data['signal_message_id'] = error_msg.message_id

@app.post("/webhook")
async def webhook(request: Request):
    """Обработчик webhook для Telegram"""
    global telegram_app
    if telegram_app:
        update = Update.de_json(await request.json(), telegram_app.bot)
        await telegram_app.process_update(update)
    return {"status": "ok"}

@app.get("/keepalive")
async def keepalive():
    """Keep-alive endpoint"""
    return {"status": "alive"}

async def main():
    """Основная функция с обработкой перезапуска"""
    max_restarts = 3
    restart_count = 0
    
    while restart_count < max_restarts:
        try:
            from config import IS_RENDER_DEPLOYMENT

            # Определяем и логируем режим запуска
            webhook_configured = bool(WEBHOOK_URL)
            is_production = ENVIRONMENT == 'production' or IS_RENDER_DEPLOYMENT

            if is_production and webhook_configured:
                logger.info("🚀 Режим: Production (Webhook)")
            elif is_production:
                logger.info("🚀 Режим: Production (без webhook - polling)")
            elif webhook_configured:
                logger.info("🔧 Режим: Development (с webhook)")
            else:
                logger.info("🔧 Режим: Development (polling)")

            logger.info(f"🌐 Запуск веб-сервера на {HOST}:{PORT} (попытка {restart_count + 1})")
            
            # Настраиваем uvicorn
            config = uvicorn.Config(
                app,
                host=HOST,
                port=PORT,
                log_level="info",
                access_log=True,
                server_header=False,
                date_header=False
            )
            server = uvicorn.Server(config)
            
            # Запускаем сервер
            await server.serve()
            
            # Если мы здесь, значит сервер завершился корректно
            break
            
        except KeyboardInterrupt:
            logger.info("Приложение завершено пользователем")
            break
        except Exception as e:
            restart_count += 1
            logger.error(f"Критическая ошибка при запуске (попытка {restart_count}): {e}")
            
            if restart_count < max_restarts:
                logger.info(f"Перезапуск через 5 секунд... ({restart_count}/{max_restarts})")
                await asyncio.sleep(5)
                shutdown_event.clear()  # Сбрасываем флаг завершения
            else:
                logger.error("Достигнуто максимальное количество перезапусков")
                sys.exit(1)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Программа завершена")
    except Exception as e:
        logger.error(f"Фатальная ошибка: {e}")
        sys.exit(1)