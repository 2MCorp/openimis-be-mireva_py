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
