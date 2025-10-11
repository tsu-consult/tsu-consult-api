from datetime import datetime
from apps.notification_app.models import Notification

def send_telegram_notification(notification: Notification): # TODO: заменить на реальную отправку через Telegram
    try:
        print(f"📢 Telegram → {notification.user.username}: {notification.title}\n{notification.message}")
        notification.status = Notification.Status.SENT
        notification.sent_at = datetime.now()
    except Exception as e:
        notification.status = Notification.Status.FAILED
        print(f"❌ Ошибка отправки уведомления пользователю {notification.user.username}: {e}")
    finally:
        notification.save(update_fields=["status", "sent_at"])
