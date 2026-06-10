import atexit
import base64
import binascii
import gzip
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from flask import Flask, Response, jsonify, render_template, request, stream_with_context
from werkzeug.utils import secure_filename

try:
    import imageio_ffmpeg
except Exception:
    imageio_ffmpeg = None

try:
    import yt_dlp
except Exception:
    yt_dlp = None

BASE_DIR = Path(__file__).resolve().parent.parent
app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)

YOUTUBE_API_URL = "https://www.googleapis.com/youtube/v3/videos"
TMP_LIMIT = int(os.getenv("TMP_SAFE_LIMIT_MB", "300")) * 1024 * 1024
FINAL_LIMIT = int(os.getenv("FINAL_FILE_LIMIT_MB", "180")) * 1024 * 1024
COOKIE_LIMIT = 512 * 1024
CHUNK_SIZE = 1024 * 1024
ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}


def env(name: str) -> str:
    return os.getenv(name, "").strip()


def extract_video_id(value: str) -> str | None:
    value = (value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        return value
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if host not in ALLOWED_HOSTS:
            return None
        if host == "youtu.be":
            candidate = parsed.path.strip("/").split("/")[0]
        elif parsed.path == "/watch":
            candidate = parse_qs(parsed.query).get("v", [""])[0]
        elif parsed.path.startswith(("/shorts/", "/embed/", "/live/")):
            parts = parsed.path.strip("/").split("/")
            candidate = parts[1] if len(parts) > 1 else ""
        else:
            candidate = ""
        return candidate if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate or "") else None
    except Exception:
        return None


def canonical_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def youtube_api_info(video_id: str) -> dict:
    api_key = env("YOUTUBE_API_KEY")
    if not api_key:
        raise RuntimeError("A variável YOUTUBE_API_KEY não está configurada no Vercel.")
    query = urlencode({
        "part": "snippet,contentDetails,status",
        "id": video_id,
        "key": api_key,
    })
    req = Request(f"{YOUTUBE_API_URL}?{query}", headers={"User-Agent": "YT-Forge/3.0"})
    try:
        with urlopen(req, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"A YouTube API recusou o pedido ({exc.code}). {detail[:220]}") from exc
    except URLError as exc:
        raise RuntimeError("Não foi possível contactar a YouTube Data API.") from exc
    items = payload.get("items") or []
    if not items:
        raise RuntimeError("Vídeo não encontrado, privado ou indisponível.")
    item = items[0]
    snippet = item.get("snippet", {})
    thumbs = snippet.get("thumbnails", {})
    thumb = (thumbs.get("maxres") or thumbs.get("standard") or thumbs.get("high") or thumbs.get("medium") or {}).get("url", "")
    return {
        "id": video_id,
        "title": snippet.get("title", "Vídeo do YouTube"),
        "channel": snippet.get("channelTitle", ""),
        "thumbnail": thumb,
        "duration": item.get("contentDetails", {}).get("duration", ""),
        "privacy": item.get("status", {}).get("privacyStatus", ""),
    }


def prepare_embedded_deno() -> tuple[str | None, str | None]:
    """Extrai o Deno Linux incluído no projeto para /tmp."""
    archive = BASE_DIR / "bin" / "deno-linux-x86_64.gz"
    if not archive.is_file():
        return None, f"arquivo não encontrado: {archive}"

    runtime_dir = Path("/tmp") / "ytforge_runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    target = runtime_dir / "deno"

    try:
        needs_extract = (
            not target.is_file()
            or target.stat().st_mtime < archive.stat().st_mtime
            or target.stat().st_size < 50 * 1024 * 1024
        )
        if needs_extract:
            temporary = runtime_dir / f"deno.{os.getpid()}.tmp"
            with gzip.open(archive, "rb") as source, temporary.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            temporary.chmod(0o755)
            os.replace(temporary, target)
        else:
            target.chmod(target.stat().st_mode | 0o111)
        return str(target), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def detect_runtime() -> dict:
    candidates: list[tuple[str, str | None, str]] = []
    errors: list[dict] = []

    explicit = env("DENO_PATH")
    if explicit:
        candidates.append(("deno", explicit, "DENO_PATH"))

    embedded_path, embedded_error = prepare_embedded_deno()
    if embedded_path:
        candidates.append(("deno", embedded_path, "embedded-gzip"))
    elif embedded_error:
        errors.append({"source": "embedded-gzip", "path": None, "error": embedded_error})

    candidates.extend([
        ("deno", shutil.which("deno"), "PATH"),
        ("node", shutil.which("node"), "PATH"),
        ("bun", shutil.which("bun"), "PATH"),
        ("quickjs", shutil.which("qjs"), "PATH"),
    ])

    seen: set[str] = set()
    for name, path, source in candidates:
        if not path:
            continue
        normalized = str(Path(path).resolve())
        if normalized in seen:
            continue
        seen.add(normalized)
        executable = Path(normalized)
        if not executable.is_file():
            errors.append({"source": source, "path": normalized, "error": "ficheiro não encontrado"})
            continue
        try:
            executable.chmod(executable.stat().st_mode | 0o111)
            result = subprocess.run(
                [normalized, "--version"],
                capture_output=True,
                text=True,
                timeout=12,
                env={**os.environ, "DENO_NO_UPDATE_CHECK": "1"},
            )
            output = (result.stdout or result.stderr or "").strip()
            if result.returncode != 0:
                errors.append({"source": source, "path": normalized, "error": f"exit {result.returncode}: {output[:300]}"})
                continue
            return {
                "name": name,
                "path": normalized,
                "version": output.splitlines()[0] if output else "disponível",
                "source": source,
                "errors": errors[-6:],
            }
        except Exception as exc:
            errors.append({"source": source, "path": normalized, "error": f"{type(exc).__name__}: {exc}"})

    return {"name": None, "path": None, "version": None, "source": None, "errors": errors[-10:]}


