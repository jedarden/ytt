"""Recovery smoke — OAuth-state loss, backup/restore, and torn-entry repair.

Unit-level pin of the operator contract documented in
``deploy/OAUTH-STATE-RUNBOOK.md`` (bead ``ytt-c53a2f56``): the ``ytt-oauth-state``
PVC is the one ytt volume whose loss costs every connected client an
interactive re-login, so each behavior the runbook's backup/restore/repair
procedures lean on is exercised here end to end at the unit level — against
the *real* storage stack the OAuthProxy builds (same key-derivation chain,
same ``FileTreeStore``, same Fernet wrapper with ``raise_on_decryption_error=False``),
not a mock.  The repo has no staging cluster (CACHE-RUNBOOK §10), so this
file is the sanctioned recovery drill for OAuth state.

Scenarios (and the runbook section each protects):

- wiped volume degrades to clean misses,   (§5 — state loss is a re-login
  rebuilds via re-registration              event, never a server failure)
- backup → swap-restore round-trip          (§4.2 — the directory-swap
  preserves registrations + token sets      restore procedure)
- rotated secret → new fingerprint dir,     (§1.1/§4.3 — same-secret
  soft misses, orphaned old tree            requirement, soft mismatch)
- torn entry file raises per-key, siblings  (§6 — torn restores are NOT
  serve, overwrite/delete repairs heal      silent; targeted repair)
- corrupt ciphertext in an intact envelope  (§1.1 — decryption misses are
  degrades to a silent miss                 soft by construction)
- backup opacity: no plaintext secret       (§1/§2/§7.2 — treat the backup
  material anywhere; envelope check         as a credential; validate by
  classifies every file                     envelope, never by content)
- runbook ↔ manifest drift guard            (§1/§3/§7 — facts the doc quotes)

The last two follow the repo's docs-pin pattern (``test_docs_env_coverage``,
``test_cache_recovery``): the runbook quotes manifest values and storage
behavior, so a manifest or fastmcp-upgrade change fails here until the doc
follows — the runbook can't silently drift into documenting a deployment or
a storage stack that no longer exists.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml  # via fastmcp (runtime dependency) — always present in the venv
from cryptography.fernet import Fernet
from fastmcp.server.auth.jwt_issuer import derive_jwt_key
from key_value.aio.adapters.pydantic import PydanticAdapter
from key_value.aio.errors import DeserializationError
from key_value.aio.errors.store import PathSecurityError
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_RUNBOOK = REPO_ROOT / "deploy" / "OAUTH-STATE-RUNBOOK.md"
MANIFEST_DIR = REPO_ROOT / "deploy" / "k8s" / "ardenone-cluster" / "ytt"

# Distinctive stand-in secret material. Generated per test — never a real
# credential; the derivation chain under test is keyed off the *shape* of the
# chain (oauth_proxy.proxy), not any particular value.
SECRET_A = "unit-test-oauth-client-secret-a-0f4e2c"
SECRET_B = "unit-test-oauth-client-secret-b-9b71ad"  # the "rotated" secret

# The logical collection names the proxy stores under (oauth_proxy.proxy) —
# quoted by OAUTH-STATE-RUNBOOK §1.2's table.
CLIENTS_COLLECTION = "mcp-oauth-proxy-clients"
TOKENS_COLLECTION = "mcp-upstream-tokens"


# --------------------------------------------------------------------------- #
# The storage stack, built exactly the way OAuthProxy builds it               #
# --------------------------------------------------------------------------- #


class StoredClient(BaseModel):
    """Stand-in for ProxyDCRClient with the fields a DCR registration carries."""

    client_id: str
    client_secret: str | None = None
    redirect_uris: list[str] = []
    grant_types: list[str] = ["authorization_code", "refresh_token"]
    token_endpoint_auth_method: str = "none"


class StoredTokenSet(BaseModel):
    """Stand-in for UpstreamTokenSet (the IdP tokens behind each session)."""

    access_token: str
    refresh_token: str | None = None


def _storage_encryption_key(client_secret: str) -> bytes:
    """The two-step derivation from oauth_proxy.proxy (runbook §1.1)."""
    jwt_signing_key = derive_jwt_key(
        high_entropy_material=client_secret, salt="fastmcp-jwt-signing-key"
    )
    return derive_jwt_key(
        high_entropy_material=jwt_signing_key.decode(),
        salt="fastmcp-storage-encryption-key",
    )


def _fingerprint_dir(home: Path, client_secret: str) -> Path:
    """`settings.home / "oauth-proxy" / sha256(storage_key)[:12]` — §1.1."""
    fingerprint = hashlib.sha256(_storage_encryption_key(client_secret)).hexdigest()[:12]
    return home / "oauth-proxy" / fingerprint


def _build_store(home: Path, client_secret: str) -> tuple[PydanticAdapter, PydanticAdapter]:
    """Fresh adapters over the same tree — the restart-equivalent reader.

    Mirrors oauth_proxy.proxy: FileTreeStore + FernetEncryptionWrapper with
    ``raise_on_decryption_error=False`` (the softness §1.1/§4.3 of the
    runbook relies on).
    """
    storage_dir = _fingerprint_dir(home, client_secret)
    storage_dir.mkdir(parents=True, exist_ok=True)
    key = _storage_encryption_key(client_secret)
    file_store = FileTreeStore(
        data_directory=storage_dir,
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(storage_dir),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(
            storage_dir
        ),
    )
    client_storage = FernetEncryptionWrapper(
        key_value=file_store,
        fernet=Fernet(key=key),
        raise_on_decryption_error=False,
    )
    clients = PydanticAdapter(
        key_value=client_storage,
        pydantic_model=StoredClient,
        default_collection=CLIENTS_COLLECTION,
        raise_on_validation_error=True,
    )
    tokens = PydanticAdapter(
        key_value=client_storage,
        pydantic_model=StoredTokenSet,
        default_collection=TOKENS_COLLECTION,
        raise_on_validation_error=True,
    )
    return clients, tokens


async def _seed_sessions(
    home: Path, client_secret: str
) -> tuple[StoredClient, StoredTokenSet]:
    """Register one client + one upstream token set (a connected client)."""
    clients, tokens = _build_store(home, client_secret)
    registration = StoredClient(
        client_id="reg_unit_test_1",
        client_secret="dcr-pairwise-secret",
        redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
    )
    token_set = StoredTokenSet(
        access_token="upstream-access-token-value",
        refresh_token="upstream-refresh-token-value",
    )
    await clients.put(registration.client_id, registration)
    # the proxy stores upstream token sets with a TTL (the longest-lived token
    # in the set); registrations are stored without one — that asymmetry is
    # what puts `expires_at` on the token envelope but not the registration's
    await tokens.put("subject:operator@example.com", token_set, ttl=3600)
    return registration, token_set


def _backup_volume(home: Path, backup_dir: Path) -> None:
    """The runbook §3 backup contract: the whole /state tree, no exclusions."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(home, backup_dir, dirs_exist_ok=True)


