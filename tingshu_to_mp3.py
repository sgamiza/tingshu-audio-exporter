#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
懒人听书（bubei.tingshu）缓存音频导出工具 —— PC 端（通过 ADB）。

手机端 APK 需要安装 Shizuku；这个脚本走 ADB，`adb shell` 本身就是 shell UID，
可以直接读取 `/sdcard/Android/data`，因此**不需要 Shizuku、不需要 root、不需要装任何 APP**，
是最快见效的路径。

懒人听书的下载目录结构（实测 8.7.91 / Android 13）：

    /sdcard/Android/data/bubei.tingshu/files/down/
    └── .<urlsafe_base64("书名|主播|作者")>/     专辑目录，点前缀隐藏
        ├── .<urlsafe_base64("004镇守鬼宅~04")>  章节音频，点前缀隐藏，无扩展名
        └── .cache/                              未完成下载的临时目录

章节文件是**未加密的标准 M4A（MP4 容器 + AAC）**，所以：
  * 导出 m4a  = 直接 pull + 改名，无损、极快，不需要任何外部工具
  * 导出 mp3  = 再用 ffmpeg 转码，需要 ffmpeg（可用 --setup-ffmpeg 自动下载）

用法示例：
    python tingshu_to_mp3.py --list
    python tingshu_to_mp3.py --album 1 --format m4a --out D:\\Tingshu
    python tingshu_to_mp3.py --format mp3 --out D:\\Tingshu
    python tingshu_to_mp3.py --setup-ffmpeg
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

PACKAGE_NAME = "bubei.tingshu"

CANDIDATE_ROOTS = [
    f"/sdcard/Android/data/{PACKAGE_NAME}/files/down",
    f"/sdcard/Android/data/{PACKAGE_NAME}/files/music/down",
    f"/sdcard/Android/data/{PACKAGE_NAME}/files/tingshu/audio",
    f"/sdcard/Android/data/{PACKAGE_NAME}/files/tingshu/song_audio",
]

# 小于这个大小的基本是元数据或下载残片
MIN_AUDIO_SIZE = 16 * 1024

NON_AUDIO_SUFFIX = (
    ".json", ".txt", ".xml", ".db", ".log", ".tmp", ".temp",
    ".download", ".jpg", ".jpeg", ".png", ".webp", ".nomedia",
)

ILLEGAL_CHARS = '<>:"/\\|?*'

FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or "http://YOUR_PROXY:8080"
TOOLS_DIR = Path(__file__).parent / "tools"


# --------------------------------------------------------------------------- 基础


def decode_name(raw: str) -> str:
    """解码懒人听书的 Base64 目录名/文件名；解码失败时原样返回，保证不丢文件。"""
    body = raw[1:] if raw.startswith(".") else raw
    if not body:
        return raw
    try:
        padded = body + "=" * (-len(body) % 4)
        data = base64.urlsafe_b64decode(padded)
        if not data:
            return raw
        text = data.decode("utf-8")
    except Exception:
        return raw
    # 解出控制字符说明这不是我们要的 Base64 名字
    if any(ord(c) < 0x20 and c not in "\n\t" for c in text):
        return raw
    return text


def sanitize(name: str, limit: int = 180) -> str:
    """去掉文件系统非法字符并限制字节长度。"""
    cleaned = "".join("_" if (c in ILLEGAL_CHARS or ord(c) < 0x20) else c for c in name)
    cleaned = cleaned.strip().rstrip(".")
    if not cleaned:
        cleaned = "未命名"
    while len(cleaned.encode("utf-8")) > limit:
        cleaned = cleaned[:-1]
    return cleaned


def human_size(n: int) -> str:
    for unit, div in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n} B"


def order_index(title: str) -> int:
    m = re.match(r"^(\d+)", title)
    return int(m.group(1)) if m else 10**9


# --------------------------------------------------------------------------- ADB


class AdbError(RuntimeError):
    pass


