"""Run once to generate the Ed25519 keypair for QR signing.

Usage: uv run python -m app.scripts.generate_qr_keypair

Prints QR_SIGNING_PRIVATE_KEY_HEX (goes in infra/.env, never committed) and QR_PUBLIC_KEY_HEX
(goes into the mobile build via --dart-define — see mobile/CONTRIBUTING.md, Task 14).
"""

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.domain.qr.crypto import derive_public_key_hex


def main() -> None:
    private_key = Ed25519PrivateKey.generate()
    private_hex = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    ).hex()

    print(f"QR_SIGNING_PRIVATE_KEY_HEX={private_hex}")
    print(f"QR_PUBLIC_KEY_HEX={derive_public_key_hex(private_hex)}")


if __name__ == "__main__":
    main()
