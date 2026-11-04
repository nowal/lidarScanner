"""Shared, bounded contract for staged context and provider photo reads."""
import stat
from pathlib import PurePosixPath

MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
MAX_UNPACKED_BYTES = 128 * 1024 * 1024
MAX_MEMBERS = 5000
MAX_MANIFEST_BYTES = 4 * 1024 * 1024


def validate(archive):
    members = archive.infolist()
    if len(members) > MAX_MEMBERS or sum(i.file_size for i in members) > MAX_UNPACKED_BYTES:
        raise ValueError('Context package exceeds image limits')
    names = set()
    for item in members:
        path = PurePosixPath(item.filename)
        if (path.is_absolute() or '..' in path.parts or '\\' in item.filename
                or item.filename in names or item.flag_bits & 1
                or stat.S_ISLNK(item.external_attr >> 16)):
            raise ValueError('Context package contains an unsafe member')
        if item.filename.endswith('.json') and item.file_size > MAX_MANIFEST_BYTES:
            raise ValueError('Context metadata exceeds image limits')
        names.add(item.filename)
    return members