def _swap_restore(backup_dir: Path, home: Path) -> None:
    """The runbook §4.2 swap: restore beside the live tree, two renames."""
    staging = home / ".restore"
    staging.mkdir(parents=True, exist_ok=True)
    shutil.copytree(backup_dir, staging, dirs_exist_ok=True)
    live = home / "oauth-proxy"
    if live.exists():
        live.rename(home / "oauth-proxy.pre-restore")
    (staging / "oauth-proxy").rename(live)


# --------------------------------------------------------------------------- #
# Wiped volume degrades to clean misses, rebuilds via re-registration          #
# --------------------------------------------------------------------------- #


async def test_wiped_volume_degrades_to_clean_misses_and_rebuilds(tmp_path: Path) -> None:
    """State loss (runbook §5) is a re-login event, never a server failure.

    An empty/absent oauth-proxy tree reads as clean misses — nothing raises —
    and the first re-registration re-seeds it: the same code path a
    first-ever boot takes.
    """
    home = tmp_path / "recreated-pvc"  # never exists — the store creates it
    clients, tokens = _build_store(home, SECRET_A)

    assert await clients.get("reg_lost_session") is None  # miss, not a crash
    assert await tokens.get("subject:operator@example.com") is None

    # the client re-runs DCR + login: a fresh registration lands and reads back
    registration = StoredClient(client_id="reg_new_session")
    await clients.put(registration.client_id, registration)
    got = await clients.get("reg_new_session")
    assert got is not None and got.client_id == "reg_new_session"
    assert _fingerprint_dir(home, SECRET_A).is_dir()


