"""Offline XOR protection packages for working-set telemetry frames.

The capture side keeps a catalogue of trusted XOR (parity) protection records
over payload bits; some frames are handed to an offline workstation as the
working set while the remaining "blind" frames never leave the capture side.
This module provides two device-free, standard-library-only entry points:

* :func:`build_protection_package` — project the catalogue onto the working
  frames and emit a canonical, JSON-lossless protection package;
* :func:`complete_frames` — in another process, combine a reloaded package
  with trusted local decode evidence and fill every payload bit that is forced
  across all allowed completions.

Projection semantics
--------------------
A record is an affine parity equation over GF(2), written as an augmented bit
row by appending one virtual right-hand-side column: ``xor(coordinates) = v``
becomes the coefficient bits plus the RHS bit when ``v`` is 1.  Bit columns
are ordered with every blind column below every working column, and the RHS
column highest of all.  One affine Gauss-Jordan RREF is computed with the RHS
column carried but never used as a pivot:

* a bare RHS residual (``0 = 1``) means the catalogue is inconsistent, and
  because blind columns pivot like any payload column this also rejects
  contradictions that live entirely on blind frames;
* the basis rows whose blind part is empty are exactly the equations in which
  every blind variable cancelled.  Re-reducing them in work coordinates gives
  the unique canonical RREF of the projected affine system.

A work assignment is therefore accepted iff some blind assignment makes it
satisfy every catalogue record; blind bits are never assumed zero (a mixed
record such as ``A xor H = 0`` alone yields no working constraint, because
``H`` can always be chosen to match).  The canonical reduced basis depends
only on the set of allowed working-bit assignments, so catalogue reordering,
duplicate records, derivable extra records or blind-only facts that the
working set can cancel all leave the package byte-identical.  Each constraint
row is serialized as one fixed-width lowercase hexadecimal string (work
coefficient bits followed by the RHS bit), which stays compact even when the
reduced rows are dense.

Completion never enumerates completions and never stores a full free-direction
matrix: the package rows and the observed-bit rows share one RREF; an
ascending pivot sweep marks a pivot bit fixed exactly when its row references
no free column.  Everything is plain JSON data; no authentication or tamper
resistance is claimed.
"""
from .checks import array, fields, ident, integer
from .io import load as _load_json
from .io import publish as _publish_json

KIND = 'telemetry_trellis.protection_package'
PROTECTION_VERSION = 1
MAX_FRAMES = 128
MAX_FRAME_PAYLOAD = 500
MAX_TOTAL_PAYLOAD = 32768
MAX_RECORDS = 512
MAX_RECORD_COORDS = 64

# Per-bit source categories on the completed frames.
SOURCE_OBSERVED = 'observed'
SOURCE_PROTECTED = 'protected'
SOURCE_UNKNOWN = 'unknown'


class ProtectionError(ValueError):
    """Raised for malformed catalogues/packages/evidence or contradictions."""


# --------------------------------------------------------------------- RREF
def _gauss_jordan_reduce(basis, row, pivot_mask):
    """Reduce one bit-mask row into a maintained full Gauss-Jordan basis.

    Only columns whose bits appear in ``pivot_mask`` may become pivots; every
    other column (e.g. the augmented RHS column) is carried along but never
    pivots.  The stored basis maps each pivot (a one-bit mask) to a reduced
    row whose lowest pivotable column is that pivot and which carries no other
    pivot column.

    Phase 1 walks the existing pivots in ascending order, XOR-ing a basis row
    in whenever the incoming row currently has that pivot.  A single pass
    suffices: a basis row's lowest pivotable bit is its own pivot, so the XOR
    clears that column and can never introduce a lower one.  The residual is
    zero for a dependent combination, a non-zero row without a pivotable
    column for an affine ``0 = 1`` inconsistency, or a row whose leading
    pivotable column introduces a new pivot.  Phase 2 XORs the fully reduced
    row into every existing row carrying that new pivot column; because the
    reduced row has no existing (lower) pivot column, every cleared row keeps
    its own pivot.  For a consistent system the result is the unique
    Gauss-Jordan RREF of the row space for the fixed column order,
    independent of insertion sequence.
    """
    current = row
    for pivot in sorted(basis):
        if current & pivot:
            current ^= basis[pivot]
    leading = current & pivot_mask
    if not leading:
        return current
    new_pivot = leading & -leading
    for existing_pivot in list(basis):
        if basis[existing_pivot] & new_pivot:
            basis[existing_pivot] ^= current
    basis[new_pivot] = current
    return 0


