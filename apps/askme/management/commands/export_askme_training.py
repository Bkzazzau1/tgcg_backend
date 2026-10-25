"""Write Ask Me's reviewed answers as training data for TGCG's own model.

    python manage.py export_askme_training --out /path/askme-training.jsonl

One JSON line per reviewed question (approved or corrected) whose asker
allowed TGCG to train on their questions: the language, the question, the
approved answer and the library items it rests on. No names, accounts or
phone numbers. Prints the count only.
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand

from apps.askme.models import Question
from apps.askme.services import REVIEWED


class Command(BaseCommand):
    help = "Export reviewed Ask Me answers (with the askers' consent) as JSON lines."

    def add_arguments(self, parser):
        parser.add_argument("--out", required=True)

    def handle(self, *args, out, **options):
        rows = (
            Question.objects.filter(review__in=REVIEWED, training_ok=True)
            .prefetch_related("items")
            .order_by("pk")
        )
        count = 0
        with Path(out).open("w", encoding="utf-8") as handle:
            for question in rows.iterator(chunk_size=500):
                record = {
                    "language": question.language,
                    "question": question.text,
                    "answer": question.shown_answer,
                    "status": question.status,
                    "items": [
                        {"title": item.title, "fact": item.fact, "argument": item.argument}
                        for item in question.items.all()
                    ],
                }
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                count += 1
        self.stdout.write(f"Wrote {count} reviewed answers.")
