import os
from dotenv import load_dotenv

load_dotenv()

# API конфигурация
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
COINGECKO_API_URL = "https://api.coingecko.com/api/v3"
BINANCE_API_URL = "https://api.binance.com"
BINANCE_API_FALLBACK = "https://api1.binance.com"  # Альтернативный endpoint

# Настройки деплоя
ENVIRONMENT = os.getenv('ENVIRONMENT', 'development')  # development или production
WEBHOOK_URL = os.getenv('WEBHOOK_URL', '')  # URL для webhook в продакшене
BOT_DISABLED = os.getenv('BOT_DISABLED', 'false').lower() == 'true'  # Полное отключение бота
# Порт для веб-сервера - используем порт из окружения или fallback
PORT = int(os.getenv('PORT', 10000))  # Render использует 10000 по умолчанию
HOST = '0.0.0.0'  # Хост для веб-сервера

# Логируем какой порт используется
print(f"   Настроенный порт: {PORT}")

# Проверка на Render deployment
IS_RENDER_DEPLOYMENT = os.getenv('RENDER') == 'true'

# Логирование конфигурации при импорте
print(f"🔧 Конфигурация:")
print(f"   ENVIRONMENT: {ENVIRONMENT}")
print(f"   IS_RENDER_DEPLOYMENT: {IS_RENDER_DEPLOYMENT}")
print(f"   WEBHOOK_URL: {WEBHOOK_URL}")

# Прокси конфигурация с балансировкой нагрузки
import time
import threading
from collections import defaultdict

_proxy_list = []
_proxy_stats = {}
_proxy_lock = threading.Lock()

# Лимиты Binance API
BINANCE_WEIGHT_LIMIT = 1200  # Лимит веса запросов в минуту
BINANCE_REQUEST_LIMIT = 10   # Лимит запросов в секунду
SAFE_MARGIN = 0.8  # 80% от лимита для безопасности

class ProxyBalancer:
    def __init__(self):
        self.proxy_requests = defaultdict(list)  # {proxy_index: [timestamp, ...]}
        self.proxy_weights = defaultdict(list)   # {proxy_index: [(timestamp, weight), ...]}
        self.proxy_errors = defaultdict(int)     # {proxy_index: error_count}
        self.last_used = 0  # Для round-robin
        
    def cleanup_old_records(self, proxy_index):
        """Очищает старые записи (старше 1 минуты)"""
        current_time = time.time()
        
        # Очищаем запросы старше 60 секунд
        self.proxy_requests[proxy_index] = [
            ts for ts in self.proxy_requests[proxy_index]
            if current_time - ts < 60
        ]
        
        # Очищаем веса старше 60 секунд
        self.proxy_weights[proxy_index] = [
            (ts, weight) for ts, weight in self.proxy_weights[proxy_index]
            if current_time - ts < 60
        ]
    
    def can_use_proxy(self, proxy_index):
        """Проверяет, можно ли использовать прокси без превышения лимитов"""
        self.cleanup_old_records(proxy_index)
        current_time = time.time()
        
        # Проверяем лимит запросов в секунду
        recent_requests = [
            ts for ts in self.proxy_requests[proxy_index]
            if current_time - ts < 1
        ]
        if len(recent_requests) >= BINANCE_REQUEST_LIMIT * SAFE_MARGIN:
            return False
        
        # Проверяем лимит веса в минуту
        current_weight = sum(
            weight for ts, weight in self.proxy_weights[proxy_index]
            if current_time - ts < 60
        )
        if current_weight >= BINANCE_WEIGHT_LIMIT * SAFE_MARGIN:
            return False
        
        # Проверяем количество ошибок
        if self.proxy_errors[proxy_index] > 3:
            return False
            
        return True
    
    def get_best_proxy(self):
        """Возвращает лучший доступный прокси с учетом нагрузки"""
        if not _proxy_list:
            return None
            
        available_proxies = []
        
        # Находим доступные прокси
        for i in range(len(_proxy_list)):
            if self.can_use_proxy(i):
                # Вычисляем загруженность прокси
                load_score = len(self.proxy_requests[i]) + len(self.proxy_weights[i])
                available_proxies.append((i, load_score))
        
        if not available_proxies:
            # Если нет доступных, сбрасываем ошибки и пробуем снова
            self.proxy_errors.clear()
            for i in range(len(_proxy_list)):
                if self.can_use_proxy(i):
                    available_proxies.append((i, 0))
        
        if not available_proxies:
            # Round-robin если все прокси перегружены
            self.last_used = (self.last_used + 1) % len(_proxy_list)
            return self.last_used
        
        # Выбираем прокси с наименьшей нагрузкой
        available_proxies.sort(key=lambda x: x[1])
        return available_proxies[0][0]
    
    def record_request(self, proxy_index, weight=1):
        """Записывает использование прокси"""
        current_time = time.time()
        self.proxy_requests[proxy_index].append(current_time)
        self.proxy_weights[proxy_index].append((current_time, weight))
    
    def record_error(self, proxy_index):
        """Записывает ошибку для прокси"""
        self.proxy_errors[proxy_index] += 1
        print(f"⚠️ Ошибка для прокси #{proxy_index + 1}, всего ошибок: {self.proxy_errors[proxy_index]}")
    
    def reset_errors(self, proxy_index):
        """Сбрасывает счетчик ошибок для прокси"""
        self.proxy_errors[proxy_index] = 0

