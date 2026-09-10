# DAO TRANSFORMER
# Resolution/frame-rate independent for normal, non-fragmented MP4.
# The input timing/sample tables are read dynamically; no 60/120 fps
# or 1080p/1440p/4K sample-count assumptions are used.
#
# Tested target classes:
#   1080p60, 1080p120
#   1440p60, 1440p120
#   4K60,   4K120
#
#!/usr/bin/env python3
"""
dao_transform.py

Low-level MP4 box transformer based on the supplied reference pattern.

It:
- preserves the original encoded H.264 video payload;
- preserves the original AAC audio payload;
- removes the last referenced sample from the primary audio/video tracks;
- creates a second AAC track;
- appends the reference auxiliary sample payload;
- rewrites chunk offsets using co64;
- preserves common MP4 sample tables where possible;
- supports normal and extended-size MP4 boxes.

Usage:
    python dao_transform.py input.mp4 output.mp4

This is a container-level transform. It does NOT decode or re-encode
the media streams.

Important:
This targets non-fragmented MP4 files containing one H.264 video track
and one AAC audio track, independent of resolution or frame rate. It deliberately rejects unsupported
layouts instead of silently producing a corrupt file.
"""

import hashlib
import json
import os
import struct
import sys
from pathlib import Path

AUX_SAMPLE_COUNT = 8244  # recalculated per input
AUX_SAMPLE_SIZE = 8
AUXILIARY_DATA_SIZE = AUX_SAMPLE_COUNT * AUX_SAMPLE_SIZE
AUXILIARY_SAMPLE = b"\x00\x00\x00\x04\x00\x00\x00\x00"
AUXILIARY_DATA = AUXILIARY_SAMPLE * AUX_SAMPLE_COUNT


def u32(data, pos):
    return struct.unpack(">I", data[pos:pos + 4])[0]


def u64(data, pos):
    return struct.unpack(">Q", data[pos:pos + 8])[0]


def box(box_type, payload):
    box_type = box_type.encode("latin1") if isinstance(box_type, str) else box_type
    size = 8 + len(payload)
    if size < 0x100000000:
        return struct.pack(">I4s", size, box_type) + payload
    return struct.pack(">I4sQ", 1, box_type, size) + payload


def parse_boxes(data, start=0, end=None):
    if end is None:
        end = len(data)

    result = []
    pos = start

    while pos + 8 <= end:
        size32 = u32(data, pos)
        box_type = data[pos + 4:pos + 8].decode("latin1")
        header = 8

        if size32 == 1:
            if pos + 16 > end:
                raise ValueError(f"Invalid extended-size {box_type} box.")
            size = u64(data, pos + 8)
            header = 16
        elif size32 == 0:
            size = end - pos
        else:
            size = size32

        if size < header or pos + size > end:
            raise ValueError(f"Invalid {box_type} box at offset {pos}.")

        result.append({
            "type": box_type,
            "offset": pos,
            "size": size,
            "header": header,
            "payload_start": pos + header,
            "payload_end": pos + size,
            "raw": data[pos:pos + size],
        })
        pos += size

    if pos != end:
        raise ValueError("MP4 box layout is not aligned.")

    return result


def children(data, parent):
    return parse_boxes(data, parent["payload_start"], parent["payload_end"])


def child(data, parent, box_type):
    for item in children(data, parent):
        if item["type"] == box_type:
            return item
    raise ValueError(f"Missing {box_type} inside {parent['type']}.")


def optional_child(data, parent, box_type):
    for item in children(data, parent):
        if item["type"] == box_type:
            return item
    return None


def replace_children(data, parent, replacements):
    output = []
    for item in children(data, parent):
        replacement = replacements.get(item["type"])
        output.append(item["raw"] if replacement is None else replacement)
    return box(parent["type"], b"".join(output))


def rebuild_children(raw_parent, replacements):
    parsed = parse_boxes(raw_parent)
    if len(parsed) != 1:
        raise ValueError("Invalid parent box.")
    parent = parsed[0]

    output = []
    for item in children(raw_parent, parent):
        replacement = replacements.get(item["type"])
        output.append(item["raw"] if replacement is None else replacement)

    return box(parent["type"], b"".join(output))


def handler_type(data, trak):
    mdia = child(data, trak, "mdia")
    hdlr = child(data, mdia, "hdlr")
    payload = data[hdlr["payload_start"]:hdlr["payload_end"]]
    if len(payload) < 12:
        raise ValueError("Invalid hdlr box.")
    return payload[8:12].decode("latin1")


def track_id(data, trak):
    tkhd = child(data, trak, "tkhd")
    payload = data[tkhd["payload_start"]:tkhd["payload_end"]]
    return u32(data, tkhd["payload_start"] + (12 if payload[0] == 0 else 20))


