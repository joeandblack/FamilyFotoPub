"""Conversion execution policy, separate from the pinned codec/output profile."""
import math

REVIEW_MESSAGE = ('MP4 created with fast verification; full playback verification was skipped. '
                  'Review playback in Videos. The original file is retained.')


def policy(config):
    settings = config.get('conversion_performance', {})
    rate = settings.get('io_mibps', 0)
    mode = settings.get('retained_original_validation', 'full')
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or not 0 <= rate <= 1024:
        raise ValueError('Conversion I/O limit must be between 0 and 1024 MiB/s; 0 means unlimited')
    if mode not in ('full', 'quick'):
        raise ValueError('Conversion validation must be full or quick')
    return {'io_mibps': rate, 'retained_original_validation': mode}
