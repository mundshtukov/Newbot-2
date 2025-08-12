import aiohttp
import json
import asyncio
import logging
from datetime import datetime, timedelta
from config import COINGECKO_API_URL, BINANCE_API_URL, EXCLUDED_COINS, CACHE_FILE, CACHE_HOURS, get_current_proxy, rotate_proxy
import os

logger = logging.getLogger(__name__)


async def make_request_with_proxy(url, params=None, timeout=10, weight=1):
    """Делает запрос с использованием балансированных прокси"""
    from config import record_proxy_usage, load_proxy_list
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json'
    }

    proxy = get_current_proxy()
    
    # Если прокси нет, делаем запрос напрямую
    if not proxy:
        logger.info("🌐 Прокси не настроены, делаем прямой запрос")
        try:
            async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.get(url, params=params, headers=headers) as response:
                    return {
                        'status': response.status,
                        'data': await response.json() if response.status == 200 else None
                    }
        except Exception as e:
            logger.error(f"❌ Ошибка прямого запроса: {e}")
            raise e
    
    try:
        connector = aiohttp.TCPConnector()
        logger.info(f"🌐 Используем балансированный прокси: {proxy}")

        async with aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(url,
                                   params=params,
                                   headers=headers,
                                   proxy=proxy) as response:
                
                # Записываем успешное использование
                record_proxy_usage(proxy, weight=weight, error=False)
                
                result = {
                    'status': response.status,
                    'data': await response.json() if response.status == 200 else None
                }
                
                if response.status == 200:
                    logger.info(f"✅ Успешный запрос через прокси (weight: {weight})")
                
                return result

    except Exception as e:
        logger.error(f"❌ Ошибка запроса с прокси {proxy}: {e}")
        
        # Записываем ошибку
        record_proxy_usage(proxy, weight=weight, error=True)
        
        # Перезагружаем список прокси на случай изменений
        load_proxy_list()
        
        # Пробуем другой прокси
        if rotate_proxy():
            logger.info("🔄 Пробуем запрос с другим прокси...")
            new_proxy = get_current_proxy()
            if new_proxy and new_proxy != proxy:
                try:
                    async with aiohttp.ClientSession(
                            connector=aiohttp.TCPConnector(),
                            timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                        async with session.get(url,
                                               params=params,
                                               headers=headers,
                                               proxy=new_proxy) as response:
                            
                            record_proxy_usage(new_proxy, weight=weight, error=False)
                            
                            return {
                                'status': response.status,
                                'data': await response.json() if response.status == 200 else None
                            }
                except Exception as e2:
                    record_proxy_usage(new_proxy, weight=weight, error=True)
                    logger.error(f"❌ Резервный запрос тоже неудачен: {e2}")
        
        # Если все прокси недоступны, пробуем без прокси
        logger.warning("⚠️ Все прокси недоступны, пробуем прямое подключение...")
        try:
            async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.get(url, params=params, headers=headers) as response:
                    logger.info("✅ Fallback: успешный прямой запрос")
                    return {
                        'status': response.status,
                        'data': await response.json() if response.status == 200 else None
                    }
        except Exception as e3:
            logger.error(f"❌ Fallback тоже неудачен: {e3}")
            raise e


async def validate_ticker_on_binance(ticker):
    """Проверяет, существует ли торговая пара на Binance с балансировкой прокси"""
    try:
        url = f"{BINANCE_API_URL}/api/v3/ticker/24hr"
        params = {'symbol': f"{ticker}USDT"}

        # Используем балансированный запрос с весом 1
        try:
            result = await make_request_with_proxy(url, params, timeout=10, weight=1)

            if result['status'] == 451:
                logger.warning(
                    f"Binance API заблокирован (451) для {ticker}, используем белый список"
                )
                return ticker.upper() in [
                    'BTC', 'ETH', 'BNB', 'ADA', 'SOL', 'XRP', 'DOGE', 'AVAX',
                    'DOT', 'MATIC', 'SHIB', 'LTC', 'ATOM', 'UNI', 'LINK', 'NEAR'
                ]

            if result['status'] == 200 and result['data']:
                logger.info(f"✅ Тикер {ticker} валиден на Binance")
                return 'symbol' in result['data']
            else:
                logger.warning(f"⚠️ Код ответа {result['status']} для {ticker}")
                return ticker.upper() in [
                    'BTC', 'ETH', 'BNB', 'ADA', 'SOL', 'XRP', 'DOGE', 'AVAX',
                    'DOT', 'MATIC', 'SHIB', 'LTC', 'NEAR'
                ]

        except Exception as e:
            logger.warning(f"⚠️ Ошибка запроса для {ticker}: {e}")
            
            # Fallback без прокси только в критических случаях
            try:
                headers = {'User-Agent': 'Mozilla/5.0 (compatible; CryptoBot/1.0)'}
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
                    async with session.get(url, params=params, headers=headers) as response:
                        if response.status == 200:
                            data = await response.json()
                            logger.info(f"✅ Fallback валидация успешна для {ticker}")
                            return 'symbol' in data
            except Exception as fallback_error:
                logger.warning(f"⚠️ Fallback тоже неудачен для {ticker}: {fallback_error}")

            # Используем расширенный белый список
            return ticker.upper() in [
                'BTC', 'ETH', 'BNB', 'ADA', 'SOL', 'XRP', 'DOGE', 'AVAX',
                'DOT', 'MATIC', 'SHIB', 'LTC', 'ATOM', 'UNI', 'LINK', 'NEAR'
            ]

    except Exception as e:
        logger.warning(f"⚠️ Общая ошибка валидации {ticker}: {e}")
        # Расширенный fallback список
        return ticker.upper() in [
            'BTC', 'ETH', 'BNB', 'ADA', 'SOL', 'XRP', 'DOGE', 'AVAX', 'DOT',
            'MATIC', 'SHIB', 'LTC', 'ATOM', 'UNI', 'LINK', 'NEAR'
        ]


async def fetch_top_coins_from_api():
    """Получает топ-50 монет из CoinGecko API"""
    try:
        url = f"{COINGECKO_API_URL}/coins/markets"
        params = {
            'vs_currency': 'usd',
            'order': 'market_cap_desc',
            'per_page': '50',
            'page': '1',
            'sparkline': 'false'
        }

        logger.info(f"Запрос к CoinGecko: {url} с параметрами: {params}")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(
                total=15)) as session:
            async with session.get(url, params=params) as response:
                logger.info(f"Ответ от CoinGecko: HTTP {response.status}")
                if response.status == 200:
                    coins = await response.json()
                    logger.info(f"Получено {len(coins)} монет от CoinGecko")
                    return coins
                else:
                    response_text = await response.text()
                    logger.error(
                        f"Ошибка CoinGecko API: HTTP {response.status}, текст: {response_text}"
                    )
                    raise aiohttp.ClientError(f"HTTP {response.status}")
    except Exception as e:
        logger.error(f"Ошибка получения данных из CoinGecko: {e}")
        return None


