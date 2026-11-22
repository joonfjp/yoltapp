"""Labs: experimental features built on semantic search, each easy to switch off or delete on its own.

Every lab is one entry in LABS. `new` puts a "New" badge on it in the UI; set it to False once the lab
has settled in. Switch a lab off with the `labs_disabled` setting (or YOLT_LABS_DISABLED as a comma list;
Settings → Labs is hidden for now): its endpoints answer 404 `lab-disabled` and the UI hides it.

To delete a lab for good, remove:
  - its entry in LABS below
  - its module here (yolt/labs/<id>.py) and its line in yolt/labs/routes.py, if it has endpoints
  - yolt/static/labs/<id>.js and its line in yolt/static/labs/index.js
  - its paths under /labs/<id> in spec/yolt.openapi.json (and any schema only they use)
  - its tests in tests/test_labs.py
A name left behind in `labs_disabled` is ignored.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

SETTING = "labs_disabled"


@dataclass(frozen=True)
class Lab:
    id: str
    name: str
    summary: str
    added: str  # the date it shipped
    new: bool = True  # badged "New" in the UI


LABS: tuple[Lab, ...] = (
    Lab("thread", "Thread", "An idea in, an ordered reel of clips out, each leading into the next. "
        "Review it and save it as a compilation.", "2026-11-12"),
    Lab("foryou", "For you", "On the home page: clips like the one you saved last.", "2026-11-12"),
)
BY_ID = {lab.id: lab for lab in LABS}


def names(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def disabled(settings: dict[str, str]) -> set[str]:
    """The labs switched off. A name that isn't a lab (one since deleted) is ignored."""
    return set(names(settings.get(SETTING, ""))) & BY_ID.keys()


def unknown(value: str) -> list[str]:
    """The names in a `labs_disabled` value that aren't labs, for validating a settings update."""
    return [name for name in names(value) if name not in BY_ID]


def listing(settings: dict[str, str]) -> list[dict]:
    off = disabled(settings)
    return [{**asdict(lab), "enabled": lab.id not in off} for lab in LABS]
