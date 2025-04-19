from django.core.cache import cache
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.utils import timezone
from .models import Project

@receiver(post_save, sender=Project)
@receiver(post_delete, sender=Project)
def invalidate_project_cache(sender, instance, **kwargs):
    cache.delete(f'home_projects_{timezone.now().date()}')
    cache.delete(f'github_data_{instance.repo_link}')
    cache.delete(f'project_requests_{instance.id}')