"""Explicit state classification; unknown state must block recovery preparation."""
from dataclasses import dataclass
import re


class UnclassifiedState(ValueError):
    pass


@dataclass(frozen=True)
class Treatment:
    category: str
    restore: str
    reason: str


CATALOG = Treatment(
    "authoritative", "restore before admitting writes",
    "Catalog identity, relationships, decisions and durable work must be preserved.",
)
SECRET = Treatment(
    "authoritative", "encrypted export; rebind machine paths before activation",
    "Settings and certificate authority material are required for recovery.",
)
CACHE = Treatment(
    "rebuildable", "rebuild on demand",
    "Derived images can be regenerated from retained source media.",
)
CONTROL = Treatment(
    "transient", "never replay automatically",
    "Process ownership and run commands do not survive a machine restore.",
)
DIAGNOSTIC = Treatment(
    "excluded", "not required for catalog recovery",
    "Diagnostic logs are not authoritative application state.",
)
DATABASE_FILES = Treatment(
    "excluded", "restored from the database dump",
    "These are the running database's own files. Only its dump is consistent and restorable.",
)
INSTALLER_STATE = Treatment(
    "excluded", "kept by the destination installer",
    "Setup journals and the install lock belong to the Windows installation.",
)
TEMPLATE = Treatment(
    "excluded", "shipped with the application",
    "The settings template in every release is not this installation's state.",
)

# Keep this list independent of the SQL files: deriving an allowlist from the
# schema would silently approve newly added state without a recovery decision.
CORE_TABLES = frozenset("""
schema_versions jobs media import_items job_events video_metadata media_adjustments
media_trash upload_sessions upload_files albums album_items face_models face_runs
face_preferences face_work face_analyses face_detections face_review_events
people_models people_runs people_work people_embeddings people people_assignments
people_constraints people_suggestions people_revision people_previews people_events
people_lifecycle_events face_redetect_targets face_ignore_events photo_duplicate_trash
photo_fingerprints photo_duplicate_items photo_duplicate_decisions auth_sessions
auth_login_limits album_collections album_collection_members album_collection_people
app_settings
""".split())
PEOPLE_TABLES = frozenset("""
auto_people_face_quality auto_people_scan_results auto_people_groups auto_people_members
auto_people_observations auto_people_blocks auto_people_receipts auto_people_events
native_matching_policies native_catalog_state native_preserved_photos
native_migration_faces native_migration_photos
""".split())
# The one People catalog (docs/PEOPLE.md). It holds no application tables.
CATALOG_TABLES = frozenset("""
catalog_versions detector space certificate policy catalog_state face_runs face_work
face_analyses face_detections face_review_events face_redetect_targets face_ignore_events
people_runs people_work embedding people people_assignments people_constraints
people_suggestions people_revision people_previews people_events people_lifecycle_events
auto_people_face_quality auto_people_scan_results auto_people_groups auto_people_members
auto_people_observations auto_people_blocks auto_people_receipts auto_people_events
native_preserved_photos native_migration_faces face_reference_evidence
""".split())


def schema_kind(name: str, core_schema: str = "public") -> str:
    if name == core_schema:
        return "core"
    if name == "people":
        return "catalog"
    if re.fullmatch(r"(?:face_native|auto_eval)_[0-9a-f]{32}", name):
        return "people"
    raise UnclassifiedState(f"Unclassified database schema: {name}")


def classify_table(schema: str, table: str, core_schema: str = "public") -> Treatment:
    kind = schema_kind(schema, core_schema)
    allowed = {"people": CORE_TABLES | PEOPLE_TABLES, "catalog": CATALOG_TABLES}.get(kind, CORE_TABLES)
    if table not in allowed:
        raise UnclassifiedState(f"Unclassified database table: {schema}.{table}")
    if table in {"auth_sessions", "auth_login_limits", "people_previews"}:
        return Treatment("authoritative", "preserve in encrypted dump; invalidate before activation",
                         "Authentication sessions and short-lived edit tokens must not be replayed.")
    return CATALOG


TOKEN = r"[0-9a-f]{32}"
PROFILE = rf"(?:face_detect/profiles|compreface/raw-local)/{TOKEN}"
CALIBRATION_RUN = r"evaluation-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}"
UPLOAD_SESSION = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
WORKER_STATUSES = ('import-status.json', 'pipeline-status.json', 'video-status.json',
                   'avi-status.json', 'photo-duplicate-status.json')
