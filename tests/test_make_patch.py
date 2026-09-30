"""make_patch.py 打出來的包：權限、內容來源、刪除清單、雜湊、殘留掃描。

在暫存 git repo 上跑，不依賴這個 repo 當下的狀態。
"""
from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / ".project" / "make_patch.py"

pytestmark = pytest.mark.skipif(not _SRC.is_file(), reason="離線安裝包不含 .project/")


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
                        "-c", "core.autocrlf=false", *args],
                       cwd=repo, capture_output=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _write(repo: Path, rel: str, data: bytes) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


@pytest.fixture()
def mp(tmp_path, monkeypatch):
    """載入 make_patch 並把 REPO 指到一個兩個 commit 的暫存 repo。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _write(repo, "APP/frontend/config/version.js", b"window.APP_VERSION = 'V1.00';\n")
    _write(repo, "APP/webvuln/main.py", b"A = 1\n")
    _write(repo, "APP/webvuln/old_name.py", b"OLD = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")

    _write(repo, "APP/frontend/config/version.js", b"window.APP_VERSION = 'V1.01';\r\n")
    _write(repo, "APP/webvuln/main.py", b"A = 2\r\nB = 3\r\n")
    (repo / "APP/webvuln/old_name.py").unlink()
    _write(repo, "APP/deploy/run.sh", b"#!/usr/bin/env bash\necho hi\n")
    _write(repo, "APP/tests/test_x.py", b"def test_x(): pass\n")
    _write(repo, "APP/patch/patch_old.tar.gz", b"\x1f\x8b old")
    _write(repo, "AI/note.md", b"private\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "second")

    spec = importlib.util.spec_from_file_location("make_patch_under_test", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "REPO", repo)
    mod.base, mod.repo = base, repo
    return mod


def _build(mp, tmp_path, monkeypatch) -> tarfile.TarFile:
    out = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", ["make_patch.py", mp.base, str(out)])
    mp.main()
    (tgz,) = out.glob("patch_*.tar.gz")
    sha = (out / (tgz.name[:-7] + ".sha256")).read_text(encoding="utf-8")
    assert sha == f"{hashlib.sha256(tgz.read_bytes()).hexdigest()}  {tgz.name}\n"
    return tarfile.open(tgz)


def _names(tf: tarfile.TarFile) -> set[str]:
    return {m.name.split("/", 1)[1] for m in tf.getmembers() if "/" in m.name}


def test_no_world_writable_entries(mp, tmp_path, monkeypatch):
    # Windows 打包會回報 0666/0777；包裡不可以有任何 group/other 可寫的項目
    with _build(mp, tmp_path, monkeypatch) as tf:
        for m in tf.getmembers():
            assert m.mode == (0o755 if m.isdir() else 0o644), m.name
            assert (m.uid, m.gid, m.uname, m.gname) == (0, 0, "", "")


def test_ships_only_app_code(mp, tmp_path, monkeypatch):
    with _build(mp, tmp_path, monkeypatch) as tf:
        names = _names(tf)
    assert {"files/webvuln/main.py", "files/frontend/config/version.js",
            "files/deploy/run.sh", "patch.sh", "MANIFEST.txt", "SHA256SUMS"} <= names
    assert not any("tests/" in n or "patch_old" in n or "note.md" in n for n in names)


def test_content_is_head_and_lf(mp, tmp_path, monkeypatch):
    # 工作目錄有未 commit 的改動時，包裡仍是 HEAD 的內容
    _write(mp.repo, "APP/webvuln/main.py", b"UNCOMMITTED = 1\n")
    with _build(mp, tmp_path, monkeypatch) as tf:
        top = tf.getmembers()[0].name
        main = tf.extractfile(f"{top}/files/webvuln/main.py").read()
        sums = tf.extractfile(f"{top}/SHA256SUMS").read().decode()
        sh = tf.extractfile(f"{top}/patch.sh").read()
    assert main == b"A = 2\nB = 3\n"
    assert f"{hashlib.sha256(main).hexdigest()}  files/webvuln/main.py" in sums.splitlines()
    assert b"\r" not in sh and sh.startswith(b"#!/usr/bin/env bash\n")


def test_deleted_file_listed(mp, tmp_path, monkeypatch):
    with _build(mp, tmp_path, monkeypatch) as tf:
        top = tf.getmembers()[0].name
        assert tf.extractfile(f"{top}/DELETE.txt").read() == b"webvuln/old_name.py\n"


def test_residual_blocks_package(mp, tmp_path, monkeypatch):
    # 位址用拼的：這支測試會進公開 repo，不要留下一眼看得出網段的字串
    leak = ".".join(["10", "9" + "0", "0", "1"])
    _write(mp.repo, "APP/webvuln/main.py", f'HOST = "{leak}"\n'.encode())
    _git(mp.repo, "commit", "-q", "-am", "leak")
    with pytest.raises(SystemExit):
        _build(mp, tmp_path, monkeypatch)
    assert not list((tmp_path / "out").glob("patch_*.tar.gz"))


@pytest.mark.parametrize("rel", ["/etc/passwd", "../x", "a/../../x", "a\\b", "~/x", "C:/x", ""])
def test_reject_unsafe_paths(mp, rel):
    with pytest.raises(SystemExit):
        mp._reject_unsafe(rel, "test")
