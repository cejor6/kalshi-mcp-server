"""Size-bounded response-body reading for the non-Kalshi egress clients.

Shared by `external_data` (public data feeds) and `scoring` (the Jev API) —
the two places this server talks to a host other than Kalshi and reads a body
whose size it does not control.

Why not just `response.aiter_bytes()` with a byte counter: httpx advertises
`Accept-Encoding: gzip, deflate` by default and DECODES each chunk in full
before handing it over, so a counter applied to the decoded chunks runs too
late — a ~10KB gzip chunk can expand to ~10MB in memory before the first cut
(a decompression bomb). The callers therefore send `Accept-Encoding: identity`
(`IDENTITY_ENCODING_HEADERS`), and this reader iterates the RAW stream so a
server that ignores that request still can't expand past the cap: gzip /
deflate are decoded here incrementally with zlib's `max_length`, so at most
`max_bytes + 1` of decoded output ever exists; any other encoding is refused.

Reading also STOPS at the compressed stream's end marker. Bytes after it
(trailing garbage, extra gzip members) are never decoded — zlib would
otherwise park every further byte in `unused_data`, an unbounded buffer — and
their presence is reported as `truncated`. A compressed stream that ends
WITHOUT its end marker is cut off, so that is `truncated` too.
"""

from __future__ import annotations

import zlib
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

# Merge into a client's default headers so a compliant upstream never
# compresses in the first place.
IDENTITY_ENCODING_HEADERS: dict[str, str] = {"Accept-Encoding": "identity"}

# How much leading input a `deflate` decoder buffers while it is still
# deciding between the zlib-wrapped form and raw (headerless) deflate. A bad
# zlib header is detected within the first two bytes; this is just slack.
_DEFLATE_PROBE_BYTES = 64


class ContentEncodingError(ValueError):
    """The upstream body used an encoding we refuse, or failed to decode."""


class _Inflater:
    """zlib decoder for gzip/deflate with httpx's raw-deflate fallback.

    `Content-Encoding: deflate` is SUPPOSED to be zlib-wrapped (RFC 9110), but
    some servers send raw deflate; httpx retries those with `-MAX_WBITS` on an
    "incorrect header check", and so do we. The fallback is only possible
    before the decoder has produced output, so the leading input is kept
    (bounded) until then and replayed into the raw decoder.
    """

    def __init__(self, *, allow_raw_fallback: bool) -> None:
        # MAX_WBITS | 32 auto-detects a gzip OR zlib header.
        self._d = zlib.decompressobj(zlib.MAX_WBITS | 32)
        self._probing = allow_raw_fallback
        self._seen = bytearray()

    def decompress(self, data: bytes, max_length: int) -> bytes:
        if not self._probing:
            return self._d.decompress(data, max_length)
        self._seen.extend(data)
        try:
            out = self._d.decompress(data, max_length)
        except zlib.error:
            self._probing = False
            self._d = zlib.decompressobj(-zlib.MAX_WBITS)
            return self._d.decompress(bytes(self._seen), max_length)
        if out or len(self._seen) > _DEFLATE_PROBE_BYTES:
            self._probing = False
            self._seen.clear()
        return out

    @property
    def eof(self) -> bool:
        return self._d.eof

    @property
    def unconsumed_tail(self) -> bytes:
        return self._d.unconsumed_tail

    @property
    def unused_data(self) -> bytes:
        return self._d.unused_data


def _decoder_for(content_encoding: str) -> _Inflater | None:
    """A bounded decoder for gzip/deflate, None for identity; else raise."""
    encoding = content_encoding.strip().lower()
    if encoding in ("", "identity"):
        return None
    if encoding in ("gzip", "x-gzip"):
        return _Inflater(allow_raw_fallback=False)
    if encoding == "deflate":
        return _Inflater(allow_raw_fallback=True)
    raise ContentEncodingError(
        f"upstream sent Content-Encoding {content_encoding!r} despite "
        "Accept-Encoding: identity; refusing to decode it"
    )


async def _has_more(chunks: AsyncIterator[bytes]) -> bool:
    """True if the raw stream yields any further non-empty chunk."""
    async for extra in chunks:
        if extra:
            return True
    return False


async def read_capped(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    """Read at most `max_bytes` of DECODED body from a streamed response.

    Returns `(body, truncated)`. `truncated` is True when more body existed
    than the cap allowed, when bytes followed the end of a compressed stream,
    or when a compressed stream ended without its end marker. Raises
    `ContentEncodingError` for an encoding other than identity/gzip/deflate,
    or a corrupt compressed stream.
    """
    if response.is_stream_consumed:
        # The body was buffered before we got it (httpx does this for a
        # Response built from in-memory bytes, e.g. under MockTransport), so
        # there is no stream left to bound — just apply the cap.
        content = response.content
        return content[:max_bytes], len(content) > max_bytes
    decoder = _decoder_for(response.headers.get("content-encoding", ""))
    out = bytearray()
    chunks = response.aiter_raw()
    try:
        async for chunk in chunks:
            if decoder is None:
                remaining = max_bytes - len(out)
                out.extend(chunk[: max(remaining, 0)])
                if len(chunk) > remaining:
                    return bytes(out), True
                continue
            data = chunk
            while data:
                # Allow ONE byte past the cap so "exactly at the cap" and "over
                # it" are distinguishable. Always >= 1 (0 would mean unlimited).
                out.extend(decoder.decompress(data, max_bytes + 1 - len(out)))
                if len(out) > max_bytes:
                    return bytes(out[:max_bytes]), True
                if decoder.eof:
                    # End of the compressed stream: stop reading. Anything
                    # after it is reported, never decoded or buffered.
                    return bytes(out), bool(decoder.unused_data) or await _has_more(chunks)
                data = decoder.unconsumed_tail
        if decoder is not None:
            # Flush what zlib may still hold (bounded), then check the stream
            # actually reached its end marker — a missing one means the body
            # was cut off.
            out.extend(decoder.decompress(b"", max_bytes + 1 - len(out)))
            if len(out) > max_bytes:
                return bytes(out[:max_bytes]), True
            return bytes(out), not decoder.eof
    except zlib.error as exc:
        raise ContentEncodingError(f"corrupt compressed body: {exc}") from exc
    return bytes(out), False
