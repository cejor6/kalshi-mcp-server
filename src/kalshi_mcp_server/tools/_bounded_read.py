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
`max_bytes` of decoded output ever exists; any other encoding is refused.
"""

from __future__ import annotations

import zlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

# Merge into a client's default headers so a compliant upstream never
# compresses in the first place.
IDENTITY_ENCODING_HEADERS: dict[str, str] = {"Accept-Encoding": "identity"}


class ContentEncodingError(ValueError):
    """The upstream body used an encoding we refuse, or failed to decode."""


def _decoder_for(content_encoding: str) -> zlib._Decompress | None:
    """A bounded zlib decoder for gzip/deflate, None for identity; else raise."""
    encoding = content_encoding.strip().lower()
    if encoding in ("", "identity"):
        return None
    if encoding in ("gzip", "x-gzip", "deflate"):
        # MAX_WBITS | 32 auto-detects a gzip OR zlib header, which covers gzip
        # and the RFC 9110 (zlib-wrapped) form of deflate.
        return zlib.decompressobj(zlib.MAX_WBITS | 32)
    raise ContentEncodingError(
        f"upstream sent Content-Encoding {content_encoding!r} despite "
        "Accept-Encoding: identity; refusing to decode it"
    )


async def read_capped(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    """Read at most `max_bytes` of DECODED body from a streamed response.

    Returns `(body, truncated)`; `truncated` is True when more body existed
    than the cap allowed. Raises `ContentEncodingError` for an encoding other
    than identity/gzip/deflate, or a corrupt compressed stream.
    """
    if response.is_stream_consumed:
        # The body was buffered before we got it (httpx does this for a
        # Response built from in-memory bytes, e.g. under MockTransport), so
        # there is no stream left to bound — just apply the cap.
        content = response.content
        return content[:max_bytes], len(content) > max_bytes
    decoder = _decoder_for(response.headers.get("content-encoding", ""))
    out = bytearray()
    try:
        async for chunk in response.aiter_raw():
            data = chunk
            while data:
                remaining = max_bytes - len(out)
                if remaining <= 0:
                    return bytes(out), True
                if decoder is None:
                    out.extend(data[:remaining])
                    if len(data) > remaining:
                        return bytes(out), True
                    break
                # max_length bounds the decoded output of THIS call; any input
                # it couldn't process yet lands in unconsumed_tail.
                out.extend(decoder.decompress(data, remaining))
                data = decoder.unconsumed_tail
                if decoder.eof:
                    break
        if decoder is not None and not decoder.eof:
            # Drain output zlib may still be holding, still bounded by the cap.
            remaining = max_bytes - len(out)
            pending = decoder.decompress(b"", remaining + 1)
            out.extend(pending[:remaining])
            if len(pending) > remaining:
                return bytes(out), True
    except zlib.error as exc:
        raise ContentEncodingError(f"corrupt compressed body: {exc}") from exc
    return bytes(out), False
