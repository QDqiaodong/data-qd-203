"""Domain data processing API; synthetic offline baseline."""
from .archive import Archive, ArchiveError, CrashSimulated
from .readback import (
    ReadbackError,
    load_continuation,
    plan_readback,
    save_continuation,
    save_order,
    settle_readback,
)
from .service import process

__all__ = [
    'process', 'Archive', 'ArchiveError', 'CrashSimulated',
    'plan_readback', 'settle_readback', 'save_continuation',
    'load_continuation', 'save_order', 'ReadbackError',
]