# --------------------------------------------------------------------------- #
# Backup → swap-restore round-trip                                             #
# --------------------------------------------------------------------------- #


async def test_backup_swap_restore_roundtrip_preserves_sessions(tmp_path: Path) -> None:
    """The §4.2 procedure restores registrations and token sets intact.

    Backup → volume loss → swap-restore at the same mount path → a fresh
    store (restart-equivalent reader, same secret) reads both the DCR
    registration and the upstream token set back field-identical — the
    "existing clients do not re-login" contract the whole runbook exists for.
    """
    home = tmp_path / "state"  # fixed path: the store records absolute paths
    registration, token_set = await _seed_sessions(home, SECRET_A)

    backup = tmp_path / "backup"
    _backup_volume(home, backup)

    # the volume is lost; its replacement binds at the same mount path
    shutil.rmtree(home)
    _swap_restore(backup, home)

    clients, tokens = _build_store(home, SECRET_A)
    got_client = await clients.get(registration.client_id)
    assert got_client is not None
    assert got_client.client_id == registration.client_id
    assert got_client.client_secret == registration.client_secret
    assert got_client.redirect_uris == registration.redirect_uris
    assert got_client.grant_types == registration.grant_types

    got_tokens = await tokens.get("subject:operator@example.com")
    assert got_tokens is not None
    assert got_tokens.access_token == token_set.access_token
    assert got_tokens.refresh_token == token_set.refresh_token


async def test_restore_to_a_different_path_hard_fails_collections(tmp_path: Path) -> None:
    """The store records absolute paths — restore at the mount path, or else.

    Collection `-info.json` files carry the absolute directory the collection
    was created under. Restoring the tree at a *different* root (runbook
    §4.2's warning) leaves those recorded paths pointing outside the live
    store, and every read of every collection raises ``PathSecurityError`` —
    the hard per-collection failure this pin exists to explain. The
    deployment fixes ``FASTMCP_HOME=/state``, so the real procedure always
    restores at the path the backup was taken from; this test is why that
    line in the runbook is load-bearing, not stylistic.
    """
    home = tmp_path / "state"
    registration, _ = await _seed_sessions(home, SECRET_A)
    backup = tmp_path / "backup"
    _backup_volume(home, backup)

    misplaced = tmp_path / "elsewhere"  # a different "mount path"
    _swap_restore(backup, misplaced)

    clients, _ = _build_store(misplaced, SECRET_A)
    with pytest.raises(PathSecurityError):
        await clients.get(registration.client_id)


# --------------------------------------------------------------------------- #
# Rotated secret: new fingerprint dir, soft misses, orphaned old tree          #
# --------------------------------------------------------------------------- #


async def test_restore_under_rotated_secret_soft_misses_and_orphans_old_tree(
    tmp_path: Path,
) -> None:
    """Same-secret requirement (§1.1/§4.3): mismatch is harmless, not fatal.

    A secret rotated between backup and restore derives a *different*
    fingerprint directory, so the restored tree orphans on the volume: reads
    miss softly (never raise — ``raise_on_decryption_error=False``), clients
    re-register, and the server stays up. This is the documented
    global-logout lever, not an incident.
    """
    home = tmp_path / "live"
    registration, _ = await _seed_sessions(home, SECRET_A)
    backup = tmp_path / "backup"
    _backup_volume(home, backup)
    old_dir_name = _fingerprint_dir(home, SECRET_A).name

    # the secret rotated while the backup sat in cold storage; now restore it
    _swap_restore(backup, home)
    assert old_dir_name in {p.name for p in (home / "oauth-proxy").iterdir()}

    clients, _ = _build_store(home, SECRET_B)  # the post-rotation pod
    assert await clients.get(registration.client_id) is None  # soft miss

    # ...and the pod's own fresh directory is a *different* fingerprint
    assert _fingerprint_dir(home, SECRET_B).name != old_dir_name


