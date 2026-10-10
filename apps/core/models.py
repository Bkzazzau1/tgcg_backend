import uuid

from django.db import models


class TimeStampedModel(models.Model):
    """Base for operational records: public UUID plus audit timestamps."""

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Sequence(models.Model):
    """Named counter for human-readable numbers (membership numbers, agent IDs)."""

    name = models.CharField(max_length=64, unique=True)
    value = models.PositiveBigIntegerField(default=0)

    def __str__(self):
        return f"{self.name}={self.value}"

    @classmethod
    def next_value(cls, name: str) -> int:
        """Atomically increment and return the counter (row-locked on PostgreSQL)."""
        from django.db import transaction

        with transaction.atomic():
            sequence, _ = cls.objects.select_for_update().get_or_create(name=name)
            sequence.value += 1
            sequence.save(update_fields=["value"])
            return sequence.value
