"""Comparing the details a member gave with the text read from their card photo."""

from apps.membership.card_match import compare, name_on_card, vin_on_card

VIN = "90F5B0E8D0295847001"
CARD = "VOTER'S CARD\nBELLO\nMUSA ABDULLAHI\nVIN: 90F5 B0E8 D029 5847 001\nM 14-05-1990"


def test_the_vin_is_found_despite_spaces_and_misread_characters():
    assert vin_on_card(CARD, VIN)
    assert vin_on_card(CARD.replace("90F5", "9OF5").replace("B0E8", "80E8"), VIN)
    assert vin_on_card(CARD.replace("001", "007"), VIN)  # one character wrong
    assert not vin_on_card(CARD.replace("001", "077"), VIN)
    assert not vin_on_card(CARD, "90F5B0E8D0295847")  # not a VIN at all
    assert not vin_on_card("", VIN)


def test_every_word_of_the_name_is_on_the_card_in_any_order():
    assert name_on_card(CARD, "Musa Abdullahi Bello")
    assert name_on_card(CARD, "BELLO Musa")
    assert name_on_card(CARD, "Alhaji Musa Bello")  # a title no card carries
    # Misread letters: digits for letters, or one letter for another.
    assert name_on_card(CARD.replace("BELLO", "8ELL0"), "Musa Bello")
    assert name_on_card(CARD.replace("ABDULLAHI", "ABDULIAHI"), "Musa Abdullahi Bello")
    # Printed as two words but given as one, and the other way round.
    assert name_on_card(CARD, "Musa Abdullahi Bello".replace("Musa Abdullahi", "Musaabdullahi"))
    assert name_on_card(CARD.replace("MUSA ABDULLAHI", "MUSA ABDUL LAHI"), "Musa Abdullahi Bello")

    assert not name_on_card(CARD, "Musa Bello Kaura")  # a word the card does not have
    assert not name_on_card(CARD, "Ada Obi")
    assert not name_on_card(CARD, "Bello")  # one word proves too little
    assert not name_on_card(CARD, "Alhaji Bello")


def test_the_result_names_what_was_missing_never_the_details():
    assert compare(CARD, "Musa Bello", VIN) == (
        True,
        "The name and VIN given match the text read from the card photo.",
    )
    matched, detail = compare(CARD, "Ada Obi", "90F5B0E8D0295847999")
    assert not matched
    assert detail == "The name and VIN given could not be found on the card photo."
    assert "Ada" not in detail and "999" not in detail
