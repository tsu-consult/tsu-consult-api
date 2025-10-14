import requests
from django.conf import settings
from django.utils import timezone
from apps.notification_app.models import Notification


def send_telegram_notification(notification: Notification):
    user = notification.user
    chat_id = getattr(user, "telegram_id", None)
    bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", None)

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
