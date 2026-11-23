"""Restore through the existing full-verification, inactive-target recovery path."""
from app.backup.library_restore import restore_library as _restore
from app.backup.library import verify_library, verify_library_thumbnails


def verify(directory, key, progress=None):
    payload = verify_library(directory, key, progress)
    verify_library_thumbnails(directory, key, payload)
    return payload


def restore_library(directory, key, target_directory, admin, pg_restore, *,
                    expected_cluster_directory, database_budget, progress=None):
    """Always copy into a new inactive target; never consume backup or activate."""
    return _restore(directory, key, target_directory, admin, pg_restore,
                    expected_cluster_directory=expected_cluster_directory,
                    database_budget=database_budget, progress=progress,
                    resume=False, in_place=False, validation_follows=False)