def set_tkhd(data, trak, new_id=None, duration=None):
    tkhd = child(data, trak, "tkhd")
    payload = bytearray(data[tkhd["payload_start"]:tkhd["payload_end"]])
    version = payload[0]

    id_pos = 12 if version == 0 else 20
    duration_pos = 20 if version == 0 else 28

    if new_id is not None:
        struct.pack_into(">I", payload, id_pos, new_id)

    if duration is not None:
        if version == 0:
            if duration > 0xFFFFFFFF:
                raise ValueError("tkhd duration exceeds version-0 range.")
            struct.pack_into(">I", payload, duration_pos, duration)
        else:
            struct.pack_into(">Q", payload, duration_pos, duration)

    return box("tkhd", bytes(payload))


def set_mdhd(data, mdhd, duration):
    payload = bytearray(data[mdhd["payload_start"]:mdhd["payload_end"]])
    version = payload[0]

    if version == 0:
        if duration > 0xFFFFFFFF:
            raise ValueError("mdhd duration exceeds version-0 range.")
        struct.pack_into(">I", payload, 16, duration)
    else:
        struct.pack_into(">Q", payload, 24, duration)

    return box("mdhd", bytes(payload))


def set_mvhd(data, moov, duration):
    mvhd = child(data, moov, "mvhd")
    payload = bytearray(data[mvhd["payload_start"]:mvhd["payload_end"]])
    version = payload[0]

    if version == 0:
        if duration > 0xFFFFFFFF:
            raise ValueError("mvhd duration exceeds version-0 range.")
        struct.pack_into(">I", payload, 16, duration)
    else:
        struct.pack_into(">Q", payload, 24, duration)

    return box("mvhd", bytes(payload))


def parse_stsz(data, stbl):
    b = child(data, stbl, "stsz")
    p = b["payload_start"]
    sample_size = u32(data, p + 4)
    count = u32(data, p + 8)

    if sample_size:
        sizes = [sample_size] * count
    else:
        end = p + 12 + count * 4
        if end > b["payload_end"]:
            raise ValueError("Invalid stsz table.")
        sizes = [u32(data, p + 12 + i * 4) for i in range(count)]

    return sizes


def make_stsz(sizes):
    payload = struct.pack(">III", 0, 0, len(sizes))
    payload += b"".join(struct.pack(">I", size) for size in sizes)
    return box("stsz", payload)


def parse_stsc(data, stbl):
    b = child(data, stbl, "stsc")
    p = b["payload_start"]
    count = u32(data, p + 4)
    entries = []

    for i in range(count):
        q = p + 8 + i * 12
        entries.append((u32(data, q), u32(data, q + 4), u32(data, q + 8)))

    return entries


def make_stsc(entries):
    payload = struct.pack(">II", 0, len(entries))
    for first_chunk, samples_per_chunk, description in entries:
        payload += struct.pack(">III", first_chunk, samples_per_chunk, description)
    return box("stsc", payload)


def parse_stts(data, stbl):
    b = child(data, stbl, "stts")
    p = b["payload_start"]
    count = u32(data, p + 4)
    entries = []

    for i in range(count):
        q = p + 8 + i * 8
        entries.append((u32(data, q), u32(data, q + 4)))

    return entries


def make_stts(entries):
    payload = struct.pack(">II", 0, len(entries))
    for count, duration in entries:
        payload += struct.pack(">II", count, duration)
    return box("stts", payload)


def parse_offsets(data, stbl):
    co64 = optional_child(data, stbl, "co64")
    if co64:
        p = co64["payload_start"]
        count = u32(data, p + 4)
        return [u64(data, p + 8 + i * 8) for i in range(count)]

    stco = optional_child(data, stbl, "stco")
    if stco:
        p = stco["payload_start"]
        count = u32(data, p + 4)
        return [u32(data, p + 8 + i * 4) for i in range(count)]

    raise ValueError("Track has neither stco nor co64.")


def make_stco(offsets):
    if any(value > 0xFFFFFFFF for value in offsets):
        return make_co64(offsets)
    payload = struct.pack(">II", 0, len(offsets))
    payload += b"".join(struct.pack(">I", value) for value in offsets)
    return box("stco", payload)


def make_co64(offsets):
    payload = struct.pack(">II", 0, len(offsets))
    payload += b"".join(struct.pack(">Q", value) for value in offsets)
    return box("co64", payload)


def expand_stsc(entries, chunk_count):
    if not entries:
        raise ValueError("Empty stsc table.")

    per_chunk = [0] * chunk_count

    for i, (first_chunk, samples_per_chunk, _description) in enumerate(entries):
        last_chunk = (
            entries[i + 1][0]
            if i + 1 < len(entries)
            else chunk_count + 1
        )

        if first_chunk < 1 or first_chunk > chunk_count:
            raise ValueError("Invalid stsc first_chunk.")

        for chunk in range(first_chunk, min(last_chunk, chunk_count + 1)):
            per_chunk[chunk - 1] = samples_per_chunk

    if any(value <= 0 for value in per_chunk):
        raise ValueError("stsc does not describe every chunk.")

    return per_chunk


