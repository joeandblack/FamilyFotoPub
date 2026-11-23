"""Standard and Fast both require full verification."""
from .diagnostics import ExplainedFailure


def capture_strategy(config, payload):
    strategy = payload.get('copy_strategy', 'standard')
    if strategy not in ('standard', 'fast-full'):
        raise ExplainedFailure('Unknown backup copy strategy')
    if payload.get('verification', 'full') != 'full':
        raise ExplainedFailure('Full verification is required')
    if strategy != 'standard' and (config.get('backup') or {}).get('experimental_fastbackup') is not True:
        raise ExplainedFailure('Fast backup is not enabled for this installation')
    return strategy
