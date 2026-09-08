"""Every code this module BORROWS has a string in every language it renders.

Not "every code this module owns" — that is the house gate, and it is green on
a deployment where thirteen of these codes render in English. ``errors.py``
imports ``stapel_attributes.errors`` on purpose: the ``validate-configs`` and
``validate-dto`` endpoints answer with that library's structured validation
codes, so thirteen ``stapel_attributes``-owned keys enter the error registry —
and the ``docs/errors.json`` — of every host that mounts this module. Forty-two
more arrive the same way from ``stapel_core``. **The registry travelled; the
strings did not.**

The two halves of that asymmetry are the two gates here:

* :func:`test_every_borrowed_code_has_a_string_in_every_rendered_language`
  resolves the registry the way a host's loader does — through
  ``stapel_core.i18n.catalogs.load_app_catalogs``, which walks the package
  directory of every registered error owner, not just INSTALLED_APPS. It
  answers "does the installed dependency carry the sentence".

* :func:`test_the_floor_of_every_upstream_error_owner_ships_those_strings`
  answers the other half — "can a host install a version where it does not".
  It reads the floors this package declares. ``stapel-attributes>=0.9.2``
  admitted a wheel that carries ``errors.py`` and no ``translations/``
  directory at all, and ``stapel-core>=0.26.0`` admitted thirty-odd releases of
  a loader that could not find a catalog for a library outside INSTALLED_APPS.
  A floor that admits those is the defect; the coverage test above cannot see
  it, because pip installs the newest thing the range allows.

Nothing here copies an upstream string into this package. That "fix" is what
:func:`test_this_module_ships_no_foreign_key` forbids: core's
``check_translation_catalogs`` calls a translated key another package owns,
while its owner already ships that language, a ``foreign`` error — and
declaring the override would make this repo the maintainer of a second,
drifting copy of somebody else's text. The strings reach a host from the
owner's wheel, which is where they stay correct.

**What is deliberately NOT gated here.** This module ships no ``translations/``
directory of its own, so the nine codes it OWNS render their English literal
in every locale. That is a real gap and a different piece of work — a
catalogue this package would author, review and own, not a dependency range —
and inventing the strings here to make a number go green would be the same
"fix by copying" this file exists to prevent, pointed inward.
:func:`test_a_catalogue_this_module_ships_covers_every_key_it_owns` is the
trip-wire that arms itself the day such a catalogue appears.
"""
import json
import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TRANSLATIONS = REPO / "translations"

#: The package whose keys are this module's own responsibility.
THIS_PACKAGE = "stapel_categories"

#: Languages a host renders these codes in — the languages the upstream owners
#: publish. Declared, not derived: derived from the INSTALLED wheel, this list
#: would be empty at exactly the floor the gate exists to reject (a wheel with
#: no ``translations/`` ships no language, so a derived gate would call the
#: defect compliant).
LANGUAGES = ["en", "ru", "es"]
TARGET_LANGUAGES = [lang for lang in LANGUAGES if lang != "en"]

#: Per upstream distribution, the first release whose WHEEL makes the strings
#: for the codes it owns reachable from a host that installs this package.
#: Verified against the built wheels, not against a changelog:
#:
#: * ``stapel-attributes`` 0.9.3 is the first wheel containing
#:   ``stapel_attributes/translations/errors.{ru,es}.json`` — 0.9.2 and every
#:   release before it ship ``errors.py`` (thirteen registered codes) and no
#:   ``translations/`` at all;
#: * ``stapel-core`` 0.60.8 is the first loader whose ``catalog_search_dirs()``
#:   includes the package directory of every registered error owner.
#:   stapel-attributes is an embedded library with no Django app, so on every
#:   earlier core its catalogue is invisible however complete it is.
#:
#: Adding a library whose codes this module re-exports means adding it here.
CATALOGUE_FLOORS = {
    "stapel-attributes": (0, 9, 3),
    "stapel-core": (0, 60, 8),
}


def _registry():
    """``{code: owning package}`` for everything this module can return.

    The live registry of the test instance PLUS the committed
    ``docs/errors.json``. The artifact is the wider of the two and it is the
    one a consumer reads: the contract emission harness mounts core surfaces
    this instance does not, and those codes are in the denominator of every
    coverage report built from the wheel.
    """
    import stapel_categories.errors  # noqa: F401  (forces the attributes import)
    from stapel_core.django.api.errors import error_owners

    owners = dict(error_owners())
    for entry in json.loads((REPO / "docs" / "errors.json").read_text()):
        owners.setdefault(entry["code"], entry.get("owner"))
    return owners


def _borrowed():
    """``{code: owner}`` for the codes another package owns and this one returns."""
    return {
        code: owner
        for code, owner in _registry().items()
        if owner and owner != THIS_PACKAGE
    }


def _declared_floors():
    """``{distribution: (major, minor, patch)}`` from this package's manifest."""
    with open(REPO / "pyproject.toml", "rb") as handle:
        requirements = tomllib.load(handle)["project"]["dependencies"]
    floors = {}
    for spec in requirements:
        match = re.match(r"^([A-Za-z0-9._-]+)\s*>=\s*(\d+)\.(\d+)\.(\d+)", spec)
        if match:
            floors[match.group(1)] = tuple(int(part) for part in match.groups()[1:])
    return floors


