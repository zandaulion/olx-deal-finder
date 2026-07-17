"""Web Push (VAPID) notifications for the PWA.

Self-served VAPID — no Firebase/Google account needed. The private key is
generated once and kept on disk (git-ignored); the public key is handed to the
browser as the ``applicationServerKey`` when it subscribes. Pushes are sent to
the browser's own push service, so they arrive even when the PWA is closed.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush

# VAPID 'sub' claim — a contact the push service can reach. Any mailto works.
VAPID_SUBJECT = "mailto:olx-deals@localhost"


def _load_or_create(path: Path) -> ec.EllipticCurvePrivateKey:
    if path.exists():
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    priv = ec.generate_private_key(ec.SECP256R1())
    path.write_bytes(priv.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    path.chmod(0o600)
    return priv


class Push:
    """Holds the VAPID key pair and sends encrypted web-push messages."""

    def __init__(self, key_path: str | Path):
        self.key_path = Path(key_path)
        self._priv = _load_or_create(self.key_path)

    def public_key_b64(self) -> str:
        """applicationServerKey: uncompressed public point, base64url (unpadded)."""
        raw = self._priv.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint)
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    def send(self, subscription: dict, payload: dict) -> None:
        """Send one push. Raises WebPushException (inspect .response.status_code)."""
        webpush(
            subscription_info=subscription,
            data=json.dumps(payload),
            vapid_private_key=str(self.key_path),
            vapid_claims={"sub": VAPID_SUBJECT},
            timeout=15,
        )

    def notify_all(self, subscriptions: list[dict], payload: dict) -> list[str]:
        """Send to every subscription; return endpoints that are gone (410/404)."""
        dead: list[str] = []
        for sub in subscriptions:
            try:
                self.send(sub, payload)
            except WebPushException as exc:
                status = getattr(exc.response, "status_code", None)
                if status in (404, 410):  # subscription expired/unsubscribed
                    dead.append(sub["endpoint"])
                # other push errors (network, 5xx): keep the sub, retry next time
            except Exception:
                # malformed sub / unexpected error — never break the sync loop
                pass
        return dead