# ------------------------------------------------------------- catalogue input
def _validate_catalog(catalog):
    if not isinstance(catalog, dict):
        raise ProtectionError('protection catalog must be an object')
    fields(catalog, ('frames', 'selection', 'records'))

    raw_frames = array(catalog['frames'], 1, MAX_FRAMES)
    frames = []
    lengths = {}
    total_payload = 0
    for entry in raw_frames:
        if not isinstance(entry, dict):
            raise ProtectionError('each frame entry must be an object')
        fields(entry, ('session_id', 'frame_id', 'payload_length'))
        key = (ident(entry['session_id']), ident(entry['frame_id']))
        if key in lengths:
            raise ProtectionError(f'duplicate frame key: {list(key)}')
        length = integer(entry['payload_length'], 1, MAX_FRAME_PAYLOAD)
        total_payload += length
        if total_payload > MAX_TOTAL_PAYLOAD:
            raise ProtectionError(
                f'total payload exceeds {MAX_TOTAL_PAYLOAD} bits')
        lengths[key] = length
        frames.append((key, length))

    # A non-empty working set; every selected key must exist and be unique.
    raw_selection = array(catalog['selection'], 1, len(frames))
    selected = set()
    for pair in raw_selection:
        array(pair, 2, 2)
        key = (ident(pair[0]), ident(pair[1]))
        if key not in lengths:
            raise ProtectionError('selection references an unknown frame')
        if key in selected:
            raise ProtectionError('duplicate working-frame selection')
        selected.add(key)

    raw_records = array(catalog['records'], 0, MAX_RECORDS)
    records = []
    for entry in raw_records:
        if not isinstance(entry, dict):
            raise ProtectionError('each protection record must be an object')
        fields(entry, ('coordinates', 'value'))
        if type(entry['value']) is not int or entry['value'] not in (0, 1):
            raise ProtectionError(
                'record value must be a strict integer 0 or 1')
        raw_coords = array(entry['coordinates'], 1, MAX_RECORD_COORDS)
        coords = []
        seen = set()
        for triple in raw_coords:
            array(triple, 3, 3)
            key = (ident(triple[0]), ident(triple[1]))
            if key not in lengths:
                raise ProtectionError(
                    'record coordinate references an unknown frame')
            index = integer(triple[2], 0, lengths[key] - 1)
            if (key, index) in seen:
                raise ProtectionError(
                    'duplicate coordinate inside a protection record')
            seen.add((key, index))
            coords.append((key, index))
        records.append((coords, entry['value']))
    return frames, lengths, selected, records