async def filter_and_validate_coins(coins):
    """Фильтрует и валидирует монеты"""
    valid_coins = []

    for coin in coins:
        symbol = coin['symbol'].upper()
        name = coin.get('name', '').upper()

        # Проверяем исключения по символу (точное совпадение)
        if symbol in EXCLUDED_COINS:
            logger.info(f"Исключена монета: {symbol} (в списке исключений)")
            continue

        # Дополнительная проверка на стейблкоины по названию
        coin_name = coin.get('name', '').lower()
        if any(stable in coin_name for stable in ['usd coin', 'tether', 'dai', 'true usd', 'binance usd']):
            logger.info(f"Исключена монета: {symbol} (стейблкоин по названию: {coin_name})")
            continue

        # Исключаем wrapped токены
        if symbol.startswith('W') and len(symbol) <= 5:
            logger.info(f"Исключена монета {symbol} - wrapped токен")
            continue

        # Исключаем токены с подозрительными суффиксами
        if symbol.endswith(('USD', 'USDT', 'USDC')):
            logger.info(f"Исключена монета {symbol} - подозрительный суффикс")
            continue

        # Валидируем на Binance
        if await validate_ticker_on_binance(symbol):
            valid_coins.append(symbol)
            logger.info(f"Добавлена валидная монета: {symbol}")

            if len(valid_coins) >= 12:
                break

        # Небольшая задержка между запросами
        await asyncio.sleep(0.1)

    return valid_coins


