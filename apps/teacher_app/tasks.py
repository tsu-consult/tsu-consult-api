from celery import shared_task

from apps.consultation_app.models import Consultation
from apps.teacher_app.models import Subscription


@shared_task
def notify_subscribers(consultation_id):
    try:
        consultation = Consultation.objects.get(id=consultation_id)
    except Consultation.DoesNotExist:
        return

    teacher = consultation.teacher
    subscriptions = Subscription.objects.filter(
        teacher=teacher
    ).select_related("student")

    for sub in subscriptions:
        student = sub.student
        print(f"📢 Уведомление для {student.email}: " # TODO: заменить на реальную отправку через Telegram
              f"Расписание преподавателя {teacher.get_full_name()} обновлено. "
              f"Консультация: {consultation.title} ({consultation.date})")
