from django.apps import AppConfig
from django.conf import settings


class MirevaConfig(AppConfig):
    """Django application configuration for the MIREVA adapter."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "mireva"
    label = "mireva"
    verbose_name = "MIREVA AI Claim Processor"

    def ready(self) -> None:
        """Register app signal handlers and touch required settings keys."""
        _ = getattr(settings, "MIREVA_URL", None)
        _ = getattr(settings, "MIREVA_API_KEY", None)
        import mireva.signals  # noqa: F401

        self._schedule_concept_sync()

    @staticmethod
    def _schedule_concept_sync() -> None:
        """Add the periodic OMOP mapping sync to Celery beat (0 minutes disables it)."""
        from celery import current_app

        minutes = int(getattr(settings, "MIREVA_OMOP_SYNC_INTERVAL_MINUTES", 60))
        if minutes <= 0:
            return
        current_app.conf.beat_schedule = {
            **(current_app.conf.beat_schedule or {}),
            "mireva-sync-omop-mappings": {
                "task": "mireva.tasks.sync_omop_mappings",
                "schedule": minutes * 60.0,
            },
        }
