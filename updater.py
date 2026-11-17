"""Atualização do app via GitHub Releases (somente biblioteca padrão).

Fluxo: consulta a última Release -> compara versões -> baixa o .exe -> confere o SHA-256 ->
um script .bat espera o app fechar, troca o .exe (guardando o antigo como .old) e reabre.
A troca automática só funciona no .exe compilado (Windows); rodando por Python, só avisa e abre a página.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional, Tuple
from urllib.parse import urlparse

import version as ver

TIMEOUT = 10
MAX_EXE_BYTES = 400 * 1024 * 1024
_ALLOWED_HOSTS = ("github.com", "api.github.com", "objects.githubusercontent.com",
                  "release-assets.githubusercontent.com", "github-releases.githubusercontent.com")


class UpdateError(Exception):
    pass


@dataclass
class ReleaseInfo:
    version: str
    tag: str
    notes: str
    html_url: str
    exe_url: Optional[str]
    sha_url: Optional[str]


def parse_version(text: str) -> Tuple[int, ...]:
    """'v1.4.0' -> (1, 4, 0). Sufixos como '-beta' são ignorados."""
    m = re.match(r"^\s*v?(\d+(?:\.\d+){0,3})", text or "")
    if not m:
        raise UpdateError(f"Versão inválida: {text!r}")
    parts = [int(x) for x in m.group(1).split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def is_newer(remote: str, local: str = ver.__version__) -> bool:
    return parse_version(remote) > parse_version(local)


def can_self_update() -> bool:
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def repo_configured() -> bool:
    r = ver.GITHUB_REPO
    return bool(re.fullmatch(r"[\w.-]+/[\w.-]+", r)) and not r.upper().startswith("SEU-USUARIO")


def _check_url(url: str) -> None:
    u = urlparse(url)
    if u.scheme != "https" or not any(u.hostname == h or (u.hostname or "").endswith("." + h) for h in _ALLOWED_HOSTS):
        raise UpdateError("Endereço de download não permitido.")


def _open(url: str, accept: str = "application/vnd.github+json"):
    _check_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": f"{ver.APP_NAME}/{ver.__version__}", "Accept": accept})
    resp = urllib.request.urlopen(req, timeout=TIMEOUT)
    try:
        _check_url(resp.geturl())          # confere também o destino final após redirecionamentos
    except UpdateError:
        resp.close()
        raise
    return resp


def check_latest(opener: Callable = _open) -> ReleaseInfo:
    if not repo_configured():
        raise UpdateError("O repositório de atualizações ainda não foi configurado em version.py (GITHUB_REPO).")
    url = f"https://api.github.com/repos/{ver.GITHUB_REPO}/releases/latest"
    try:
        with opener(url) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError("Nenhuma versão publicada no repositório ainda.") from e
        raise UpdateError(f"O GitHub respondeu com erro {e.code}.") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise UpdateError("Não consegui acessar o GitHub (sem internet ou bloqueado).") from e
    tag = data.get("tag_name") or ""
    exe = sha = None
    for a in data.get("assets", []):
        if a.get("name") == ver.ASSET_NAME:
            exe = a.get("browser_download_url")
        elif a.get("name") == ver.HASH_ASSET_NAME:
            sha = a.get("browser_download_url")
    return ReleaseInfo(version=".".join(map(str, parse_version(tag))), tag=tag,
                       notes=(data.get("body") or "").strip(), html_url=data.get("html_url") or "",
                       exe_url=exe, sha_url=sha)


def download(info: ReleaseInfo, progress: Callable[[int, int], None] = lambda d, t: None,
             cancel=None, opener: Callable = _open) -> str:
    """Baixa o .exe novo e confere o SHA-256. Devolve o caminho do arquivo baixado."""
    if not info.exe_url or not info.sha_url:
        raise UpdateError("A Release não tem o .exe e o arquivo .sha256; a atualização automática foi recusada.")
    with opener(info.sha_url, "application/octet-stream") as r:
        text = r.read(4096).decode("ascii", "ignore")
    m = re.search(r"\b([0-9a-fA-F]{64})\b", text)
    if not m:
        raise UpdateError("Arquivo de verificação (.sha256) inválido.")
    expected = m.group(1).lower()

    tmpdir = tempfile.mkdtemp(prefix="skp2ifc_update_")
    dest = os.path.join(tmpdir, ver.ASSET_NAME)
    h = hashlib.sha256()
    done = 0
    with opener(info.exe_url, "application/octet-stream") as r, open(dest, "wb") as out:
        total = int(r.headers.get("Content-Length") or 0)
        while True:
            if cancel is not None and cancel.is_set():
                raise UpdateError("Atualização cancelada.")
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            done += len(chunk)
            if done > MAX_EXE_BYTES:
                raise UpdateError("Arquivo grande demais; download interrompido.")
            h.update(chunk)
            out.write(chunk)
            progress(done, total)
    if h.hexdigest() != expected:
        os.remove(dest)
        raise UpdateError("A verificação de integridade (SHA-256) falhou; o arquivo baixado foi descartado.")
    if os.path.getsize(dest) < 1_000_000:
        raise UpdateError("O arquivo baixado parece incompleto.")
    return dest


def apply_update(new_exe: str) -> None:
    """Escreve e dispara o script que troca o .exe depois que este processo fechar.
    O chamador deve encerrar o app logo em seguida."""
    if not can_self_update():
        raise UpdateError("A troca automática só funciona no programa .exe (Windows).")
    target = os.path.abspath(sys.executable)
    bat = os.path.join(os.path.dirname(new_exe), "aplicar_atualizacao.bat")
    script = f'''@echo off
chcp 65001 >nul
set "TARGET={target}"
set "NEW={new_exe}"
set /a n=0
:wait
set /a n+=1
if %n% gtr 60 goto fail
timeout /t 1 /nobreak >nul
move /y "%TARGET%" "%TARGET%.old" >nul 2>&1
if errorlevel 1 goto wait
move /y "%NEW%" "%TARGET%" >nul 2>&1
if errorlevel 1 (
  move /y "%TARGET%.old" "%TARGET%" >nul 2>&1
  goto fail
)
set "_MEIPASS2="
set "PYINSTALLER_RESET_ENVIRONMENT=1"
start "" "%TARGET%"
goto end
:fail
echo Nao foi possivel concluir a atualizacao. O programa antigo foi mantido.
pause
:end
(goto) 2>nul & del "%~f0"
'''
    with open(bat, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(script)
    env = {k: v for k, v in os.environ.items() if k != "_MEIPASS2"}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    flags = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
    subprocess.Popen(["cmd", "/c", bat], env=env, creationflags=flags, close_fds=True)


def cleanup_old() -> None:
    """Chamado na abertura: apaga o .exe antigo deixado pela última atualização."""
    if getattr(sys, "frozen", False):
        old = os.path.abspath(sys.executable) + ".old"
        try:
            if os.path.exists(old):
                os.remove(old)
        except OSError:
            pass