def build_protection_package(catalog):
    """Build the canonical working-set protection package for one catalogue.

    The catalogue is plain JSON data::

        {
          "frames": [{"session_id", "frame_id", "payload_length"}, ...],
          "selection": [[session_id, frame_id], ...],
          "records": [{"coordinates": [[session_id, frame_id, index], ...],
                       "value": 0|1}, ...]
        }

    Frames (1..128, total payload <= 32768 bits) are keyed by the
    ``(session_id, frame_id)`` pair, so the same frame id may exist in several
    sessions.  ``selection`` is the non-empty working-frame set; duplicate
    frame keys or selections are rejected.  Each record (0..512) names 1..64
    distinct existing payload-bit coordinates and one strict integer RHS.
    Duplicate records are allowed; invalid coordinates, boolean values,
    malformed structures and mutually contradictory records (including
    contradictions confined to blind bits) raise :class:`ProtectionError`.

    The returned package contains only working frames and their projected
    constraints — no blind frame identity, coordinate, value or original mixed
    record.  Only the layout, selection and protection records are read; no
    frame payload or decode representative is consulted.
    """
    try:
        frames, lengths, selected, records = _validate_catalog(catalog)
    except ProtectionError:
        raise
    except ValueError as exc:
        # Shared check helpers raise plain ValueErrors; keep one error type.
        raise ProtectionError(str(exc)) from exc

    # Canonical bit numbering follows the catalogue-independent frame-key
    # order, then the original zero-based local index.  Working frames occupy
    # the leading columns and blind frames the trailing ones; which subset a
    # frame belongs to never changes its relative numbering.
    ordered_frames = sorted(frames)
    work_frames = [(key, lengths[key]) for key, _ in ordered_frames
                   if key in selected]
    blind_frames = [(key, length) for key, length in ordered_frames
                    if key not in selected]
    work_offset = {}
    cursor = 0
    for key, length in work_frames:
        work_offset[key] = cursor
        cursor += length
    work_count = cursor
    blind_offset = {}
    cursor = 0
    for key, length in blind_frames:
        blind_offset[key] = cursor
        cursor += length
    blind_count = cursor
    rhs_bit = blind_count + work_count

    # Build one augmented row per record.  Columns: blind payload bits in the
    # low positions, then working payload bits, then the single virtual RHS
    # column.  Blind bits are never assumed zero; they are real variables that
    # the capture side may choose to satisfy a record.
    row_masks = []
    for coords, value in records:
        mask = 0
        for key, index in coords:
            if key in work_offset:
                column = blind_count + work_offset[key] + index
            else:
                column = blind_offset[key] + index
            mask |= 1 << column
        if value:
            mask |= 1 << rhs_bit
        row_masks.append(mask)

    # One AFFINE Gauss-Jordan RREF over the payload columns, with the RHS
    # column carried but never used as a pivot.  A bare RHS residual is
    # ``0 = 1`` and means the catalogue has no satisfying assignment; because
    # blind columns pivot like every payload column, this also catches
    # contradictions confined entirely to blind frames.  The basis rows whose
    # blind part is empty are precisely the equations in which every blind
    # variable cancelled along elimination.  They describe exactly the
    # projection of the catalogue's feasible set onto the working bits — a
    # work assignment is feasible iff some blind assignment satisfies the
    # catalogue, and blind bits are never assumed zero.  Re-reducing those
    # rows in work coordinates yields the unique canonical RREF of the
    # projected affine system, independent of catalogue order, duplicates,
    # derivable records or blind-only facts that the working set can cancel.
    variable_mask = (1 << rhs_bit) - 1
    affine = {}
    for mask in row_masks:
        if _gauss_jordan_reduce(affine, mask, variable_mask):
            raise ProtectionError(
                'contradictory protection records: catalogue has no '
                'satisfying assignment')

    blind_mask = (1 << blind_count) - 1 if blind_count else 0
    work_pivot_mask = (1 << work_count) - 1
    projected = {}
    for row in affine.values():
        if row & blind_mask:
            continue
        shifted = row >> blind_count
        if not (shifted & work_pivot_mask):
            continue
        if _gauss_jordan_reduce(projected, shifted, work_pivot_mask):
            raise ProtectionError(
                'contradictory protection records: catalogue has no '
                'satisfying assignment')

    # Constraints are ordered by their pivot bit (the lowest set coefficient
    # column), which is unique across an RREF basis; sorting by the raw row
    # integer would instead move RHS-carrying rows out of pivot order.
    ordered_rows = sorted(projected.values(),
                          key=lambda row: (row & work_pivot_mask)
                          & -(row & work_pivot_mask))
    constraints = [
        {'row': _encode_row(row, work_count)}
        for row in ordered_rows
    ]

    return {
        'kind': KIND,
        'version': PROTECTION_VERSION,
        'frames': [
            {'session_id': key[0], 'frame_id': key[1],
             'payload_length': length}
            for key, length in work_frames
        ],
        'constraints': constraints,
    }


