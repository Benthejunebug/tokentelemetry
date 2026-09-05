"""Session export: hand back the file the trace is read from.

The three outcomes each have a way of going quietly wrong:
  - FIDELITY. A "source" export that isn't byte-identical to the file on disk is
    worthless; the point is to get the original, not a re-serialization of it.
  - HONESTY. Hermes and OpenCode have no source file. Returning a synthesized
    JSON that looks like one is the failure mode this feature must avoid.
  - CAUSE. In a container most agent directories aren't mounted, so "no export"
    must carry a reason and a fix, not a bare 404.
"""

import io
import os
import sqlite3
import sys
import zipfile

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(__file__))
import main  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    # The export routes sit behind RemoteAuthMiddleware, which reads
    # TT_AUTH_TOKEN from the environment on every request. TestClient presents a
    # non-loopback peer, so a token left set by another module would 401 these
    # tests. Clear it explicitly; the gate itself is covered below.
    monkeypatch.delenv("TT_AUTH_TOKEN", raising=False)
    return TestClient(main.app)


# --------------------------------------------------------------------------- #
# Single file — the common case (13 of 16 agents)
# --------------------------------------------------------------------------- #

@pytest.fixture
def claude_session(tmp_path, monkeypatch):
    """A Claude session as it lives on disk: one .jsonl under projects/."""
    root = tmp_path / ".claude"
    proj = root / "projects" / "-Users-dev-app"
    proj.mkdir(parents=True)
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    body = (
        '{"type":"user","message":{"role":"user","content":"hi"}}\n'
        '{"type":"assistant","message":{"role":"assistant","content":"hello"}}\n'
    )
    path = proj / f"{sid}.jsonl"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setattr(main, "CLAUDE_DIR", root)
    return sid, path, body


def test_single_file_exports_byte_for_byte(client, claude_session):
    """The download must BE the file, not a re-serialization of its contents."""
    sid, path, body = claude_session
    r = client.get(f"/sessions/{sid}/export", params={"agent": "claude"})
    assert r.status_code == 200
    assert r.content == path.read_bytes()
    assert r.content.decode() == body


def test_single_file_download_is_named_for_the_session(client, claude_session):
    sid, _path, _ = claude_session
    r = client.get(f"/sessions/{sid}/export", params={"agent": "claude"})
    assert f'filename="claude-{sid}.jsonl"' in r.headers["content-disposition"]


def test_export_info_reports_the_real_path_without_downloading(client, claude_session):
    sid, path, body = claude_session
    info = client.get(f"/sessions/{sid}/export-info", params={"agent": "claude"}).json()
    assert info["kind"] == "file"
    assert info["paths"] == [str(path)]
    assert info["bytes"] == len(body.encode())
    assert info["filename"] == f"claude-{sid}.jsonl"
    assert info["reason"] is None


def test_compressed_sources_are_not_decompressed(tmp_path, monkeypatch, client):
    """dsh stores zstd JSONL. Decompressing would make it a different file."""
    sid = "session-11111111-2222-3333-4444-555555555555"
    src = tmp_path / f"{sid}.jsonl.zstd"
    src.write_bytes(b"\x28\xb5\x2f\xfd not-really-zstd-but-opaque-bytes")
    monkeypatch.setattr(main, "_dsh_session_file", lambda s: src if s == sid else None)

    info = client.get(f"/sessions/{sid}/export-info", params={"agent": "dsh"}).json()
    assert info["filename"].endswith(".jsonl.zstd")

    r = client.get(f"/sessions/{sid}/export", params={"agent": "dsh"})
    assert r.content == src.read_bytes()


# --------------------------------------------------------------------------- #
# Several files — one session spread across a directory
# --------------------------------------------------------------------------- #