def remove_last_sample(stsc_entries, offsets, sizes):
    if not sizes:
        raise ValueError("Track has no samples.")

    per_chunk = expand_stsc(stsc_entries, len(offsets))

    if sum(per_chunk) != len(sizes):
        raise ValueError(
            f"stsc/stco mismatch: {sum(per_chunk)} samples described, "
            f"but stsz contains {len(sizes)}."
        )

    if per_chunk[-1] > 1:
        per_chunk[-1] -= 1
        kept_offsets = offsets[:]
    else:
        per_chunk = per_chunk[:-1]
        kept_offsets = offsets[:-1]

    if not per_chunk:
        raise ValueError("Removing the final sample would remove all chunks.")

    new_stsc = []
    previous = None

    for chunk_number, samples_per_chunk in enumerate(per_chunk, 1):
        if samples_per_chunk != previous:
            new_stsc.append((chunk_number, samples_per_chunk, 1))
            previous = samples_per_chunk

    return sizes[:-1], new_stsc, kept_offsets


def trim_stts(entries, keep_count):
    result = []
    remaining = keep_count

    for count, duration in entries:
        if remaining <= 0:
            break

        take = min(count, remaining)

        if take:
            if result and result[-1][1] == duration:
                result[-1] = (result[-1][0] + take, duration)
            else:
                result.append((take, duration))

        remaining -= take

    if remaining:
        raise ValueError("stts contains fewer samples than stsz.")

    return result


def trim_ctts(data, stbl, keep_count):
    b = optional_child(data, stbl, "ctts")
    if not b:
        return None

    p = b["payload_start"]
    payload = data[p:b["payload_end"]]

    if len(payload) < 8:
        raise ValueError("Invalid ctts box.")

    version_flags = payload[:4]
    count = u32(data, p + 4)
    pos = p + 8
    remaining = keep_count
    entries = []

    for _ in range(count):
        if pos + 8 > b["payload_end"]:
            raise ValueError("Invalid ctts entries.")

        sample_count = u32(data, pos)
        offset = u32(data, pos + 4)
        pos += 8

        if remaining <= 0:
            break

        take = min(sample_count, remaining)
        entries.append((take, offset))
        remaining -= take

    if remaining:
        raise ValueError("ctts contains fewer samples than stsz.")

    out = bytearray(version_flags)
    out += struct.pack(">I", len(entries))

    for count, offset in entries:
        out += struct.pack(">II", count, offset)

    return box("ctts", bytes(out))


def trim_stss(data, stbl, original_count):
    b = optional_child(data, stbl, "stss")
    if not b:
        return None

    p = b["payload_start"]
    count = u32(data, p + 4)

    samples = [
        u32(data, p + 8 + i * 4)
        for i in range(count)
    ]

    # stss uses one-based sample numbers. We removed the final sample.
    samples = [sample for sample in samples if sample != original_count]

    out = bytearray(data[p:p + 4])
    out += struct.pack(">I", len(samples))
    out += b"".join(struct.pack(">I", sample) for sample in samples)

    return box("stss", bytes(out))


def trim_sdtp(data, stbl, keep_count):
    b = optional_child(data, stbl, "sdtp")
    if not b:
        return None

    p = b["payload_start"]
    values = data[p + 4:b["payload_end"]]

    if len(values) < keep_count:
        raise ValueError("sdtp contains fewer samples than stsz.")

    return box("sdtp", data[p:p + 4] + values[:keep_count])


def trim_saiz(data, stbl, keep_count):
    b = optional_child(data, stbl, "saiz")
    if not b:
        return None

    p = b["payload_start"]
    payload = bytearray(data[p:b["payload_end"]])

    if len(payload) < 9:
        raise ValueError("Invalid saiz box.")

    default_size = payload[4]
    original_count = u32(data, p + 5)

    if default_size == 0:
        if len(payload) < 9 + original_count:
            raise ValueError("Invalid saiz entry array.")
        payload[5:9] = struct.pack(">I", keep_count)
        payload = payload[:9 + keep_count]
    else:
        payload[5:9] = struct.pack(">I", keep_count)

    return box("saiz", bytes(payload))


