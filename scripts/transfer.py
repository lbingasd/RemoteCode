"""Strict, restartable HTTP Range transfers; the full official hash is authoritative."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import re
import shutil
import threading
import time
from common import read, request, require, sha256, write


def content_range(response, start, end, total=None):
    value = response.headers.get("Content-Range", "")
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", value)
    require(response.status == 206 and match, "服务器没有返回合法的 206 Content-Range；拒绝拼接")
    actual_start, actual_end, actual_total = map(int, match.groups())
    require((actual_start, actual_end) == (start, end), "服务器返回了错误的字节范围")
    require(actual_total > end and (total is None or actual_total == total), "分段总长度变化")
    require(int(response.headers.get("Content-Length", end - start + 1)) == end - start + 1, "分段 Content-Length 不匹配")
    return actual_total


def segmented_download(url, dest, expected, mirror=None, workers=8, chunk_size=1024 * 1024):
    require(re.fullmatch(r"[a-fA-F0-9]{64}", expected or ""), "需要可信 SHA-256")
    require(1 <= workers <= 16 and 65536 <= chunk_size <= 16 * 1024 * 1024, "并发或分段大小超出范围")
    expected = expected.lower()
    dest = Path(dest)
    if dest.is_file() and sha256(dest) == expected:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    parts = dest.with_name(dest.name + ".chunks")
    parts.mkdir(exist_ok=True)
    sources = [url]
    if mirror:
        require(mirror.count("{url}") == 1, "镜像模板必须包含一次 {url}")
        sources.insert(0, mirror.replace("{url}", url))
    errors = []
    for source in sources:
        try:
            with request(source, {"Range": "bytes=0-0", "Accept-Encoding": "identity"}) as probe:
                total = content_range(probe, 0, 0)
                require(len(probe.read()) == 1, "Range 探测长度不匹配")
                etag = probe.headers.get("ETag")
            identity = {"schema": 1, "sha256": expected, "size": total, "chunk_size": chunk_size}
            manifest = parts / "manifest.json"
            if manifest.exists() and read(manifest) != identity:
                shutil.rmtree(parts)
                parts.mkdir()
            write(manifest, identity)
            count = (total + chunk_size - 1) // chunk_size
            print(f"分段下载: {total:,} 字节，{count} 段，{workers} 并发", flush=True)
            counter = [0, 0]
            mutex = threading.Lock()

            def fetch(index):
                start = index * chunk_size
                end = min(total, start + chunk_size) - 1
                length = end - start + 1
                path = parts / f"{index:06d}.bin"
                checksum = parts / f"{index:06d}.sha256"
                cached = path.is_file() and checksum.is_file() and path.stat().st_size == length and sha256(path) == checksum.read_text().strip()
                if not cached:
                    temp = path.with_suffix(".part")
                    partial_hash = path.with_suffix(".partial-sha256")
                    valid_partial = temp.is_file() and partial_hash.is_file() and temp.stat().st_size <= length and sha256(temp) == partial_hash.read_text().strip()
                    if not valid_partial:
                        temp.unlink(missing_ok=True)
                    position = temp.stat().st_size if temp.exists() else 0
                    failures = 0
                    span = min(chunk_size, 256 * 1024)
                    while position < length:
                        before = position
                        requested_start = start + position
                        requested_end = min(end, requested_start + span - 1)
                        requested_length = requested_end - requested_start + 1
                        try:
                            headers = {"Range": f"bytes={requested_start}-{requested_end}", "Accept-Encoding": "identity"}
                            if etag and not etag.startswith("W/"):
                                headers["If-Range"] = etag
                            with request(source, headers) as response:
                                content_range(response, requested_start, requested_end, total)
                                require(not etag or response.headers.get("ETag") == etag, "服务器 ETag 已变化")
                                data = response.read(requested_length + 1)
                                require(len(data) <= requested_length, "分段内容超长")
                                if data:
                                    with temp.open("ab") as prefix:
                                        prefix.write(data)
                                    position += len(data)
                                    write(partial_hash, sha256(temp) + "\n")
                                if len(data) != requested_length:
                                    raise OSError("子分段传输截断，保留已核对范围的前缀")
                            failures = 0
                        except OSError:
                            if position > before:
                                failures = 0
                                span = max(65536, span // 2)
                            else:
                                failures += 1
                            if failures >= 4:
                                raise
                            time.sleep(min(3, failures))
                    digest = sha256(temp)
                    temp.replace(path)
                    write(checksum, digest + "\n")
                    partial_hash.unlink(missing_ok=True)
                with mutex:
                    counter[0] += 1
                    counter[1] += int(cached)
                    if counter[0] % 20 == 0 or counter[0] == count:
                        print(f"完成 {counter[0]}/{count} 段（复用 {counter[1]} 段）", flush=True)

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(fetch, index) for index in range(count)]
                try:
                    for future in as_completed(futures):
                        future.result()
                except BaseException:
                    for future in futures:
                        future.cancel()
                    raise
            temp = dest.with_name(dest.name + ".part")
            with temp.open("wb") as output:
                for index in range(count):
                    with (parts / f"{index:06d}.bin").open("rb") as chunk:
                        shutil.copyfileobj(chunk, output)
            if sha256(temp) != expected:
                temp.unlink(missing_ok=True)
                shutil.rmtree(parts)
                parts.mkdir()
                raise ValueError("完整文件 SHA-256 不匹配，已清除不可信分段")
            temp.replace(dest)
            shutil.rmtree(parts)
            return dest
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            print(f"分段源失败，已完成的分段保留: {type(exc).__name__}", flush=True)
    raise RuntimeError("分段下载失败；再次运行可恢复。" + "; ".join(errors))