def test_multi_file_session_exports_as_a_zip(tmp_path, monkeypatch, client):
    """Grok keeps summary + dialogue + lifecycle events; all three travel."""
    sid = "01a06372-255f-7b03-ba6f-6d34ffb9ec5f"
    sess = tmp_path / "bucket" / sid
    sess.mkdir(parents=True)
    (sess / main.GROK_SUMMARY).write_text('{"created_at":"2026-01-01T00:00:00Z"}',
                                          encoding="utf-8")
    (sess / "chat_history.jsonl").write_text('{"type":"user","content":[]}\n', encoding="utf-8")
    (sess / "events.jsonl").write_text('{"event":"start"}\n', encoding="utf-8")
    monkeypatch.setattr(main, "GROK_SESSIONS_DIR", tmp_path)

    info = client.get(f"/sessions/{sid}/export-info", params={"agent": "grok"}).json()
    assert info["kind"] == "files"
    assert info["filename"] == f"grok-{sid}.zip"

    r = client.get(f"/sessions/{sid}/export", params={"agent": "grok"})
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert sorted(zf.namelist()) == sorted(
            [main.GROK_SUMMARY, "chat_history.jsonl", "events.jsonl"])
        # Members must be the originals, not re-encoded.
        assert zf.read("events.jsonl") == (sess / "events.jsonl").read_bytes()


# --------------------------------------------------------------------------- #
# Serialized — no source file exists, and the export must say so
# --------------------------------------------------------------------------- #

@pytest.fixture
def hermes_db(tmp_path, monkeypatch):
    """Two sessions in ONE database — which is why the DB can't just be copied."""
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE sessions (id TEXT, model TEXT, title TEXT)")
    con.execute("CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT, "
                "timestamp INT, tool_name TEXT, tool_calls TEXT, tool_call_id TEXT, "
                "reasoning_content TEXT)")
    con.execute("INSERT INTO sessions VALUES ('sess-mine','grok-4.6','mine')")
    con.execute("INSERT INTO sessions VALUES ('sess-other','grok-4.6','SOMEONE ELSE')")
    con.execute("INSERT INTO messages VALUES ('sess-mine','user','hi',1,NULL,NULL,NULL,NULL)")
    con.execute("INSERT INTO messages VALUES ('sess-other','user','SECRET',1,NULL,NULL,NULL,NULL)")
    con.commit()
    con.close()
    monkeypatch.setattr(main, "_hermes_dbs", lambda: [db])
    return db


def test_db_backed_session_is_labelled_a_reconstruction(client, hermes_db):
    """The ask was "the same file you're reading". When there isn't one, saying
    so plainly is the whole job."""
    info = client.get("/sessions/sess-mine/export-info", params={"agent": "hermes"}).json()
    assert info["kind"] == "serialized"
    # The filename itself carries the caveat, so it survives being saved.
    assert info["filename"] == "hermes-sess-mine.reconstructed.json"

    payload = client.get("/sessions/sess-mine/export", params={"agent": "hermes"}).json()
    assert payload["_source"] == "reconstructed"
    assert "no single source file exists" in payload["_note"]
    assert payload["session"]["title"] == "mine"
    assert [m["content"] for m in payload["messages"]] == ["hi"]


def test_a_serialized_export_leaks_no_other_session(client, hermes_db):
    """Copying the shared DB would hand over every other session in it."""
    r = client.get("/sessions/sess-mine/export", params={"agent": "hermes"})
    assert "SECRET" not in r.text
    assert "SOMEONE ELSE" not in r.text


def test_missing_rows_are_a_404_not_an_empty_reconstruction(client, hermes_db):
    r = client.get("/sessions/no-such-session/export", params={"agent": "hermes"})
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Unavailable — the container case, which must explain itself
# --------------------------------------------------------------------------- #

def test_unavailable_carries_a_reason_and_a_fix(client, tmp_path, monkeypatch):
    """compose.yml ships with only ~/.claude uncommented, so most agents resolve
    to nothing in a container. A bare 404 sends the user hunting for a bug that
    is really a missing mount."""
    monkeypatch.setattr(main, "CLAUDE_DIR", tmp_path / "nothing-here")
    info = client.get("/sessions/ghost/export-info", params={"agent": "claude"}).json()
    assert info["kind"] == "unavailable"
    assert info["reason"]
    assert "compose.yml" in info["hint"]

    r = client.get("/sessions/ghost/export", params={"agent": "claude"})
    assert r.status_code == 404
    assert r.json()["detail"]["hint"]


