"""Joining TGCG by USSD, from any phone.

The provider posts every step of a session in Africa's Talking's format, which
most Nigerian USSD aggregators share: `sessionId`, `serviceCode`,
`phoneNumber`, and `text`, everything typed so far joined by "*". The menu
replays those answers to find where the session is, so nothing is stored
between steps, and typing 18*23*01*001 on one screen answers the four DELIM
questions at once.

The mobile network supplies the phone number, so no SMS code is needed. There
is no photo of the member or the card: members who join this way wait for a
coordinator's review like everyone else, and cannot be verified until they
add a selfie.

Never log a request: `text` holds the member's PIN.
"""

import re
from dataclasses import dataclass
from datetime import date

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.phone import normalize_ng_phone
from apps.core.choices import Gender, MembershipState, VerificationStatus
from apps.geography.models import Lga, PollingUnit, Ward

from . import registration
from .models import Member

# What every phone shows on one screen; longer text gets cut off.
SCREEN_LIMIT = 160
_NAME = re.compile(r"[A-Za-z][A-Za-z .'\-]*")


@dataclass(frozen=True)
class Screen:
    text: str
    end: bool = False

    def render(self) -> str:
        return ("END " if self.end else "CON ") + self.text


def _end(text: str) -> Screen:
    return Screen(text, end=True)


