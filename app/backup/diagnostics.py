"""Bounded tool-output classification; never retain or publish raw stderr."""
import threading

PATTERNS = {
    'disk-full': (b'no space left', b'disk full', b'not enough space', b'disk quota exceeded'),
    'permission-denied': (b'permission denied', b'access is denied', b'not permitted', b'must be owner'),
    'version-mismatch': (b'version mismatch', b'unsupported version', b'server version'),
    'connection-failed': (b'connection refused', b'could not connect', b'connection timed out', b'authentication failed'),
    'source-changed': (b'file changed', b'state changed', b'no such file', b'cannot find the file'),
}
CATEGORIES = frozenset({*PATTERNS, 'unclassified-tool-output', 'diagnostic-read-failed'})


class ProcessDiagnostics:
    def __init__(self):
        self.has_output = False
        self.categories = set()
        self.lock = threading.Lock()

    def drain(self, stream):
        tail = b''
        try:
            while chunk := stream.read(65536):
                self.has_output = True
                text = tail + chunk.lower()
                for category, patterns in PATTERNS.items():
                    if any(pattern in text for pattern in patterns):
                        with self.lock:
                            self.categories.add(category)
                tail = text[-128:]
        except OSError:
            self.has_output = True
            with self.lock:
                self.categories.add('diagnostic-read-failed')

    def failure(self, exception_type, message):
        error = exception_type(message)
        with self.lock:
            error.diagnostic_categories = sorted(self.categories or {'unclassified-tool-output'})
        return error


class ExplainedFailure(ValueError):
    """A failure we can explain. Its message is ours, with no paths or tool output, so it can be shown."""


def safe_categories(error):
    values = getattr(error, 'diagnostic_categories', ())
    if not isinstance(values, (list, tuple, set)):
        return []
    return sorted({value for value in values if isinstance(value, str) and value in CATEGORIES})
