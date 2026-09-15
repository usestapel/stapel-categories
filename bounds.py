"""Fitting editor input into bounded columns, derived from the model.

The feature editor takes a list of items whose ``feature`` is a raw dict — a
serializer normalises the SHAPE of the list, not the length of the strings
inside it — and writes those strings straight into ``CharField``s. Django
validates ``max_length`` in forms, never on a write, so a column bound
declared in a model is not a bound enforced on the path that writes it.
Postgres does not truncate: it raises ``StringDataRightTruncation``, the
transaction rolls back, and a catalogue edit answers 500.

The narrow columns are the dangerous ones, and they are not the obvious ones.
``name`` is 200 and nobody types 200 characters by accident, but
``visibility`` and ``translate`` are **10** and ``axis_role`` is **16** — one
mistyped value in an editor payload is enough.

Limits are read off the fields and never restated here, because a limit typed
twice makes the next ``max_length`` change a silent data-loss bug: the column
grows and the truncation does not, or it shrinks and the truncation does not.

Two functions, and the distinction between them is the whole point:

* :func:`fit` — prose. ``name``, ``comment``, ``icon``, ``example``,
  ``group``. Cut, with the cut MARKED, so a reader can tell "this is the whole
  value" from "this is the front of a longer one". A silent truncation is a
  lie about the data.
* :func:`choice_or_default` — a ``choices`` column. ``visibility``,
  ``translate`` and ``axis_role`` are VOCABULARIES, not prose: a value that is
  not in the vocabulary is not made to fit by cutting it, because
  ``"publiccccccc"[:10]`` is not a visibility and a truncated ``"restricted"``
  is not one either. It falls back to the field's own default, which is a
  value the rest of the module can reason about — and it does so for a value
  that is merely *wrong* as well as one that is too long, which is the case
  cutting could never have handled.

What is deliberately NOT fitted: ``slug``. It is the identity of the feature
within its parent and the key every edit is matched on; a cut one would
silently address a different row, or collide with one. An over-long slug is a
refusal, not a truncation, and that refusal belongs in the serializer.
"""
from __future__ import annotations

#: The marker that says "there was more". One character, so the arithmetic is
#: honest: Django's ``max_length`` and Postgres' ``varchar(n)`` both count
#: CHARACTERS, so a one-character marker costs exactly one character of budget
#: whatever it encodes to in bytes.
ELLIPSIS = "…"


def max_length_of(model, field_name: str):
    """The declared ``max_length`` of *field_name*, or ``None`` if unbounded."""
    return model._meta.get_field(field_name).max_length


def fit(model, field_name: str, value) -> str:
    """*value* as a string that fits *field_name*, with any cut marked.

    ``None`` becomes ``""`` — every one of these columns is either
    ``blank=True, default=""`` or required, and neither wants a literal
    ``"None"``. A field with no ``max_length`` (a ``TextField``) is returned
    whole, so this is safe to call across a model without knowing which of its
    fields are bounded.
    """
    text = "" if value is None else str(value)
    limit = max_length_of(model, field_name)
    if limit is None or len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1] + ELLIPSIS


def choice_or_default(model, field_name: str, value):
    """*value* if it is one of *field_name*'s choices, else the field default.

    A vocabulary column is not made to fit by cutting. This catches the
    over-long value the column could not hold AND the merely-wrong one it
    could, which is why it is not simply a length check with a different name.
    """
    field = model._meta.get_field(field_name)
    allowed = {str(choice) for choice, _label in (field.choices or [])}
    text = "" if value is None else str(value)
    if not allowed or text in allowed:
        return text
    default = field.get_default()
    return "" if default is None else default


__all__ = ["ELLIPSIS", "max_length_of", "fit", "choice_or_default"]