async def save_cache(coins):
    """Сохраняет кэш монет в файл И переменные окружения"""
    cache_data = {'coins': coins, 'timestamp': datetime.now().isoformat()}

    # Сохраняем в файл (для локальной разработки)
    try:
        with open(CACHE_FILE, 'w') as f:
            json.dump(cache_data, f)
        logger.info(f"Кэш сохранен в файл: {len(coins)} монет")
    except Exception as e:
        logger.error(f"Ошибка сохранения в файл: {e}")

    # Сохраняем в переменные окружения (для Autoscale)
    try:
        # Сохраняем список монет
        coins_str = ','.join(coins)
        os.environ['CACHED_COINS'] = coins_str

        # Сохраняем timestamp
        os.environ['CACHE_TIMESTAMP'] = cache_data['timestamp']

        logger.info(f"✅ Кэш сохранен в переменные окружения: {len(coins)} монет")
        logger.info(f"Монеты: {coins_str}")

    except Exception as e:
        logger.error(f"Ошибка сохранения в переменные окружения: {e}")


async def load_cache():
    """Загружает кэш монет из переменных окружения или файла"""
    # Сначала пытаемся загрузить из переменных окружения
    cached_coins_env = os.environ.get('CACHED_COINS')
    cache_timestamp_env = os.environ.get('CACHE_TIMESTAMP')

    if cached_coins_env and cache_timestamp_env:
        try:
            coins = cached_coins_env.split(',')
            cache_time = datetime.fromisoformat(cache_timestamp_env)
            if datetime.now() - cache_time < timedelta(hours=CACHE_HOURS):
                logger.info(
                    f"Загружен актуальный кэш из переменных окружения: {len(coins)} монет"
                )
                return coins
            else:
                logger.info("Кэш из переменных окружения устарел")
        except Exception as e:
            logger.error(f"Ошибка загрузки кэша из переменных окружения: {e}")

    # Если из переменных окружения не удалось, пытаемся загрузить из файла
    try:
        with open(CACHE_FILE, 'r') as f:
            cache_data = json.load(f)

        # Проверяем возраст кэша
        cache_time = datetime.fromisoformat(cache_data['timestamp'])
        if datetime.now() - cache_time < timedelta(hours=CACHE_HOURS):
            logger.info(
                f"Загружен актуальный кэш из файла: {len(cache_data['coins'])} монет")
            # Сохраняем в переменные окружения для последующего использования
            await save_cache(cache_data['coins'])
            return cache_data['coins']
        else:
            logger.info("Кэш-файл устарел")
            return None
    except FileNotFoundError:
        logger.info("Кэш-файл не найден")
        return None
    except Exception as e:
        logger.error(f"Ошибка загрузки кэша из файла: {e}")
        return None


async def get_top_coins():
    """Получает топ-12 монет (из кэша или API)"""
    # Попытка загрузить из кэша (переменные окружения или файл)
    cached_coins = await load_cache()
    if cached_coins:
        return cached_coins

    # Если кэш не актуален, обновляем
    return await update_coins_cache()


# Глобальная переменная для отслеживания последнего обновления
_last_update_time = None
_update_cooldown_minutes = 10  # Минимальный интервал между обновлениями


async def update_coins_cache():
    """Принудительно обновляет кэш монет с защитой от частых обновлений"""
    global _last_update_time

    now = datetime.now()

    # Проверяем, не было ли недавнее обновление
    if _last_update_time and (now - _last_update_time).total_seconds() < _update_cooldown_minutes * 60:
        logger.info(f"Слишком частый запрос на обновление, последнее обновление было: {_last_update_time}")
        # Если было недавнее обновление, пытаемся вернуть последние сохраненные данные
        return await load_cache()


    logger.info("Обновление кэша монет...")
    _last_update_time = now

    # Получаем данные из API
    coins_data = await fetch_top_coins_from_api()
    if not coins_data:
        # Если API недоступен, попытка загрузить старый кэш
        old_cache = await load_cache()
        if old_cache:
            logger.warning(
                "Используется устаревший кэш из-за недоступности API")
            return old_cache
        else:
            logger.error("Нет доступных данных!")
            return [
                'BTC', 'ETH', 'ADA', 'SOL', 'XRP', 'DOGE', 'AVAX', 'DOT',
                'MATIC', 'SHIB', 'LTC', 'ATOM'
            ]

    # Фильтруем и валидируем
    valid_coins = await filter_and_validate_coins(coins_data)

    if len(valid_coins) < 12:
        logger.warning(f"Найдено только {len(valid_coins)} валидных монет")

    # Сохраняем кэш
    await save_cache(valid_coins)

    return valid_coins