def rebuild_sample_table(
    data,
    stbl,
    sizes,
    stsc_entries,
    stts_entries,
    offsets,
    original_count,
    keep_count,
    offset_delta,
):
    replacements = {
        "stsz": make_stsz(sizes),
        "stsc": make_stsc(stsc_entries),
        "stts": make_stts(stts_entries),
        "co64": make_stco([x + offset_delta for x in offsets]),
    }

    ctts = trim_ctts(data, stbl, keep_count)
    if ctts is not None:
        replacements["ctts"] = ctts

    stss = trim_stss(data, stbl, original_count)
    if stss is not None:
        replacements["stss"] = stss

    sdtp = trim_sdtp(data, stbl, keep_count)
    if sdtp is not None:
        replacements["sdtp"] = sdtp

    saiz = trim_saiz(data, stbl, keep_count)
    if saiz is not None:
        replacements["saiz"] = saiz

    output = []

    for item in children(data, stbl):
        if item["type"] in ("stco", "co64"):
            continue

        replacement = replacements.get(item["type"])

        if replacement is None:
            output.append(item["raw"])
        else:
            output.append(replacement)

    # If the source did not contain one of the replacement boxes, add it.
    existing = {item["type"] for item in children(data, stbl)}

    # The source offset box was removed above, so always append the rebuilt
    # offset table. It is stco when possible and co64 when required.
    output.append(replacements["co64"])
    for typ in ("stsz", "stsc", "stts"):
        if typ not in existing:
            output.append(replacements[typ])

    return box("stbl", b"".join(output))


def remove_edts(data, raw_trak):
    trak = parse_boxes(raw_trak)[0]
    output = []

    for item in children(raw_trak, trak):
        if item["type"] != "edts":
            output.append(item["raw"])

    return box("trak", b"".join(output))


def rebuild_primary_audio(data, trak, offset_delta, target_id=3, preserve_all=False):
    mdia = child(data, trak, "mdia")
    minf = child(data, mdia, "minf")
    stbl = child(data, minf, "stbl")

    source_sizes = parse_stsz(data, stbl)
    source_stsc = parse_stsc(data, stbl)
    source_stts = parse_stts(data, stbl)
    source_offsets = parse_offsets(data, stbl)

    if len(source_sizes) < 2:
        raise ValueError("Audio track must contain at least two samples.")

    original_count = len(source_sizes)

    if preserve_all:
        sizes, stsc_entries, offsets, stts_entries = (
            source_sizes, source_stsc, source_offsets, source_stts
        )
    else:
        sizes, stsc_entries, offsets = remove_last_sample(
            source_stsc, source_offsets, source_sizes,
        )
        stts_entries = trim_stts(source_stts, len(sizes))

    new_stbl = rebuild_sample_table(
        data,
        stbl,
        sizes,
        stsc_entries,
        stts_entries,
        offsets,
        original_count,
        len(sizes),
        offset_delta,
    )

    new_minf = replace_children(data, minf, {"stbl": new_stbl})
    new_mdia = replace_children(data, mdia, {"minf": new_minf})

    new_tkhd = set_tkhd(data, trak, target_id)

    return remove_edts(
        data,
        replace_children(
            data,
            trak,
            {
                "tkhd": new_tkhd,
                "mdia": new_mdia,
            },
        ),
    )


def rebuild_video(data, trak, offset_delta, target_id=2, preserve_all=False):
    mdia = child(data, trak, "mdia")
    minf = child(data, mdia, "minf")
    stbl = child(data, minf, "stbl")

    # IMPORTANT:
    # original_count is captured before remove_last_sample().
    # This fixes the previous undefined "source_sizes" bug.
    source_sizes = parse_stsz(data, stbl)
    original_count = len(source_sizes)

    source_stsc = parse_stsc(data, stbl)
    source_stts = parse_stts(data, stbl)
    source_offsets = parse_offsets(data, stbl)

    if original_count < 2:
        raise ValueError("Video track must contain at least two samples.")

    if preserve_all:
        sizes, stsc_entries, offsets, stts_entries = (
            source_sizes, source_stsc, source_offsets, source_stts
        )
    else:
        sizes, stsc_entries, offsets = remove_last_sample(
            source_stsc, source_offsets, source_sizes,
        )
        stts_entries = trim_stts(source_stts, len(sizes))

    new_stbl = rebuild_sample_table(
        data,
        stbl,
        sizes,
        stsc_entries,
        stts_entries,
        offsets,
        original_count,
        len(sizes),
        offset_delta,
    )

    new_minf = replace_children(data, minf, {"stbl": new_stbl})
    new_mdia = replace_children(data, mdia, {"minf": new_minf})
    new_tkhd = set_tkhd(data, trak, target_id)

    return remove_edts(
        data,
        replace_children(
            data,
            trak,
            {
                "tkhd": new_tkhd,
                "mdia": new_mdia,
            },
        ),
    )


def audio_kept_duration(data, trak, keep_samples):
    mdia = child(data, trak, "mdia")
    minf = child(data, mdia, "minf")
    stbl = child(data, minf, "stbl")
    stts = parse_stts(data, stbl)

    remaining = keep_samples
    total = 0

    for count, duration in stts:
        take = min(count, remaining)
        total += take * duration
        remaining -= take

        if remaining == 0:
            break

    if remaining:
        raise ValueError("Audio stts contains too few samples.")

    return total


