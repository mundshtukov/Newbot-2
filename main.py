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
    try:
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
            logger.warning(f"🚫 Обнаружены другие экземпляры main.py: {python_processes}")
            logger.warning("Завершаю работу во избежание конфликтов")
            yield
            return
    except Exception as e:
        logger.warning(f"Не удалось проверить процессы: {e}")

    # Создаем Telegram Application с правильными настройками
    try:
        # Используем более простой подход к созданию Application
        builder = Application.builder()
        builder.token(TELEGRAM_BOT_TOKEN)
        
        # Отключаем автоматические механизмы для лучшего контроля
        builder.concurrent_updates(False)
        builder.read_timeout(30)
        builder.write_timeout(30)
        builder.connect_timeout(30)
        builder.pool_timeout(30)
        
        telegram_app = builder.build()
        logger.info("✅ Telegram Application создан успешно")
        
    except Exception as e:
        logger.error(f"❌ Ошибка создания Telegram Application: {e}")
        # Пробуем самый простой способ создания
        try:
            telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
            logger.info("✅ Telegram Application создан успешно (простой способ)")
        except Exception as e2:
            logger.error(f"❌ Критическая ошибка создания бота: {e2}")
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

    # Используем fallback монеты для быстрого старта
    cached_coins = [
        'BTC', 'ETH', 'ADA', 'SOL', 'XRP', 'DOGE', 'AVAX', 'DOT', 'MATIC',
        'SHIB', 'LTC', 'ATOM'
    ]
    logger.info(f"Загружено {len(cached_coins)} монет (fallback)")

    # Инициализация бота
    try:
        await telegram_app.initialize()
        logger.info("✅ Telegram Application инициализирован")
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации Telegram Application: {e}")
        yield
        return

    # Определяем режим работы
    from config import IS_RENDER_DEPLOYMENT
    use_webhook = bool(WEBHOOK_URL and (ENVIRONMENT == 'production' or IS_RENDER_DEPLOYMENT))

    if use_webhook:
        logger.info("🚀 Продакшн режим (webhook)")
    else:
        logger.info("🔧 Режим разработки (polling)")

    # Запускаем в соответствующем режиме
    try:
        # Убираем вызов start() для webhook режима
        if not use_webhook:
            await telegram_app.start()
            logger.info("✅ Telegram бот запущен в polling режиме")
        else:
            logger.info("✅ Telegram бот готов к работе в webhook режиме")
    except Exception as e:
        logger.error(f"❌ Ошибка запуска Telegram бота: {e}")
        yield
        return

    if use_webhook:
        # Продакшн режим - ТОЛЬКО webhook
        try:
            logger.info("🚀 Настройка webhook режима...")
            
            # Удаляем предыдущий webhook
            try:
                await telegram_app.bot.delete_webhook(drop_pending_updates=True)
                await asyncio.sleep(2)
                logger.info("✅ Предыдущий webhook удален")
            except Exception as e:
                logger.warning(f"Предупреждение при удалении webhook: {e}")
            
            # Устанавливаем новый webhook
            webhook_url = f"{WEBHOOK_URL}/webhook"
            result = await telegram_app.bot.set_webhook(
                url=webhook_url,
                drop_pending_updates=True,
                max_connections=40,
                allowed_updates=["message", "callback_query"]
            )
            
            if result:
                logger.info(f"✅ Webhook установлен: {webhook_url}")
                
                # Проверяем webhook
                webhook_info = await telegram_app.bot.get_webhook_info()
                logger.info(f"📋 Webhook info: {webhook_info.url}, pending: {webhook_info.pending_update_count}")
            else:
                logger.error("❌ Не удалось установить webhook")
                
        except Exception as e:
            logger.error(f"❌ Ошибка настройки webhook: {e}")
    else:
        # Режим разработки - ТОЛЬКО polling
        try:
            logger.info("🔧 Настройка polling режима...")
            await telegram_app.bot.delete_webhook(drop_pending_updates=True)
            await asyncio.sleep(2)
            logger.info("✅ Webhook удален, готов к polling")
            
            # Запускаем polling в отдельной задаче
            async def run_polling():
                try:
                    logger.info("🔄 Запуск polling...")
                    await telegram_app.run_polling(
                        poll_interval=1.0,
                        timeout=10,
                        bootstrap_retries=-1,
                        read_timeout=30,
                        write_timeout=30,
                        connect_timeout=30,
                        pool_timeout=30,
                        drop_pending_updates=True,
                        close_loop=False,
                        stop_signals=None
                    )
                except Exception as e:
                    logger.error(f"❌ Ошибка polling: {e}")
            
            # Создаем задачу для polling
            polling_task = asyncio.create_task(run_polling())
            
        except Exception as e:
            logger.error(f"❌ Ошибка настройки polling: {e}")

    # Фоновые задачи
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
            else:
                logger.warning("⚠️ Не удалось загрузить актуальные монеты, используем fallback")
                cached_coins = await get_top_coins()
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки актуальных монет: {e}")
            try:
                cached_coins = await get_top_coins()
            except Exception:
                pass

    # Автоматическое обновление каждые 6 часов
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

    # Keep-alive механизм
    async def keep_alive():
        """Внутренний keep-alive механизм"""
        while not shutdown_event.is_set():
            try:
                await asyncio.sleep(180)  # Каждые 3 минуты
                if not shutdown_event.is_set():
                    logger.info("🫀 Keep-alive ping - бот активен")
                    if telegram_app:
                        try:
                            # Простая проверка состояния бота
                            me = await telegram_app.bot.get_me()
                            logger.info(f"📱 Telegram бот @{me.username} в рабочем состоянии")
                        except Exception as e:
                            logger.warning(f"⚠️ Проблема с состоянием бота: {e}")
            except Exception as e:
                logger.error(f"❌ Ошибка keep-alive: {e}")

    # Запускаем фоновые задачи
    asyncio.create_task(load_coins_after_startup())
    asyncio.create_task(auto_update_coins())
    asyncio.create_task(keep_alive())

    # Yield для работы приложения
    try:
        yield
    finally:
        # Shutdown
        logger.info("Завершение работы приложения...")
        shutdown_event.set()
        if telegram_app:
            try:
                await telegram_app.stop()
                await telegram_app.shutdown()
            except Exception as e:
                logger.error(f"Ошибка при остановке бота: {e}")
        logger.info("Приложение завершено")