# ------------------------------------------------------------ package loading
def _validate_package(package):
    if not isinstance(package, dict):
        raise ProtectionError('protection package must be an object')
    fields(package, ('kind', 'version', 'frames', 'constraints'))
    if package['kind'] != KIND:
        raise ProtectionError('not a telemetry_trellis protection package')
    if type(package['version']) is not int or \
            package['version'] != PROTECTION_VERSION:
        raise ProtectionError('unsupported protection package version')

    raw_frames = array(package['frames'], 1, MAX_FRAMES)
    frames = []
    offsets = {}
    lengths = {}
    cursor = 0
    previous_key = None
    total_payload = 0
    for entry in raw_frames:
        if not isinstance(entry, dict):
            raise ProtectionError('each package frame must be an object')
        fields(entry, ('session_id', 'frame_id', 'payload_length'))
        key = (ident(entry['session_id']), ident(entry['frame_id']))
        if key in lengths:
            raise ProtectionError(f'duplicate package frame key: {list(key)}')
        if previous_key is not None and key <= previous_key:
            raise ProtectionError('package frames must be sorted by frame key')
        previous_key = key
        length = integer(entry['payload_length'], 1, MAX_FRAME_PAYLOAD)
        total_payload += length
        if total_payload > MAX_TOTAL_PAYLOAD:
            raise ProtectionError(
                f'total package payload exceeds {MAX_TOTAL_PAYLOAD} bits')
        offsets[key] = cursor
        lengths[key] = length
        frames.append((key, length))
        cursor += length
    work_count = cursor

    raw_constraints = array(package['constraints'], 0, MAX_RECORDS)
    rows = []
    previous_pivot = None
    work_pivot_mask = (1 << work_count) - 1
    rhs_only = 1 << work_count
    for entry in raw_constraints:
        if not isinstance(entry, dict):
            raise ProtectionError('each package constraint must be an object')
        fields(entry, ('row',))
        mask = _decode_row(entry['row'], work_count)
        pivot_mask = mask & work_pivot_mask
        pivot = (pivot_mask & -pivot_mask).bit_length() - 1
        if previous_pivot is not None and pivot <= previous_pivot:
            raise ProtectionError(
                'constraints must be sorted by ascending pivot bit')
        previous_pivot = pivot
        rows.append(mask)

    # Re-reduce every decoded row as an affine system.  The input is a valid
    # package only when the rows already are the unique RREF of their own row
    # space: no residual (0 = 1), every row survives unchanged and the pivot
    # columns stay unique.  This also rejects rows whose pivot column appears
    # in another row (not fully reduced).
    check = {}
    for mask in rows:
        residual = _gauss_jordan_reduce(check, mask, work_pivot_mask)
        if residual:
            raise ProtectionError(
                'package constraints are contradictory (0 = 1)')
    if sorted(check.values()) != sorted(rows) or len(check) != len(rows):
        raise ProtectionError(
            'package constraints are not in reduced row echelon form')
    constraints = [
        {'mask': mask, 'rhs': 1 if mask & rhs_only else 0}
        for mask in sorted(rows)
    ]

    return {
        'frames': frames,
        'offsets': offsets,
        'lengths': lengths,
        'work_count': work_count,
        'constraints': constraints,
    }


def load_protection_package(path):
    """Load and structurally validate a protection package JSON file.

    Returns the external JSON-shaped package (not an internal normalized
    view), so the result can be handed straight to :func:`complete_frames`
    in another process.
    """
    package = _load_json(path)
    _validate_package(package)
    return package


def save_protection_package(path, package):
    """Atomically write a protection package to a new JSON file.

    The target must not exist yet, mirroring the other atomic publishers.
    """
    _publish_json(path, _validate_package(package) and package)


# ------------------------------------------------------------------ evidence
def _validate_evidence(evidence, pkg):
    items = array(evidence, 0, len(pkg['frames']))
    seen = set()
    normalized = []
    for item in items:
        if not isinstance(item, dict):
            raise ProtectionError('each evidence item must be an object')
        fields(item, ('session_id', 'id', 'payload', 'ambiguous'))
        key = (ident(item['session_id']), ident(item['id']))
        if key not in pkg['offsets']:
            raise ProtectionError(
                f'evidence for frame not in package: {list(key)}')
        if key in seen:
            raise ProtectionError('duplicate evidence for one frame key')
        seen.add(key)
        length = pkg['lengths'][key]
        payload = array(item['payload'], length, length)
        for bit in payload:
            if type(bit) is not int or bit not in (0, 1):
                raise ProtectionError(
                    'evidence payload must contain strict 0/1 integers')
        if type(item['ambiguous']) is not bool:
            raise ProtectionError('evidence ambiguous flag must be boolean')
        normalized.append({
            'key': key,
            'payload': list(payload),
            'ambiguous': item['ambiguous'],
        })
    return normalized


def _row_width_hex(work_count):
    """Number of hex digits covering the work columns plus the RHS column."""
    return (work_count + 1 + 3) // 4


def _encode_row(mask, work_count):
    """Canonical fixed-width lowercase hex of one augmented RREF row.

    Bits 0..work_count-1 are the variable coefficients and bit
    ``work_count`` is the RHS; the string is zero-padded so every package
    row has one identical width, making the text order identical to the
    numeric row order.
    """
    return f'{mask:0{_row_width_hex(work_count)}x}'


def _decode_row(text, work_count):
    """Parse one canonical row string, rejecting any non-canonical form."""
    if not isinstance(text, str):
        raise ProtectionError('constraint row must be a hex string')
    width = _row_width_hex(work_count)
    if len(text) != width or any(ch not in '0123456789abcdef'
                                 for ch in text):
        raise ProtectionError(
            'constraint row must be fixed-width lowercase hexadecimal')
    mask = int(text, 16)
    if mask >> (work_count + 1):
        raise ProtectionError('constraint row sets bits beyond the RHS')
    if not (mask & ((1 << work_count) - 1)):
        raise ProtectionError('constraint row has no variable coefficient')
    return mask