def clone_audio_track(data, source_trak, new_id, offset_delta, auxiliary_offset, preserve_all=False):
    mdia = child(data, source_trak, "mdia")
    minf = child(data, mdia, "minf")
    stbl = child(data, minf, "stbl")

    source_sizes = parse_stsz(data, stbl)
    source_stsc = parse_stsc(data, stbl)
    source_stts = parse_stts(data, stbl)
    source_offsets = parse_offsets(data, stbl)

    if len(source_sizes) < 2:
        raise ValueError("Audio track must contain at least two samples.")

    original_count = len(source_sizes)

    if preserve_all:
        first_sizes, first_stsc, first_offsets = (
            source_sizes, source_stsc, source_offsets
        )
        first_count = len(first_sizes)
        kept_stts = source_stts
    else:
        first_sizes, first_stsc, first_offsets = remove_last_sample(
            source_stsc, source_offsets, source_sizes,
        )
        first_count = len(first_sizes)
        kept_stts = trim_stts(source_stts, first_count)

    # Add the auxiliary samples as one additional chunk.
    new_stsc = list(first_stsc)
    new_stsc.append((len(first_offsets) + 1, AUX_SAMPLE_COUNT, 1))

    new_stts = kept_stts + [(AUX_SAMPLE_COUNT, 1)]
    new_sizes = first_sizes + [AUX_SAMPLE_SIZE] * AUX_SAMPLE_COUNT
    new_offsets = [x + offset_delta for x in first_offsets]
    new_offsets.append(auxiliary_offset)

    # Sample-dependent tables describe the combined cloned track.
    # The source ctts/stss/sdtp/saiz are trimmed to the retained source
    # samples; auxiliary samples have no corresponding source entries.
    output = []

    for item in children(data, stbl):
        typ = item["type"]

        if typ == "stts":
            output.append(make_stts(new_stts))
        elif typ == "stsc":
            output.append(make_stsc(new_stsc))
        elif typ == "stsz":
            output.append(make_stsz(new_sizes))
        elif typ in ("stco", "co64"):
            # Replaced once below by a co64 table.
            continue
        elif typ == "ctts":
            ctts = trim_ctts(data, stbl, first_count)
            if ctts is not None:
                output.append(ctts)
        elif typ == "stss":
            stss = trim_stss(data, stbl, original_count)
            if stss is not None:
                output.append(stss)
        elif typ == "sdtp":
            sdtp = trim_sdtp(data, stbl, first_count)
            if sdtp is not None:
                output.append(sdtp)
        elif typ == "saiz":
            saiz = trim_saiz(data, stbl, first_count)
            if saiz is not None:
                output.append(saiz)
        else:
            output.append(item["raw"])

    output.append(make_stco(new_offsets))

    new_stbl = box("stbl", b"".join(output))
    new_minf = replace_children(data, minf, {"stbl": new_stbl})

    source_mdhd = child(data, mdia, "mdhd")
    source_duration = audio_kept_duration(data, source_trak, first_count)
    new_mdhd = set_mdhd(data, source_mdhd, source_duration + AUX_SAMPLE_COUNT)

    new_mdia = replace_children(
        data,
        mdia,
        {
            "minf": new_minf,
            "mdhd": new_mdhd,
        },
    )

    new_tkhd = set_tkhd(data, source_trak, new_id)

    cloned = replace_children(
        data,
        source_trak,
        {
            "tkhd": new_tkhd,
            "mdia": new_mdia,
        },
    )

    return remove_edts(data, cloned)


def _reference_video_udta():
    # Metadata block observed in the supplied after-file.
    return bytes.fromhex(
        "0000008e75647461000000866d65746100000000"
        "0000002168646c7200000000000000006d646972"
        "0000000000000000000000000000000059696c7374"
        "00000051a9746f6f000000496461746100000001"
        "0000000054696b517569636b205175616c697479"
        "204d6574686f64202d2068747470733a2f2f7469"
        "6b717569636b2e6f6e6c696e652f202d20763139"
        "00"
    )


def add_video_udta(video):
    trak = parse_boxes(video)[0]
    parts = []
    for item in children(video, trak):
        parts.append(item["raw"])
    parts.append(_reference_video_udta())
    return box("trak", b"".join(parts))


def make_moov(data, original_moov, primary_audio, cloned_audio, video,
              preserve_all=False):
    output = [child(data, original_moov, "mvhd")["raw"]]

    for item in children(data, original_moov):
        if item["type"] in ("mvhd", "trak"):
            continue
        # The supplied fragmented before/after pair moves metadata into the
        # video trak rather than keeping the root udta.
        if preserve_all and item["type"] == "udta":
            continue
        output.append(item["raw"])

    if preserve_all:
        video = add_video_udta(video)
        output.extend([video, primary_audio, cloned_audio])
    else:
        output.extend([primary_audio, cloned_audio, video])

    return box("moov", b"".join(output))


