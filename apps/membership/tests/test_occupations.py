"""Occupations are typed freely: the page counts the work, not the spelling."""

import pytest

from apps.access.tests.test_access import appoint, client_for, custom_role, member_account
from apps.accounts.models import User
from apps.core.choices import Role
from apps.core.permissions import Capability
from apps.membership.occupations import SHOWN, occupation_group, summarise
from conftest import PASSWORD, make_member

ANALYSIS = "/api/v1/members/analysis/"


@pytest.mark.parametrize(
    ("typed", "group"),
    [
        ("House wife", "Housewives"),
        ("HOUSEWIFE", "Housewives"),
        ("housewives.", "Housewives"),
        ("House-wife", "Housewives"),
        ("Matar aure", "Housewives"),
        ("Students", "Students"),
        ("STUDENT", "Students"),
        ("student nurse", "Students"),
        ("Farmer", "Farmers"),
        ("crop farming", "Farmers"),
        ("farmers", "Farmers"),
        ("Manomi", "Farmers"),
        ("Business", "Traders and business"),
        ("Business man", "Traders and business"),
        ("businesswoman", "Traders and business"),
        ("Dan kasuwa", "Traders and business"),
        ("Trader", "Traders and business"),
        ("  civil   servant ", "Civil servants"),
        ("Teacher", "Teachers"),
        ("Malami", "Teachers"),
        ("Retired teacher", "Retired"),
        ("self employed", "Self-employed"),
        ("Unemployed graduate", "Unemployed"),
        ("Community health worker", "Health workers"),
        ("Okada rider", "Drivers"),
        ("Tailor", "Artisans"),
    ],
)
def test_the_same_work_lands_in_the_same_group(typed, group):
    assert occupation_group(typed) == group


@pytest.mark.parametrize(
    ("typed", "own_words"),
    [
        ("Photographer", "Photographer"),
        ("graphic designers", "Graphic designer"),
        ("PHOTOGRAPHERS", "Photographer"),
        # A word that only contains a group's word is not that group.
        ("Metalworker", "Metalworker"),
    ],
)
def test_other_work_keeps_the_members_own_words(typed, own_words):
    assert occupation_group(typed) == own_words


@pytest.mark.parametrize("typed", ["", "   ", "-", ".", "none", "NIL", "N/A", "n.a", "unknown"])
def test_answers_that_say_nothing_are_not_an_occupation(typed):
    assert occupation_group(typed) is None


def test_the_chart_adds_up_with_every_spelling_gathered():
    summary = summarise(
        [
            ("House wife", 300),
            ("HOUSEWIFE", 200),
            ("Students", 700),
            ("student", 50),
            ("Farmer", 560),
            ("", 40),
            (None, 2),
            ("none", 8),
        ]
    )
    assert summary == {
        "top": [
            {"name": "Students", "count": 750},
            {"name": "Farmers", "count": 560},
            {"name": "Housewives", "count": 500},
        ],
        "other": 0,
        "not_given": 50,
    }


def test_words_one_member_typed_stay_in_other_as_they_could_point_to_them():
    summary = summarise([("Commissioner for Rural Roads", 1), ("Photographer", 2), ("Farmer", 1)])
    assert summary["top"] == [
        {"name": "Photographer", "count": 2},
        {"name": "Farmers", "count": 1},
    ]
    assert summary["other"] == 1


def test_only_the_largest_groups_are_listed_and_the_rest_are_still_counted():
    rows = [(f"Craft number {chr(97 + index)}", 100 - index) for index in range(SHOWN + 3)]
    summary = summarise(rows)
    assert len(summary["top"]) == SHOWN
    assert summary["top"][0] == {"name": "Craft number a", "count": 100}
    assert sum(row["count"] for row in summary["top"]) + summary["other"] == sum(
        members for _, members in rows
    )


def test_ties_come_out_in_the_same_order_whatever_the_database_returned():
    forward = summarise([("Tailor", 5), ("Farmer", 5)])
    backward = summarise([("Farmer", 5), ("Tailor", 5)])
    assert forward == backward


@pytest.fixture
def admin(db):
    return User.objects.create_user(
        "JOBS-NA", PASSWORD, full_name="National Admin", role=Role.NATIONAL_ADMINISTRATOR
    )


def test_the_analysis_counts_occupations_and_men_only_inside_the_area(admin):
    make_member(phone="08036660001", gender="male", occupation="Farmer")
    make_member(phone="08036660002", gender="female", occupation="House wife")
    make_member(phone="08036660003", gender="female", occupation="housewife")
    make_member(phone="08036660004")
    make_member(phone="08036660005", lga="KN-AJINGI", gender="male", occupation="Farmer")

    national = client_for(admin).get(ANALYSIS).json()
    assert national["members"]["occupations"]["top"] == [
        {"name": "Farmers", "count": 2},
        {"name": "Housewives", "count": 2},
    ]
    assert national["members"]["occupations"]["not_given"] == 1
    parts = {part["code"]: part for part in national["parts"]}
    assert parts["KD"]["men"] == 1 and parts["KD"]["women"] == 2
    assert parts["KN"]["men"] == 1 and parts["KN"]["women"] == 0

    operator = member_account("08036660006")
    role = custom_role(admin, "Jobs Kaduna Reader", [Capability.MANAGE_MEMBERSHIP])
    assert appoint(admin, operator, role, [{"level": "state", "stateId": "KD"}]).status_code == 201
    kaduna = client_for(operator.user).get(ANALYSIS, {"level": "state", "code": "KD"}).json()
    assert kaduna["members"]["occupations"]["top"] == [
        {"name": "Housewives", "count": 2},
        {"name": "Farmers", "count": 1},
    ]


def test_the_exco_count_says_men_as_well_as_women(admin):
    secretary = member_account("08036660010")
    secretary.gender = "male"
    secretary.save(update_fields=["gender"])
    role = custom_role(admin, "Jobs State Secretary", [Capability.MANAGE_MEMBERSHIP])
    assert appoint(admin, secretary, role, [{"level": "state", "stateId": "KD"}]).status_code == 201
    exco = client_for(admin).get(ANALYSIS).json()["exco"]
    assert exco["men"] == 1
    assert exco["women"] == 0


def test_an_empty_area_has_no_occupations_to_list(admin):
    summary = client_for(admin).get(ANALYSIS).json()["members"]["occupations"]
    assert summary == {"top": [], "other": 0, "not_given": 0}