# FastAPI приложение - ДОЛЖНО БЫТЬ ПОСЛЕ ОПРЕДЕЛЕНИЯ lifespan
app = FastAPI(lifespan=lifespan)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    try:
        user_id = update.effective_user.id
        logger.info(f"👤 Команда /start от пользователя {user_id}")
        
        if not await has_basic_access(context.bot, user_id):
            await update.message.reply_text(get_chat_access_denied_message())
            return

        keyboard = [
            [KeyboardButton("💰 К списку монет"), KeyboardButton("📋 Инструкция")],
        ]

        if is_super_admin(user_id):
            keyboard.append([KeyboardButton("🔄 Обновить список"), KeyboardButton("📊 Статистика прокси")])
        elif is_admin_user(user_id):
            keyboard.append([KeyboardButton("🔍 Анализ по тикеру")])

        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        
        welcome_message = """
🚀 Добро пожаловать в Crypto Signals Bot!

📊 Получайте мгновенные торговые сигналы на основе ИИ-анализа в реальном времени.

🎯 Выберите действие ниже:
"""
        await update.message.reply_text(welcome_message, reply_markup=reply_markup, parse_mode='Markdown')
        logger.info(f"✅ Отправлен welcome message пользователю {user_id}")
        
    except Exception as e:
        logger.error(f"❌ Ошибка в обработчике /start: {e}")
        try:
            await update.message.reply_text("❌ Произошла ошибка. Попробуйте снова.")
        except:
            pass

async def show_coins_list(update, context, edit_message=True):
    """Показывает список топ-монет"""
    try:
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
        logger.error(f"❌ Ошибка отображения списка монет: {e}")
        try:
            await update.message.reply_text("❌ Ошибка отображения списка. Попробуйте снова.")
        except:
            pass

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
    try:
        query = update.callback_query
        await query.answer()

        user_id = query.from_user.id
        data = query.data
        
        logger.info(f"👤 Callback {data} от пользователя {user_id}")

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
                try:
                    await context.bot.delete_message(
                        chat_id=query.message.chat_id,
                        message_id=progress_msg.message_id
                    )
                except:
                    pass

                # Отправляем сообщение об ошибке
                try:
                    error_msg = await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text=f"❌ Ошибка при анализе {ticker.upper()}. Попробуйте позже.",
                        parse_mode='Markdown'
                    )
                    context.user_data['signal_message_id'] = error_msg.message_id
                except:
                    pass
                    
    except Exception as e:
        logger.error(f"❌ Ошибка в handle_message: {e}")

@app.post("/webhook")
async def webhook(request: Request):
    """Обработчик webhook для Telegram"""
    try:
        global telegram_app
        if telegram_app:
            json_data = await request.json()
            logger.info(f"📥 Получен webhook: {json_data}")
            
            update = Update.de_json(json_data, telegram_app.bot)
            await telegram_app.process_update(update)
            
            return {"status": "ok"}
        else:
            logger.error("❌ Telegram app не инициализирован")
            return {"status": "error", "message": "Bot not initialized"}
    except Exception as e:
        logger.error(f"❌ Ошибка обработки webhook: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/keepalive")
async def keepalive():
    """Keep-alive endpoint"""
    return {"status": "alive", "timestamp": datetime.now().isoformat()}

@app.get("/")
async def root():
    """Root endpoint"""
    return {"status": "Crypto Signals Bot is running", "timestamp": datetime.now().isoformat()}

@app.head("/")
async def root_head():
    """HEAD endpoint для health checks"""
    return {"status": "ok"}

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
            elif webhook_configured:
                logger.info("🔧 Режим: Development (с webhook)")
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
                date_header=False,
                timeout_keep_alive=30,
                timeout_graceful_shutdown=10
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
        sys.exit(1):
        logger.error(f"❌ Ошибка в button_callback: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений"""
    try:
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
        
        logger.info(f"👤 Сообщение '{text}' от пользователя {user_id}")

        # Проверяем базовый доступ: либо админ, либо участник чата
        if not is_admin_user(user_id) and not await is_chat_member(context.bot, user_id):
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

                try:
                    await context.bot.delete_message(
                        chat_id=update.message.chat_id,
                        message_id=progress_msg.message_id)
                except:
                    pass

                try:
                    error_msg = await update.message.reply_text(
                        f"❌ Ошибка при анализе {ticker}. Проверьте правильность тикера.",
                        parse_mode='Markdown')
                    context.user_data['signal_message_id'] = error_msg.message_id
                except:
                    pass
                    
    except Exception as e