def validate_output(filename):
    with open(filename, "rb") as f:
        data = f.read()

    # The reference format stores the auxiliary 8-byte samples as raw bytes
    # immediately after mdat, so parse only the valid MP4 boxes first.
    top = []
    pos = 0
    while pos + 8 <= len(data):
        size32 = u32(data, pos)
        if size32 < 8 or pos + size32 > len(data):
            break
        h = 8
        size = size32
        if size32 == 1:
            if pos + 16 > len(data):
                break
            size = u64(data, pos + 8)
            h = 16
        elif size32 == 0:
            size = len(data) - pos
        top.append({
            "type": data[pos + 4:pos + 8].decode("latin1"),
            "offset": pos,
            "size": size,
            "header": h,
            "payload_start": pos + h,
            "payload_end": pos + size,
            "raw": data[pos:pos + size],
        })
        pos += size

    types = [item["type"] for item in top]
    for required in ("ftyp", "moov", "mdat"):
        if required not in types:
            raise ValueError(f"Output is missing {required}.")

    moov = next(item for item in top if item["type"] == "moov")
    traks = [item for item in children(data, moov) if item["type"] == "trak"]

    if len(traks) != 3:
        raise ValueError(f"DAO output must contain 3 tracks (1 video + 2 audio); found {len(traks)}.")

    audio = [t for t in traks if handler_type(data, t) == "soun"]
    video = [t for t in traks if handler_type(data, t) == "vide"]

    if len(audio) != 2 or len(video) != 1:
        raise ValueError("DAO output must contain exactly 1 video and 2 audio tracks.")

    for trak in traks:
        mdia = child(data, trak, "mdia")
        minf = child(data, mdia, "minf")
        stbl = child(data, minf, "stbl")

        if not (optional_child(data, stbl, "stco") or
                optional_child(data, stbl, "co64")):
            raise ValueError(
                f"Track {track_id(data, trak)} has no valid chunk-offset table."
            )

    mdat = next(item for item in top if item["type"] == "mdat")
    aux_start = mdat["payload_end"]
    aux_tail = data[aux_start:]
    if len(aux_tail) != AUXILIARY_DATA_SIZE:
        raise ValueError(
            f"Auxiliary tail is {len(aux_tail)} bytes; expected "
            f"{AUXILIARY_DATA_SIZE}."
        )
    if aux_tail != AUXILIARY_DATA:
        raise ValueError("Auxiliary tail contents do not match the generated samples.")

    print(f"OUTPUT SIZE: {os.path.getsize(filename)} bytes")
    print(f"OUTPUT STREAMS: {len(video)} video, {len(audio)} audio")
    print(f"MDAT PAYLOAD: {mdat['size'] - mdat['header']} bytes")
    print(f"AUXILIARY DATA: {AUXILIARY_DATA_SIZE} bytes")
    print("CHUNK OFFSETS: stco/co64")
    print(f"OUTPUT SHA256: {sha256_file(filename)}")


def sha256_file(filename):
    digest = hashlib.sha256()

    with open(filename, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)

    return digest.hexdigest()


def _is_fragmented_mp4(filename):
    with open(filename, "rb") as f:
        return any(x["type"] == "moof" for x in parse_boxes(f.read()))


def _flatten_fragmented_mp4(input_file):
    import subprocess
    import tempfile

    fd, temp_path = tempfile.mkstemp(prefix="dao_flat_", suffix=".mp4")
    os.close(fd)
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", input_file, "-map", "0:v:0", "-map", "0:a:0",
             "-c", "copy", "-movflags", "+faststart", temp_path],
            capture_output=True, text=True
        )
        if result.returncode:
            raise RuntimeError(
                "ffmpeg could not flatten the fragmented MP4.\n" +
                (result.stderr or "unknown ffmpeg error").strip()
            )
        return temp_path
    except FileNotFoundError:
        raise RuntimeError(
            "Fragmented MP4 input requires ffmpeg. In Termux run: pkg install ffmpeg"
        )
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


def _fragmented_video_duration_ts(input_file):
    import subprocess
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=time_base,duration_ts", "-of", "json",
         input_file],
        capture_output=True, text=True
    )
    if result.returncode:
        raise RuntimeError("ffprobe could not read the fragmented video timing.")
    obj = json.loads(result.stdout)
    streams = obj.get("streams", [])
    if not streams or streams[0].get("duration_ts") is None:
        return None
    return int(streams[0]["duration_ts"])


