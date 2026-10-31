"""Members' occupations, gathered for the Members & Exco page and the dashboard.

Occupation is free text, so "House wife", "HOUSEWIFE" and "housewives." are one
kind of work. Common work is gathered into groups here; anything else keeps the
member's own words, so a new kind of work shows up without a change here.
"""

import re
from collections import Counter
from functools import lru_cache

# The chart's rows: the largest groups, then the rest as "other".
SHOWN = 12
# Words only one member typed stay in "other": they could point to that person.
MIN_OWN_WORDS = 2

# First match wins, so a "retired teacher" is retired and a "student nurse" a
# student. Hausa words are here because many members type them.
GROUPS = (
    ("Retired", ("retired", "retiree", "pensioner", "rtd")),
    ("Students", ("student", "undergraduate", "schooling", "pupil", "corper", "dalibi", "daliba")),
    (
        "Housewives",
        ("housewife", "house wife", "housewive", "house wive", "homemaker", "home maker")
        + ("matar aure",),
    ),
    ("Unemployed", ("unemployed", "jobless", "job seeker", "applicant")),
    ("Self-employed", ("self employed", "self employ")),
    (
        "Farmers",
        ("farmer", "farming", "agriculture", "agric", "agriculturist", "poultry", "manomi")
        + ("fisherman", "fishing", "herder", "herdsman", "livestock"),
    ),
    (
        "Teachers",
        ("teacher", "teaching", "lecturer", "educationist", "educator", "tutor", "headmaster")
        + ("headmistress", "malami", "malama"),
    ),
    (
        "Health workers",
        ("nurse", "nursing", "midwife", "doctor", "medical", "health", "pharmacist", "pharmacy")
        + ("laboratory", "lab scientist"),
    ),
    (
        "Security",
        ("police", "policeman", "soldier", "military", "army", "navy", "security", "vigilante")
        + ("civil defence", "civil defense", "nscdc", "soja"),
    ),
    (
        "Civil servants",
        ("civil servant", "public servant", "government worker", "government staff")
        + ("govt staff", "local government"),
    ),
    ("Drivers", ("driver", "driving", "okada", "keke", "transporter", "transport", "direba")),
    (
        "Artisans",
        ("artisan", "tailor", "tela", "mechanic", "carpenter", "electrician", "welder", "plumber")
        + ("bricklayer", "mason", "barber", "hairdresser", "hair stylist", "vulcanizer")
        + ("vulcaniser", "painter", "cobbler", "blacksmith", "fashion designer", "technician"),
    ),
    (
        "Traders and business",
        ("trader", "trading", "business", "businessman", "businessmen", "businesswoman")
        + ("businesswomen", "business man", "business woman", "merchant", "marketer", "sales")
        + ("salesman", "entrepreneur", "dan kasuwa", "yar kasuwa"),
    ),
    ("Lawyers", ("lawyer", "barrister", "solicitor", "legal practitioner", "attorney")),
    ("Engineers", ("engineer", "engr")),
    ("Accountants and bankers", ("accountant", "accounting", "banker", "bank staff", "auditor")),
    ("Politicians", ("politician", "politics")),
    ("Religious leaders", ("imam", "pastor", "cleric", "islamic scholar", "reverend")),
)
GROUP_NAMES = frozenset(name for name, _ in GROUPS)
# Answers that say nothing about the member's work.
NO_ANSWER = frozenset({"none", "nil", "nill", "na", "n a", "no", "nothing", "null", "unknown"})


def _singular(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def _tidy(text: str) -> str:
    """Lower case, words only (in any language), each one singular."""
    return " ".join(_singular(word) for word in re.sub(r"[\W_]+", " ", text.lower()).split())


_PATTERNS = tuple(
    (name, re.compile(r"(?<!\w)(?:" + "|".join(re.escape(_tidy(k)) for k in keys) + r")(?!\w)"))
    for name, keys in GROUPS
)


@lru_cache(maxsize=20_000)
def occupation_group(typed: str) -> str | None:
    """The group `typed` belongs to, its own tidied words when none fits, or
    None when it says nothing about the member's work."""
    text = _tidy(typed)
    if not text or text in NO_ANSWER:
        return None
    for name, pattern in _PATTERNS:
        if pattern.search(text):
            return name
    return text[0].upper() + text[1:]


def summarise(rows) -> dict:
    """The largest groups from (occupation, members) rows, most members first."""
    groups = Counter()
    not_given = 0
    for typed, members in rows:
        group = occupation_group(typed or "")
        if group is None:
            not_given += members
        else:
            groups[group] += members
    shown = sorted(
        (
            (name, members)
            for name, members in groups.items()
            if name in GROUP_NAMES or members >= MIN_OWN_WORDS
        ),
        key=lambda row: (-row[1], row[0]),
    )[:SHOWN]
    return {
        "top": [{"name": name, "count": members} for name, members in shown],
        "other": groups.total() - sum(members for _, members in shown),
        "not_given": not_given,
    }