PAUSE_MARKERS = ('PAUSE', 'PIPELINE-PAUSE', 'VIDEO-PAUSE', 'AVI-PAUSE', 'PHOTO-DUPLICATE-PAUSE')
FILE_RULES = (
    ("data", r"\.recovery-admission/library\.json", CATALOG),
    ("data", r"\.recovery-admission/(?:copy\.lock|maintenance\.json|retired\.json|[0-9a-f]{32}/copy\.lock)", CONTROL),
    ("config", r"local\.json", SECRET),
    ("config", r"example\.json", TEMPLATE),
    ("config", r"tls/(?:ca|ca-key|server|server-key)\.pem", SECRET),
    # Held while settings are saved (app/config_lock.py); it carries no data.
    ("config", r"local\.json\.lock", CONTROL),
    # Left by the read-only and limited-edit previews. A recovered library has no use for them.
    ("config", r"(?:read-only-preview|limited-edit-preview)\.json|readonly-canary\.txt", CONTROL),
    ("data", r"people-preview\.json", SECRET),
    ("data", r"face-service-settings\.json", SECRET),
    ("data", rf"{PROFILE}/runtime\.json", SECRET),
    ("data", rf"{PROFILE}/calibration/(?:identity-equivalences|latest-evaluation|activation)\.json", CATALOG),
    ("data", rf"{PROFILE}/calibration/{CALIBRATION_RUN}/(?:evaluation|labeled-inputs)\.json", CATALOG),
    ("data", r"conversion-recovery/[1-9][0-9]*/retain-original\.json", CATALOG),
    ("data", rf"uploads/{UPLOAD_SESSION}/(?:0|[1-9][0-9]*)\.[a-z0-9]{{1,12}}", CATALOG),
    ("data", '(?:' + '|'.join(re.escape(name) for name in WORKER_STATUSES) + ')', CATALOG),
    ("data", r"(?:avi|flv|mts)-job\.json", SECRET),
    ("data", r"conversion-temp/[1-9][0-9]*/(?:input\.(?:avi|flv|mts)|output\.mp4|previous-[0-9]+\.mp4|(?:flv|mts)-plan\.json|frame-validation\.json)", CATALOG),
    ("data", rf"{PROFILE}/jobs/[^/]+/(?:raw|import|checkpoint)\.json", CATALOG),
    ("data", rf"{PROFILE}/jobs/[^/]+/(?:control|process)\.json", CONTROL),
    # Every server stop leaves this, so a plan made while the server runs can't see it.
    ("data", rf"{PROFILE}/DESKTOP-PREVIEW-STOP", CONTROL),
    ("data", rf"{PROFILE}/thumbnails/[^/]+\.(?:jpg|jpeg|png|webp)", CACHE),
    ("data", r"people/thumbnails/[^/]+\.(?:jpg|jpeg|png|webp)", CACHE),
    # A thumbnail write cut short leaves its private temporary file; it's only a cache.
    ("data", r"people/thumbnails/\.[1-9][0-9]*\.[0-9a-f]{32}\.tmp", CACHE),
    ("data", rf"{PROFILE}/desktop-worker\.log(?:\.[0-9]+)?", DIAGNOSTIC),
    ("data", r"thumbnails/(?:[^/]+/)*[^/]+\.(?:jpg|jpeg|png|webp)", CACHE),
    ("data", r"previews/(?:[^/]+/)*[^/]+\.(?:jpg|jpeg|png|webp)", CACHE),
    ("data", '(?:' + '|'.join(PAUSE_MARKERS) + ')', CONTROL),
    ("data", r"monitor\.json", CONTROL),
    ("data", r"performance\.json", DIAGNOSTIC),
    ("data", r"thumbnail-repair-[^/]+\.json", DIAGNOSTIC),
    ("data", r"(?:recovery-audit|heif-repair|thumbnail-migration|video-sample-review)\.json", CATALOG),
    ("data", r"(?:DESKTOP-SERVER-STOP|foreground-activity|import-handoff\.json|video-decoder-busy\.json)", CONTROL),
    ("data", r"post-import-review\.json", CATALOG),
    ("data", r"logs/(?:[^/]+/)*[^/]+\.log(?:\.[0-9]+)?", DIAGNOSTIC),
    ("data", rf"{PROFILE}/desktop-server\.log(?:\.[0-9]+)?", DIAGNOSTIC),
    ("data", r"reports/[^/]+/[^/]+\.json", DIAGNOSTIC),
    ("data", r"video-handoff\.json", CONTROL),
    ("data", r"setup/install(?:\.lock|\.json(?:\.new|\.previous)?)", INSTALLER_STATE),
    ("data", r"postgres/.+", DATABASE_FILES),
)

DIRECTORY_RULES = {
    "config": (r"tls",),
    "data": (
        r"\.recovery-admission", r"\.recovery-admission/[0-9a-f]{32}",
        r"face_detect", r"face_detect/profiles", r"compreface", r"compreface/raw-local",
        PROFILE, rf"{PROFILE}/jobs", rf"{PROFILE}/jobs/[^/]+",
        rf"{PROFILE}/calibration", rf"{PROFILE}/calibration/{CALIBRATION_RUN}",
        r"conversion-recovery", r"conversion-recovery/[1-9][0-9]*",
        r"uploads", rf"uploads/{UPLOAD_SESSION}",
        r"conversion-temp", r"conversion-temp/[1-9][0-9]*", r"previews(?:/[^/]+)*",
        rf"{PROFILE}/thumbnails", r"people", r"people/thumbnails",
        r"thumbnails(?:/[^/]+)*", r"logs(?:/[^/]+)*",
        r"reports", r"reports/[^/]+", r"setup", r"postgres(?:/.+)?",
    ),
}


def check_directory(root: str, relative: str) -> None:
    if not any(re.fullmatch(pattern, relative) for pattern in DIRECTORY_RULES.get(root, ())):
        raise UnclassifiedState(f"Unclassified state directory: {root}/{relative}")


def classify_file(root: str, relative: str) -> Treatment:
    if root not in {"config", "data"}:
        raise UnclassifiedState(f"Unclassified state root: {root}")
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts) or "\\" in relative or ":" in relative:
        raise UnclassifiedState("Unsafe relative state path")
    for rule_root, pattern, treatment in FILE_RULES:
        if root == rule_root and re.fullmatch(pattern, relative):
            return treatment
    raise UnclassifiedState(f"Unclassified state file: {root}/{relative}")
