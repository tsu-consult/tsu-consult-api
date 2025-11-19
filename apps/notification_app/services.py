import redis
import requests
import logging
from django.utils import timezone

from apps.notification_app.models import Notification
from config.settings import REDIS_FLAGS_URL, TELEGRAM_BOT_TOKEN

redis_flags = redis.StrictRedis.from_url(
    REDIS_FLAGS_URL,
    decode_responses=True,
)

logger = logging.getLogger(__name__)


def send_telegram_notification(notification: Notification):
    user = notification.user
    chat_id = getattr(user, "telegram_id", None)
    bot_token = TELEGRAM_BOT_TOKEN

    if not bot_token:
        logger.warning("⚠️ TELEGRAM_BOT_TOKEN не задан в settings.py")
        notification.status = Notification.Status.FAILED
        notification.save(update_fields=["status"])
        return

    if not chat_id:
        logger.warning(f"⚠️ У пользователя {user.username} нет telegram_id — уведомление не отправлено")
        notification.status = Notification.Status.FAILED
        notification.save(update_fields=["status"])
        return

    try:
        logged_in = redis_flags.get(f"logged_in:{chat_id}")
        if logged_in != "1":
            logger.warning(f"❌ Пользователь {user.username} ({chat_id}) не залогинен — уведомление не отправлено")
            notification.status = Notification.Status.FAILED
            notification.save(update_fields=["status"])
            return
    except redis.exceptions.RedisError:
        logger.exception("⚠️ Ошибка при проверке Redis")
        notification.status = Notification.Status.FAILED
        notification.save(update_fields=["status"])
        return

    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": f"📢 <b>{notification.title}</b>\n{notification.message}",
            "parse_mode": "HTML",
        }
        response = requests.post(url, json=payload, timeout=10)

        if response.status_code == 200 and response.json().get("ok"):
            notification.status = Notification.Status.SENT
            notification.sent_at = timezone.now()
            logger.info(f"✅ Telegram → {user.username}: {notification.title}")
        else:
            notification.status = Notification.Status.FAILED
            logger.error(f"❌ Ошибка Telegram API: {response.text}")
    except (requests.RequestException, ValueError):
        notification.status = Notification.Status.FAILED
        logger.exception(f"❌ Ошибка отправки уведомления пользователю {user.username}")
    finally:
        notification.save(update_fields=["status", "sent_at"])
