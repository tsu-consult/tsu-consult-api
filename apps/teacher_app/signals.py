from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from apps.consultation_app.models import Consultation
from apps.teacher_app.tasks import notify_subscribers

@receiver([post_save, post_delete], sender=Consultation)
def trigger_consultation_update(sender, instance, **kwargs):
    notify_subscribers.delay(instance.id)
