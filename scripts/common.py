"""RemoteCode helpers; Python 3.12+, standard library only."""
import hashlib
from contextlib import contextmanager
from email.parser import Parser
from email.message import Message
import gzip
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import urllib.error
import zipfile


class HTTPResult:
    def __init__(self, stream, status, headers, ranged=False):
        self.stream, self.status = stream, status
        self.headers = Message()
        for key, value in headers.items():
            self.headers[key] = str(value)
        encoding = self.headers.get("Content-Encoding", "identity").lower()
        if encoding != "identity":
            require(not ranged, "Range 响应包含 HTTP 内容编码，拒绝拼接")
            require(encoding == "gzip", "不支持的 HTTP Content-Encoding")
            self.stream = gzip.GzipFile(fileobj=stream)
            del self.headers["Content-Length"]
            del self.headers["Content-Encoding"]

    def read(self, size=-1):
        return self.stream.read(size)


def require(ok, message):
    if not ok:
        raise ValueError(message)


def private_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def write(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(data)
    temp.chmod(mode)
    temp.replace(path)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@contextmanager
def request(url, headers=None):
    require(urllib.parse.urlsplit(url).scheme == "https", "下载地址必须使用 HTTPS")
    headers = {"Accept-Encoding": "identity", **(headers or {})}
    ranged = any(key.lower() == "range" for key in headers)
    curl = os.environ.get("REMOTECODE_CURL") or ("/usr/bin/curl" if sys.platform == "darwin" else shutil.which("curl"))
    backend = os.environ.get("REMOTECODE_HTTP", "auto")
    require(backend in ("auto", "curl", "urllib"), "REMOTECODE_HTTP 必须为 auto/curl/urllib")
    if backend == "curl" or backend == "auto" and curl:
        require(curl and Path(curl).is_file(), "找不到 curl；可用 REMOTECODE_CURL 指定完整路径")
        with tempfile.TemporaryFile() as stream, tempfile.TemporaryDirectory() as header_dir:
            header_path = Path(header_dir) / "headers"
            command = [curl, "--fail", "--silent", "--show-error", "--location",
                 "--proto", "=https", "--proto-redir", "=https", "--connect-timeout", "15",
                 "--max-time", "90", "--dump-header", header_path,
                 "--user-agent", "RemoteCode-Deployment/1.0"]
            for key, value in (headers or {}).items():
                command += ["--header", f"{key}: {value}"]
            result = subprocess.run([str(arg) for arg in command + [url]], stdout=stream,
                                    stderr=subprocess.PIPE, check=False)
            blocks = re.split(rb"\r?\n\r?\n", header_path.read_bytes())
            final = next((block for block in reversed(blocks) if block.startswith(b"HTTP/")), None)
            if not final:
                raise OSError(f"curl HTTPS 请求失败，退出码 {result.returncode}")
            status_line, fields = final.split(b"\n", 1)
            status = int(status_line.split()[1])
            response_headers = Parser().parsestr(fields.decode("iso-8859-1"))
            if status >= 400:
                raise urllib.error.HTTPError(url, status, "HTTPS request failed", response_headers, None)
            stream.seek(0)
            yield HTTPResult(stream, status, response_headers, ranged=ranged)
            if result.returncode:
                raise OSError(f"curl 传输未完成，退出码 {result.returncode}")
    else:
        req = urllib.request.Request(url, headers={"User-Agent": "RemoteCode-Deployment/1.0", **(headers or {})})
        with urllib.request.urlopen(req, timeout=90) as response:
            require(urllib.parse.urlsplit(response.url).scheme == "https", "拒绝跳转到 HTTP 下载")
            yield HTTPResult(response, response.status, response.headers, ranged=ranged)


def metadata(url):
    with request(url) as response:
        return json.load(response)


def download(url, dest, expected, mirror=None):
    """Mirrors affect bytes only. Expected hash must come from trusted metadata/lock."""
    require(re.fullmatch(r"[0-9a-fA-F]{64}", expected or ""), "缺少可信 SHA-256，拒绝下载")
    dest = Path(dest)
    if dest.exists() and sha256(dest) == expected.lower():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    candidates = [url]
    if mirror:
        require(mirror.count("{url}") == 1, "镜像模板必须包含一次 {url}")
        candidates.insert(0, mirror.replace("{url}", url))
    temp = dest.with_name(dest.name + ".part")
    resume = dest.with_name(dest.name + ".part.json")
    for source in candidates:
        identity = {"url": source, "sha256": expected.lower()}
        try:
            reusable = resume.is_file() and read(resume) == identity
        except (OSError, ValueError):
            reusable = False
        if not reusable:
            temp.unlink(missing_ok=True)
        write(resume, identity)
        for attempt in range(3):
            try:
                if temp.is_file() and sha256(temp) == expected.lower():
                    temp.replace(dest)
                    resume.unlink(missing_ok=True)
                    return dest
                offset = temp.stat().st_size if temp.exists() else 0
                headers = {"Accept-Encoding": "identity"}
                if offset:
                    headers["Range"] = f"bytes={offset}-"
                with request(source, headers) as response:
                    status = response.status
                    mode = "wb"
                    if offset and status == 206:
                        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                        require(match and int(match[1]) == offset and int(match[2]) + 1 == int(match[3]), "续传 Content-Range 不匹配")
                        mode = "ab"
                    else:
                        require(status == 200, "下载必须返回完整的 200，或有效续传的 206")
                    received = 0
                    with temp.open(mode) as target:
                        while True:
                            chunk = response.read(256 * 1024)
                            if not chunk:
                                break
                            target.write(chunk)
                            received += len(chunk)
                    length = response.headers.get("Content-Length")
                    if length is not None and received != int(length):
                        raise OSError("下载传输被截断，已保留可续传片段")
                require(sha256(temp) == expected.lower(), "下载 SHA-256 不匹配")
                temp.replace(dest)
                resume.unlink(missing_ok=True)
                return dest
            except Exception as exc:
                # Invalid data/ranges are discarded. Transport failures preserve a prefix
                # across process restarts; its final full-file hash is never bypassed.
                if isinstance(exc, ValueError) or getattr(exc, "code", None) == 416:
                    temp.unlink(missing_ok=True)
                if attempt == 2:
                    print(f"下载源失败: {urllib.parse.urlsplit(source).hostname}: {type(exc).__name__}")
                else:
                    time.sleep(1 + attempt)
    if not temp.exists():
        resume.unlink(missing_ok=True)
    raise RuntimeError("所有下载源失败；可重运行续传、使用 --segmented，或安全复制完整缓存")


def extract(archive, dest):
    """Reject traversal, links, devices and ambiguous paths before extracting."""
    dest = Path(dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)

    def safe(name):
        normalized = name.replace("\\", "/")
        require(not normalized.startswith("/") and ":" not in normalized,
                "压缩包包含绝对路径")
        require((dest / normalized).resolve().is_relative_to(dest), "压缩包路径越界")

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                safe(member.filename)
                require((member.external_attr >> 16) & 0o170000 != 0o120000,
                        "压缩包不允许符号链接")
            source.extractall(dest)
            if os.name != "nt":
                for member in source.infolist():
                    mode = (member.external_attr >> 16) & 0o777
                    if mode:
                        (dest / member.filename).chmod(mode)
    else:
        with tarfile.open(archive) as source:
            for member in source.getmembers():
                safe(member.name)
                require(member.isfile() or member.isdir(), "压缩包不允许链接或设备")
            source.extractall(dest, filter="data")


def identifier(value):
    require(re.fullmatch(r"[a-z][a-z0-9-]{0,24}", value), "机器名必须是 1–25 位小写字母/数字/连字符，字母开头")
    return value
