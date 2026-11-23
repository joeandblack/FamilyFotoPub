"""Standard and Fast both require full verification."""
from .diagnostics import ExplainedFailure


def capture_strategy(payload, *, default='fast-full'):
    strategy = payload.get('copy_strategy', default)
    if strategy not in ('standard', 'fast-full'):
        raise ExplainedFailure('Unknown backup copy strategy')
    if payload.get('verification', 'full') != 'full':
        raise ExplainedFailure('Full verification is required')
    return strategy
