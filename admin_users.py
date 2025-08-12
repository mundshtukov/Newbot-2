
import logging
from telegram.error import TelegramError

logger = logging.getLogger(__name__)

# ID чата для базового доступа
CHAT_ID = -1002492484357

# ID супер-админа (полный доступ ко всем функциям)
SUPER_ADMIN_USER_ID = 161007346

# Список ID пользователей с расширенными правами (3 кнопки: список монет, инструкция, анализ по тикеру)
ADMIN_USER_IDS = {
    219539208, 
    1356408500, # Новый админ пользователь
    # 123456789,  # Пример нового пользователя - раскомментируйте и замените на реальный ID
}

# ID пользователя с доступом к статистике прокси
PROXY_STATS_USER_ID = 161007346

def format_proxy_stats_message(stats):
    """Форматирует сообщение со статистикой прокси"""
    if stats['total'] == 0:
        return "🚫 Прокси не настроены"
    
    message = f"""
📊 **Статистика балансировки прокси:**

🔢 Всего прокси: {stats['total']}
✅ Доступных: {stats['available_count']}
⚖️ Текущий режим: Умная балансировка нагрузки

🔒 **Лимиты безопасности:**
• Запросов в секунду: {stats['limits']['requests_per_second']}
• Вес в минуту: {stats['limits']['weight_per_minute']}

📈 **Детальная статистика:**
"""
    
    for stat in stats['detailed_stats']:
        status_icon = "✅" if stat['available'] else "❌"
        proxy_display = stat['url'].replace('http://', '').split('@')[-1] if '@' in stat['url'] else stat['url'].replace('http://', '')
        
        message += f"""
{status_icon} **Прокси #{stat['index'] + 1}:** `{proxy_display}`
   📊 Нагрузка: {stat['request_load_percent']}% | {stat['weight_load_percent']}%
   📈 Запросы: {stat['requests_last_minute']}/мин | Всего: {stat['total_requests']}
   ❌ Ошибки: {stat['total_errors']}
"""
    
    return message

async def is_chat_member(bot, user_id):
    """Проверяет, является ли пользователь участником чата"""
    try:
        member = await bot.get_chat_member(chat_id=CHAT_ID, user_id=user_id)
        # Участник чата если статус: member, administrator, creator
        return member.status in ['member', 'administrator', 'creator']
    except TelegramError as e:
        logger.error(f"Ошибка проверки участника чата {user_id}: {e}")
        return False
    except Exception as e:
        logger.error(f"Неожиданная ошибка при проверке участника {user_id}: {e}")
        return False

def is_super_admin(user_id):
    """Проверяет, является ли пользователь супер-админом (все функции)"""
    return user_id == SUPER_ADMIN_USER_ID

def is_admin_user(user_id):
    """Проверяет, является ли пользователь администратором (3 кнопки: список монет, инструкция, анализ по тикеру)"""
    return user_id in ADMIN_USER_IDS

def can_view_proxy_stats(user_id):
    """Проверяет, может ли пользователь видеть статистику прокси"""
    return user_id == PROXY_STATS_USER_ID

def get_access_denied_message():
    """Возвращает сообщение об отказе в доступе"""
    return "🚫 Данная функция доступна только избранным пользователям.\n\n💬 Обратитесь к администратору для получения доступа."

async def has_basic_access(bot, user_id):
    """Проверяет базовый доступ: либо супер-админ, либо админ, либо участник чата"""
    return is_super_admin(user_id) or is_admin_user(user_id) or await is_chat_member(bot, user_id)

def get_chat_access_denied_message():
    """Возвращает сообщение об отказе в доступе к чату"""
    return "🚫 Доступ к боту ограничен.\n\n💬 Для получения доступа вступите в наш чат или обратитесь к администратору."
