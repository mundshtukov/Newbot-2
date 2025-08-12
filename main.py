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

# Отключаем лишние логи
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNINGവ

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

    # Проверяем наличие токена
    if not TELEGRAM_BOT_TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN не задан")
        yield
        return
    
    logger.info(f"🔑 Токен бота присутствует (длина: {len(TELEGRAM_BOT_TOKEN)})")

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
    try:
        telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        logger.info("✅ Telegram Application создан успешно")
    except Exception as e:
        logger.error(f"❌ Ошибка создания Telegram Application: {e}")
        yield
        return

    # Добавляем обработчики
    try:
        telegram_app.add_handler(CommandHandler("start", start))
        telegram_app.add_handler(CallbackQueryHandler(button_callback))
        telegram_app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        logger.info("✅ Обработчики добавлены успешно")
    except Exception as e:
        logger.error(f"❌ Ошибка добавления обработчиков: {e}")
        yield
        return

    # Инициализируем приложение
    try:
        await telegram_app.initialize()
        logger.info("✅ Telegram Application инициализирован")
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации Telegram Application: {e}")
        yield
        return

    # Настройка webhook или polling
    try:
        if WEBHOOK_URL:
            # Устанавливаем webhook
            webhook_path = f"{WEBHOOK_URL}/webhook"
            logger.info(f"🌐 Устанавливаем webhook: {webhook_path}")
            
            # Увеличиваем timeout для webhook
            await asyncio.wait_for(
                telegram_app.bot.set_webhook(webhook_path), 
                timeout=30
            )
            logger.info(f"✅ Webhook установлен: {webhook_path}")
        else:
            # Запускаем polling в отдельной задаче
            logger.info("🔄 Запуск polling...")
            
            await telegram_app.start()
            
            # Запускаем polling в фоновой задаче
            polling_task = asyncio.create_task(telegram_app.updater.start_polling())
            logger.info("✅ Polling запущен")
            
    except asyncio.TimeoutError:
        logger.error("❌ Timeout при установке webhook")
        yield
        return
    except Exception as e:
        logger.error(f"❌ Ошибка настройки webhook/polling: {e}")
        yield
        return

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
            fresh_coins = await update_coins_cache()
            if fresh_coins and len(fresh_coins) >= 10:
                cached_coins = fresh_coins
                logger.info(f"✅ После перезапуска загружены актуальные монеты: {len(fresh_coins)}")
                logger.info(f"Обновленный список: {', '.join(fresh_coins)}")
            else:
                logger.warning("⚠️ Не удалось загрузить актуальные монеты, используем fallback")
                cached_coins = await get_top_coins()
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки актуальных монет: {e}")
            try:
                cached_coins = await get_top_coins()
            except Exception:
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
                    if telegram_app and telegram_app.running:
                        logger.info("📱 Telegram бот работает")
            except Exception as e:
                logger.error(f"❌ Ошибка в keep-alive: {e}")

    try:
        # Запускаем задачи
        asyncio.create_task(load_coins_after_startup())
        asyncio.create_task(auto_update_coins())
        asyncio.create_task(keep_alive())
        
        yield
        
    finally:
        # Shutdown
        logger.info("Shutdown приложения...")
        try:
            if telegram_app:
                if WEBHOOK_URL:
                    await telegram_app.bot.delete_webhook()
                    logger.info("✅ Webhook удален")
                else:
                    await telegram_app.updater.stop()
                    await telegram_app.stop()
                await telegram_app.shutdown()
                logger.info("✅ Telegram Application завершено")
        except Exception as e:
            logger.error(f"Ошибка при shutdown: {e}")

