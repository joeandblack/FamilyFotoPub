"""Conservative capacity admission for backup writers; never reserves OS space.

    Other applications can consume capacity after a check, so callers must also
    handle ENOSPC/EDQUOT from write and fsync and never publish incomplete output.
"""
from dataclasses import asdict, dataclass
import os
from pathlib import Path
import shutil


GIB = 1024 ** 3


def byte_count(value: int) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("Byte estimates must be non-negative integers")
    return value


@dataclass(frozen=True)
class Requirement:
    role: str
    directory: Path
    remaining_bytes: int


@dataclass(frozen=True)
class Capacity:
    roles: tuple[str, ...]
    free_bytes: int
    remaining_bytes: int
    reserve_bytes: int

    @property
    def shortfall_bytes(self):
        return max(0, self.remaining_bytes + self.reserve_bytes - self.free_bytes)

    def report(self):
        return {**asdict(self), "shortfall_bytes": self.shortfall_bytes}


class InsufficientSpace(OSError):
    def __init__(self, capacities):
        self.capacities = tuple(capacities)
        super().__init__("Not enough disk space; preserve unfinished work and retry after freeing space")


class StorageUnavailable(OSError):
    pass


def directory_identity(path: Path) -> tuple[int, int]:
    """Same-process identity only; a resumed job also needs a durable volume marker."""
    path = Path(os.path.abspath(path))
    try:
        for parent in (*reversed(path.parents), path):
            if parent.is_symlink() or parent.is_junction():
                raise StorageUnavailable("Linked backup directories are not supported")
        if not path.is_dir():
            raise StorageUnavailable("Backup directory is unavailable")
        info = path.stat()
        return info.st_dev, info.st_ino
    except OSError as error:
        raise StorageUnavailable("Cannot identify backup directory") from error


class SpaceGuard:
    """Track peak remaining allocation by filesystem, not by directory.

    Estimates include simultaneously present temporary copies. They must not assume
    compression/dedup savings that haven't been measured. Not thread-safe: one job
    owns a guard; parallel jobs require admission/reservations above this layer.
    """
    def __init__(self, requirements, *, minimum_reserve=GIB, reserve_percent=5,
                 identify=directory_identity, disk_usage=shutil.disk_usage):
        byte_count(minimum_reserve)
        if type(reserve_percent) is not int or not 0 <= reserve_percent <= 100:
            raise ValueError("Reserve percentage must be an integer between 0 and 100")
        self.identify = identify
        self.disk_usage = disk_usage
        self.requirements = {}
        self.identities = {}
        self.remaining = {}
        self.reserves = {}
        for requirement in requirements:
            if not requirement.role or requirement.role in self.requirements:
                raise ValueError("Unique storage roles are required")
            byte_count(requirement.remaining_bytes)
            path = Path(os.path.abspath(requirement.directory))
            self.requirements[requirement.role] = Requirement(requirement.role, path, requirement.remaining_bytes)
            self.identities[requirement.role] = identify(path)
            self.remaining[requirement.role] = requirement.remaining_bytes
        if not self.requirements:
            raise ValueError("At least one storage requirement is required")
        for role, identity in self.identities.items():
            volume = identity[0]
            total = sum(self.remaining[name] for name, item in self.identities.items() if item[0] == volume)
            self.reserves[volume] = max(minimum_reserve, (total * reserve_percent + 99) // 100)

    def check(self, *, next_role=None, next_bytes=0):
        """Check at preflight/resume and immediately before each bounded write."""
        byte_count(next_bytes)
        if next_role is not None and next_role not in self.remaining:
            raise ValueError("Unknown storage role")
        if next_bytes and next_role is None:
            raise ValueError("A write role is required")
        groups = {}
        for role, requirement in self.requirements.items():
            expected = self.identities[role]
            try:
                if self.identify(requirement.directory) != expected:
                    raise StorageUnavailable("Backup storage changed; verify the disk before resuming")
                free = self.disk_usage(requirement.directory).free
                if type(free) is not int or free < 0:
                    raise StorageUnavailable("Cannot determine free disk space")
            except OSError as error:
                raise StorageUnavailable("Backup storage is unavailable or changed") from error
            group = groups.setdefault(expected[0], {"roles": [], "free": free, "remaining": 0})
            group["roles"].append(role)
            group["free"] = min(group["free"], free)
            group["remaining"] += max(self.remaining[role], next_bytes if role == next_role else 0)
        capacities = [Capacity(tuple(item["roles"]), item["free"], item["remaining"], self.reserves[volume])
                      for volume, item in groups.items()]
        if any(item.shortfall_bytes for item in capacities):
            raise InsufficientSpace(capacities)
        return capacities

    def written(self, role: str, size: int):
        """Call after a successful allocation; free-space readings already include it."""
        byte_count(size)
        self.remaining[role] = max(0, self.remaining[role] - size)


def catalog_requirements(destination, temporary, *, state_bytes, database_budget_bytes):
    """Conservative provisional budgets, not a PostgreSQL dump-size guarantee.

    The caller supplies the database budget. Keep a full temporary dump/state copy
    and budget destination output at 150% plus 64 KiB for a completion envelope.
    This is a conservative envelope budget, not a compression assumption. Runtime
    checks remain required if the estimate proves low.
    """
    total = byte_count(state_bytes) + byte_count(database_budget_bytes)
    return [Requirement("destination", Path(destination), (total * 150 + 99) // 100 + 64 * 1024),
            Requirement("temporary", Path(temporary), total)]
