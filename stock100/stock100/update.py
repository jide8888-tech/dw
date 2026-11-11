"""GitHub 최신 코드로 업데이트 — config.yaml 과 data 폴더는 건드리지 않는다.

    python -m stock100 update          # 업데이트
    python -m stock100 update --check  # 새 버전이 있는지만 확인
    python -m stock100.update          # (update 명령이 없는 옛 버전에서 처음 한 번)

표준 라이브러리만 쓰므로 이 파일 하나만 내려받아 실행해도 동작한다.
바뀌는 파일은 backup/날짜시각/ 에 먼저 복사해 둔다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO = "jide8888-tech/dw"
BRANCH = "main"
SUBDIR = "stock100"                     # 저장소 안에서 이 프로그램이 들어 있는 폴더
ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ".version"
PROTECT = ("config.yaml", "data/", "data_demo/", "backup/", VERSION_FILE)


def _get(url: str, accept: str | None = None, timeout=60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "stock100-updater",
                                               **({"Accept": accept} if accept else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def latest_sha(repo=REPO, branch=BRANCH) -> str | None:
    try:
        return _get(f"https://api.github.com/repos/{repo}/commits/{branch}",
                    accept="application/vnd.github.sha", timeout=20).decode().strip()[:40]
    except Exception:
        return None


def download_zip(repo=REPO, branch=BRANCH) -> bytes:
    return _get(f"https://codeload.github.com/{repo}/zip/refs/heads/{branch}")


def _protected(rel: str) -> bool:
    return any(rel == p or rel.startswith(p) for p in PROTECT)


def plan(zbytes: bytes, root: Path = ROOT, subdir: str = SUBDIR) -> list[tuple[str, bytes, str]]:
    """[(상대경로, 내용, 'new'|'changed')] — 내용이 같은 파일과 보호 파일은 제외"""
    z = zipfile.ZipFile(io.BytesIO(zbytes))
    names = z.namelist()
    top = names[0].split("/")[0]
    prefix = f"{top}/{subdir}/"
    if f"{prefix}stock100/__init__.py" not in names:
        raise RuntimeError(f"받은 파일에 {subdir}/stock100 이 없습니다. 저장소 구조를 확인하세요.")
    out = []
    for n in names:
        if not n.startswith(prefix) or n.endswith("/"):
            continue
        rel = n[len(prefix):]
        if _protected(rel):
            continue
        data = z.read(n)
        dst = root / rel
        if not dst.exists():
            out.append((rel, data, "new"))
        elif dst.read_bytes() != data:
            out.append((rel, data, "changed"))
    return out


def apply(changes, root: Path = ROOT) -> Path | None:
    backup = None
    for rel, data, kind in changes:
        dst = root / rel
        if kind == "changed":
            if backup is None:
                backup = root / "backup" / dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            b = backup / rel
            b.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, b)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)          # 바이트 그대로 → .bat 의 CRLF 유지
    return backup


def run(check=False, repo=REPO, branch=BRANCH, root: Path = ROOT, pip=True,
        _download=download_zip, _sha=latest_sha) -> int:
    print(f"[업데이트] 설치 위치: {root}")
    if not (root / "stock100" / "__init__.py").exists():
        print("[오류] stock100 폴더 위치를 찾지 못했습니다.")
        return 1
    vf = root / VERSION_FILE
    cur = vf.read_text(encoding="utf-8").strip() if vf.exists() else ""
    new = _sha(repo, branch)
    if new:
        print(f"[업데이트] 현재 {cur[:7] or '(기록 없음)'} → GitHub {branch} {new[:7]}")
    try:
        zbytes = _download(repo, branch)
    except Exception as e:
        print(f"[오류] GitHub 에서 받지 못했습니다: {e.__class__.__name__}: {e}")
        return 1
    changes = plan(zbytes, root)
    if not changes:
        print("[업데이트] 이미 최신입니다. 바뀐 파일이 없습니다.")
        if new:
            vf.write_text(new, encoding="utf-8")
        return 0
    for rel, _, kind in changes:
        print(f"   {'추가' if kind == 'new' else '변경'}  {rel}")
    if check:
        print(f"[업데이트] 새 버전이 있습니다: 파일 {len(changes)}개. 적용하려면 --check 없이 실행하세요.")
        return 0
    backup = apply(changes, root)
    if new:
        vf.write_text(new, encoding="utf-8")
    print(f"[업데이트] 완료: 파일 {len(changes)}개 반영. config.yaml 과 data 폴더는 그대로입니다.")
    if backup:
        print(f"   이전 파일 백업: {backup}")
    if pip and any(rel == "requirements.txt" for rel, _, _ in changes):
        print("[업데이트] 필요한 패키지가 바뀌어 설치합니다...")
        subprocess.call([sys.executable, "-m", "pip", "install", "-q", "-r", str(root / "requirements.txt")])
    print("   감시(watch) 창이 켜져 있다면 닫았다가 다시 켜야 새 코드가 적용됩니다.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="stock100 update", description="GitHub 최신 코드로 업데이트")
    ap.add_argument("--check", action="store_true", help="새 버전이 있는지만 확인")
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--branch", default=BRANCH)
    ap.add_argument("--no-pip", action="store_true", help="패키지 설치 생략")
    a = ap.parse_args(argv)
    return run(check=a.check, repo=a.repo, branch=a.branch, pip=not a.no_pip)


if __name__ == "__main__":
    sys.exit(main())
