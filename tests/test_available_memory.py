import sys
import pytest
from app.photos import fingerprint as photo_fingerprint


def test_available_memory_is_positive_and_below_physical_ram():
    import os
    value = photo_fingerprint.available_memory()
    assert isinstance(value, int) and value > 0
    if hasattr(os, 'sysconf') and 'SC_PHYS_PAGES' in os.sysconf_names:
        assert value <= os.sysconf('SC_PHYS_PAGES') * os.sysconf('SC_PAGE_SIZE')


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS sysctl counters')
def test_darwin_failure_is_reported_as_unavailable(monkeypatch):
    import ctypes
    class Broken:
        def sysctlbyname(self, *args):
            return -1
    monkeypatch.setattr(ctypes, 'CDLL', lambda *a, **k: Broken())
    with pytest.raises(ValueError, match='could not be checked'):
        photo_fingerprint.available_memory()


PAGE = 16384


@pytest.fixture
def counters(monkeypatch):
    """Deterministic macOS counters on any platform; arithmetic only, no live sysctl."""
    values = {}
    monkeypatch.setattr(photo_fingerprint, '_darwin_counter', values.__getitem__)
    monkeypatch.setattr(photo_fingerprint.os, 'sysconf', lambda name: PAGE, raising=False)
    return values


def test_darwin_does_not_double_count_speculative_pages(counters):
    # XNU includes speculative pages in the external pageable count.
    counters.update({'vm.page_free_count': 100, 'vm.page_speculative_count': 600,
                     'vm.page_purgeable_count': 0, 'vm.page_pageable_external_count': 700})
    assert photo_fingerprint._darwin_available_memory() == 800 * PAGE


def test_darwin_adds_free_purgeable_and_file_cache_pages(counters):
    counters.update({'vm.page_free_count': 10, 'vm.page_purgeable_count': 5,
                     'vm.page_pageable_external_count': 20})
    assert photo_fingerprint._darwin_available_memory() == 35 * PAGE
