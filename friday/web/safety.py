"""
Guard rails for browser actions.

Friday may read anything, but an action that could spend money, send or
publish something, submit a form on a real account, or delete data must be
confirmed by the user first. :func:`needs_confirmation` decides; the caller
supplies a ``confirm(question) -> bool`` callback. Non-interactive runs (the
daemon) use :func:`deny`, so those steps stop and report instead of acting.
"""

import re
import sys
from collections.abc import Callable

Confirm = Callable[[str], bool]

_SENSITIVE_RE = re.compile(
    r"\b(buy|purchase|pay|payment|checkout|check out|place (?:your )?order|order now|complete order|"
    r"subscribe|donate|transfer|withdraw|send|post|publish|tweet|submit|confirm|apply now|book now|reserve|"
    r"delete|cancel (?:my )?(?:account|subscription|order|booking)|unsubscribe|sign up|register|"
    r"create account|authorize|approve|upload)\b",
    re.I,
)
_SEARCHY_RE = re.compile(r"\b(search|find|go|filter|look ?up|next|previous|more|load more|show more)\b", re.I)
_PAYMENT_FIELD_RE = re.compile(r"(cc-|\bcard|cvv|cvc|iban|account.?number|routing|ssn|social.?security)", re.I)


def is_sensitive_label(label: str) -> bool:
    """True when a button/link label reads like a committing action."""
    label = " ".join((label or "").split())
    if not label:
        return False
    return bool(_SENSITIVE_RE.search(label))


def is_payment_field(descriptor: str) -> bool:
    """True for card / bank / ID-number fields (by name, id, label or autocomplete)."""
    return bool(_PAYMENT_FIELD_RE.search(descriptor or ""))


def needs_confirmation(action: str, label: str = "", form: dict | None = None) -> str | None:
    """Return the reason an action must be confirmed, or None when it is safe.

    ``action`` is click / press / fill / submit; ``label`` describes the target
    element; ``form`` (when the target is inside one) has ``submit_labels``,
    ``has_password``, ``has_payment`` and ``is_search``.
    """
    form = form or {}
    if action == "fill":
        if is_payment_field(label):
            return f"type into a payment/ID field ({label.strip()[:60]})"
        return None
    if action == "click":
        if is_sensitive_label(label):
            return f"click “{label.strip()[:80]}”"
        if label.strip().lower() in ("ok", "yes", "continue") and (form.get("has_payment") or form.get("has_password")):
            return f"click “{label.strip()}” on a login/payment form"
        return None
    if action in ("press", "submit"):
        if action == "press" and not form:
            return None  # Enter outside any form (e.g. a JS search box)
        if form.get("is_search"):
            return None
        if form.get("has_payment"):
            return "submit a payment form"
        labels = [lbl for lbl in form.get("submit_labels") or [] if lbl.strip()]
        if any(is_sensitive_label(lbl) for lbl in labels):
            return f"submit the form (“{labels[0][:60]}”)"
        if labels and all(_SEARCHY_RE.search(lbl) for lbl in labels):
            return None
        return "submit a form on this site"
    return None


def deny(question: str) -> bool:
    return False


def allow(question: str) -> bool:
    return True


def ask_user(question: str) -> bool:
    """Ask on the terminal; refuse when nobody is there to answer."""
    if not sys.stdin or not sys.stdin.isatty():
        return False
    import typer

    return typer.confirm(f"  Friday wants to {question}. Go ahead?", default=False)


class ConfirmationRequired(Exception):
    """A committing action was refused or could not be confirmed."""