# FastAPI приложение
app = FastAPI(lifespan=lifespan)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user_id = update.effective_user.id
    
    # Проверяем базовый доступ
    if not await has_basic_access(context.bot, user_id):
        await update.message.reply_text(get_chat_access_denied_message())
        return

    keyboard = [
        [
            KeyboardButton("💰 К списку монет"),
            KeyboardButton("🔍 Анализ по тикеру") if is_super_admin(user_id) or is_admin_user(user_id) else None
        ],
        [KeyboardButton("📋 Инструкция")]
    ]
    
    # Добавляем кнопку статистики прокси для разрешенных пользователей
    if can_view_proxy_stats(user_id):
        keyboard.append([KeyboardButton("📊 Статистика прокси")])
    
    # Добавляем кнопку обновления для супер-админа
    if is_super_admin(user_id):
        keyboard.append([KeyboardButton("🔄 Обновить список")])

    # Фильтруем None из списка кнопок
    keyboard[0] = [btn for btn in keyboard[0] if btn is not None]
    
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        """🤖 Привет! Я твой ИИ-помощник в трейдинге

💡 Уникальная система анализа криптовалют

🎯 Получай готовые торговые сигналы:
• Точки входа с максимальной вероятностью успеха
• Четкие уровни стоп-лосс и тейк-профит
• Обоснование каждого сигнала
• Анализ на основе современных алгоритмов

⚡ Почему именно мы:
✅ Технологии машинного обучения
✅ Анализ множества индикаторов одновременно
✅ Постоянное обновление данных
✅ Простота использования для любого уровня

📈 Готов помочь тебе в трейдинге!

🚀 Выбери любую из топ-12 криптовалют для получения торгового сигнала или изучи инструкцию для максимальной эффективности 👇""",
        parse_mode='Markdown',
        reply_markup=reply_markup
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик callback-запросов от inline-кнопок"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    data = query.data

    if not await has_basic_access(context.bot, user_id):
        await query.message.reply_text(get_chat_access_denied_message())
        return

    if data == "show_coins":
        await show_coins_list(update, context, edit_message=True)
    elif data.startswith("analyze_"):
        ticker = data.split("_")[1]
        
        # Удаляем предыдущий сигнал если есть
        await delete_signal_message(context)
        
        progress_msg = await query.message.reply_text("🔄 Анализирую...")
        
        try:
            signal = await analyze_ticker(ticker, update, progress_msg.message_id, context.bot)
            
            await context.bot.delete_message(
                chat_id=query.message.chat_id,
                message_id=progress_msg.message_id)
            
            signal_msg = await query.message.reply_text(signal, parse_mode='Markdown')
            context.user_data['signal_message_id'] = signal_msg.message_id
            
        except Exception as e:
            logger.error(f"Ошибка анализа {ticker}: {e}")
            await context.bot.delete_message(
                chat_id=query.message.chat_id,
                message_id=progress_msg.message_id)
            error_msg = await query.message.reply_text(
                f"❌ Ошибка при анализе {ticker}. Проверьте правильность тикера.",
                parse_mode='Markdown')
            context.user_data['signal_message_id'] = error_msg.message_id

async def delete_signal_message(context:/ContextTypes.DEFAULT_TYPE):
    """Удаляет предыдущее сообщение с сигналом если оно есть"""
    if 'signal_message_id' in context.user_data:
        try:
            await context.bot.delete_message(
                chat_id=context.user_data['chat_id'],
                message_id=context.user_data['signal_message_id'])
            del context.user_data['signal_message_id']
        except Exception as e:
            logger.error(f"Ошибка удаления сообщения: {e}")

