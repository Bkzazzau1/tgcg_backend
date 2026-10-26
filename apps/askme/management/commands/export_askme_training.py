"""Write Ask Me's reviewed answers as training data for TGCG's own model.

    python manage.py export_askme_training --out /path/askme-training.jsonl
    python manage.py export_askme_training --out FILE --audio-dir /path/audio/

One JSON line per reviewed question (approved or corrected) whose asker
allowed TGCG to train on their questions: the language, the question (as
heard, and as a reviewer corrected it), the approved answer and the library
items it rests on. With --audio-dir, the kept recordings of spoken questions
(their askers allowed it) are copied there, named in "audio", for training
TGCG's listening AI. No names, accounts or phone numbers. Prints counts only.
"""

import json
import shutil
from pathlib import Path

from django.core.management.base import BaseCommand

from apps.askme.models import Question
from apps.askme.services import REVIEWED


class Command(BaseCommand):
    help = "Export reviewed Ask Me answers (with the askers' consent) as JSON lines."

    def add_arguments(self, parser):
        parser.add_argument("--out", required=True)
        parser.add_argument("--audio-dir", default="")

    def handle(self, *args, out, audio_dir="", **options):
        rows = (
            Question.objects.filter(review__in=REVIEWED, training_ok=True)
            .prefetch_related("items")
            .order_by("pk")
        )
        count = copied = 0
        audio_dir = Path(audio_dir) if audio_dir else None
        if audio_dir:
            audio_dir.mkdir(parents=True, exist_ok=True)
        with Path(out).open("w", encoding="utf-8") as handle:
            for question in rows.iterator(chunk_size=500):
                record = {
                    "language": question.language,
                    "channel": question.channel,
                    "question": question.corrected_text or question.text,
                    "heard": question.text,
                    "answer": question.shown_answer,
                    "status": question.status,
                    "items": [
                        {"title": item.title, "fact": item.fact, "argument": item.argument}
                        for item in question.items.all()
                    ],
                }
                if audio_dir and question.recording:
                    name = f"{question.uuid}{Path(question.recording.name).suffix.lower()}"
                    with (
                        question.recording.open("rb") as source,
                        (audio_dir / name).open("wb") as target,
                    ):
                        shutil.copyfileobj(source, target)
                    record["audio"] = name
                    copied += 1
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                count += 1
        self.stdout.write(f"Wrote {count} reviewed answers and {copied} recordings.")
