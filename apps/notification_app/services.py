import redis
import requests
from decouple import config
from django.utils import timezone

from apps.notification_app.models import Notification
from config.settings import REDIS_FLAGS_URL

redis_flags = redis.StrictRedis.from_url(
    REDIS_FLAGS_URL,
    decode_responses=True,
)

def send_telegram_notification(notification: Notification):
    user = notification.user
    chat_id = getattr(user, "telegram_id", None)
    bot_token = config('TELEGRAM_BOT_TOKEN', default='')

    if not bot_token:
        print("⚠️ TELEGRAM_BOT_TOKEN не задан в settings.py")
        notification.status = Notification.Status.FAILED
        notification.save(update_fields=["status"])
        return

    if not chat_id:
        print(f"⚠️ У пользователя {user.username} нет telegram_id — уведомление не отправлено")
        notification.status = Notification.Status.FAILED
        notification.save(update_fields=["status"])
        return

    try:
        flag_key = f"logged_in:{chat_id}"
        logged_in = redis_flags.get(flag_key)

        if logged_in != "1":
            print(f"⏸ Пользователь {user.username} ({chat_id}) не залогинен — уведомление отложено")
            return
    except Exception as e:
        print(f"⚠️ Ошибка при проверке Redis: {e}")
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
            print(f"✅ Telegram → {user.username}: {notification.title}")
        else:
            notification.status = Notification.Status.FAILED
            print(f"❌ Ошибка Telegram API: {response.text}")
    except Exception as e:
        notification.status = Notification.Status.FAILED
        print(f"❌ Ошибка отправки уведомления пользователю {user.username}: {e}")
    finally:
        notification.save(update_fields=["status", "sent_at"])