def adb(*args: str, timeout: int = 120) -> str:
    """执行 adb 命令并返回 stdout。"""
    cmd = ["adb", *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=timeout,
        )
    except FileNotFoundError:
        raise AdbError("找不到 adb，请把 platform-tools 加入 PATH") from None
    except subprocess.TimeoutExpired:
        raise AdbError(f"adb 命令超时：{' '.join(args)}") from None
    out = proc.stdout.decode("utf-8", errors="replace")
    err = proc.stderr.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise AdbError(f"adb {' '.join(args)} 失败：{err.strip() or out.strip()}")
    return out


def check_device() -> str:
    out = adb("devices", "-l", timeout=60)
    devices = [
        line for line in out.splitlines()[1:]
        if line.strip() and "device " in line + " " and "offline" not in line
    ]
    if not devices:
        raise AdbError(
            "没有检测到已授权的设备。请确认：\n"
            "  1) 手机已用 USB 连接\n"
            "  2) 已开启「开发者选项 → USB 调试」\n"
            "  3) 手机上已点击「允许此计算机调试」"
        )
    return devices[0].split()[0]


def shell(command: str, timeout: int = 120) -> str:
    return adb("shell", command, timeout=timeout)


def quote(path: str) -> str:
    """给 adb shell 用的单引号转义。"""
    return "'" + path.replace("'", "'\\''") + "'"


@dataclass
class RemoteEntry:
    name: str
    is_dir: bool
    size: int


def list_dir(path: str) -> list[RemoteEntry]:
    """
    列出远端目录。

    懒人听书的名字是 Base64（不含空格），所以解析 `ls -la` 是安全的；
    仍然按「最后一个字段为名字」处理，容忍异常条目。
    """
    try:
        out = shell(f"ls -la {quote(path)} 2>/dev/null")
    except AdbError:
        return []
    entries: list[RemoteEntry] = []
    for line in out.splitlines():
        line = line.rstrip()
        if not line or line.startswith("total ") or line.startswith("ls:"):
            continue
        fields = line.split(None, 7)
        if len(fields) < 8:
            continue
        perm, size_field, name = fields[0], fields[4], fields[7]
        if name in (".", ".."):
            continue
        try:
            size = int(size_field)
        except ValueError:
            size = 0
        entries.append(RemoteEntry(name=name, is_dir=perm.startswith("d"), size=size))
    return entries


# --------------------------------------------------------------------------- 扫描


@dataclass
class Chapter:
    remote_path: str
    raw_name: str
    title: str
    size: int


@dataclass
class Album:
    remote_path: str
    raw_name: str
    decoded_name: str
    chapters: list[Chapter] = field(default_factory=list)

    @property
    def parts(self) -> list[str]:
        """
        专辑名用 `|` 分隔，但字段数量和顺序都不固定，实测见过：
          · 典藏民间鬼故事|聊斋志异|李庆丰评书  （第一段像书名）
          · 俽林叔叔讲|封神榜                  （第一段反而像主播）
          · 李庆丰文化评书：封神演义            （一段，信息全挤在一起）
        所以不猜语义，只把第一段当主标识，其余原样保留。
        """
        return [p.strip() for p in self.decoded_name.split("|") if p.strip()]

    @property
    def book_name(self) -> str:
        return self.parts[0] if self.parts else self.decoded_name

    @property
    def extra_info(self) -> str:
        """除主标识以外的信息（可能含主播、作者、系列名）。"""
        return " · ".join(self.parts[1:])

    @property
    def total_size(self) -> int:
        return sum(c.size for c in self.chapters)


def is_valid_chapter(entry: RemoteEntry) -> bool:
    if entry.is_dir or entry.size < MIN_AUDIO_SIZE:
        return False
    return not entry.name.lower().endswith(NON_AUDIO_SUFFIX)


def find_root() -> str | None:
    for root in CANDIDATE_ROOTS:
        entries = list_dir(root)
        if any(e.is_dir for e in entries):
            return root
    return None