def ffmpeg_path() -> str | None:
    direct = shutil.which("ffmpeg")
    if direct:
        return direct
    if imageio_ffmpeg:
        try:
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None
    return None


def write_cookies(work_dir: Path) -> Path | None:
    encoded = env("YOUTUBE_COOKIES_B64")
    if not encoded:
        return None
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError("YOUTUBE_COOKIES_B64 não contém Base64 válido.") from exc
    if len(raw) > COOKIE_LIMIT:
        raise RuntimeError("O ficheiro de cookies é demasiado grande.")
    text = raw.decode("utf-8-sig", errors="strict").replace("\r\n", "\n")
    if "Netscape HTTP Cookie File" not in text and ".youtube.com\t" not in text:
        raise RuntimeError("O ficheiro de cookies não está no formato Netscape.")
    path = work_dir / "youtube_cookies.txt"
    path.write_text(text, encoding="utf-8", newline="\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def folder_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            pass
    return total


def locate_result(work_dir: Path) -> Path:
    ignored = {".part", ".ytdl", ".temp", ".jpg", ".jpeg", ".png", ".webp"}
    candidates = [p for p in work_dir.iterdir() if p.is_file() and p.suffix.lower() not in ignored and p.name != "youtube_cookies.txt"]
    if not candidates:
        raise RuntimeError("O processo terminou, mas o ficheiro final não foi encontrado.")
    return max(candidates, key=lambda p: (p.stat().st_mtime, p.stat().st_size))


def clean_error(text: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", text or "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    important = [line for line in lines if "ERROR:" in line or "Sign in to confirm" in line or "challenge" in line.lower()]
    result = important[-1] if important else (lines[-1] if lines else "Erro desconhecido do yt-dlp.")
    return result.replace("ERROR:", "").strip()[:1000]


def build_command(url: str, choice: str, work_dir: Path, cookies: Path | None) -> list[str]:
    runtime = detect_runtime()
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise RuntimeError("FFmpeg não foi encontrado no servidor.")
    if not runtime["name"] or not runtime["path"]:
        raise RuntimeError(
            "Nenhum runtime JavaScript foi encontrado. Confirma que o pacote deno está instalado no requirements.txt."
        )

    command = [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist",
        "--newline",
        "--no-progress",
        "--retries", "5",
        "--fragment-retries", "5",
        "--extractor-retries", "3",
        "--socket-timeout", "30",
        "--ffmpeg-location", ffmpeg,
        "--remote-components", "ejs:github",
        "--output", str(work_dir / "%(title).160B [%(id)s].%(ext)s"),
        "--print", "after_move:filepath",
    ]
    command += ["--js-runtimes", f'{runtime["name"]}:{runtime["path"]}']
    if cookies:
        command += ["--cookies", str(cookies)]

    if choice == "audio-original":
        command += ["--format", "bestaudio/best", "--embed-metadata"]
    elif choice == "mp3":
        command += [
            "--format", "bestaudio/best",
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", "0",
            "--embed-metadata",
            "--embed-thumbnail",
        ]
    elif choice == "mp4-1080":
        command += [
            "--format", "bestvideo*[height<=1080]+bestaudio/best[height<=1080]/best",
            "--merge-output-format", "mp4",
            "--embed-metadata",
        ]
    else:
        command += [
            "--format", "bestvideo*+bestaudio/best",
            "--merge-output-format", "mp4",
            "--embed-metadata",
        ]
    command.append(url)
    return command


def run_download(url: str, choice: str, work_dir: Path) -> Path:
    cookies = write_cookies(work_dir)
    command = build_command(url, choice, work_dir, cookies)
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    stdout_lines, stderr_lines = [], []

    def read_stream(stream, sink):
        for line in iter(stream.readline, ""):
            sink.append(line)
            if folder_size(work_dir) > TMP_LIMIT:
                try:
                    proc.kill()
                except Exception:
                    pass
                break

    t1 = threading.Thread(target=read_stream, args=(proc.stdout, stdout_lines), daemon=True)
    t2 = threading.Thread(target=read_stream, args=(proc.stderr, stderr_lines), daemon=True)
    t1.start(); t2.start()
    try:
        code = proc.wait(timeout=285)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError("O processamento excedeu o tempo seguro de 285 segundos.")
    t1.join(timeout=2); t2.join(timeout=2)

    if folder_size(work_dir) > TMP_LIMIT:
        raise RuntimeError("O processamento ultrapassou o limite seguro do /tmp. Escolhe um ficheiro menor.")
    if code != 0:
        raise RuntimeError(clean_error("\n".join(stderr_lines + stdout_lines)))

    result = locate_result(work_dir)
    if result.stat().st_size > FINAL_LIMIT:
        raise RuntimeError(f"O ficheiro final tem mais de {FINAL_LIMIT // (1024*1024)} MB e excede o limite seguro configurado.")
    return result


def stream_file_and_cleanup(file_path: Path, work_dir: Path):
    def generate():
        try:
            with file_path.open("rb") as fh:
                while True:
                    chunk = fh.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    filename = secure_filename(file_path.name) or f"youtube_download{file_path.suffix}"
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    response = Response(stream_with_context(generate()), mimetype=mime)
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Content-Length"] = str(file_path.stat().st_size)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/preview")
def preview():
    data = request.get_json(silent=True) or {}
    video_id = extract_video_id(data.get("url", ""))
    if not video_id:
        return jsonify(ok=False, error="Introduz um link válido do YouTube."), 400
    try:
        return jsonify(ok=True, video=youtube_api_info(video_id))
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 400


@app.post("/api/download")
def download():
    url = (request.form.get("url") or "").strip()
    choice = (request.form.get("format") or "mp4-best").strip()
    if choice not in {"mp4-best", "mp4-1080", "audio-original", "mp3"}:
        return jsonify(ok=False, error="Formato inválido."), 400
    video_id = extract_video_id(url)
    if not video_id:
        return jsonify(ok=False, error="Link do YouTube inválido."), 400

    work_dir = Path(tempfile.mkdtemp(prefix="ytforge_", dir="/tmp" if Path("/tmp").exists() else None))
    try:
        result = run_download(canonical_url(video_id), choice, work_dir)
        return stream_file_and_cleanup(result, work_dir)
    except Exception as exc:
        shutil.rmtree(work_dir, ignore_errors=True)
        return jsonify(ok=False, error=f"Não foi possível obter o ficheiro: {exc}"), 400


@app.get("/api/health")
def health():
    runtime = detect_runtime()
    ffmpeg = ffmpeg_path()
    return jsonify({
        "ok": True,
        "python": sys.version.split()[0],
        "yt_dlp_installed": yt_dlp is not None,
        "yt_dlp_version": getattr(getattr(yt_dlp, "version", None), "__version__", None) if yt_dlp else None,
        "youtube_api_key": bool(env("YOUTUBE_API_KEY")),
        "youtube_cookies_configured": bool(env("YOUTUBE_COOKIES_B64")),
        "javascript_runtime": runtime,
        "embedded_deno_archive": (BASE_DIR / "bin" / "deno-linux-x86_64.gz").is_file(),
        "deno_python_package": bool(find_deno_bin),
        "ffmpeg_found": bool(ffmpeg),
        "ffmpeg_path": ffmpeg,
        "tmp_safe_limit_mb": TMP_LIMIT // (1024 * 1024),
        "final_file_limit_mb": FINAL_LIMIT // (1024 * 1024),
    })


@app.errorhandler(404)
def not_found(_):
    return jsonify(ok=False, error="Rota não encontrada."), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
