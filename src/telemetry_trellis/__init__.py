"""Domain data processing API; synthetic offline baseline."""
from .archive import Archive, ArchiveError, CrashSimulated
from .service import process

__all__ = ['process', 'Archive', 'ArchiveError', 'CrashSimulated']
