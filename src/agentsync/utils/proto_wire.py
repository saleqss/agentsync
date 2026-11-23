"""Zero-dependency Protobuf wire-format encoder/decoder for Antigravity stores.

All wire field numbers are verified directly against live Antigravity 2.34 databases:
- agyhub_summaries_proto.pb:
    repeated field 1 (entry message):
      field 1 (str): conversation_id
      field 2 (bytes): raw_summary (exact byte match with conversation_summaries.raw_summary)
- conversation_summaries.raw_summary:
    field 1 (str): title
    field 2 (varint): step_count
    field 3 (Timestamp): last_modified_time
    field 4 (str): trajectory_id
    field 5 (varint): run_status enum (1 = CASCADE_RUN_STATUS_IDLE, 2 = CASCADE_RUN_STATUS_RUNNING)
    field 7 (Timestamp): created_at
    field 9 (msg): workspace_info {1: plain_uri, 2: plain_uri}
    field 10 (Timestamp): last_user_input_time
    field 15 (msg): annotation {1: title, 7: last_modified Timestamp}
    field 16 (varint): last_user_input_step_index
    field 17 (bytes): trajectory_metadata_blob
    field 21 (varint): 1
    field 22 (varint): 4
    field 26 (varint): 1
    field 28 (varint): 4
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from agentsync.utils.atomic_io import atomic_write_bytes

RUN_STATUS_IDLE = 1
RUN_STATUS_RUNNING = 2


# --- Low-level wire primitives ---


def encode_varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            break
    return bytes(out)


def decode_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    length = len(buf)
    while pos < length:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 70:
            raise ValueError("Malformed varint (>10 bytes)")
    raise ValueError("Truncated varint")


def field_varint(field_num: int, value: int) -> bytes:
    return encode_varint((field_num << 3) | 0) + encode_varint(value)


def field_bytes(field_num: int, data: bytes) -> bytes:
    return encode_varint((field_num << 3) | 2) + encode_varint(len(data)) + data


def field_str(field_num: int, text: str) -> bytes:
    return field_bytes(field_num, text.encode("utf-8"))


def parse_wire(buf: bytes) -> list[tuple[int, int, Any]]:
    """Decode top-level protobuf fields into `[(field_num, wire_type, value), ...]`."""
    pos = 0
    length = len(buf)
    fields: list[tuple[int, int, Any]] = []
    while pos < length:
        tag, pos = decode_varint(buf, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:
            val, pos = decode_varint(buf, pos)
            fields.append((field_num, wire_type, val))
        elif wire_type == 1:
            if pos + 8 > length:
                raise ValueError("Truncated 64-bit field")
            fields.append((field_num, wire_type, buf[pos : pos + 8]))
            pos += 8
        elif wire_type == 2:
            size, pos = decode_varint(buf, pos)
            if pos + size > length:
                raise ValueError("Truncated length-delimited field")
            fields.append((field_num, wire_type, buf[pos : pos + size]))
            pos += size
        elif wire_type == 5:
            if pos + 4 > length:
                raise ValueError("Truncated 32-bit field")
            fields.append((field_num, wire_type, buf[pos : pos + 4]))
            pos += 4
        else:
            raise ValueError(f"Unsupported protobuf wire type: {wire_type}")
    return fields


def first_bytes(fields: list[tuple[int, int, Any]], field_num: int) -> bytes | None:
    for fn, wt, val in fields:
        if fn == field_num and wt == 2:
            return val
    return None


def first_str(fields: list[tuple[int, int, Any]], field_num: int) -> str | None:
    raw = first_bytes(fields, field_num)
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def first_varint(fields: list[tuple[int, int, Any]], field_num: int) -> int | None:
    for fn, wt, val in fields:
        if fn == field_num and wt == 0:
            return int(val)
    return None


def encode_timestamp_ms(ts_ms: int) -> bytes:
    secs = max(0, ts_ms // 1000)
    nanos = max(0, (ts_ms % 1000) * 1_000_000)
    return field_varint(1, secs) + (field_varint(2, nanos) if nanos else b"")


def decode_timestamp_ms(buf: bytes) -> int:
    fields = parse_wire(buf)
    secs = first_varint(fields, 1) or 0
    nanos = first_varint(fields, 2) or 0
    return secs * 1000 + nanos // 1_000_000


# --- Antigravity encoders & decoders ---


def encode_trajectory_metadata(
    cascade_id: str,
    created_at_ms: int,
    workspace_uri: str = "",
    project_id: str = "",
) -> bytes:
    parts: list[bytes] = []
    if workspace_uri:
        plain_uri = workspace_uri.replace("%3A", ":").replace("%3a", ":")
        ws_msg = field_str(1, plain_uri) + field_str(2, plain_uri)
        parts.append(field_bytes(1, ws_msg))
    parts.append(field_bytes(2, encode_timestamp_ms(created_at_ms)))
    parts.append(field_str(3, str(uuid.uuid5(uuid.NAMESPACE_URL, f"agy-meta:{cascade_id}"))))
    parts.append(field_str(6, cascade_id))
    if workspace_uri:
        parts.append(field_str(7, workspace_uri))
    effective_pid = project_id or "outside-of-project"
    parts.append(field_str(18, effective_pid))
    return b"".join(parts)


def decode_trajectory_metadata(blob: bytes) -> dict[str, Any]:
    fields = parse_wire(blob)
    ts_raw = first_bytes(fields, 2)
    created_at_ms = decode_timestamp_ms(ts_raw) if ts_raw else 0
    ws_uri = first_str(fields, 7) or ""
    if not ws_uri:
        ws_msg = first_bytes(fields, 1)
        if ws_msg:
            ws_uri = first_str(parse_wire(ws_msg), 1) or ""
    return {
        "cascade_id": first_str(fields, 6) or "",
        "workspace_uri": ws_uri,
        "project_id": first_str(fields, 18) or "",
        "created_at_ms": created_at_ms,
    }


def encode_step_metadata(
    timestamp_ms: int,
    source_enum: int,
    trajectory_id: str,
    cascade_id: str,
    step_idx: int,
    turn_uuid: str,
) -> bytes:
    ts_blob = encode_timestamp_ms(timestamp_ms)
    traj_ref = (
        field_str(1, trajectory_id)
        + (field_varint(2, step_idx) if step_idx > 0 else b"")
        + field_str(4, cascade_id)
    )
    parts = [
        field_bytes(1, ts_blob),
        field_varint(3, source_enum),
    ]
    if source_enum == 2:
        parts.append(field_bytes(7, ts_blob))
    parts.append(field_str(12, turn_uuid))
    parts.append(field_bytes(20, traj_ref))
    return b"".join(parts)


def encode_user_input_step(
    text: str,
    timestamp_ms: int,
    trajectory_id: str,
    cascade_id: str,
    step_idx: int,
    turn_uuid: str,
) -> tuple[bytes, bytes]:
    """Return `(metadata_blob, step_payload_blob)` for step_type=14 (USER_INPUT)."""
    meta_blob = encode_step_metadata(
        timestamp_ms=timestamp_ms,
        source_enum=4,
        trajectory_id=trajectory_id,
        cascade_id=cascade_id,
        step_idx=step_idx,
        turn_uuid=turn_uuid,
    )
    user_msg = field_str(2, text) + field_bytes(3, field_str(1, text))
    payload = (
        field_varint(1, 14)
        + field_varint(4, 3)
        + field_bytes(5, meta_blob)
        + field_bytes(19, user_msg)
    )
    return meta_blob, payload


def encode_planner_response_step(
    text: str,
    timestamp_ms: int,
    trajectory_id: str,
    cascade_id: str,
    step_idx: int,
    turn_uuid: str,
) -> tuple[bytes, bytes]:
    """Return `(metadata_blob, step_payload_blob)` for step_type=15 (PLANNER_RESPONSE)."""
    meta_blob = encode_step_metadata(
        timestamp_ms=timestamp_ms,
        source_enum=2,
        trajectory_id=trajectory_id,
        cascade_id=cascade_id,
        step_idx=step_idx,
        turn_uuid=turn_uuid,
    )
    bot_id = f"bot-{uuid.uuid5(uuid.NAMESPACE_URL, f'{cascade_id}:{step_idx}')}"
    resp_msg = (
        field_str(1, text)
        + field_str(6, bot_id)
        + field_str(8, text)
        + field_varint(12, 2)
    )
    payload = (
        field_varint(1, 15)
        + field_varint(4, 3)
        + field_bytes(5, meta_blob)
        + field_bytes(20, resp_msg)
    )
    return meta_blob, payload


def decode_step_text(step_type: int, payload: bytes) -> tuple[str, int]:
    """Extract `(text, timestamp_ms)` from an Antigravity `steps.step_payload` blob."""
    if not payload:
        return "", 0
    fields = parse_wire(payload)
    ts_ms = 0
    meta_raw = first_bytes(fields, 5)
    if meta_raw:
        try:
            ts_raw = first_bytes(parse_wire(meta_raw), 1)
            if ts_raw:
                ts_ms = decode_timestamp_ms(ts_raw)
        except ValueError:
            pass

    if step_type == 14:
        u_raw = first_bytes(fields, 19)
        if u_raw:
            u_fields = parse_wire(u_raw)
            txt = first_str(u_fields, 2)
            if txt:
                return txt, ts_ms
            nested = first_bytes(u_fields, 3)
            if nested:
                txt = first_str(parse_wire(nested), 1)
                if txt:
                    return txt, ts_ms
    elif step_type == 15:
        p_raw = first_bytes(fields, 20)
        if p_raw:
            p_fields = parse_wire(p_raw)
            txt = first_str(p_fields, 8) or first_str(p_fields, 1)
            if txt:
                return txt, ts_ms
    return "", ts_ms


def encode_raw_summary(
    title: str,
    step_count: int,
    created_at_ms: int,
    last_modified_ms: int,
    last_user_input_ms: int,
    trajectory_id: str,
    cascade_id: str,
    workspace_uri: str = "",
    project_id: str = "",
    last_user_input_step_index: int = 0,
    run_status: int = RUN_STATUS_IDLE,
) -> bytes:
    """Encode native Antigravity raw_summary protobuf matching live 2.34 summary store format."""
    traj_meta = encode_trajectory_metadata(
        cascade_id=cascade_id,
        created_at_ms=created_at_ms,
        workspace_uri=workspace_uri,
        project_id=project_id,
    )
    mod_ts = encode_timestamp_ms(last_modified_ms)
    cre_ts = encode_timestamp_ms(created_at_ms)
    usr_ts = encode_timestamp_ms(last_user_input_ms or created_at_ms)

    parts = [
        field_str(1, title),
        field_varint(2, step_count),
        field_bytes(3, mod_ts),
        field_str(4, trajectory_id),
        field_varint(5, run_status),
        field_bytes(7, cre_ts),
    ]
    if workspace_uri:
        plain_uri = workspace_uri.replace("%3A", ":").replace("%3a", ":")
        parts.append(field_bytes(9, field_str(1, plain_uri) + field_str(2, plain_uri)))
    parts.append(field_bytes(10, usr_ts))
    ann = field_str(1, title) + field_bytes(7, mod_ts)
    parts.append(field_bytes(15, ann))
    if last_user_input_step_index >= 0:
        parts.append(field_varint(16, last_user_input_step_index))
    parts.append(field_bytes(17, traj_meta))
    parts.append(field_varint(21, 1))
    parts.append(field_varint(22, 4))
    parts.append(field_varint(26, 1))
    parts.append(field_varint(28, 4))
    return b"".join(parts)


def read_agyhub_summaries_pb(pb_path: Path) -> dict[str, bytes]:
    """Read `agyhub_summaries_proto.pb` into an ordered map `{conversation_id: raw_summary_bytes}`."""
    entries: dict[str, bytes] = {}
    if not pb_path.exists():
        return entries
    buf = pb_path.read_bytes()
    if not buf:
        return entries
    for fn, wt, val in parse_wire(buf):
        if fn == 1 and wt == 2:
            sub = parse_wire(val)
            cid = first_str(sub, 1)
            raw_sum = first_bytes(sub, 2)
            if cid and raw_sum is not None:
                entries[cid] = raw_sum
    return entries


def encode_agyhub_summaries_pb(entries: dict[str, bytes]) -> bytes:
    """Serialize `{conversation_id: raw_summary_bytes}` into `agyhub_summaries_proto.pb` wire bytes."""
    chunks: list[bytes] = []
    for cid, raw_sum in entries.items():
        entry_msg = field_str(1, cid) + field_bytes(2, raw_sum)
        chunks.append(field_bytes(1, entry_msg))
    return b"".join(chunks)


def upsert_agyhub_summaries_pb(pb_path: Path, updates: dict[str, bytes]) -> bool:
    """Atomically upsert conversation summaries into `agyhub_summaries_proto.pb`, preserving existing entries."""
    existing = read_agyhub_summaries_pb(pb_path)
    changed = False
    for cid, raw_sum in updates.items():
        if existing.get(cid) != raw_sum:
            existing[cid] = raw_sum
            changed = True
    if not changed and pb_path.exists():
        return False
    encoded = encode_agyhub_summaries_pb(existing)
    return atomic_write_bytes(pb_path, encoded)