def scan(root: str) -> list[Album]:
    albums: list[Album] = []
    for entry in list_dir(root):
        if not entry.is_dir or entry.name.lstrip(".").lower() == "cache":
            continue
        album_path = f"{root}/{entry.name}"
        chapters = [
            Chapter(
                remote_path=f"{album_path}/{f.name}",
                raw_name=f.name,
                title=decode_name(f.name),
                size=f.size,
            )
            for f in list_dir(album_path)
            if is_valid_chapter(f)
        ]
        if not chapters:
            continue
        chapters.sort(key=lambda c: (order_index(c.title), c.title))
        albums.append(
            Album(
                remote_path=album_path,
                raw_name=entry.name,
                decoded_name=decode_name(entry.name),
                chapters=chapters,
            )
        )
    albums.sort(key=lambda a: a.book_name)
    return albums


def print_albums(albums: list[Album], show_chapters: bool = False) -> None:
    total_chapters = sum(len(a.chapters) for a in albums)
    total_bytes = sum(a.total_size for a in albums)
    print(f"\n共 {len(albums)} 个专辑 / {total_chapters} 集 / {human_size(total_bytes)}\n")
    for i, album in enumerate(albums, 1):
        suffix = f"  [{album.extra_info}]" if album.extra_info else ""
        print(f"  [{i}] {album.book_name}{suffix}")
        print(f"      {len(album.chapters)} 集 · {human_size(album.total_size)}")
        if show_chapters:
            for chapter in album.chapters:
                print(f"        - {chapter.title}  ({human_size(chapter.size)})")
    print()


# --------------------------------------------------------------------------- ffmpeg


def find_ffmpeg() -> str | None:
    bundled = TOOLS_DIR / "ffmpeg" / "bin" / "ffmpeg.exe"
    if bundled.is_file():
        return str(bundled)
    found = shutil.which("ffmpeg")
    return found


def setup_ffmpeg() -> str:
    """下载并解压 ffmpeg 到 tools/ffmpeg。"""
    import urllib.request

    print(f"正在下载 ffmpeg（约 40 MB）…\n  {FFMPEG_URL}")
    proxy_handler = urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
    opener = urllib.request.build_opener(proxy_handler)
    try:
        with opener.open(FFMPEG_URL, timeout=900) as resp:
            data = resp.read()
    except Exception as exc:
        print(f"通过代理下载失败（{exc}），尝试直连…")
        with urllib.request.urlopen(FFMPEG_URL, timeout=900) as resp:
            data = resp.read()

    print(f"下载完成（{human_size(len(data))}），正在解压…")
    target = TOOLS_DIR / "ffmpeg"
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        # 压缩包内是 ffmpeg-X.Y-essentials_build/bin/ffmpeg.exe，去掉最外层
        for member in zf.namelist():
            parts = member.split("/", 1)
            if len(parts) != 2 or not parts[1]:
                continue
            dest = target / parts[1]
            if member.endswith("/"):
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)

    exe = target / "bin" / "ffmpeg.exe"
    if not exe.is_file():
        raise RuntimeError(f"解压后找不到 {exe}")
    print(f"ffmpeg 就绪：{exe}")
    return str(exe)


# --------------------------------------------------------------------------- 导出


@dataclass
class TaskResult:
    title: str
    ok: bool
    output: str = ""
    error: str = ""