def _restore_fragmented_video_timing(flat_file, original_file):
    """Restore the final video sample duration from the fragmented source."""
    target_duration = _fragmented_video_duration_ts(original_file)
    if target_duration is None:
        return

    data = bytearray(Path(flat_file).read_bytes())
    top = parse_boxes(data)
    moov = next(x for x in top if x["type"] == "moov")
    video_trak = next(
        x for x in children(data, moov)
        if x["type"] == "trak" and handler_type(data, x) == "vide"
    )
    mdia = child(data, video_trak, "mdia")
    minf = child(data, mdia, "minf")
    stbl = child(data, minf, "stbl")
    stts = child(data, stbl, "stts")

    entries = parse_stts(data, stbl)
    if not entries:
        return

    current = sum(c * d for c, d in entries)
    if target_duration <= current:
        return

    # Add the missing tail duration to the final sample/run.
    extra = target_duration - current
    last_count, last_duration = entries[-1]
    if last_count == 1:
        entries[-1] = (1, last_duration + extra)
    else:
        entries[-1] = (last_count - 1, last_duration)
        entries.append((1, last_duration + extra))

    new_stts = make_stts(entries)

    # Restore both the sample-table duration and mdhd duration. The
    # fragmented source's final sample can be longer than the flattened
    # remuxer's default duration.
    source_mdhd = child(data, mdia, "mdhd")
    new_mdhd = set_mdhd(data, source_mdhd, target_duration)

    # Rebuild only the moov hierarchy; media payload bytes stay untouched.
    new_stbl = replace_children(data, stbl, {"stts": new_stts})
    new_minf = replace_children(data, minf, {"stbl": new_stbl})
    new_mdia = replace_children(data, mdia, {"minf": new_minf, "mdhd": new_mdhd})
    new_trak = replace_children(data, video_trak, {"mdia": new_mdia})
    new_moov = replace_children(data, moov, {"trak": new_trak})

    # replace_children cannot replace a particular trak when there are
    # multiple traks, so rebuild moov explicitly.
    moov_items = []
    for item in children(data, moov):
        moov_items.append(new_trak if item["type"] == "trak" and item["offset"] == video_trak["offset"]
                          else item["raw"])
    new_moov = box("moov", b"".join(moov_items))

    rebuilt = bytearray()
    for item in top:
        rebuilt += new_moov if item["type"] == "moov" else item["raw"]
    Path(flat_file).write_bytes(rebuilt)