# Глобальный балансировщик
_proxy_balancer = ProxyBalancer()

def load_proxy_list():
    """Загружает список прокси из переменных окружения с проверкой актуальности"""
    global _proxy_list, _proxy_stats, _proxy_balancer
    
    # Сохраняем старые прокси для сравнения
    old_proxy_list = _proxy_list.copy()
    
    _proxy_list = []
    _proxy_stats = {}
    
    for i in range(1, 11):  # Поддержка до 10 прокси
        proxy_key = f'proxy{i}'
        proxy_value = os.getenv(proxy_key)
        if proxy_value:
            parts = proxy_value.split(':')
            
            if len(parts) == 2:
                # Обычный прокси: IP:PORT
                ip, port = parts
                proxy_url = f"http://{ip}:{port}"
                _proxy_list.append(proxy_url)
                _proxy_stats[len(_proxy_list) - 1] = {
                    'url': proxy_url,
                    'requests': 0,
                    'errors': 0,
                    'last_used': None
                }
                print(f"Загружен обычный прокси {proxy_key}: {ip}:{port}")
                
            elif len(parts) == 4:
                # Приватный прокси: IP:PORT:USERNAME:PASSWORD
                ip, port, username, password = parts
                proxy_url = f"http://{username}:{password}@{ip}:{port}"
                _proxy_list.append(proxy_url)
                _proxy_stats[len(_proxy_list) - 1] = {
                    'url': proxy_url,
                    'requests': 0,
                    'errors': 0,
                    'last_used': None
                }
                print(f"Загружен приватный прокси {proxy_key}: {username}:***@{ip}:{port}")
                
            else:
                print(f"Неверный формат прокси {proxy_key}: {proxy_value}")
                print(f"Поддерживаемые форматы: IP:PORT или IP:PORT:USERNAME:PASSWORD")
    
    # Очищаем статистику для удаленных прокси
    if old_proxy_list and old_proxy_list != _proxy_list:
        print(f"🔄 Обнаружены изменения в списке прокси")
        print(f"   Было: {len(old_proxy_list)} прокси")
        print(f"   Стало: {len(_proxy_list)} прокси")
        
        # Сбрасываем балансировщик для новой конфигурации
        _proxy_balancer = ProxyBalancer()
        print("   Статистика балансировщика сброшена")
    
    if _proxy_list:
        print(f"✅ Загружено {len(_proxy_list)} прокси с балансировкой нагрузки")
        print(f"🔒 Лимиты безопасности: {int(BINANCE_REQUEST_LIMIT * SAFE_MARGIN)} req/sec, {int(BINANCE_WEIGHT_LIMIT * SAFE_MARGIN)} weight/min")
    else:
        print("⚠️ Прокси не настроены - все запросы будут без прокси")
        # Сбрасываем балансировщик если прокси нет
        _proxy_balancer = ProxyBalancer()

def get_current_proxy():
    """Возвращает оптимальный прокси с учетом балансировки нагрузки"""
    with _proxy_lock:
        if not _proxy_list:
            load_proxy_list()
        
        if not _proxy_list:
            return None
        
        proxy_index = _proxy_balancer.get_best_proxy()
        if proxy_index is not None and proxy_index < len(_proxy_list):
            # Обновляем статистику
            _proxy_stats[proxy_index]['last_used'] = time.time()
            return _proxy_list[proxy_index]
        
        # Если индекс некорректный, перезагружаем список
        print("⚠️ Некорректный индекс прокси, перезагружаем список...")
        load_proxy_list()
        return None