def export_chapter(
    album: Album,
    chapter: Chapter,
    index: int,
    out_dir: Path,
    fmt: str,
    ffmpeg: str | None,
    bitrate: str,
    overwrite: bool,
    mono: bool,
) -> TaskResult:
    album_dir = out_dir / sanitize(album.book_name)
    album_dir.mkdir(parents=True, exist_ok=True)
    ext = "mp3" if fmt == "mp3" else "m4a"
    dest = album_dir / f"{sanitize(chapter.title)}.{ext}"

    if dest.is_file() and dest.stat().st_size > 1024 and not overwrite:
        return TaskResult(chapter.title, True, str(dest))

    # 转码输出先落到 .part，成功后再改名。直接写 dest 的话，进程被强杀
    # （Ctrl+C、断电）会留下半成品，而它体积远超 1 KB，会被上面那句
    # 「已存在就跳过」误判成完成品，导致这一集永远缺一段且没有任何提示。
    part = dest.with_suffix(dest.suffix + ".part")

    tmp_fd, tmp_path = tempfile.mkstemp(prefix="tingshu_", suffix=".m4a")
    os.close(tmp_fd)
    try:
        # adb pull 不接受被单引号包裹的远端路径，直接传原始路径即可
        adb("pull", chapter.remote_path, tmp_path, timeout=600)
        if not os.path.getsize(tmp_path):
            return TaskResult(chapter.title, False, error="拉取到的文件为空")

        if fmt == "m4a":
            # 直拷本来就是先写临时文件再 move，move 是原子的，不存在半成品问题
            shutil.move(tmp_path, dest)
            tmp_path = ""
            return TaskResult(chapter.title, True, str(dest))

        if not ffmpeg:
            return TaskResult(chapter.title, False, error="缺少 ffmpeg")

        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", tmp_path,
            "-vn",
            # 丢掉源文件自带的 major_brand / iTunSMPB / 章节等噪音标签，只写我们自己的
            "-map_metadata", "-1",
            "-map_chapters", "-1",
            "-c:a", "libmp3lame",
            "-b:a", bitrate,
        ]
        if mono:
            cmd += ["-ac", "1"]
        cmd += [
            "-metadata", f"title={chapter.title}",
            "-metadata", f"artist={album.extra_info or '懒人听书'}",
            "-metadata", f"album={album.book_name}",
            "-metadata", f"track={index}",
            "-id3v2_version", "3",
            str(part),
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=1800)
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace").strip()
            part.unlink(missing_ok=True)
            return TaskResult(chapter.title, False, error=f"ffmpeg 失败：{err[:200]}")
        os.replace(part, dest)
        return TaskResult(chapter.title, True, str(dest))
    except BaseException as exc:
        # 用 BaseException 才能覆盖 Ctrl+C：KeyboardInterrupt 不是 Exception 的子类，
        # 漏掉它就会在目标目录留下半成品
        part.unlink(missing_ok=True)
        if isinstance(exc, KeyboardInterrupt):
            raise
        return TaskResult(chapter.title, False, error=str(exc))
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


def export(
    albums: list[Album],
    out_dir: Path,
    fmt: str,
    ffmpeg: str | None,
    bitrate: str,
    jobs: int,
    overwrite: bool,
    mono: bool,
) -> list[TaskResult]:
    tasks = [
        (album, chapter, i)
        for album in albums
        for i, chapter in enumerate(album.chapters, 1)
    ]
    total = len(tasks)
    if not total:
        print("没有可导出的章节")
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"开始导出 {total} 集 → {out_dir}（格式 {fmt}，并发 {jobs}）\n")

    results: list[TaskResult] = []
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {
            pool.submit(
                export_chapter, album, chapter, idx, out_dir, fmt,
                ffmpeg, bitrate, overwrite, mono,
            ): chapter
            for album, chapter, idx in tasks
        }
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            done += 1
            flag = "OK " if result.ok else "FAIL"
            detail = result.error if not result.ok else ""
            print(f"[{done}/{total}] {flag} {result.title} {detail}")

    return results


# --------------------------------------------------------------------------- CLI