# --------------------------------------------------------------------------- #
# Torn entry: per-key raise, siblings fine, overwrite/delete repairs heal      #
# --------------------------------------------------------------------------- #


def _clients_collection_dir(home: Path, client_secret: str) -> Path:
    storage_dir = _fingerprint_dir(home, client_secret)
    return next(
        d
        for d in storage_dir.iterdir()
        if d.is_dir() and d.name.startswith("S_mcp_oauth_proxy_clients-")
    )


async def test_torn_entry_raises_per_key_siblings_serve_and_repairs_heal(
    tmp_path: Path,
) -> None:
    """A structurally broken entry file is NOT a silent miss (runbook §6).

    The soft-miss magic only covers intact envelopes: a truncated file (the
    torn-restore shape) fails JSON parsing *before* decryption, and that
    per-key error is exactly why §4.2 validates envelopes before the swap.
    The targeted repairs §6 prescribes — overwrite, or delete-the-one-file —
    both heal without touching sibling sessions.
    """
    home = tmp_path / "live"
    clients, _ = _build_store(home, SECRET_A)
    for cid in ("reg_aaa", "reg_bbb", "reg_ccc"):
        await clients.put(cid, StoredClient(client_id=cid, client_secret=cid))

    col = _clients_collection_dir(home, SECRET_A)
    (col / "reg_bbb.json").write_bytes(b'{"created_at": "2026-09-25T00:00:0')  # torn

    fresh_clients, _ = _build_store(home, SECRET_A)  # restart-equivalent reader
    with pytest.raises(DeserializationError):
        await fresh_clients.get("reg_bbb")  # the torn key raises…
    assert await fresh_clients.get("reg_aaa") is not None  # …siblings keep serving
    assert await fresh_clients.get("reg_ccc") is not None

    # repair #1: the client re-registers over the torn key — heals
    await fresh_clients.put(
        "reg_bbb", StoredClient(client_id="reg_bbb", client_secret="fresh")
    )
    healed = await fresh_clients.get("reg_bbb")
    assert healed is not None and healed.client_secret == "fresh"

    # repair #2: targeted delete of a torn key degrades it to a clean miss
    (col / "reg_ccc.json").write_bytes(b"\x00\x01not json")
    with pytest.raises(DeserializationError):
        await fresh_clients.get("reg_ccc")
    (col / "reg_ccc.json").unlink()
    assert await fresh_clients.get("reg_ccc") is None  # miss, ready to re-register


# --------------------------------------------------------------------------- #
# Corrupt ciphertext in an intact envelope: silent miss                        #
# --------------------------------------------------------------------------- #


async def test_corrupt_ciphertext_in_intact_envelope_degrades_to_miss(
    tmp_path: Path,
) -> None:
    """Undecryptable-but-well-formed entries read back as misses (§1.1).

    ``raise_on_decryption_error=False`` is the property that makes a key
    mismatch a re-login event instead of a 500 — a flipped byte inside the
    Fernet token never surfaces as an exception to the proxy.
    """
    home = tmp_path / "live"
    registration, _ = await _seed_sessions(home, SECRET_A)
    col = _clients_collection_dir(home, SECRET_A)
    entry_file = col / "reg_unit_test_1.json"

    envelope = json.loads(entry_file.read_text(encoding="utf-8"))
    token = envelope["value"]["__encrypted_data__"]
    mid = len(token) // 2
    envelope["value"]["__encrypted_data__"] = (
        token[:mid] + ("x" if token[mid] != "x" else "y") + token[mid + 1 :]
    )
    entry_file.write_text(json.dumps(envelope), encoding="utf-8")

    clients, _ = _build_store(home, SECRET_A)
    assert await clients.get(registration.client_id) is None  # silent miss

    # the DCR re-registration path replaces the dead entry and the session lives
    await clients.put(
        registration.client_id,
        StoredClient(client_id=registration.client_id, client_secret="re-registered"),
    )
    got = await clients.get(registration.client_id)
    assert got is not None and got.client_secret == "re-registered"


