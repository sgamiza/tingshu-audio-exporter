# 阿米听书导出 (TingshuExporter)

> Find audiobook files that **懒人听书** (`bubei.tingshu`) downloaded onto the phone, decode readable Chinese names, and export them as MP3 or lossless M4A. There is an **Android APK** path and a **PC Python script** path.

The UI follows a Shizuku + grouped list + progress-bar pattern, but the audio pipeline is different: Lazy Audio Book does not need A/V muxing. The hard parts are **filename Base64 decode** and **building an MP3 encoder on Android from scratch**.

---

## Contents

- [Project overview](#project-overview)
- [Core reverse-engineering notes](#core-reverse-engineering-notes)
- [Features](#features)
- [Tech stack and dependencies](#tech-stack-and-dependencies)
- [Quick start: PC script (try this first)](#quick-start-pc-script-try-this-first)
- [Android APK usage](#android-apk-usage)
- [How to build the APK](#how-to-build-the-apk)
- [File structure](#file-structure)
- [Device test results](#device-test-results)
- [Key technical decisions](#key-technical-decisions)
- [Pitfalls](#pitfalls)
- [Known limits](#known-limits)

---

## Project overview

**Problem**: Lazy Audio Book hides downloads under `Android/data`. Directory and file names are Base64 with a `.` prefix (hidden files), and files have no extension. Result:

- A file manager cannot find them (Android 11+ blocks `Android/data`, and they are hidden)
- Even if you find them, you cannot tell which book is which (names like `.5YW46JeP5rCR6Ze06ay85pWF5LqL...`)
- Even if you copy them out, you do not know if they will play (no extension)

**What this project ships**:

| Deliverable | Path | When to use it |
|-------------|------|----------------|
| **PC script** | `tingshu_to_mp3.py` | USB debugging is on; **no phone-side setup**; fastest |
| **Android APK** | `TingshuExporter/` | Do everything on the phone; needs Shizuku |

Both paths are equivalent: scan → decode Chinese names → group by album → export MP3/M4A → write Chinese ID3 tags.

---

## Core reverse-engineering notes

Most valuable findings. Tested on: **懒人听书 8.7.91 / Android 13 / MIUI V816 / Redmi 21091116AC**.

### Download layout

```
/sdcard/Android/data/bubei.tingshu/files/down/
├── .<urlsafe_base64("典藏民间鬼故事|聊斋志异|李庆丰评书")>/   ← album dir (dot prefix = hidden)
│   ├── .<urlsafe_base64("001典藏民间鬼故事·镇守鬼宅~01")>    ← chapter audio (hidden, no extension)
│   ├── .<urlsafe_base64("002镇守鬼宅~02")>
│   └── .cache/                                              ← incomplete download temp (ignore)
└── ...
```

Other locations that appeared / may appear (the project probes them in order):

```
/sdcard/Android/data/bubei.tingshu/files/music/down
/sdcard/Android/data/bubei.tingshu/files/tingshu/audio
/sdcard/Android/data/bubei.tingshu/files/tingshu/song_audio
```

### Filename encoding

- Encoding: **URL-safe Base64** (`-` `_` instead of `+` `/`), UTF-8 after decode
- Padding is sometimes missing; pad to a multiple of 4 before decode
- Leading `.` so media scanners and file managers ignore the files

Decode examples (`python tingshu_to_mp3.py --list`):

| Raw name | Decoded |
|----------|---------|
| `.5YW46JeP5rCR6Ze06ay85pWF5LqLfOiBiuaWi-W_l-W8gnzmnY7luobkuLDor4TkuaY=` | `典藏民间鬼故事\|聊斋志异\|李庆丰评书` |
| `.MDA06ZWH5a6I6ay85a6FfjA0` | `004镇守鬼宅~04` |
| `.MDA15YW46JeP5rCR6Ze06ay85pWF5LqLwrfprLzpgrvlsYV-MDE=` | `005典藏民间鬼故事·鬼邻居~01` |

**Album-name field meaning is unreliable.** Fields are `|`-separated, but count and order change:

- `典藏民间鬼故事|聊斋志异|李庆丰评书` — three parts; first looks like the book title
- `俽林叔叔讲|封神榜` — two parts; first looks like the narrator
- `李庆丰文化评书：封神演义` — one part; everything mashed together

This project **does not guess** title vs narrator vs author: the first segment is the main id (folder name and ID3 `album`); remaining segments go into ID3 `artist`. Nothing is dropped, nothing is mislabeled.

### Audio format (important)

**Audio is not encrypted.** It is a standard MP4 container:

```
File header: 00 00 00 20 66 74 79 70 6d 70 34 32 ...
                       f  t  y  p  m  p  4  2
brands: mp42 / M4A / isom
```

Measured encoding:

| Item | Value |
|------|-------|
| Codec | **HE-AAC** (AAC+, with SBR) |
| Sample rate | 44100 Hz |
| Channels | 2 (stereo) |
| Bitrate | ~48 kbps |
| Chapter length | ~7 minutes (2.5 MB) |

Direct consequences:

1. **Rename to `.m4a` and it plays** — no transcode, lossless, instant
2. MP3 **grows**: 48 kbps HE-AAC is far more efficient than MP3. A 2.5 MB source at 96 kbps MP3 became 4.9 MB (~2×)
3. Because of HE-AAC, transcode **must use the decoder’s output sample rate/channels**, not the container-declared values (SBR changes decoder output; the wrong values pitch-shift)

---

## Features

### Android APK (阿米听书导出 v1.0)

**Finding files**
- Auto-probe 4 candidate download roots
- Recursive fallback: if the first layer has no audio, drill one level (extra wrapper folder from a manual copy)
- Base64 dir/file names → Chinese; keep the original name if decode fails (never drop a file)
- Filter metadata, download fragments, and `.cache` by size (≥16 KB) and extension

**Three access modes**
1. **Shizuku (the only reliable option on Android 11+)** — bind a remote service running as shell UID (2000)
2. **All-files access** — `MANAGE_EXTERNAL_STORAGE`; only some older MIUI builds
3. **Custom folder** — SAF-pick a folder you already copied out with a file manager (workaround)

**List UX**
- Group by album; select the whole group; tap to expand/collapse; expand/collapse all
- Live search (book or chapter); hits auto-expand that album
- 4 sorts: time new→old / old→new / name A→Z / Z→A
- Collapsible top toolbar; a compact Export button stays on the title bar when collapsed
- Chapter titles natural-sort on leading digits (`002` before `010`)

**Export**
- Two formats:
  - **MP3** — MediaCodec decode AAC → LAME re-encode; widely compatible
  - **M4A** — copy + rename; lossless, seconds, smaller files
- MP3 knobs: quality (fast / balanced / high), bitrate (auto / 64 / 96 / 128 kbps)
- **Auto bitrate**: 2× source bitrate, snapped to a legal LAME step (48k source → 96k out)
- Parallel transcode (default half of physical cores, clamped 2–4, to avoid thermal throttle)
- **UTF-16 Chinese ID3v2.3**: title / artist / album / track
- Skip outputs that already exist and look complete; resume after interrupt
- **Write `.part` then atomic rename**: if the target exists it is complete. Frozen/killed leftovers are not treated as done (see [Pitfalls](#pitfalls))
- Optional delete sources after export (dangerous; second confirm); delete only after a complete output exists
- Notify `MediaScanner` so files show up in music apps and MTP immediately
- Failures are not silent: partial files are cleaned; errors are summarized in a result dialog

**Progress and background**
- Progress panel sits **directly under the title bar**, still visible when the toolbar is collapsed or in landscape (inside the tool area it gets pushed off-screen)
- Percent, done/total, parallelism, elapsed, **ETA**, current chapter
- Progress is **smooth**: in-file transcode progress counts toward the total, not one jump per finished chapter (a chapter takes tens of seconds)
- **Foreground-service notification** with progress: lock screen and background; Stop from the notification
- Export runs in the `ExportController` singleton, not Activity `lifecycleScope`; UI recycle/rebuild does not stop the job; re-entering the app restores the progress UI immediately
- Holds `PARTIAL_WAKE_LOCK` + keep-screen-on during transcode
  (**Note**: on MIUI a wake lock alone is not enough; you need a foreground notification — see [Pitfalls](#pitfalls))

Output layout:

```
/sdcard/Download/TingshuExport/
└── 典藏民间鬼故事/
    ├── 001典藏民间鬼故事·镇守鬼宅~01.mp3
    └── 002镇守鬼宅~02.mp3
```

### PC script (`tingshu_to_mp3.py`)

- Reads `Android/data` via `adb shell` (adb shell is already shell UID).
  **No Shizuku, no root, nothing to install on the phone**
- `--list` lists albums/chapters with decoded Chinese names and sizes
- `--album 1,3,5-7` pick albums by number; `--limit N` first N chapters per album (preview)
- `--format m4a` lossless copy (no extra deps) / `--format mp3` ffmpeg transcode
- `--mono` downmix (storytelling + `--bitrate 64k` can halve size)
- `--setup-ffmpeg` downloads ffmpeg into `tools/` (uses an HTTP/HTTPS proxy if set)
- Multi-thread (`--jobs`, default 4), resume (existing outputs skipped; `--overwrite` to force)
- Clean Chinese ID3v2.3 (`-map_metadata -1` strips source noise: `major_brand` / `iTunSMPB` / chapters)

---

## Tech stack and dependencies

### Android APK

| Dependency | Version | Role |
|------------|---------|------|
| Kotlin | 1.8.10 | Primary language |
| AGP | 7.4.2 | Build plugin |
| Gradle | 7.5 | Build system |
| JDK | 17 | Compile (**not 21/25** — see Pitfalls) |
| compileSdk / targetSdk | 34 | — |
| minSdk | 26 (Android 8) | — |
| **de.sciss:jump3r** | 1.0.5 (stripped local jar) | **Pure-Java LAME MP3 encoder** |
| dev.rikka.shizuku:api / provider | 13.1.5 | Shizuku client |
| androidx.appcompat | 1.6.1 | Compat |
| com.google.android.material | 1.9.0 | Material UI |
| androidx.recyclerview | 1.3.2 | Grouped list |
| androidx.documentfile | 1.0.1 | SAF wrapper |
| kotlinx-coroutines-android | 1.7.3 | Parallel transcode |
| androidx.lifecycle-runtime-ktx | 2.7.0 | `lifecycleScope` |

APK is about 5.9 MB (jump3r ~330 KB).

### PC script

- Python 3.10+ (`X | None` annotations)
- `adb` (Android platform-tools, on PATH)
- ffmpeg — **only** for `--format mp3`; `--setup-ffmpeg` can fetch it

### Phone permissions

| Permission | Role |
|------------|------|
| `READ_EXTERNAL_STORAGE` | Read storage on Android ≤12 (`maxSdkVersion=32`) |
| `WRITE_EXTERNAL_STORAGE` | Write storage on Android ≤9 (`maxSdkVersion=29`) |
| `MANAGE_EXTERNAL_STORAGE` | Mode ②; Google Play forbids it, sideload does not |
| `WAKE_LOCK` | Keep CPU awake during transcode |

---

## Quick start: PC script (try this first)

### Prerequisites

1. Phone on USB
2. Developer options → USB debugging, and allow this computer
3. `adb` on PATH

### 1. See what is on the phone

```powershell
python tingshu_to_mp3.py --list
```

Example (decoded names):

```
设备：HI85UK4XQ4VSOJPR
下载目录：/sdcard/Android/data/bubei.tingshu/files/down

共 3 个专辑 / 321 集 / 2.3 GB

  [1] 典藏民间鬼故事  [聊斋志异 · 李庆丰评书]
      50 集 · 126.0 MB
        - 001典藏民间鬼故事·镇守鬼宅~01  (2.5 MB)
        - 002镇守鬼宅~02  (2.6 MB)
  [2] 李庆丰文化评书：封神演义
      221 集 · 1.4 GB
  [3] 李庆丰文化评书：聊斋志异
      50 集 · 773.2 MB
```

### 2. Lossless export (fastest, no ffmpeg)

```powershell
# Export album 1 as m4a
python tingshu_to_mp3.py --album 1 --format m4a --out D:\Tingshu

# Export everything
python tingshu_to_mp3.py --format m4a --out D:\Tingshu
```

Measured: 50 chapters / 126 MB in about **19 seconds** (4 workers).

### 3. Export MP3

```powershell
# First time: download ffmpeg (~106 MB; uses proxy env if set)
python tingshu_to_mp3.py --setup-ffmpeg

# Try 2 chapters
python tingshu_to_mp3.py --album 1 --format mp3 --limit 2 --out D:\Tingshu

# Then the rest; storytelling can use mono to save space
python tingshu_to_mp3.py --album 1 --format mp3 --mono --bitrate 64k --out D:\Tingshu
```

Measured: ~10–16 seconds per 7-minute chapter.

### Flag cheat sheet

| Flag | Meaning |
|------|---------|
| `--list` | List only, no export |
| `--chapters` | Show every chapter when listing |
| `--album 1,3,5-7` | Album numbers |
| `--limit N` | At most N chapters per album |
| `--format mp3\|m4a` | Output format, default `mp3` |
| `--out DIR` | Output dir, default `./TingshuExport` |
| `--bitrate 96k` | MP3 bitrate, default `96k` |
| `--mono` | Downmix to mono |
| `--jobs 4` | Parallelism |
| `--overwrite` | Overwrite existing files |
| `--root PATH` | Manual download root on the phone |
| `--setup-ffmpeg` | Download ffmpeg into `tools/` |

---

## Android APK usage

### Install

APK: `TingshuExporter/app/build/outputs/apk/debug/app-debug.apk`

> **MIUI**: `adb install` fails with `INSTALL_FAILED_USER_RESTRICTED`.
> That is a hard MIUI limit; `pm install` / install sessions fail too.
> Enable **Settings → Additional settings → Developer options → USB debugging (Security settings)** (Xiaomi account required).
>
> After a build, install `TingshuExporter/app/build/outputs/apk/debug/app-debug.apk`, or copy the APK to the phone and install from a file manager.

### First run

1. **Prepare Shizuku** (required on Android 11+)

   Shizuku starts a shell-privileged service; this app uses it to read `Android/data`.

   - Install: https://github.com/RikkaApps/Shizuku/releases
   - Start A (phone only): in the Shizuku app, “Start via wireless debugging”
   - Start B (computer ADB):

     ```powershell
     # Shizuku 13.6 no longer ships start.sh; use the native starter inside the APK
     adb shell "pm path moe.shizuku.privileged.api"
     # Take that path, replace base.apk with lib/arm64/libshizuku.so
     adb shell "/data/app/~~xxx/moe.shizuku.privileged.api-yyy/lib/arm64/libshizuku.so"
     ```

     Success looks like `info: shizuku_server pid is NNNNN`.

   After reboot Shizuku stops; start it again.

2. **Open this app**. The status card shows Android version, whether Lazy Audio Book is installed, and Shizuku state

3. Tap **“① Shizuku scan”**; allow the permission prompt the first time

4. When the list appears: check albums or chapters → tap “Export selected audio”

5. In the options dialog pick format (MP3 / M4A), quality, bitrate, parallelism → “Start export”

6. Files land in `/sdcard/Download/TingshuExport/<book>/`; USB to a PC shows them immediately

### Fallbacks

- **Mode ② “All files access”**: only some older MIUI builds can read `Android/data`; most Android 13 devices block it
- **Mode ③ “Pick a copied folder”**: with MIUI Files copy `Android/data/bubei.tingshu/files/down` to `Internal storage/Download/`, then pick that `down` folder in the app (MIUI Files has extra access to `Android/data`)

---

## How to build the APK

### Environment

| Tool | Version | Example local path |
|------|---------|---------------------|
| JDK | **17** (not 21/25) | `C:\Program Files\Eclipse Adoptium\jdk-17.0.8.101-hotspot` |
| Gradle | 7.5 | `C:\Users\<user>\.gradle\wrapper\dists\gradle-7.5-bin\...\gradle-7.5\bin\gradle.bat` |
| Android SDK | platform-tools + platforms;android-34 + build-tools;34.0.0 | `C:\Android\sdk` |

### Build (Windows PowerShell)

```powershell
$env:JAVA_HOME  = "C:\Program Files\Eclipse Adoptium\jdk-17.0.8.101-hotspot"
$env:ANDROID_HOME = "C:\Android\sdk"
$env:HTTP_PROXY  = "http://YOUR_PROXY:8080"   # first dependency fetch on a proxied LAN
$env:HTTPS_PROXY = "http://YOUR_PROXY:8080"
.\gradlew.bat -p TingshuExporter assembleDebug --no-daemon
```

Output: `TingshuExporter/app/build/outputs/apk/debug/app-debug.apk`

### Rebuild the Android jump3r jar

`app/libs/jump3r-android-1.0.5.jar` is the official jar with classes that do not exist on Android stripped. To regenerate:

```powershell
# 1. Download the official jar
curl.exe -o jump3r.jar "https://repo1.maven.org/maven2/de/sciss/jump3r/1.0.5/jump3r-1.0.5.jar"

# 2. Drop classes that need javax.sound / java.beans (not on Android)
python -c @"
import zipfile
src = zipfile.ZipFile('jump3r.jar')
drop = {'de/sciss/jump3r/lowlevel/LameEncoder.class'}          # javax.sound
drop |= {n for n in src.namelist() if n.startswith('de/sciss/jump3r/Main')}  # java.beans
out = zipfile.ZipFile('jump3r-android-1.0.5.jar', 'w', zipfile.ZIP_DEFLATED)
for n in src.namelist():
    if n not in drop:
        out.writestr(src.getinfo(n), src.read(n))
out.close()
"@
```

After strip: 116 classes (~330 KB). Core `de.sciss.jump3r.mp3.*` and `de.sciss.jump3r.mpg.*` are pure Java and run on Android.

---

## File structure

```
tingshu-audio-exporter/
├── README.md                        # This file
├── tingshu_to_mp3.py                # PC export script (adb; no phone setup)
├── .gitignore
├── tools/
│   └── ffmpeg/                      # Optional: script-downloaded ffmpeg
│
└── TingshuExporter/                 # Android project root
    ├── settings.gradle              # rootProject.name + include ':app'
    ├── build.gradle                 # buildscript: AGP 7.4.2 + Kotlin 1.8.10
    ├── gradle.properties            # JVM args + AndroidX + proxy
    ├── local.properties             # sdk.dir=...
    ├── gradle/wrapper/
    │   └── gradle-wrapper.properties  # gradle-7.5-bin
    │
    └── app/
        ├── build.gradle             # Module config and deps
        ├── proguard-rules.pro       # Keep jump3r and AIDL Stub
        ├── libs/
        │   └── jump3r-android-1.0.5.jar   # Stripped pure-Java LAME
        │
        └── src/main/
            ├── AndroidManifest.xml  # Permissions, queries, ShizukuProvider
            │
            ├── aidl/com/tingshuexport/downloader/
            │   └── IUserService.aidl        # Shizuku remote API (openRead passes an fd)
            │
            ├── java/com/tingshuexport/downloader/
            │   ├── TingshuModels.kt         # Models + TingshuNaming (Base64, candidate paths)
            │   ├── TingshuScanner.kt        # FileAccess + Direct/Shizuku + scan/sort
            │   ├── UserService.kt           # Shizuku remote service (shell UID)
            │   ├── ShizukuHelper.kt         # Shizuku status, grant, bind
            │   ├── Mp3Encoder.kt            # jump3r/LAME wrapper (PCM → MP3 + Chinese ID3)
            │   ├── AudioConverter.kt        # MediaExtractor + MediaCodec AAC → Mp3Encoder
            │   ├── TingshuExporter.kt       # Export: parallel, name sanitize, progress, report
            │   ├── AudioAdapter.kt          # RecyclerView grouped adapter (album header + chapter)
            │   └── MainActivity.kt          # UI: env check, scan, select, export, wake lock
            │
            └── res/
                ├── layout/
                │   ├── activity_main.xml
                │   ├── item_album_header.xml
                │   ├── item_chapter.xml
                │   └── dialog_export_options.xml
                ├── values/
                │   ├── strings.xml
                │   ├── colors.xml
                │   ├── themes.xml
                │   └── ic_launcher_background.xml
                ├── drawable/
                │   ├── ic_arrow_up.xml
                │   ├── ic_arrow_down.xml
                │   ├── ic_close.xml
                │   └── ic_launcher_foreground.xml
                └── mipmap-anydpi-v26/
                    ├── ic_launcher.xml
                    └── ic_launcher_round.xml
```

---

## Device test results

Device: **Redmi 21091116AC / Android 13 (API 33) / MIUI V816**, APK v1.0, Shizuku granted.

### Self-check

On launch the app reports:

```
Android 13（API 33） · Xiaomi 21091116AC
懒人听书: 已安装
所有文件访问权限: 未授予
Shizuku: Shizuku 就绪，可直接扫描懒人听书下载目录。
```

### Shizuku scan

After “① SHIZUKU scan”:

```
扫描完成（Shizuku）
目录：/sdcard/Android/data/bubei.tingshu/files/down
共 3 个专辑 / 321 集 / 2.28 GB
源文件是未加密的 M4A(AAC)，可无损导出或转成 MP3。
```

Matches the PC script: Base64 names decode to Chinese, sizes are correct. Group expand/collapse/check works.

### Transcode

One chapter (7 min, 2.6 MB source), “Convert to MP3” + “Fast”: about **20 seconds**.
ffprobe on the output:

| Item | Result |
|---|---|
| Container / codec | MP3 |
| Sample rate | 44100 Hz |
| Channels | 2 |
| Bitrate | 128 kbps |
| Duration | 273 s (matches source) |
| Size | 4.17 MB |
| ID3 | title / artist / album / track Chinese all correct |

Path `/sdcard/Download/TingshuExport/<book>/<chapter>.mp3`. Media scan runs after export; the system music player sees the files immediately.

**Conclusion: Shizuku read, Base64 decode, MediaCodec AAC decode, jump3r MP3 encode, Chinese ID3 — the full chain works on a real device.**

### Progress and lock-screen continue (v2)

Export “典藏民间鬼故事” 50 chapters as MP3. UI showed:

```
3%  ·  1 / 50  ·  4 个并行
[████░░░░░░░░░░░░░░░░░░░░░░░░░░]
已用 12 秒  ·  剩余约 5:14
049四吊钱~02
```

Notification (`dumpsys notification`):

```
android.title    = 正在导出音频
android.subText  = 8%
android.text     = 1 / 50  ·  剩余约 7:49
android.progress = 8 / 100
android.bigText  = 1 / 50 · 剩余约 7:49 · 已用 44 秒
                   048典藏民间鬼故事·四吊钱~01
actions          = [停止]
numForegroundService = 160
```

**Lock-screen continue**: after 75 s locked, MP3 count 9 → 13, process CPU 429% (4 cores), `Wake Locks: size=1`. Before the fix the same action produced nothing for 12 minutes (see [Pitfalls](#pitfalls)).

This round exported 43 chapters (278 MB). Spot-check of new output:

```
duration = 432.51 s        bit_rate = 96003
title    = 010尸变~04
artist   = 聊斋志异 · 李庆丰评书
album    = 典藏民间鬼故事      track = 10
```

Auto bitrate chose 96 kbps (2× 48 kbps source). `artist` kept the extra album-name fields.

**`.part` in practice**: `am force-stop` mid-transcode left 4 `*.mp3.part` files, not truncated `.mp3` that look finished. Re-export redid those chapters.

> While transcoding, file size grows as it writes; LAME flushes by frame, so a stuck size for a short time is not a hang. Wait for the app to report completion.

---

## Key technical decisions

### 1. Why ship an MP3 encoder

**Android `MediaCodec` has an MP3 decoder, not an MP3 encoder.** System audio encoders are AAC, AMR, FLAC, Opus. Real MP3 means one of:

| Approach | Notes |
|----------|-------|
| NDK-build libmp3lame | Fastest, but NDK (~1 GB) + CMake/JNI |
| ffmpeg-kit | Huge; project unmaintained; binaries pulled from Maven |
| **Pure-Java LAME (jump3r)** | **330 KB, no NDK — this is the one** |

`jump3r` is a pure-Java port of LAME 3.98.4. Of 119 classes, only 2 need packages Android lacks (`LameEncoder` → `javax.sound`, `Main` → `java.beans`); those are convenience wrappers. After stripping them, `de.sciss.jump3r.mp3.*` works.

**The full chain was proven on JDK 17 on a PC before any Kotlin**: LAME 3.98.4 init OK, 2 s of 128 kbps → 33017 bytes (theory 32000; delta is Xing header), frame header `ff fb 90 64` = MPEG-1 Layer III / 128 kbps / 44.1 kHz, 79 frames, structure valid.

### 2. Chinese ID3 tags

jump3r `id3tag_set_title()` is Latin-1; Chinese garbles. Use `id3tag_set_textinfo_ucs2(gfp, "TIT2", "\uFEFF" + text)` — **text must start with BOM (U+FEFF)** or the function returns `-3`. Track `TRCK` is numeric Latin-1; use `id3tag_set_track()`.

PC verification:

```
ID3v2.3.0  tagsize=157
  TIT2  enc=1  ->  '004镇守鬼宅~04'
  TPE1  enc=1  ->  '李庆丰评书'
  TALB  enc=1  ->  '典藏民间鬼故事'
  TRCK  enc=0  ->  '4'
```

### 3. Zero-copy source reads

A common pattern is “Shizuku copies the file to public storage, then the app processes it.” This project adds to AIDL:

```aidl
ParcelFileDescriptor openRead(String path) = 6;
```

The Shizuku service opens the file as shell UID and **passes the fd over binder**. The app feeds `MediaExtractor.setDataSource(fd, 0, length)`.

Benefit: transcoding 2.3 GB of audiobooks does not copy a staging set first — half the I/O and no extra disk. This works because `ParcelFileDescriptor.open()` returns a real seekable file fd.

### 4. Decoder output format, not container-declared format

Source is **HE-AAC** (SBR). MediaCodec on different devices may output the core sample rate or the SBR-reconstructed 2× rate. Initializing LAME at the container’s 44100 on a device that outputs 22050 pitch-shifts everything.

`AudioConverter` therefore **creates `Mp3Encoder` only after `INFO_OUTPUT_FORMAT_CHANGED`**, using the decoder’s `KEY_SAMPLE_RATE` / `KEY_CHANNEL_COUNT`. It also handles `ENCODING_PCM_FLOAT` instead of 16-bit on some devices.

### 5. Bitrate choice

Source is 48 kbps HE-AAC. HE-AAC is much more efficient than MP3, so:

- MP3 at 48 kbps sounds worse
- Fixed 128 kbps makes files ~2.7× the source

Compromise: **source bitrate × 2**, snap to a legal LAME step (48k → 96k). If source bitrate is unknown, estimate from sample rate/channels.

### 6. Parallelism = half the cores

A pure-Java encoder on every core heats the phone and throttles; measured slower. Default `availableProcessors() / 2`, clamped 2–4.

---

## Pitfalls

### Kotlin nested block comments

A KDoc path `/sdcard/Android/data/*` fails with `Unclosed comment` — Kotlin (unlike Java) **nests block comments**, so `/*` opens another level, the file fails to parse, and other files “cannot see” classes in this file. Do not put `/*` in comments.

### MIUI blocks every ADB install

`INSTALL_FAILED_USER_RESTRICTED: Install canceled by user`. These all **fail**:

```powershell
adb install -r app-debug.apk
adb shell pm install -r -t /data/local/tmp/x.apk
adb shell pm install -r -t --user 0 /data/local/tmp/x.apk
adb shell pm install-create / install-write / install-commit   # create succeeds, commit denied
```

`settings get secure install_non_market_apps` is already `1`. Enable MIUI “USB debugging (Security settings)” (Xiaomi account), or copy the APK onto the phone and tap-install.

### MIUI freezes background apps that hold a wake lock (export silently stops)

**Symptom**: after lock screen, export stops but the process is still there:

```
State:  D (disk sleep)          # kernel-suspended
%CPU:   0.0                     # doing nothing
Wake Locks: size=0              # PARTIAL_WAKE_LOCK taken back
```

No new files for 12 minutes; the app still *looks* like it is exporting.

**Cause**: `PARTIAL_WAKE_LOCK` does not beat MIUI background policy. After lock, a normal background process is frozen and the wake lock is reclaimed.

**Fix**: a **foreground-service notification**. That is what (including MIUI) requires to not freeze the process. Same 75 s lock-screen comparison after the fix:

| | Wake lock only | Foreground service + wake lock |
|---|---|---|
| Files | 0 in 12 min | +4 in 75 s |
| CPU | 0.0% | 429% (4 cores) |
| Process | `D (disk sleep)` | `S` (normal) |
| Wake Locks | `size=0` | `size=1` held |

Bonus: progress is visible on lock screen and in the background.

### Partial files were treated as “already exported”

Skip-if-done used to be:

```kotlin
if (destFile.isFile && destFile.length() > 1024) {
    return ExportResult(chapter, true, destFile.absolutePath)   // treat as success
}
```

If the process is frozen/killed, partials of several MB sit above the 1 KB threshold. The next export **skips the truncated file with no warning**.

Hit in testing: 4 files frozen mid-transcode; ffprobe showed 165 s vs 1356 s for a complete sibling — 80% missing, but the start still plays.

**Fix**: write `<name>.mp3.part`, then `renameTo` the target. Rename is atomic, so a target that exists is complete.
The PC script does the same, and catches `BaseException` so `Ctrl+C` is covered (`KeyboardInterrupt` is not a subclass of `Exception`).

Also: delete the source only after rename succeeds, or an interrupted transcode permanently loses content.

### Shizuku 13.6 no longer ships start.sh

The widely copied `adb shell sh /sdcard/Android/data/moe.shizuku.privileged.api/start.sh` on 13.6 is `No such file or directory`.
`app_process` + `moe.shizuku.starter.ServerStarter` `Aborted` immediately.

13.6 packs the native starter as `lib/<abi>/libshizuku.so`. Run that:

```powershell
adb shell "pm path moe.shizuku.privileged.api"
adb shell "/data/app/~~xxx/moe.shizuku.privileged.api-yyy/lib/arm64/libshizuku.so"
# info: shizuku_server pid is 32346
```

### ffmpeg copies source metadata into MP3

Without cleanup the MP3 picks up `TXXX:major_brand`, `TXXX:iTunSMPB`, `CTOC`/`CHAP`. Strip with `-map_metadata -1 -map_chapters -1` then write our tags.

### Encoding in PowerShell

`python -c "print(中文)"` raises `UnicodeEncodeError: 'charmap' codec`. The script wraps stdout with `io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')`; one-off commands need their own wrap.

### Gradle / JDK combos

- Gradle 7.5 + JDK 21/25 → `Unsupported class file major version 69`
- Gradle 8.x + JDK 11 → fails to start
- AGP 8.x + Gradle 7.x → mismatch

Stable combo: **Gradle 7.5 + AGP 7.4.2 + JDK 17**.
If JDK 25 is installed, set `$env:JAVA_HOME` to JDK 17 for the build.

---

## Known limits

- **APK must be installed by hand**: MIUI blocks every ADB sideload (`pm install`, install sessions, even launching the system installer). Use a file manager tap, or enable “USB debugging (Security settings)”. After install, features were verified on device — see [Device test results](#device-test-results).
- **MP3 makes files larger** (48k HE-AAC → 96k MP3 ~2×). If format does not matter, `--format m4a` is lossless, smaller, and tens of times faster.
- Pure-Java LAME is slower than native libmp3lame; hundreds of chapters on a phone take a while. Prefer M4A; if you need MP3, use Fast quality and more workers.
- Album `|` field meaning is undefined; this project does not guess (see [Core reverse-engineering notes](#core-reverse-engineering-notes)).
- SAF folder pick only supports internal storage (`primary:`); SD card paths cannot be turned back into real paths.
- Verified on 懒人听书 8.7.91. A major app update that changes layout or encrypts audio needs another reverse pass; `TingshuNaming.ALTERNATIVE_ROOTS` is the extension point for extra candidate paths.
