"""RSA-PSS request signing for the Kalshi trade API.

Reference:
    https://docs.kalshi.com/getting_started/api_keys
    https://docs.kalshi.com/getting_started/making_your_first_request

Signing contract (do not change without understanding the Kalshi spec):

    message = f"{timestamp_ms}{HTTP_METHOD}{PATH}".encode("utf-8")
    signature = RSA-PSS(SHA-256, MGF1-SHA256, salt_length=32) over message
    header value = base64(signature)

Important details:
    - PATH is the request path WITHOUT the query string. The "?foo=bar"
      portion is NOT signed. Kalshi will reject the request with a
      signature mismatch otherwise.
    - timestamp_ms is the current unix time in MILLISECONDS (not seconds).
    - The request BODY is NOT included in the signed message.
    - Demo and production use SEPARATE key pairs. Reusing a key across
      environments will produce signature failures (and is a footgun for
      "I thought I was on demo" mistakes).

WebSocket auth uses the same contract with method=GET and path=
"/trade-api/ws/v2"; the headers are sent on the upgrade handshake.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_mcp_server.errors import AuthError

HEADER_KEY = "KALSHI-ACCESS-KEY"
HEADER_SIG = "KALSHI-ACCESS-SIGNATURE"
HEADER_TS = "KALSHI-ACCESS-TIMESTAMP"


@dataclass(frozen=True)
class SignedHeaders:
    """The three headers required on every authenticated Kalshi request."""

    key: str
    signature: str
    timestamp_ms: str

    def as_dict(self) -> dict[str, str]:
        return {
            HEADER_KEY: self.key,
            HEADER_SIG: self.signature,
            HEADER_TS: self.timestamp_ms,
        }


def load_private_key(
    *,
    pem_path: str | Path | None = None,
    pem_text: str | None = None,
) -> rsa.RSAPrivateKey:
    """Load an RSA private key from a PEM file or inline PEM string.

    Exactly one of `pem_path` / `pem_text` must be provided.
    """
    if (pem_path is None) == (pem_text is None):
        raise AuthError(
            "Exactly one of pem_path or pem_text must be provided to load_private_key()."
        )

    if pem_path is not None:
        try:
            pem_bytes = Path(pem_path).read_bytes()
        except FileNotFoundError as exc:
            raise AuthError(f"Private key file not found: {pem_path}") from exc
        except OSError as exc:
            raise AuthError(f"Failed to read private key file {pem_path}: {exc}") from exc
    else:
        # Allow users to pass `\n` literally in env vars; normalize to real newlines.
        assert pem_text is not None  # for type checker
        pem_bytes = pem_text.replace("\\n", "\n").encode("utf-8")

    try:
        key = serialization.load_pem_private_key(pem_bytes, password=None)
    except (ValueError, TypeError) as exc:
        raise AuthError(f"Could not parse private key as PEM: {exc}") from exc

    if not isinstance(key, rsa.RSAPrivateKey):
        raise AuthError(
            f"Expected an RSA private key, got {type(key).__name__}. "
            "Kalshi keys are RSA — check you saved the right file."
        )
    return key


def _path_without_query(path_or_url: str) -> str:
    """Return the path component, stripped of query string and fragment.

    Accepts either a full URL or a path. Kalshi signs only the path,
    NOT including the query string.
    """
    parts = urlsplit(path_or_url)
    return parts.path or path_or_url


def _now_ms() -> int:
    return int(time.time() * 1000)


class KalshiSigner:
    """Produces the three Kalshi auth headers for a given request.

    Construct once with the loaded private key and key ID, reuse for the
    lifetime of the process.
    """

    def __init__(
        self,
        *,
        key_id: str,
        private_key: rsa.RSAPrivateKey,
        clock: callable = _now_ms,  # type: ignore[type-arg]
    ) -> None:
        if not key_id:
            raise AuthError("KalshiSigner requires a non-empty key_id.")
        self._key_id = key_id
        self._private_key = private_key
        self._clock = clock

    @classmethod
    def from_env(
        cls,
        *,
        key_id: str,
        pem_path: str | Path | None = None,
        pem_text: str | None = None,
    ) -> KalshiSigner:
        key = load_private_key(pem_path=pem_path, pem_text=pem_text)
        return cls(key_id=key_id, private_key=key)

    @property
    def key_id(self) -> str:
        return self._key_id

    def sign(
        self,
        *,
        method: str,
        path: str,
        timestamp_ms: int | None = None,
    ) -> SignedHeaders:
        """Sign a request and return the three auth headers.

        Args:
            method: HTTP method (e.g. "GET", "POST"). Case matters — Kalshi
                signs the exact bytes you send.
            path: Request path or full URL. Query string is stripped before
                signing.
            timestamp_ms: Override the timestamp (mostly for testing). If
                omitted, current time is used.
        """
        ts = timestamp_ms if timestamp_ms is not None else self._clock()
        method_up = method.upper()
        clean_path = _path_without_query(path)

        message = f"{ts}{method_up}{clean_path}".encode()
        sig_bytes = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return SignedHeaders(
            key=self._key_id,
            signature=base64.b64encode(sig_bytes).decode("ascii"),
            timestamp_ms=str(ts),
        )
