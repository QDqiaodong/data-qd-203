"""Domain data processing API; synthetic offline baseline."""
from .alignment import AlignmentError, align_meter_facts
from .archive import Archive, ArchiveError, CrashSimulated
from .metering import (
    MeteringError,
    build_meter_facts,
    evidence_digest,
    validate_facts,
)
from .readback import (
    ReadbackError,
    load_continuation,
    plan_readback,
    save_continuation,
    save_order,
    settle_readback,
)
from .protection import (
    ProtectionError,
    SOURCE_OBSERVED,
    SOURCE_PROTECTED,
    SOURCE_UNKNOWN,
    build_protection_package,
    complete_frames,
    load_protection_package,
    save_protection_package,
)
from .service import process
from .session import SessionError, decode_session
from .streaming import (
    StreamingError,
    append,
    confirm,
    finish,
    load_state,
    new_state,
    progress,
    replace,
    save_state,
)

__all__ = [
    'process', 'decode_session', 'SessionError', 'Archive', 'ArchiveError', 'CrashSimulated',
    'plan_readback', 'settle_readback', 'save_continuation',
    'load_continuation', 'save_order', 'ReadbackError',
    'new_state', 'append', 'replace', 'confirm', 'finish', 'progress',
    'save_state', 'load_state', 'StreamingError',
    'build_meter_facts', 'evidence_digest', 'align_meter_facts',
    'MeteringError', 'AlignmentError', 'validate_facts',
    'build_protection_package', 'complete_frames',
    'load_protection_package', 'save_protection_package',
    'ProtectionError', 'SOURCE_OBSERVED', 'SOURCE_PROTECTED', 'SOURCE_UNKNOWN',
]