def parse_album_selection(spec: str, count: int) -> list[int]:
    """解析 "1,3,5-7" 形式的编号，返回 0-based 下标。"""
    chosen: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            for n in range(int(a), int(b) + 1):
                chosen.add(n - 1)
        else:
            chosen.add(int(part) - 1)
    return sorted(i for i in chosen if 0 <= i < count)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="通过 ADB 导出懒人听书缓存音频并转换为 MP3",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--list", action="store_true", help="只列出专辑，不导出")
    parser.add_argument("--chapters", action="store_true", help="列出时同时显示每一集")
    parser.add_argument("--album", help="要导出的专辑编号，如 1,3,5-7；缺省为全部")
    parser.add_argument(
        "--format", choices=["mp3", "m4a"], default="mp3",
        help="输出格式；m4a 为无损直拷（不需要 ffmpeg），mp3 需要 ffmpeg",
    )
    parser.add_argument("--out", default="./TingshuExport", help="本地输出目录")
    parser.add_argument(
        "--bitrate", default="96k",
        help="MP3 码率，默认 96k。源是 48k HE-AAC，MP3 效率低得多，"
             "低于 96k 立体声会有明显损失；配合 --mono 时 64k 就够",
    )
    parser.add_argument(
        "--mono", action="store_true",
        help="转成单声道。评书/有声书基本是单声道内容，配合 --bitrate 64k 可减半体积",
    )
    parser.add_argument("--jobs", type=int, default=4, help="并发数，默认 4")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在的输出文件")
    parser.add_argument(
        "--limit", type=int, default=0,
        help="每个专辑最多导出前 N 集，用于先试听效果；0 表示不限制",
    )
    parser.add_argument("--root", help="手动指定手机上的下载目录")
    parser.add_argument("--setup-ffmpeg", action="store_true", help="下载 ffmpeg 到 tools/")
    args = parser.parse_args()

    if args.setup_ffmpeg:
        try:
            setup_ffmpeg()
            return 0
        except Exception as exc:
            print(f"下载 ffmpeg 失败：{exc}")
            return 1

    try:
        serial = check_device()
        print(f"设备：{serial}")
    except AdbError as exc:
        print(f"错误：{exc}")
        return 1

    root = args.root or find_root()
    if not root:
        print("没找到懒人听书的下载目录。已尝试：")
        for candidate in CANDIDATE_ROOTS:
            print(f"  · {candidate}")
        print("\n如果确实下载过音频，可以用 --root 手动指定路径。")
        return 1
    print(f"下载目录：{root}")

    albums = scan(root)
    if not albums:
        print("目录存在但没有找到可导出的音频文件。")
        return 1

    print_albums(albums, show_chapters=args.chapters or args.list)
    if args.list:
        return 0

    if args.album:
        indices = parse_album_selection(args.album, len(albums))
        if not indices:
            print(f"编号 {args.album} 没有匹配到任何专辑")
            return 1
        albums = [albums[i] for i in indices]
        print("已选择：" + "、".join(a.book_name for a in albums) + "\n")

    if args.limit > 0:
        for album in albums:
            album.chapters = album.chapters[: args.limit]
        print(f"已限制每个专辑最多 {args.limit} 集\n")

    ffmpeg = None
    if args.format == "mp3":
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            print(
                "输出 MP3 需要 ffmpeg，但没有找到。\n"
                "  · 自动下载：python tingshu_to_mp3.py --setup-ffmpeg\n"
                "  · 或者改用无损直拷（不需要 ffmpeg）：--format m4a\n"
            )
            return 1
        print(f"ffmpeg：{ffmpeg}")

    results = export(
        albums=albums,
        out_dir=Path(args.out),
        fmt=args.format,
        ffmpeg=ffmpeg,
        bitrate=args.bitrate,
        jobs=max(1, args.jobs),
        overwrite=args.overwrite,
        mono=args.mono,
    )

    ok = sum(1 for r in results if r.ok)
    failed = [r for r in results if not r.ok]
    print(f"\n完成：成功 {ok} · 失败 {len(failed)}")
    print(f"输出目录：{Path(args.out).resolve()}")
    if failed:
        print("\n失败明细（最多 10 条）：")
        for r in failed[:10]:
            print(f"  · {r.title}：{r.error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
