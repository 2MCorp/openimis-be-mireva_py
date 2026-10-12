"""Queue CHECKED claims that never received a MIREVA result."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from mireva.tasks import queue_unevaluated_claims


class Command(BaseCommand):
    """Run the unevaluated-claims sweep once from the command line."""

    help = "Queue CHECKED claims without a MIREVA result (same logic as the periodic sweep task)."

    def add_arguments(self, parser) -> None:
        """Register CLI options."""
        parser.add_argument("--limit", type=int, default=100, help="Maximum claims to queue.")

    def handle(self, *args, **options) -> None:
        """Queue claims and report how many."""
        queued = queue_unevaluated_claims(options["limit"])
        self.stdout.write(self.style.SUCCESS(f"Queued {queued} unevaluated CHECKED claims."))
