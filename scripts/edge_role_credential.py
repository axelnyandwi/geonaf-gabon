"""Print the statement that gives geonaf_edge_reader LOGIN with a SCRAM-SHA-256 verifier (D5, manual tool).

The password is typed twice, hidden (getpass), and is never printed, logged or written to a file. Only the
salted SCRAM-SHA-256 verifier is printed, inside an ALTER ROLE statement to paste in the SQL Editor after
13_edge_reader_role.sql. PostgreSQL stores a verifier as given: the editor history and the server logs then
hold the verifier, never the password. A verifier does not let anyone log in (the client key cannot be
derived from it); it only allows an offline guessing attack, defeated by a long random password.

Password rule: 32 to 128 characters among A-Z a-z 0-9 _ - (no percent-encoding needed in the connection
URL, and SASLprep leaves it unchanged, so PostgreSQL and Python derive the same keys).
No network access. Usage: python scripts/edge_role_credential.py
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import os
import re
import sys

ROLE = "geonaf_edge_reader"
ITERATIONS = 4096                       # PostgreSQL's default scram_iterations.
PASSWORD = re.compile(r"[A-Za-z0-9_-]{32,128}")
VERIFIER = re.compile(r"SCRAM-SHA-256\$4096:[A-Za-z0-9+/]{22}==\$[A-Za-z0-9+/]{43}=:[A-Za-z0-9+/]{43}=")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def scram_sha256_verifier(password: str, *, salt: bytes, iterations: int = ITERATIONS) -> str:
    """RFC 5802 / 7677 verifier in PostgreSQL's format: SCRAM-SHA-256$<i>:<salt>$<StoredKey>:<ServerKey>."""
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    server_key = hmac.new(salted, b"Server Key", "sha256").digest()
    return f"SCRAM-SHA-256${iterations}:{_b64(salt)}${_b64(hashlib.sha256(client_key).digest())}:{_b64(server_key)}"


def login_statement(verifier: str) -> str:
    if not VERIFIER.fullmatch(verifier):
        raise ValueError("Vérificateur SCRAM-SHA-256 inattendu.")
    return f"ALTER ROLE {ROLE} WITH LOGIN PASSWORD '{verifier}';"


def main() -> int:
    first = getpass.getpass("Mot de passe du rôle Edge (masqué) : ")
    if not PASSWORD.fullmatch(first):
        print("Refusé : 32 à 128 caractères parmi A-Z a-z 0-9 _ - (aucune valeur affichée).", file=sys.stderr)
        return 2
    if not hmac.compare_digest(first, getpass.getpass("Confirmation : ")):
        print("Refusé : les deux saisies diffèrent.", file=sys.stderr)
        return 2
    print(login_statement(scram_sha256_verifier(first, salt=os.urandom(16))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