# --------------------------------------------------------------------------- #
# Backup opacity + the §7.2 envelope check                                     #
# --------------------------------------------------------------------------- #


async def test_backup_tree_leaks_no_plaintext_and_envelope_check_classifies_files(
    tmp_path: Path,
) -> None:
    """The backup is ciphertext (§2: treat it as a credential) — and the §7.2
    validation works without a key: every file parses, entry files carry the
    encrypted envelope, no file is empty, and no secret material appears in
    plaintext anywhere in the tree.
    """
    home = tmp_path / "live"
    await _seed_sessions(home, SECRET_A)
    backup = tmp_path / "backup"
    _backup_volume(home, backup)

    secrets_in_play = (
        SECRET_A,
        "dcr-pairwise-secret",
        "upstream-access-token-value",
        "upstream-refresh-token-value",
        "operator@example.com",
    )
    empty: list[Path] = []
    for path in sorted(backup.rglob("*")):
        if path.is_dir():
            continue
        body = path.read_bytes()
        if not body:
            empty.append(path)
            continue
        text = body.decode("utf-8")  # every file the store writes is UTF-8 JSON
        for needle in secrets_in_play:
            assert needle not in text, f"plaintext secret material leaked into {path.name}"
        if path.name.endswith("-info.json"):
            continue  # collection metadata, not an entry
        envelope = json.loads(text)
        if "value" in envelope:  # entry files (info files have no value key)
            assert "__encrypted_data__" in envelope["value"], f"{path.name} lost its envelope"
            assert "__encryption_version__" in envelope["value"]

    assert empty == [], f"torn/empty files in backup: {empty}"

    # the plaintext *timestamps* (§1) remain operator-readable without a key —
    # the sanctioned way to eyeball session age and expiry edges. The shape is
    # per-collection: the proxy stores registrations without a TTL, so their
    # envelopes carry `created_at` alone, while TTL'd token sets carry both.
    clients_dir = _clients_collection_dir(backup, SECRET_A)
    registration_entry = json.loads(
        (clients_dir / "reg_unit_test_1.json").read_text(encoding="utf-8")
    )
    assert "created_at" in registration_entry
    assert "expires_at" not in registration_entry  # stored without a TTL

    tokens_dir = next(
        d
        for d in _fingerprint_dir(backup, SECRET_A).iterdir()
        if d.is_dir() and d.name.startswith("S_mcp_upstream_tokens-")
    )
    token_files = [
        p for p in tokens_dir.glob("*.json") if not p.name.endswith("-info.json")
    ]
    assert len(token_files) == 1
    token_entry = json.loads(token_files[0].read_text(encoding="utf-8"))
    assert "created_at" in token_entry and "expires_at" in token_entry


# --------------------------------------------------------------------------- #
# Runbook ↔ manifest drift guard                                               #
# --------------------------------------------------------------------------- #


def _manifest_docs(name: str) -> list[dict[str, Any]]:
    path = MANIFEST_DIR / name
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]


def _ytt_container(deployment: dict[str, Any]) -> dict[str, Any]:
    return next(
        c
        for c in deployment["spec"]["template"]["spec"]["containers"]
        if c["name"] == "ytt"
    )


def _env_map(container: dict[str, Any]) -> dict[str, str]:
    return {e["name"]: e.get("value", "") for e in container.get("env", [])}