ALL_AGENTS = [
    "claude", "codex", "grok", "pi", "dsh", "qoder", "antigravity", "gemini",
    "muse", "prime", "qwen", "vibe", "cursor", "copilot", "smallcode", "cline",
    "hermes", "opencode",
]


@pytest.mark.parametrize("agent", ALL_AGENTS)
def test_every_agent_returns_a_well_formed_verdict(agent):
    """The resolver must always answer with a readable `kind`.

    Several branches used to `return _export_files(...)` unguarded, which yields
    a dict with no "kind" when a file vanishes between the glob and the stat —
    a 500 from the endpoint instead of an honest "unavailable".
    """
    src = main._session_source(agent, "definitely-not-a-real-session-id")
    assert src["kind"] in {"file", "files", "serialized", "unavailable"}
    if src["kind"] == "unavailable":
        assert src["reason"] and src["hint"]
    # _export_filename must cope with whatever the resolver returned.
    main._export_filename(agent, "definitely-not-a-real-session-id", src)


def test_unknown_agent_says_so_rather_than_pretending(client):
    info = client.get("/sessions/x/export-info", params={"agent": "notanagent"}).json()
    assert info["kind"] == "unavailable"
    assert "not implemented" in info["reason"]


# --------------------------------------------------------------------------- #
# Filename safety — session ids come off disk
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("sid,expected", [
    ("plain-123", "claude-plain-123"),
    ('a"b', "claude-a_b"),                      # would break the quoted header
    ("../../etc/passwd", "claude-.._.._etc_passwd"),
    ("with spaces", "claude-with_spaces"),
    ("", "claude-session"),
])
def test_download_filenames_are_sanitized(sid, expected):
    assert main._safe_session_stem("claude", sid) == expected


# --------------------------------------------------------------------------- #
# Remote auth — an export hands over a whole transcript, so the gate must hold
# --------------------------------------------------------------------------- #

def test_remote_export_without_a_token_is_refused(claude_session, monkeypatch):
    """Export is the most sensitive read in the API: it returns the entire
    session verbatim. It must sit behind the same gate as everything else."""
    sid, _path, _ = claude_session
    monkeypatch.setenv("TT_AUTH_TOKEN", "s3cret")
    remote = TestClient(main.app)  # TestClient's peer is not loopback
    assert remote.get(f"/sessions/{sid}/export", params={"agent": "claude"}).status_code == 401
    assert remote.get(f"/sessions/{sid}/export-info", params={"agent": "claude"}).status_code == 401


def test_remote_export_accepts_the_token_as_a_query_param(claude_session, monkeypatch):
    """A browser download can't set an Authorization header, so the frontend
    passes ?token= via artifactUrl(). If that stopped working, Export would be
    broken for exactly the remote users who need it."""
    sid, path, _ = claude_session
    monkeypatch.setenv("TT_AUTH_TOKEN", "s3cret")
    remote = TestClient(main.app)
    r = remote.get(f"/sessions/{sid}/export", params={"agent": "claude", "token": "s3cret"})
    assert r.status_code == 200
    assert r.content == path.read_bytes()

    # The header form keeps working too.
    r2 = remote.get(f"/sessions/{sid}/export", params={"agent": "claude"},
                    headers={"Authorization": "Bearer s3cret"})
    assert r2.status_code == 200


def test_export_does_not_rewrite_paths_inside_the_file(client, claude_session):
    """A byte-for-byte copy is what was asked for. Sanitizing usernames inside
    the payload would make it not the file — the warning belongs in the UI."""
    sid, path, _ = claude_session
    path.write_text('{"cwd":"/Users/somebody/code/app"}\n', encoding="utf-8")
    r = client.get(f"/sessions/{sid}/export", params={"agent": "claude"})
    assert b"/Users/somebody/code/app" in r.content
