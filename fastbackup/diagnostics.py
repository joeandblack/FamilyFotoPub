"""Safe operator diagnostics, shared by the CLI and bounded worker channel."""
import errno

from app.backup.diagnostics import ExplainedFailure, safe_categories
from app.backup.admission import MaintenanceBlocked
from app.backup.private_storage import UnprotectedDestination
from app.backup.space import InsufficientSpace


class CopyFailure(RuntimeError):
    def __init__(self, details):
        super().__init__('Media copy failed; retain the attempt and use a new destination')
        self.details = details


def describe(error):
    if isinstance(error, CopyFailure):
        return error.details
    result = dict(error_type=type(error).__name__, categories=safe_categories(error))
    if getattr(error, 'failure_marker_unavailable', False) is True:
        result['warning'] = 'Could not persist the failure marker; require full verification before trusting this copy.'
    if isinstance(error, ExplainedFailure):
        result['message'] = str(error)
    else:
        for kind, message in (
            (UnprotectedDestination, 'Destination cannot enforce privacy; use NTFS/APFS or explicitly accept exposure.'),
            (InsufficientSpace, 'Not enough free space including the safety reserve.'),
            (MaintenanceBlocked, 'Maintenance admission blocked; inspect the existing maintenance state before retrying.'),
            (FileExistsError, 'Destination already exists; choose a new directory. Existing attempts are retained.'),
            (TimeoutError, 'Operation timed out; incomplete attempt retained.'),
        ):
            if isinstance(error, kind):
                result['message'] = message
                break
    if isinstance(error, OSError):
        for field in ('errno', 'winerror'):
            value = getattr(error, field, None)
            if type(value) is int:
                result[field] = value
        category = {errno.ENOSPC: 'disk-full', errno.EDQUOT: 'disk-full',
                    errno.EACCES: 'permission-denied', errno.EPERM: 'permission-denied'}.get(error.errno)
        if category:
            result['categories'] = sorted(set(result['categories']) | {category})
    result.setdefault('message', 'Operation failed; retain the attempt and inspect the error category and code.')
    return result