def test_oauth_state_runbook_matches_state_manifests() -> None:
    """Every state-volume fact the runbook quotes must match the manifests.

    Guards the couplings OAUTH-STATE-RUNBOOK §1 is built on: the PVC shape,
    the /state mount, and FASTMCP_HOME pointing the proxy at it. If this
    fails after a manifest change, update deploy/OAUTH-STATE-RUNBOOK.md in
    the same commit.
    """
    pvc = _manifest_docs("oauth-state-pvc.yml")[0]
    assert pvc["metadata"]["name"] == "ytt-oauth-state"
    assert pvc["metadata"]["namespace"] == "ytt"
    assert pvc["spec"]["storageClassName"] == "longhorn"
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert pvc["spec"]["resources"]["requests"]["storage"] == "256Mi"

    deployment = next(
        d for d in _manifest_docs("deployment.yml") if d.get("kind") == "Deployment"
    )
    assert deployment["spec"]["replicas"] == 1  # runbook §5: one store, one pod
    assert deployment["spec"]["strategy"]["type"] == "Recreate"  # swap semantics

    spec = deployment["spec"]["template"]["spec"]
    container = _ytt_container(deployment)
    env = _env_map(container)

    state_volumes = [v for v in spec["volumes"] if v["name"] == "oauth-state"]
    assert len(state_volumes) == 1
    assert state_volumes[0]["persistentVolumeClaim"]["claimName"] == "ytt-oauth-state"
    state_mounts = [m for m in container["volumeMounts"] if m["name"] == "oauth-state"]
    assert [m["mountPath"] for m in state_mounts] == ["/state"]
    assert env["FASTMCP_HOME"] == "/state"

    # §1.1 quotes the reference deployment as NOT decoupling the signing key.
    # If you now set YTT_JWT_SIGNING_SECRET in the manifest, update the
    # runbook's same-secret guidance (§4.1) in the same commit — restore
    # compatibility rules change when the key stops tracking the client secret.
    assert "YTT_JWT_SIGNING_SECRET" not in env


def test_oauth_state_runbook_quotes_the_facts_it_depends_on() -> None:
    """The runbook must keep carrying the literals these procedures rely on.

    A doc refactor that drops e.g. the fingerprint derivation or the
    collection names would leave operators with procedures that reference
    nothing findable.
    """
    doc = STATE_RUNBOOK.read_text(encoding="utf-8")
    for literal in (
        "ytt-oauth-state",            # the PVC name (§1)
        "256Mi",                      # the volume request (§1)
        "longhorn",                   # storage class (§1)
        "/state",                     # mount path (§1)
        "FASTMCP_HOME",               # the env that relocates the store (§1)
        "oauth-proxy",                # the tree under home (§1)
        "fingerprint",                # the per-key directory (§1.1)
        "fastmcp-jwt-signing-key",    # derivation salt 1 (§1.1)
        "fastmcp-storage-encryption-key",  # derivation salt 2 (§1.1)
        "YTT_OAUTH_CLIENT_SECRET",    # the root of trust (§1.1/§4.1)
        "YTT_JWT_SIGNING_SECRET",     # the decoupling override (§1.1)
        "raise_on_decryption_error=False",  # why mismatches are soft (§1.1)
        CLIENTS_COLLECTION,           # durable collection (§1.2)
        TOKENS_COLLECTION,            # durable collection (§1.2)
        "mcp-jti-mappings",           # durable collection (§1.2)
        "mcp-refresh-tokens",         # durable collection (§1.2)
        "mcp-oauth-transactions",     # transient collection (§1.2)
        "mcp-authorization-codes",    # transient collection (§1.2)
        "-info.json",                 # belongs in the backup (§1/§3)
        "expires_at",                 # plaintext timestamps, not mtime (§1)
        "write_file_atomic",          # why mid-write backups are safe (§3)
        "unable to upgrade connection: Forbidden",  # the exec boundary (§3)
        "tar czf - -C /state .",      # the backup one-liner (§3)
        "mv /state/oauth-proxy",      # the swap (§4.2)
        "DeserializationError",       # the torn-entry failure (§6)
        "CACHE-RUNBOOK.md",           # the opposite value profile (§2)
        "tests/unit/test_oauth_state_recovery.py",  # this drill (§8)
    ):
        assert literal in doc, f"OAUTH-STATE-RUNBOOK.md lost the literal {literal!r}"