def _short(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _message(error: ValidationError) -> str:
    if hasattr(error, "error_dict"):
        return next(iter(error.message_dict.values()))[0]
    return error.messages[0]


def _number(value: str, digits: int) -> int | None:
    """One number of a DELIM code: up to `digits` digits, not zero."""
    if not re.fullmatch(rf"[0-9]{{1,{digits}}}", value) or int(value) == 0:
        return None
    return int(value)


def _title(name: str) -> str:
    """MUSA BELLO or musa bello -> Musa Bello (and O'Neil, Abdul-Malik)."""
    return re.sub(r"(^|[\s'\-])([a-z])", lambda m: m[1] + m[2].upper(), name.lower())


def _state_label(name: str) -> str:
    return name if name.endswith("Territory") else f"{name} State"


def _welcome(member: Member) -> Screen:
    first = _short(member.full_name.split(" ")[0], 20)
    return _end(
        f"Welcome to TGCG, {first}! Your membership number is {member.membership_number}. "
        "Sign in to the TGCG app with this phone number and your PIN."
    )


def _membership(phone: str) -> Screen:
    member = Member.objects.select_related("polling_unit").filter(phone_number=phone).first()
    if member is None:
        return _end("This number is not a TGCG member yet. Dial again and choose 1 to join.")
    if member.membership_state == MembershipState.SUSPENDED:
        status = "suspended"
    elif member.membership_state == MembershipState.DEACTIVATED:
        status = "ended"
    elif member.verification_status == VerificationStatus.VERIFIED:
        status = "verified"
    elif member.verification_status == VerificationStatus.REJECTED:
        status = "identity not confirmed"
    else:
        status = "awaiting review"
    unit = member.polling_unit.name if member.polling_unit else "not set"
    return _end(
        f"{_short(member.full_name, 40)}\nMember no. {member.membership_number}\n"
        f"Polling unit: {_short(unit, 45)}\nStatus: {status}"
    )


class _Session:
    """Where a session stands, rebuilt from the answers typed so far."""

    def __init__(self, phone: str, session_id: str):
        self.phone = phone
        self.session_id = session_id
        self.step = "menu"
        self.error = ""
        self.name = ""
        self.state_number: int | None = None
        self.state_name = ""
        self.lga: Lga | None = None
        self.ward: Ward | None = None
        self.unit: PollingUnit | None = None
        self.gender = ""
        self.born: date | None = None
        self.vin: str | None = None
        self.pin = ""

    def answer(self, value: str) -> Screen | None:
        """Take one answer. Returns a screen only when the session ends."""
        self.error = ""
        return getattr(self, f"_{self.step}")(value.strip())

    # --- steps: each takes an answer, and moves on or sets an error ---------------

    def _menu(self, value):
        if value == "2":
            return _membership(self.phone)
        if value != "1":
            self.error = "Choose 1 or 2."
            return None
        member = Member.objects.filter(phone_number=self.phone).first()
        if member is not None:
            return _end(
                f"This number is already a TGCG member ({member.membership_number}). "
                "Dial again and choose 2 to see your membership."
            )
        if User.objects.filter(phone_number=self.phone).exists():
            return _end(
                "This number belongs to a TGCG staff account, so it cannot join as a member."
            )
        self.step = "name"
        return None

    def _name(self, value):
        name = " ".join(value.split())
        words = [word for word in name.split(" ") if len(re.sub(r"[^A-Za-z]", "", word)) >= 2]
        if not _NAME.fullmatch(name) or len(words) < 2 or len(name) > 150:
            self.error = "Enter at least two names, in letters."
            return None
        self.name = _title(name)
        self.step = "state"
        return None

    def _state(self, value):
        number = _number(value, 2)
        lga = None
        if number is not None:
            lga = (
                Lga.objects.select_related("state")
                .filter(inec_code__startswith=f"{number:02d}-")
                .first()
            )
        if lga is None:
            self.error = "No state has that number."
            return None
        self.state_number, self.state_name = number, lga.state.name
        self.step = "lga"
        return None

    def _lga(self, value):
        number = _number(value, 2)
        lga = None
        if number is not None:
            lga = Lga.objects.filter(inec_code=f"{self.state_number:02d}-{number:02d}").first()
        if lga is None:
            self.error = "There is no LGA with that number."
            return None
        self.lga = lga
        self.step = "ward"
        return None

    def _ward(self, value):
        number = _number(value, 2)
        ward = None
        if number is not None:
            ward = Ward.objects.filter(code=f"{self.lga.inec_code}-{number:02d}").first()
        if ward is None:
            self.error = "There is no ward with that number."
            return None
        self.ward = ward
        self.step = "unit"
        return None

    def _unit(self, value):
        number = _number(value, 3)
        unit = None
        if number is not None:
            unit = PollingUnit.objects.filter(code=f"{self.ward.code}-{number:03d}").first()
        if unit is None:
            self.error = "There is no polling unit with that number."
            return None
        self.unit = unit
        self.step = "confirm_unit"
        return None

    def _confirm_unit(self, value):
        if value == "1":
            self.step = "gender"
        elif value == "2":
            self.state_number, self.lga, self.ward, self.unit = None, None, None, None
            self.step = "state"
        else:
            self.error = "Choose 1 or 2."
        return None

    def _gender(self, value):
        gender = {"1": Gender.MALE, "2": Gender.FEMALE}.get(value)
        if gender is None:
            self.error = "Choose 1 or 2."
            return None
        self.gender = gender
        self.step = "born"
        return None

    def _born(self, value):
        if value == "0":
            self.born = None
            self.step = "vin"
            return None
        try:
            if not re.fullmatch(r"[0-9]{8}", value):
                raise ValueError
            born = date(int(value[4:]), int(value[2:4]), int(value[:2]))
        except ValueError:
            self.error = "Use 8 digits: day, month, year."
            return None
        today = timezone.localdate()
        age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
        if not 16 <= age <= 110:
            self.error = "Check your date of birth."
            return None
        self.born = born
        self.step = "vin"
        return None

    def _vin(self, value):
        if value == "0":
            self.vin = None
            self.step = "pin"
            return None
        vin = "".join(value.split()).upper()
        if not re.fullmatch(r"[A-Z0-9]{19}", vin):
            self.error = "A VIN has 19 letters and digits."
        elif Member.objects.filter(vin=vin).exists():
            self.error = "That VIN is already registered."
        else:
            self.vin = vin
            self.step = "pin"
        return None

    def _pin(self, value):
        try:
            self.pin = registration.validate_pin(value)
        except ValidationError as error:
            self.error = _message(error)
            return None
        self.step = "pin_again"
        return None

    def _pin_again(self, value):
        if value != self.pin:
            self.pin = ""
            self.error = "The PINs did not match."
            self.step = "pin"
            return None
        self.step = "agree"
        return None

    def _agree(self, value):
        if value == "2":
            return _end("You have not joined TGCG. Dial again when you are ready.")
        if value != "1":
            self.error = "Choose 1 or 2."
            return None
        try:
            member = registration.ussd_register(
                phone_number=self.phone,
                session_id=self.session_id,
                full_name=self.name,
                polling_unit=self.unit,
                gender=self.gender,
                date_of_birth=self.born,
                vin=self.vin,
                pin=self.pin,
            )
        except ValidationError as error:
            return _end(f"You could not join: {_short(_message(error), 120)}")
        return _welcome(member)

    # --- the next question ---------------------------------------------------------

    def prompt(self) -> Screen:
        match self.step:
            case "menu":
                text = "Welcome to TGCG\n1. Join TGCG\n2. My membership"
            case "name":
                text = "Enter your full name as written on your PVC:"
            case "state":
                text = (
                    "Enter the DELIM code on your PVC with * between the numbers, "
                    "e.g. 18*23*01*001:"
                )
            case "lga":
                text = (
                    f"{_state_label(_short(self.state_name, 25))}. "
                    "Enter the 2nd number of the DELIM code:"
                )
            case "ward":
                text = f"{_short(self.lga.name, 28)} LGA. Enter the 3rd number of the DELIM code:"
            case "unit":
                text = (
                    f"{_short(self.ward.name, 30)} ward. Enter the last number of the DELIM code:"
                )
            case "confirm_unit":
                text = (
                    f"Polling unit: {_short(self.unit.name, 50)}\n"
                    f"{_short(self.ward.name, 25)}, {_short(self.lga.name, 20)}\n"
                    "1. Correct\n2. Enter it again"
                )
            case "gender":
                text = "Are you\n1. Male\n2. Female"
            case "born":
                text = "Enter your date of birth as DDMMYYYY, e.g. 14051990. Or enter 0 to skip:"
            case "vin":
                text = "Enter the VIN on your PVC (19 letters and digits), or enter 0 to skip:"
            case "pin":
                text = (
                    "Choose a 6-digit PIN. With this phone number, it signs you in to the TGCG app:"
                )
            case "pin_again":
                text = "Enter the same PIN again:"
            case _:  # agree
                text = (
                    "TGCG keeps your details to manage your membership and to contact you "
                    "about party activities.\n1. I agree, join TGCG\n2. Cancel"
                )
        return Screen(f"{self.error}\n{text}" if self.error else text)


def respond(*, phone: str, session_id: str, text: str, service_code: str = "") -> Screen:
    """The next screen of a USSD session, given everything typed so far."""
    if not session_id or (
        settings.USSD_SERVICE_CODE and service_code != settings.USSD_SERVICE_CODE
    ):
        return _end("This service is not available.")
    phone = normalize_ng_phone(phone)
    if phone is None:
        return _end("TGCG membership by USSD is for Nigerian mobile numbers.")
    joined = Member.objects.filter(client_id=f"ussd:{session_id}"[:64], phone_number=phone)
    if member := joined.first():
        return _welcome(member)  # the provider sent the last step again
    session = _Session(phone, session_id)
    for value in text.split("*") if text else []:
        screen = session.answer(value)
        if screen is not None:
            return screen
    return session.prompt()
