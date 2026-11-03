"""Where a person on a group's list is, from the names the list gives.

A polling unit code places them exactly. Otherwise the state, LGA and ward
are matched by name, ignoring case, spaces and punctuation, each inside the
one above it; an LGA alone is placed when only one LGA has that name. A name
that matches nothing is left out, and the person is placed as precisely as
the rest allows, or not at all.
"""

import re

from apps.core.choices import GeographyLevel
from apps.geography.models import Lga, PollingUnit, State, Ward

_FILLER = ("localgovernmentarea", "localgovernment", "lga", "state", "ward")


def _key(name: str) -> str:
    text = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    for word in _FILLER:
        if text.endswith(word) and len(text) > len(word):
            text = text[: -len(word)]
    return text


class Places:
    """Looks places up by name, remembering what it found, for one list."""

    def __init__(self):
        self._states = None
        self._lgas = {}
        self._wards = {}
        self._lgas_by_name = None

    def state(self, name):
        if self._states is None:
            self._states = {}
            for state in State.objects.all():
                for key in (_key(state.name), _key(state.code)):
                    self._states[key] = state
                if state.is_fct:
                    for key in ("fct", "abuja"):
                        self._states[key] = state
        return self._states.get(_key(name)) if name else None

    def lga(self, name, state=None):
        if not name:
            return None
        if state is not None:
            if state.pk not in self._lgas:
                self._lgas[state.pk] = {
                    _key(lga.name): lga for lga in Lga.objects.filter(state=state)
                }
            return self._lgas[state.pk].get(_key(name))
        if self._lgas_by_name is None:
            self._lgas_by_name = {}
            for lga in Lga.objects.select_related("state"):
                self._lgas_by_name.setdefault(_key(lga.name), []).append(lga)
        found = self._lgas_by_name.get(_key(name), [])
        return found[0] if len(found) == 1 else None

    def ward(self, name, lga):
        if not name or lga is None:
            return None
        if lga.pk not in self._wards:
            self._wards[lga.pk] = {_key(ward.name): ward for ward in Ward.objects.filter(lga=lga)}
        return self._wards[lga.pk].get(_key(name))

    def place(self, *, state="", lga="", ward="", unit=""):
        """The scope to register the person at, as the API takes it, and its
        name; (None, "") when nothing matched."""
        if re.fullmatch(r"\d{2}-\d{2}-\d{2}-\d{3}", unit or ""):
            found = PollingUnit.objects.select_related("ward__lga__state").filter(code=unit).first()
            if found is not None:
                return (
                    {"level": GeographyLevel.POLLING_UNIT, "pollingUnitId": found.code},
                    ", ".join(
                        [
                            found.name.title(),
                            found.ward.name.title(),
                            found.ward.lga.name,
                            found.ward.lga.state.name,
                        ]
                    ),
                )
        found_state = self.state(state)
        found_lga = self.lga(lga, found_state) if (found_state or not state) else None
        if found_lga is not None and found_state is None:
            found_state = found_lga.state
        found_ward = self.ward(ward, found_lga)
        if found_ward is not None:
            return (
                {"level": GeographyLevel.WARD, "wardId": found_ward.code},
                f"{found_ward.name.title()}, {found_lga.name}, {found_state.name}",
            )
        if found_lga is not None:
            return (
                {"level": GeographyLevel.LGA, "lgaId": found_lga.code},
                f"{found_lga.name}, {found_state.name}",
            )
        if found_state is not None:
            return {"level": GeographyLevel.STATE, "stateId": found_state.code}, found_state.name
        return None, ""


def place_of(member) -> str:
    """A member's place in a few words: ward, LGA and state, as far as known."""
    parts = []
    if member.ward_id:
        parts.append(member.ward.name.title())
    if member.lga_id:
        parts.append(member.lga.name)
    if member.state_id:
        parts.append(member.state.name)
    return ", ".join(parts)