def build_output(input_file, output_file, preserve_all=False):
    if _is_fragmented_mp4(input_file):
        flat = _flatten_fragmented_mp4(input_file)
        try:
            _restore_fragmented_video_timing(flat, input_file)
            return build_output(flat, output_file, preserve_all=True)
        finally:
            try:
                os.remove(flat)
            except OSError:
                pass

    with open(input_file, "rb") as f:
        data = f.read()

    top = parse_boxes(data)

    ftyp = next((x for x in top if x["type"] == "ftyp"), None)
    moov = next((x for x in top if x["type"] == "moov"), None)
    mdat = next((x for x in top if x["type"] == "mdat"), None)

    if not ftyp or not moov or not mdat:
        raise ValueError("Input must contain ftyp, moov and mdat.")

    traks = [
        x for x in children(data, moov)
        if x["type"] == "trak"
    ]

    audio_tracks = [
        x for x in traks
        if handler_type(data, x) == "soun"
    ]

    video_tracks = [
        x for x in traks
        if handler_type(data, x) == "vide"
    ]

    if not video_tracks:
        raise ValueError("Input contains no video track.")
    if not audio_tracks:
        raise ValueError("Input contains no audio track.")

    video = video_tracks[0]

    # If the MP4 has multiple audio tracks, choose the first AAC/mp4a track
    # as the DAO source instead of rejecting the entire file.
    audio = None
    for candidate in audio_tracks:
        stbl_candidate = child(
            data,
            child(data, child(data, candidate, "mdia"), "minf"),
            "stbl",
        )
        stsd_candidate = child(data, stbl_candidate, "stsd")
        payload = data[
            stsd_candidate["payload_start"]:stsd_candidate["payload_end"]
        ]
        if b"mp4a" in payload:
            audio = candidate
            break

    if audio is None:
        raise ValueError("Input contains no AAC/mp4a audio track.")

    print(
        f"INPUT TRACKS: {len(traks)} total "
        f"({len(video_tracks)} video, {len(audio_tracks)} audio); "
        f"selected video track #{traks.index(video)+1}, "
        f"selected AAC track #{traks.index(audio)+1}"
    )

    # Resolution/frame-rate independent: do not hard-code 1080p, 4K, 30fps, or 60fps.
    # Support AVC/H.264 and HEVC/H.265 sample entries without changing the
    # encoded media payload.
    video_stbl = child(
        data,
        child(data, child(data, video, "mdia"), "minf"),
        "stbl",
    )
    audio_stbl = child(
        data,
        child(data, child(data, audio, "mdia"), "minf"),
        "stbl",
    )

    video_stsd = child(data, video_stbl, "stsd")
    audio_stsd = child(data, audio_stbl, "stsd")

    video_stsd_payload = data[
        video_stsd["payload_start"]:video_stsd["payload_end"]
    ]
    audio_stsd_payload = data[
        audio_stsd["payload_start"]:audio_stsd["payload_end"]
    ]

    if not any(x in video_stsd_payload for x in (b"avc1", b"avc3", b"hvc1", b"hev1")):
        raise ValueError(
            "Unsupported video codec. Supported sample entries: "
            "avc1/avc3 (H.264) and hvc1/hev1 (H.265)."
        )

    if b"mp4a" not in audio_stsd_payload:
        raise ValueError("Input audio is not AAC/mp4a.")

    global AUX_SAMPLE_COUNT, AUXILIARY_DATA_SIZE, AUXILIARY_DATA
    source_audio_frames = len(parse_stsz(data, audio_stbl))
    AUX_SAMPLE_COUNT = source_audio_frames * 9
    AUXILIARY_DATA_SIZE = AUX_SAMPLE_COUNT * AUX_SAMPLE_SIZE
    AUXILIARY_DATA = AUXILIARY_SAMPLE * AUX_SAMPLE_COUNT
    print(f"AUDIO SOURCE FRAMES: {source_audio_frames}")
    print(f"AUXILIARY SAMPLE COUNT: {AUX_SAMPLE_COUNT}")
    print(f"AUXILIARY DATA SIZE: {AUXILIARY_DATA_SIZE} bytes")

    old_payload = data[mdat["payload_start"]:mdat["payload_end"]]
    old_payload_start = mdat["payload_start"]

    # Initial estimates. The loop converges because the final moov size is
    # itself a function of the rewritten chunk offsets.
    delta = ftyp["size"] + moov["size"] + 8 - old_payload_start
    aux_offset = 0

    final_moov = None

    for _ in range(16):
        primary = rebuild_primary_audio(data, audio, delta, 3, preserve_all)
        video_box = rebuild_video(data, video, delta, 2, preserve_all)

        clone = clone_audio_track(data, audio, 3, delta, aux_offset, preserve_all)

        final_moov = make_moov(
            data,
            moov,
            primary,
            clone,
            video_box,
            preserve_all,
        )

        new_mdat_payload_start = (
            ftyp["size"] + len(final_moov) + 8
        )

        new_delta = new_mdat_payload_start - old_payload_start
        new_aux_offset = new_mdat_payload_start + len(old_payload)

        if new_delta == delta and new_aux_offset == aux_offset:
            break

        delta = new_delta
        aux_offset = new_aux_offset
    else:
        raise ValueError("MP4 layout/offset calculation did not converge.")

    # Final rebuild with the converged offsets.
    primary = rebuild_primary_audio(data, audio, delta, 3, preserve_all)
    video_box = rebuild_video(data, video, delta, 2, preserve_all)
    clone = clone_audio_track(data, audio, 3, delta, aux_offset, preserve_all)

    final_moov = make_moov(
        data,
        moov,
        primary,
        clone,
        video_box,
        preserve_all,
    )

    new_mdat_payload_start = ftyp["size"] + len(final_moov) + 8
    expected_delta = new_mdat_payload_start - old_payload_start
    expected_aux = new_mdat_payload_start + len(old_payload)

    if expected_delta != delta or expected_aux != aux_offset:
        # One final correction pass.
        delta = expected_delta
        aux_offset = expected_aux

        primary = rebuild_primary_audio(data, audio, delta, 3, preserve_all)
        video_box = rebuild_video(data, video, delta, 2, preserve_all)
        clone = clone_audio_track(data, audio, 3, delta, aux_offset, preserve_all)

        final_moov = make_moov(
            data,
            moov,
            primary,
            clone,
            video_box,
            preserve_all,
        )

        new_mdat_payload_start = ftyp["size"] + len(final_moov) + 8
        if new_mdat_payload_start - old_payload_start != delta:
            raise ValueError("Final MP4 chunk offsets are inconsistent.")

    final_mdat = box("mdat", old_payload)

    with open(output_file, "wb") as f:
        f.write(ftyp["raw"])
        f.write(final_moov)
        f.write(final_mdat)
        f.write(AUXILIARY_DATA)

    validate_output(output_file)


def main(input_file, output_file):
    if not os.path.isfile(input_file):
        raise FileNotFoundError(input_file)

    if os.path.abspath(input_file) == os.path.abspath(output_file):
        raise ValueError("Input and output files must be different.")

    print("Reading MP4...")
    print(f"INPUT SIZE: {os.path.getsize(input_file)} bytes")
    build_output(input_file, output_file)

    print("========================================")
    print("DAO TRANSFORMATION COMPLETE")
    print("========================================")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python dao_transform.py input.mp4 output.mp4")
        sys.exit(1)

    try:
        main(sys.argv[1], sys.argv[2])
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