def complete_frames(package, evidence):
    """Complete working frames from a protection package and local evidence.

    ``package`` is a package dict as returned by
    :func:`build_protection_package` or reloaded from its JSON form.
    ``evidence`` is a list of trusted items, each using the corresponding
    :func:`telemetry_trellis.decode_session` result's ``session_id`` plus the
    frame row's ``id``, ``payload`` and ``ambiguous``; items may come from
    different sessions and are paired solely by the ``(session_id, id)`` frame
    key, never by array position.  Payloads must match the package layout and
    contain strict 0/1 integers even for ambiguous frames.

    Only items with ``ambiguous=false`` contribute observed bits; a missing or
    ambiguous frame is treated as fully erased (no lexicographic representative
    is used).  Observed values are authoritative: when no completion satisfies
    both the observations and the constraints, :class:`ProtectionError` is
    raised for the whole batch without flipping observed bits or returning
    partial frames.  Every unknown bit taking the same value in all allowed
    completions is filled (source ``protected``); the rest stay ``null``
    (source ``unknown``).  Inputs are never modified.
    """
    pkg = _validate_package(package)
    try:
        items = _validate_evidence(evidence, pkg)
    except ProtectionError:
        raise
    except ValueError as exc:
        raise ProtectionError(str(exc)) from exc

    work_count = pkg['work_count']
    rhs_bit = work_count
    # Affine reduction: the RHS column never becomes a pivot, so a bare RHS
    # residual is exactly the ``0 = 1`` contradiction signal.
    work_pivot_mask = (1 << work_count) - 1
    basis = {}
    for constraint in pkg['constraints']:
        if _gauss_jordan_reduce(basis, constraint['mask'], work_pivot_mask):
            raise ProtectionError(
                'protection package contains contradictory constraints')

    # Seed directly observed bits.  Ambiguous frames contribute nothing even
    # though their representative payload still had to validate structurally.
    observed = {}
    for item in items:
        if item['ambiguous']:
            continue
        offset = pkg['offsets'][item['key']]
        for index, bit in enumerate(item['payload']):
            column = offset + index
            observed[column] = bit
            residual = _gauss_jordan_reduce(
                basis, (1 << column) | (bit << rhs_bit), work_pivot_mask)
            if residual:
                raise ProtectionError(
                    'observed evidence contradicts the protection constraints')

    # Full RREF: every non-pivot work column is free.  Sweep pivot rows from
    # the lowest pivot upwards, since a reduced row references only free
    # columns and pivots strictly below its own; when a row is resolved every
    # referenced pivot has already been resolved.  A row touching any free
    # column leaves its own pivot free as well, but the sweep continues because
    # later pivots may still be fully determined.  This is O(rows x row width)
    # work and O(working bits) memory — no completion enumeration and no full
    # free-direction matrix.
    fixed = dict(observed)
    free = set()
    # Basis keys are one-bit masks; normalize them to column positions.
    pivot_set = {pivot.bit_length() - 1 for pivot in basis
                 if pivot < (1 << rhs_bit)}
    for pivot in sorted(pivot_set):
        if pivot in fixed:
            continue
        row = basis[1 << pivot]
        acc = (row >> rhs_bit) & 1
        rest = row & ~(1 << pivot) & ~(1 << rhs_bit)
        depends_on_free = False
        while rest:
            low = rest & -rest
            column = low.bit_length() - 1
            if column in free or (column not in fixed
                                  and column not in pivot_set):
                depends_on_free = True
                break
            acc ^= fixed[column]
            rest ^= low
        if depends_on_free:
            free.add(pivot)
        else:
            fixed[pivot] = acc

    result = []
    for key, length in pkg['frames']:
        offset = pkg['offsets'][key]
        values = []
        sources = []
        for index in range(length):
            column = offset + index
            if column in observed:
                values.append(observed[column])
                sources.append(SOURCE_OBSERVED)
            elif column in fixed:
                values.append(fixed[column])
                sources.append(SOURCE_PROTECTED)
            else:
                values.append(None)
                sources.append(SOURCE_UNKNOWN)
        result.append({
            'frame_key': [key[0], key[1]],
            'complete': all(value is not None for value in values),
            'values': values,
            'sources': sources,
        })
    return result