def record_proxy_usage(proxy_url, weight=1, error=False):
    """Записывает использование прокси для статистики"""
    with _proxy_lock:
        if not _proxy_list:
            return
            
        # Находим индекс прокси
        proxy_index = None
        for i, p_url in enumerate(_proxy_list):
            if p_url == proxy_url:
                proxy_index = i
                break
        
        if proxy_index is not None:
            if error:
                _proxy_balancer.record_error(proxy_index)
                _proxy_stats[proxy_index]['errors'] += 1
            else:
                _proxy_balancer.record_request(proxy_index, weight)
                _proxy_stats[proxy_index]['requests'] += 1
                _proxy_balancer.reset_errors(proxy_index)  # Сбрасываем ошибки при успешном запросе

def rotate_proxy():
    """Принудительно переключается на следующий доступный прокси"""
    with _proxy_lock:
        if not _proxy_list:
            load_proxy_list()
        
        if _proxy_list:
            # Находим следующий доступный прокси
            for _ in range(len(_proxy_list)):
                proxy_index = _proxy_balancer.get_best_proxy()
                if proxy_index is not None:
                    current_proxy = _proxy_list[proxy_index]
                    print(f"🔄 Переключился на оптимальный прокси #{proxy_index + 1}")
                    return True
            
            print("⚠️ Нет доступных прокси для ротации")
            return False
        
        return False

def get_proxy_stats():
    """Возвращает детальную статистику прокси с балансировкой нагрузки"""
    global _proxy_list, _proxy_stats, _proxy_balancer
    
    if not _proxy_list:
        load_proxy_list()
    
    detailed_stats = []
    current_time = time.time()
    
    for i, proxy_url in enumerate(_proxy_list):
        # Получаем статистику балансировщика
        _proxy_balancer.cleanup_old_records(i)
        
        # Запросы в последнюю минуту
        recent_requests = len(_proxy_balancer.proxy_requests[i])
        
        # Вес в последнюю минуту
        current_weight = sum(
            weight for ts, weight in _proxy_balancer.proxy_weights[i]
            if current_time - ts < 60
        )
        
        # Доступность
        can_use = _proxy_balancer.can_use_proxy(i)
        
        # Загруженность (в процентах)
        request_load = min(100, (recent_requests / (BINANCE_REQUEST_LIMIT * SAFE_MARGIN)) * 100)
        weight_load = min(100, (current_weight / (BINANCE_WEIGHT_LIMIT * SAFE_MARGIN)) * 100)
        
        proxy_stat = {
            'index': i,
            'url': proxy_url,
            'available': can_use,
            'requests_last_minute': recent_requests,
            'weight_last_minute': int(current_weight),
            'total_requests': _proxy_stats.get(i, {}).get('requests', 0),
            'total_errors': _proxy_balancer.proxy_errors[i],
            'request_load_percent': int(request_load),
            'weight_load_percent': int(weight_load),
            'last_used': _proxy_stats.get(i, {}).get('last_used')
        }
        detailed_stats.append(proxy_stat)
    
    return {
        'total': len(_proxy_list),
        'available_count': sum(1 for stat in detailed_stats if stat['available']),
        'current_proxy': get_current_proxy(),
        'limits': {
            'requests_per_second': int(BINANCE_REQUEST_LIMIT * SAFE_MARGIN),
            'weight_per_minute': int(BINANCE_WEIGHT_LIMIT * SAFE_MARGIN)
        },
        'detailed_stats': detailed_stats
    }

# Исключаемые категории монет
EXCLUDED_COINS = {
    # Stablecoins
    'USDT', 'USDC', 'BUSD', 'DAI', 'TUSD', 'USDP', 'GUSD', 'USDD', 'FDUSD', 'USDS', 'FRAX', 'LUSD', 'MIM', 'USTC',
    # Exchange tokens  
    'BNB', 'CRO', 'FTT', 'HT', 'KCS', 'LEO', 'OKB', 'GT',
    # Wrapped tokens
    'WBTC', 'WETH', 'WBNB'
}

# Настройки кэширования
CACHE_FILE = 'top_coins_cache.json'
CACHE_HOURS = 6  # Автообновление каждые 6 часов
