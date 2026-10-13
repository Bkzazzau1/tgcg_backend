"""Short USSD joining with durable progress and private callback replay checks.

Provider text is cumulative, but only new answers are consumed. Never store or
log that text: it can include an app PIN. Progress belongs to the normalized
caller phone, while a new provider session explicitly resumes it with menu 1.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from apps.accounts.models import User
from apps.accounts.phone import normalize_ng_phone
from apps.core.choices import MembershipState, RegistrationChannel, VerificationStatus
from apps.geography.models import Lga, PollingUnit, Ward

from . import registration
from .models import Member, UssdSession
from .models import UssdRegistrationDraft as Draft

SCREEN_LIMIT = 160


@dataclass(frozen=True)
class Screen:
    text: str
    end: bool = False

    def render(self) -> str:
        return ("END " if self.end else "CON ") + self.text


def _end(text):
    return Screen(text, end=True)


def _short(text, limit):
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _digest(parts):
    return salted_hmac("membership.ussd.prefix", "*".join(parts), algorithm="sha256").hexdigest()


def _cached(session):
    return Screen(session.last_response[4:], end=session.last_response.startswith("END "))


def _cutoff():
    return timezone.now() - timedelta(days=max(1, settings.USSD_DRAFT_TTL_DAYS))


def _membership(phone):
    member = Member.objects.select_related("polling_unit").filter(phone_number=phone).first()
    if member is None:
        return _end("This number is not a TGCG member yet. Dial again and choose 1 to join.")
    status = {
        MembershipState.SUSPENDED: "suspended",
        MembershipState.DEACTIVATED: "ended",
    }.get(member.membership_state, "active member")
    if status == "active member" and member.verification_status == VerificationStatus.VERIFIED:
        status += ", ID checked"
    unit = member.polling_unit.name if member.polling_unit else "not set"
    return _end(
        f"{_short(member.full_name or 'Name not added yet', 32)}\n"
        f"Member no. {member.membership_number}\n"
        f"Polling unit: {_short(unit, 40)}\nStatus: {status}"
    )


def _joined(member):
    return _end(
        f"Welcome to TGCG! Member no. {member.membership_number}. "
        "Dial again and choose 1 to add your name and app PIN later."
    )


def _menu(phone):
    member = Member.objects.filter(phone_number=phone).first()
    if member and not member.user_id and member.registration_channel == RegistrationChannel.USSD:
        choice = "Complete profile"
    elif Draft.objects.filter(phone_number=phone, updated_at__gte=_cutoff()).exists():
        choice = "Continue joining"
    else:
        choice = "Join TGCG"
    return Screen(
        f"Welcome to TGCG\n1. {choice}\n2. My membership\n"
        f"Progress saved {max(1, settings.USSD_DRAFT_TTL_DAYS)} days. "
        "Redial to continue. 0 cancels."
    )


class _Flow:
    def __init__(self, phone, draft):
        self.phone, self.draft, self.error = phone, draft, ""

    def answer(self, value):
        self.error = ""
        value = value.strip()
        if value == "0":
            return _end("Saved progress cancelled. Your existing membership, if any, is unchanged.")
        return getattr(self, f"_{self.draft.step}")(value)

    def _vin(self, value):
        vin = "".join(value.split()).upper()
        if not re.fullmatch(r"[A-Z0-9]{19}", vin):
            self.error = "A VIN has 19 letters and digits."
        elif Member.objects.filter(vin=vin).exists():
            self.error = "That VIN is already registered."
        else:
            self.draft.vin, self.draft.step = vin, Draft.Step.UNIT

    def _unit(self, value):
        draft = self.draft
        if re.search(r"[/\-.\s]", value):
            try:
                unit = registration.polling_unit_from_delim(value)
            except ValidationError:
                self.error = "Unit code not found. Enter the full code again."
                draft.unit_parts = []
                return
            draft.polling_unit, draft.step, draft.unit_parts = unit, Draft.Step.CONFIRM, []
            return
        parts = list(draft.unit_parts)
        index = len(parts)
        digits = 3 if index == 3 else 2
        if index > 3 or not re.fullmatch(rf"[0-9]{{1,{digits}}}", value) or int(value) == 0:
            self.error = "Check that number in your polling unit code."
            return
        candidate = [*parts, f"{int(value):0{digits}d}"]
        code = "-".join(candidate)
        if index == 0:
            found = Lga.objects.filter(inec_code__startswith=code + "-").exists()
        elif index == 1:
            found = Lga.objects.filter(inec_code=code).exists()
        elif index == 2:
            found = Ward.objects.filter(code=code).exists()
        else:
            unit = PollingUnit.objects.select_related("ward__lga__state").filter(code=code).first()
            found = unit is not None
        if not found:
            self.error = "Code not found. Check the number and try again."
            return
        draft.unit_parts = candidate
        if index == 3:
            draft.polling_unit, draft.step = unit, Draft.Step.CONFIRM

    def _confirm(self, value):
        if value == "2":
            self.draft.polling_unit, self.draft.unit_parts = None, []
            self.draft.step = Draft.Step.UNIT
        elif value != "1":
            self.error = "Choose 1, 2 or 0."
        else:
            try:
                member = registration.ussd_register_minimal(
                    phone_number=self.phone,
                    draft_id=self.draft.pk,
                    polling_unit=self.draft.polling_unit,
                    vin=self.draft.vin,
                )
            except ValidationError:
                # A unit or VIN can change while the confirmation is on screen.
                self.draft.step, self.draft.vin = Draft.Step.VIN, ""
                self.draft.polling_unit, self.draft.unit_parts = None, []
                self.error = "Could not join. Check your VIN and unit."
                return
            return _joined(member)

    def _name(self, value):
        name = " ".join(value.split())
        words = [word for word in name.split() if sum(c.isalpha() for c in word) >= 2]
        if len(name) > 150 or len(words) < 2 or any(not (c.isalpha() or c in " .'-") for c in name):
            self.error = "Enter at least two names, in letters."
            return
        name = re.sub(r"(^|[\s'\-])([a-z])", lambda m: m[1] + m[2].upper(), name.lower())
        self.draft.full_name, self.draft.step = name, Draft.Step.PIN

    def _pin(self, value):
        try:
            pin = registration.validate_pin(value)
        except ValidationError:
            self.error = "Use 6 digits; avoid repeated or simple PINs."
            return
        self.draft.pin_hash = make_password(pin)
        self.draft.step = Draft.Step.PIN_CONFIRM

    def _pin_confirm(self, value):
        if not check_password(value, self.draft.pin_hash):
            self.draft.pin_hash, self.draft.step = "", Draft.Step.PIN
            self.error = "PINs did not match. Choose again."
            return
        member = Member.objects.filter(phone_number=self.phone).first()
        if member is None:
            return _end("Membership could not be found. Contact a TGCG coordinator.")
        try:
            member = registration.ussd_complete_profile(
                member=member, full_name=self.draft.full_name, pin_hash=self.draft.pin_hash
            )
        except ValidationError:
            return _end("App account could not be added. Contact a TGCG coordinator.")
        return _end(
            f"Profile complete! Member no. {member.membership_number}. "
            "Sign in to the TGCG app with this phone number and your PIN."
        )

    def prompt(self):
        draft = self.draft
        match draft.step:
            case Draft.Step.VIN:
                text = "Enter the VIN on your PVC (19 letters and digits):\n0. Cancel"
            case Draft.Step.UNIT:
                if draft.unit_parts:
                    label = ["", "2nd", "3rd", "last"][len(draft.unit_parts)]
                    text = f"Enter the {label} number of the polling unit code:\n0. Cancel"
                else:
                    text = (
                        "Enter polling unit CODE on your PVC, e.g. "
                        "18-23-01-001 or 18*23*01*001:\n0. Cancel"
                    )
            case Draft.Step.CONFIRM:
                unit = draft.polling_unit
                if unit is None:
                    draft.step, draft.unit_parts = Draft.Step.UNIT, []
                    return self.prompt()
                text = (
                    f"Polling unit: {_short(unit.name, 20)}\n"
                    f"{_short(unit.ward.name, 10)}, {_short(unit.ward.lga.name, 10)}\n"
                    "TGCG uses details for membership & party contact.\n"
                    "1. Agree & join\n2. Change unit\n0. Cancel"
                )
            case Draft.Step.NAME:
                text = "Optional app profile: enter your full name as on your PVC:\n0. Cancel progress"
            case Draft.Step.PIN:
                text = "Choose a 6-digit PIN for signing in to the TGCG app:\n0. Cancel progress"
            case Draft.Step.PIN_CONFIRM:
                text = "Enter the same PIN again:\n0. Cancel progress"
            case _:
                return _end("Please redial and choose 1 to continue.")
        # Keep all choices intact even on the longest confirmation with an error.
        if self.error:
            room = SCREEN_LIMIT - len(text) - 1
            text = f"{_short(self.error, room)}\n{text}"
        return Screen(text)


def _start(phone, session_id, code):
    member = Member.objects.select_related("user").filter(phone_number=phone).first()
    if member:
        if member.user_id:
            return _end(
                f"Member no. {member.membership_number}. "
                "Sign in to the TGCG app with your phone and PIN."
            )
        if member.membership_state == MembershipState.DEACTIVATED:
            return _end("This membership has ended. Contact a TGCG coordinator.")
        if member.registration_channel != RegistrationChannel.USSD:
            return _end("Set up this membership's app account in the TGCG app.")
    if User.objects.filter(phone_number=phone).exists():
        return _end("This number belongs to a TGCG staff account, so it cannot join as a member.")
    kind = Draft.Kind.PROFILE if member else Draft.Kind.JOIN
    saved, _ = Draft.objects.get_or_create(phone_number=phone, defaults={"kind": kind})
    # Final completion can remove the row while an opening callback waits.
    while True:
        locked = Draft.objects.select_for_update().filter(pk=saved.pk).first()
        if locked is not None:
            saved = locked
            break
        saved, _ = Draft.objects.get_or_create(phone_number=phone, defaults={"kind": kind})
    member = Member.objects.filter(phone_number=phone).first()
    if member and member.user_id:
        saved.delete()
        return _end(f"Member no. {member.membership_number}. Sign in with your phone and PIN.")
    if member and (
        member.membership_state == MembershipState.DEACTIVATED
        or member.registration_channel != RegistrationChannel.USSD
    ):
        saved.delete()
        return _end("Contact a TGCG coordinator to set up this membership's app account.")
    if User.objects.filter(phone_number=phone).exists():
        saved.delete()
        return _end("This number already has an account. Contact a TGCG coordinator.")
    kind = Draft.Kind.PROFILE if member else Draft.Kind.JOIN
    if saved.updated_at < _cutoff() or saved.kind != kind:
        saved.delete()
        saved = Draft.objects.create(phone_number=phone, kind=kind)
    if kind == Draft.Kind.PROFILE and saved.step not in (
        Draft.Step.NAME,
        Draft.Step.PIN,
        Draft.Step.PIN_CONFIRM,
    ):
        saved.full_name = member.full_name
        saved.step = Draft.Step.PIN if member.full_name else Draft.Step.NAME
    # Another identical opening callback may have created this session while
    # this request waited for the phone's draft lock.
    session, created = UssdSession.objects.get_or_create(
        phone_number=phone,
        service_code=code,
        provider_session_id=session_id,
        defaults={"draft": saved},
    )
    if not created:
        return _cached(session)
    saved.active_session_key = session.pk
    screen = _Flow(phone, saved).prompt()
    saved.save()
    session.answer_count, session.prefix_digest = 1, _digest(["1"])
    session.last_response = screen.render()
    session.save()
    return screen


@transaction.atomic
def respond(*, phone, session_id, text, service_code=""):
    if (
        not session_id
        or len(session_id) > 128
        or len(service_code) > 64
        or (settings.USSD_SERVICE_CODE and service_code != settings.USSD_SERVICE_CODE)
    ):
        return _end("This service is not available.")
    phone = normalize_ng_phone(phone)
    if phone is None:
        return _end("TGCG membership by USSD is for Nigerian mobile numbers.")
    if len(text) > 1024:
        return _end("Please redial and choose 1 to continue your saved progress.")
    if not text:
        return _menu(phone)
    parts = text.split("*")
    session = UssdSession.objects.filter(
        phone_number=phone, service_code=service_code, provider_session_id=session_id
    ).first()
    if session is None:
        if text == "2":
            return _membership(phone)
        if text == "0":
            saved = Draft.objects.select_for_update().filter(phone_number=phone).first()
            if saved:
                saved.delete()
            return _end("Saved progress cancelled. Your existing membership, if any, is unchanged.")
        if text == "1":
            return _start(phone, session_id, service_code)
        return _end("Please redial and choose 1 to start or continue.")
    # Lock progress before its session consistently, including new openings.
    draft = Draft.objects.select_for_update().filter(pk=session.draft_id).first()
    session = UssdSession.objects.select_for_update().filter(pk=session.pk).first()
    if session is None:
        return _end("Saved progress expired. Please redial and choose 1.")
    if session.ended:
        return _cached(session)
    if draft is None or draft.updated_at < _cutoff():
        return _end("Saved progress expired. Please redial and choose 1.")
    if draft.active_session_key != session.pk:
        return _end("A newer session is active. Please use it or redial to continue.")
    if len(parts) < session.answer_count:
        return _cached(session)
    if not constant_time_compare(_digest(parts[: session.answer_count]), session.prefix_digest):
        return _end("Please redial and choose 1 to continue your saved progress.")
    if len(parts) == session.answer_count:
        return _cached(session)
    flow = _Flow(phone, draft)
    screen = None
    for value in parts[session.answer_count :]:
        previous_step = draft.step
        screen = flow.answer(value)
        if screen is not None or flow.error or draft.step != previous_step:
            # Display each new question before accepting its answer.
            # This still accepts all four star-separated unit parts together.
            break
    screen = screen or flow.prompt()
    session.answer_count, session.prefix_digest = len(parts), _digest(parts)
    session.last_response, session.ended = screen.render(), screen.end
    if screen.end:
        draft.delete()
        session.draft = None
    else:
        draft.save()
    session.save()
    return screen
