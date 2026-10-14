"""Exceptions raised by stateledger."""


class LedgerError(Exception):
    """Base class for all stateledger errors."""


class ValidationError(LedgerError):
    """An argument failed validation (empty statement, bad confidence...)."""


class UnknownEvidenceError(LedgerError):
    """Referenced an evidence id that is not in the ledger."""


class UnknownConclusionError(LedgerError):
    """Referenced a conclusion id that is not in the ledger."""


class InvalidTransitionError(LedgerError):
    """A lifecycle transition that the conclusion's status forbids,
    e.g. superseding or retracting a conclusion that is not active."""