def test_every_borrowed_code_has_a_string_in_every_rendered_language():
    """The borrowed half of the registry, resolved on the host's read path.

    ``load_app_catalogs`` is what renders an error message in a deployment:
    owner packages first, then INSTALLED_APPS in order, then
    ``EXTRA_CATALOG_DIRS``, merged later-wins. A code present here with no
    entry in the merged catalogue is a code that renders its English floor on
    a Russian screen.
    """
    from stapel_core.i18n.catalogs import load_app_catalogs

    borrowed = _borrowed()
    assert borrowed, "no borrowed codes resolved — the registry came back empty"

    for lang in TARGET_LANGUAGES:
        catalog = load_app_catalogs("errors", lang)
        missing = sorted(code for code in borrowed if code not in catalog)
        assert not missing, (
            f"{lang}: {len(missing)} code(s) this module returns have no "
            f"string — they render the English floor: "
            + ", ".join(f"{code} (owner: {borrowed[code]})" for code in missing[:10])
        )


@pytest.mark.parametrize("distribution", sorted(CATALOGUE_FLOORS))
def test_the_floor_of_every_upstream_error_owner_ships_those_strings(distribution):
    """A host must not be able to resolve a version where the strings are absent.

    The test above passes on whatever pip installed, which is the newest thing
    the range allows. This one is about the OTHER end of the range: the
    resolution a lockfile is entitled to produce.
    """
    declared = _declared_floors().get(distribution)
    required = CATALOGUE_FLOORS[distribution]
    assert declared is not None, (
        f"{distribution} is not a declared dependency of this package, but this "
        f"module re-exports codes it owns"
    )
    assert declared >= required, (
        f"pyproject declares {distribution}>="
        + ".".join(str(part) for part in declared)
        + ", which admits releases that do not make the strings for the codes "
        "this module re-exports reachable; the floor must be >="
        + ".".join(str(part) for part in required)
    )


def test_the_installed_upstream_owners_actually_ship_their_catalogues():
    """Named per owner, so a regression says which wheel stopped shipping.

    The coverage test reports a missing string; this one reports a missing
    catalogue file, which is the shape the defect actually had.
    """
    from stapel_core.i18n.catalogs import owner_catalog

    borrowed = _borrowed()
    assert "stapel_attributes" in set(borrowed.values()), (
        "the attributes registration no longer runs — errors.py must keep "
        "importing stapel_attributes.errors, or the validate-configs endpoint "
        "answers with codes no consumer has declared"
    )
    for owner in sorted(set(borrowed.values())):
        owned = {code for code, pkg in borrowed.items() if pkg == owner}
        for lang in TARGET_LANGUAGES:
            catalog = owner_catalog(owner, "errors", lang)
            missing = sorted(owned - set(catalog))
            assert not missing, (
                f"{owner} owns {len(owned)} code(s) this module can return and "
                f"its installed wheel ships no {lang} string for {len(missing)} "
                f"of them: {missing[:8]}"
            )


def test_this_module_ships_no_foreign_key():
    """Coverage must NOT have been bought by copying somebody else's strings.

    Core's gate: a key this package does not own, translated here while the
    owner already ships that language, is an ``error``-level ``foreign``
    issue — the duplication that had five libraries each maintaining the same
    41 core keys. The fix for the gap the other tests measure is the
    dependency floor, not a copy, and this is what keeps it that way.

    Only ``foreign`` is asserted on. The same call also reports this module's
    own nine keys as ``missing`` — it ships no catalogue — and that is the
    separate, honest gap named in the module docstring, not something this
    release closes.
    """
    from stapel_core.i18n import check_translation_catalogs, source_texts

    issues = check_translation_catalogs(
        "errors", TRANSLATIONS,
        source_texts=source_texts("errors"),
        languages=LANGUAGES,
        owner=THIS_PACKAGE,
    )
    foreign = [issue for issue in issues if issue.code == "foreign"]
    assert not foreign, "\n".join(
        f"[{issue.code}/{issue.language}] {issue.message}" for issue in foreign
    )


def test_a_catalogue_this_module_ships_covers_every_key_it_owns():
    """The trip-wire for the day this package starts translating its own keys.

    Today there is no ``translations/`` directory, so this asserts nothing —
    deliberately: a package that ships no catalogue has made no translation
    claim to break. The moment one appears, every key this module owns has to
    be in it, and it has to be packaged, or the new catalogue would ship a
    locale that covers some screens and not others.
    """
    if not TRANSLATIONS.is_dir():
        return

    from stapel_core.i18n.catalogs import load_catalog_file

    owned = {code for code, owner in _registry().items() if owner == THIS_PACKAGE}
    for lang in TARGET_LANGUAGES:
        path = TRANSLATIONS / f"errors.{lang}.json"
        if not path.is_file():
            continue
        catalog = load_catalog_file(path)
        missing = sorted(owned - set(catalog))
        assert not missing, (
            f"{lang}: this module ships a catalogue that misses "
            f"{len(missing)} key(s) it owns: {missing[:8]}"
        )

    with open(REPO / "pyproject.toml", "rb") as handle:
        patterns = tomllib.load(handle)["tool"]["setuptools"]["package-data"]
    assert "translations/*.json" in patterns[THIS_PACKAGE], (
        "the catalogue is not in package-data — a catalogue outside the wheel "
        "is a catalogue no deployment ever reads"
    )