async def show_coins_list(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_message=False):
    """Показывает список топ-монет с inline-кнопками"""
    global cached_coins
    
    user_id = update.effective_user.id
    if not await has_basic_access(context.bot, user_id):
        await update.message.reply_text(get_chat_access_denied_message())
        return

    # Проверяем кэш
    if not cached_coins:
        cached_coins = await get_top_coins()
    
    # Создаем inline-кнопки
    keyboard = []
    for i in range(0, len(cached_coins), 3):
        row = []
        for coin in cached_coins[i:i+3]:
            row.append(InlineKeyboardButton(f"{coin}", callback_data=f"analyze_{coin}"))
        keyboard.append(row)
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message_text = (
        "📈 **Топ-монеты для анализа**\n\n"
        f"Выберите монету для анализа ({len(cached_coins)} доступно):"
    )
    
    try:
        if edit_message and update.callback_query:
            await update.callback_query.message.edit_text(
                message_text,
                parse_mode='Markdown',
                reply_markup=reply_markup)
        else:
            await update.message.reply_text(
                message_text,
                parse_mode='Markdown',
                reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Ошибка отображения списка монет: {e}")
        await update.message.reply_text(
            "❌ Ошибка отображения списка монет. Попробуйте снова.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений"""
    user_id = update.effective_user.id
    text = update.message.text

    if not await has_basic_access(context.bot, user_id):
        await update.message.reply_text(get_chat_access_denied_message())
        return

    if text == "💰 К списку монет":
        await show_coins_list(update, context, edit_message=False)
    
    elif text == "🔄 Обновить список":
        if not is_super_admin(user_id):
            await update.message.reply_text(get_access_denied_message())
            return

        status_msg = await update.message.reply_text("🔄 Обновление списка монет...")

        try:
            new_cached_coins = await update_coins_cache()
            if new_cached_coins:
                cached_coins = new_cached_coins
                await status_msg.edit_text("✅ Список обновлен!")
                await show_coins_list(update, context, edit_message=False)
            else:
                await status_msg.edit_text("⚠️ API временно недоступен. Используем кэшированные данные.")
                await show_coins_list(update, context, edit_message=False)
        except Exception as e:
            logger.error(f"Ошибка обновления списка: {e}")
            await status_msg.edit_text("❌ Слишком частые обновления или API недоступен. Попробуйте позже (минимум через 10 минут).")
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
            await update.message.reply_text(get_chat_access_denied_message())
            context.user_data['waiting_for_ticker'] = False
            return

        # Удаляем предыдущий сигнал если есть
        await delete_signal_message(context)

        ticker = update.message.text.upper().strip()
        context.user_data['waiting_for_ticker'] = False

        progress_msg = await update.message.reply_text("🔄 Анализирую...")

        class TempUpdate:
            def __init__(self, message):
                self.message = message

        temp_update = TempUpdate(update.message)

        try:
            signal = await analyze_ticker(ticker, temp_update, progress_msg.message_id, context.bot)

            await context.bot.delete_message(
                chat_id=update.message.chat_id,
                message_id=progress_msg.message_id)

            signal_msg = await update.message.reply_text(signal, parse_mode='Markdown')
            context.user_data['signal_message_id'] = signal_msg.message_id

        except Exception as e:
            logger.error(f"Ошибка анализа {ticker}: {e}")

            await context.bot.delete_message(
                chat_id=update.message.chat_id,
                message_id=progress_msg.message_id)

            error_msg = await update.message.reply_text(
                f"❌ Ошибка при анализе {ticker}. Проверьте правильность тикера.",
                parse_mode='Markdown')
            context.user_data['signal_message_id'] = error_msg.message_id

@app.post("/webhook")
async def webhook(request: Request):
    """Обработчик webhook для Telegram"""
    global telegram_app
    
    try:
        if telegram_app and telegram_app.bot:
            data = await request.json()
            logger.info(f"Получен webhook: {data}")
            update = Update.de_json(data, telegram_app.bot)
            
            # Создаем новый контекст для каждого обновления
            await telegram_app.process_update(update)
            
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Ошибка обработки webhook: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/keepalive")
async def keepalive():
    """Keep-alive endpoint"""
    return {
        "status": "alive", 
        "timestamp": datetime.now().isoformat(),
        "bot_status": "running" if telegram_app and telegram_app.running else "stopped"
    }

async def main():
    """Основная функция с обработкой перезапуска"""
    max_restarts = 3
    restart_count = 0
    
    while restart_count < max_restarts:
        try:
            from config import IS_RENDER_DEPLOYMENT

            webhook_configured = bool(WEBHOOK_URL)
            is_production = ENVIRONMENT == 'production' or IS_RENDER_DEPLOYMENT

            if is_production and webhook_configured:
                logger.info("🚀 Режим: Production (Webhook)")
            elif is_production:
                logger.info("🚀 Режим: Production (без webhook - polling)")
            else:
                logger.info("🔧 Режим: Development (polling)")

            logger.info(f"🌐 Запуск веб-сервера на {HOST}:{PORT} (попытка {restart_count + 1})")
            
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
            
            await server.serve()
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
                shutdown_event.clear()
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
