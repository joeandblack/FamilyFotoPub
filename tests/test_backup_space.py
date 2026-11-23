from pathlib import Path
from types import SimpleNamespace

import pytest

from app.backup.space import (
    GIB, InsufficientSpace, Requirement, SpaceGuard, StorageUnavailable,
    catalog_requirements, directory_identity,
)


def guard(requirements, free, *, identities=None, **options):
    return SpaceGuard(requirements, minimum_reserve=10, reserve_percent=0,
                      identify=lambda path: (identities or {}).get(path.name, (1, 1)),
                      disk_usage=lambda path: SimpleNamespace(free=free[path.name]), **options)


def test_same_disk_combines_peak_requirements():
    item = guard([Requirement("destination", Path("dest"), 60), Requirement("temporary", Path("temp"), 40)],
                 {"dest": 100, "temp": 100})
    with pytest.raises(InsufficientSpace) as error:
        item.check()
    capacity, = error.value.capacities
    assert capacity.remaining_bytes == 100
    assert capacity.shortfall_bytes == 10
    assert capacity.roles == ("destination", "temporary")


def test_separate_disks_check_independently():
    item = guard([Requirement("destination", Path("dest"), 60), Requirement("temporary", Path("temp"), 40)],
                 {"dest": 100, "temp": 49}, identities={"dest": (1, 1), "temp": (2, 1)})
    with pytest.raises(InsufficientSpace) as error:
        item.check()
    assert [capacity.shortfall_bytes for capacity in error.value.capacities] == [0, 1]


def test_available_space_changes_during_copy():
    free = {"dest": 100}
    item = guard([Requirement("destination", Path("dest"), 80)], free)
    item.check()
    item.written("destination", 20)
    free["dest"] = 80
    assert item.check()[0].remaining_bytes == 60
    free["dest"] = 69  # Another process consumed space.
    with pytest.raises(InsufficientSpace):
        item.check(next_role="destination", next_bytes=10)


def test_underestimate_still_checks_next_write():
    free = {"dest": 20}
    item = guard([Requirement("destination", Path("dest"), 0)], free)
    item.check()
    with pytest.raises(InsufficientSpace):
        item.check(next_role="destination", next_bytes=11)


def test_default_headroom_uses_larger_of_one_gib_and_five_percent():
    item = SpaceGuard([Requirement("destination", Path("dest"), 100 * GIB)],
                     identify=lambda path: (1, 1), disk_usage=lambda path: SimpleNamespace(free=105 * GIB))
    assert item.check()[0].reserve_bytes == 5 * GIB
    item = SpaceGuard([Requirement("destination", Path("dest"), 10)],
                     identify=lambda path: (1, 1), disk_usage=lambda path: SimpleNamespace(free=GIB + 10))
    assert item.check()[0].reserve_bytes == GIB


def test_replacement_disk_rejected():
    identities = {"dest": (1, 1)}
    item = guard([Requirement("destination", Path("dest"), 10)], {"dest": 100}, identities=identities)
    identities["dest"] = (2, 1)
    with pytest.raises(StorageUnavailable):
        item.check()


def test_disconnection_rejected(tmp_path):
    directory = tmp_path / "destination"
    directory.mkdir()
    item = SpaceGuard([Requirement("destination", directory, 0)])
    directory.rmdir()
    with pytest.raises(StorageUnavailable):
        item.check()
    assert not directory.exists()  # No fallback mkdir on the system drive.


def test_missing_directory_rejected(tmp_path):
    with pytest.raises(StorageUnavailable):
        directory_identity(tmp_path / "missing")


@pytest.mark.parametrize("value", [-1, True, 1.5, "100"])
def test_invalid_estimates_rejected(value):
    with pytest.raises(ValueError):
        guard([Requirement("destination", Path("dest"), value)], {"dest": 100})


def test_duplicate_roles_rejected():
    with pytest.raises(ValueError):
        guard([Requirement("destination", Path("a"), 1), Requirement("destination", Path("b"), 1)], {})


def test_catalog_budget_accounts_for_staging_and_overhead():
    dest, temp = catalog_requirements("dest", "temp", state_bytes=10, database_budget_bytes=91)
    assert dest.remaining_bytes == 152 + 64 * 1024
    assert temp.remaining_bytes == 101


def test_no_rounding_loss_at_large_sizes():
    size = 2 ** 60 + 1
    dest, temp = catalog_requirements("dest", "temp", state_bytes=size, database_budget_bytes=0)
    assert dest.remaining_bytes == (size * 150 + 99) // 100 + 64 * 1024
    assert temp.remaining_bytes == size
