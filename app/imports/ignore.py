"""Case-insensitive filename exclusions and explicit metadata-directory rules."""
from fnmatch import fnmatchcase
import re
from pathlib import PureWindowsPath
from mimetypes import MimeTypes
from ..library.media import IMAGES, VIDEOS, AUDIO

IGNORE_PATTERNS = ('*.moff', '*.modd', '*.thm', '*.aae', '*.db', '*.info', '*.htm', '*.html', '*.ini', '*.vcd', '*.txt', '*.log', '*.bup', '*.ifo', '*.zip', '*.gz', '*.mp2', '*.dvd', '*.ses', '*.spu', '*.xmp', '*.pages', '*.numbers', '*.dmg', '*.lrf', '*.srt', '*.asf', '*.comicdoc', '*.doc', '*.docx', '*.isf', '*.lnk', '*.cb', '*.cnb', '.*')
IGNORE_PATTERNS += ('*.bak', '*.kmproject', '*.vcf', '*.prin', '*.prproj')
# Recognized documents/text are non-media, not unsupported photo/video formats.
# Use the standard library's built-in map, not machine-specific registry mappings.
# PDFs can contain photos; leave them for user review rather than ignoring them.
_nonmedia_types = {'application/json', 'application/rtf',
                   'application/msword', 'application/zip', 'application/gzip'}
_supported = IMAGES | VIDEOS | AUDIO | {'.ts', '.m3u', '.m3u8'}
_mime_maps = MimeTypes(filenames=()).types_map
AUTO_IGNORE_PATTERNS = tuple(sorted('*'+extension for extension,mime in {**_mime_maps[0], **_mime_maps[1]}.items()
    if extension not in _supported and re.fullmatch(r'\.[a-zA-Z0-9_-]+',extension)
    and (mime.startswith('text/') or mime in _nonmedia_types
         or mime.startswith('application/vnd.openxmlformats-officedocument.')
         or mime.startswith('application/vnd.oasis.opendocument.'))))
IGNORE_PATTERNS += tuple(p for p in AUTO_IGNORE_PATTERNS if p not in IGNORE_PATTERNS)
# Explicit extensionless rule, never an empty glob or wildcard-all.
IGNORE_NO_EXTENSION = True
IGNORE_DIRECTORIES = ('_vti_cnf',)
IMPORT15_EXTENSIONS = ('ithmb','jpgz','gifx','ais','url','jpgr','jpge','jpgs','jpgg','jpgii','jpgd','jpgj','jpgl','jpgss','jpgff','jpgvv','jpgf','jpgx','jpgaa','jpga','jpgw','jpgk','jpgyy','jpgu','jpgzzz','jpgc','tof','ion','!!!','thumbnail','avif')
IGNORE_PATTERNS += tuple('*.' + extension for extension in IMPORT15_EXTENSIONS)
# Generate the PostgreSQL suffix predicate from the same extension list.
_extensions = [re.escape(pattern[2:]) for pattern in IGNORE_PATTERNS if pattern.startswith('*.')]
IGNORE_SQL_PATTERN = r'(?i)(\.(' + '|'.join(_extensions) + r')$|(^|[/\\])\.[^/\\]*$|(^|[/\\])[^./\\]+$|(^|[/\\])[^/\\]*\.$)'
# Temporary-save suffixes contain generated tokens, not a media extension.
_TEMP_SAVE = r'\.sb-[a-z0-9]+-[a-z0-9]+$'
IGNORE_SQL_PATTERN = IGNORE_SQL_PATTERN[:-1] + '|' + _TEMP_SAVE + ')'
IGNORE_SQL_PATTERN = IGNORE_SQL_PATTERN[:-1] + r'|(^|[/\\])_vti_cnf[/\\])'


def ignored_directory(path):
    return any(part.casefold() in IGNORE_DIRECTORIES for part in PureWindowsPath(path).parts)


def should_ignore(path):
    if ignored_directory(PureWindowsPath(path).parent):
        return True
    name=PureWindowsPath(path).name.casefold()
    return (IGNORE_NO_EXTENSION and bool(name) and not PureWindowsPath(name).suffix) or bool(re.search(_TEMP_SAVE,name)) or any(fnmatchcase(name,pattern.casefold()) for pattern in IGNORE_PATTERNS)
