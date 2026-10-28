#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
救砖工具 BrickRescue v1.0
=========================================================================
刷内核成砖（无限重启 / 卡一屏 / 卡 fastboot / 只进 recovery / 黑屏 9008）
的一体化救援工具：选症状 → 出方案 → 绑定救砖文件 → 一键执行。

设计要点：
  * 13 种症状 → 13 套方案，按症状自动匹配排序
  * 救砖文件库自动识别（boot / dtbo / vbmeta / vendor_boot / recovery /
    super / 全量包 / firehose），也支持手动指定
  * 所有命令先预览再执行，未确认不动设备
  * 执行前强制走 preflight（设备状态 / 解锁状态 / 文件校验）
  * 每个方案可导出成独立 .bat，脱离本工具也能跑
  * 全程日志落盘 logs/rescue_*.log

用法：
  python brick_rescue.py                      # 打开图形界面
  python brick_rescue.py --cli                # 命令行交互模式
  python brick_rescue.py --list               # 列出症状与方案
  python brick_rescue.py --dry-run P01        # 只渲染命令不执行
  python brick_rescue.py --selftest           # 自检（不接触设备）
=========================================================================
"""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

APP_NAME = "救砖工具 BrickRescue"
VERSION = "1.3"

ROOT = Path(__file__).resolve().parent
LIB_DIR = ROOT / "rescue_files"
LOG_DIR = ROOT / "logs"
BACKUP_DIR = ROOT / "backups"
OUT_DIR = ROOT / "out"
CONFIG_FILE = ROOT / "config.json"

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# ---------------------------------------------------------------------------
# 全局配置
# ---------------------------------------------------------------------------
CONFIG = {"platform_tools": "", "library": str(LIB_DIR),
          "log_dir": str(LOG_DIR), "keep_data": False}
DEFAULT_CONFIG = dict(CONFIG)


def load_config():
    global CONFIG
    try:
        if CONFIG_FILE.is_file():
            CONFIG.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass


def save_config():
    try:
        CONFIG_FILE.write_text(json.dumps(CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def lib_dir() -> Path:
    p = Path(CONFIG.get("library") or LIB_DIR)
    return p


def log_dir() -> Path:
    p = Path(CONFIG.get("log_dir") or LOG_DIR)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        return LOG_DIR
    return p


def keep_data() -> bool:
    return bool(CONFIG.get("keep_data"))


def ensure_dirs():
    for d in (LIB_DIR, LOG_DIR, BACKUP_DIR, OUT_DIR):
        d.mkdir(parents=True, exist_ok=True)
    log_dir()
    for sub in ("boot", "dtbo", "vbmeta", "vendor_boot", "recovery", "super", "fullpackage", "edl"):
        (LIB_DIR / sub).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 保留资料模式：任何会清空用户数据的命令都在这里被识别出来
# ---------------------------------------------------------------------------
DATA_PARTS = {"userdata", "userdata_a", "userdata_b", "metadata", "metadata_a",
              "metadata_b", "cache", "cache_a", "cache_b", "persist"}
DATA_VERBS = ("erase", "format", "flash", "wipe", "w", "delete")


def wipe_reason(*texts) -> str:
    """命中会清空数据的命令时返回原因，否则返回空串"""
    s = " ".join(str(t) for t in texts if t).strip().lower()
    if not s:
        return ""
    if re.search(r"(?:^|\s)-w(?:\s|$)", s) or "--wipe" in s or " -w " in f" {s} ":
        return "带 -w / --wipe 参数，会清空 userdata + metadata + cache"
    for m in re.finditer(r"\b(erase|format|flash|wipe)\s+([a-z0-9_\-]+)", s):
        if m.group(2) in DATA_PARTS:
            return f"{m.group(1)} {m.group(2)} 会清空该数据分区"
    if re.search(r"\bclean\s+all\b", s):
        return "clean all（MiFlash 清空全部并上锁）"
    return ""


# ---------------------------------------------------------------------------
# 工具链定位
# ---------------------------------------------------------------------------
KNOWN_TOOL_DIRS = [
    r"D:\dsh\android-sdk\platform-tools",
    r"C:\platform-tools",
    r"C:\adb",
    os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\platform-tools"),
    os.path.expandvars(r"%USERPROFILE%\platform-tools"),
    os.path.expandvars(r"%USERPROFILE%\Downloads\platform-tools"),
    os.path.expandvars(r"%USERPROFILE%\Desktop\platform-tools"),
    r"D:\软件\LeiDian\LDPlayer9",
]

TOOLS = {"adb": None, "fastboot": None}


def _exe(name):
    return name + ".exe" if os.name == "nt" else name


def find_tool(name: str) -> str:
    cands = []
    pt = (CONFIG.get("platform_tools") or "").strip()
    if pt:
        cands += [Path(pt) / _exe(name), Path(pt) / name]
    for base in (ROOT / "platform-tools", ROOT / "tools" / "platform-tools", ROOT):
        cands += [base / _exe(name), base / name]
    w = shutil.which(name)
    if w:
        cands.append(Path(w))
    for d in KNOWN_TOOL_DIRS:
        if d:
            cands += [Path(d) / _exe(name), Path(d) / name]
    for c in cands:
        try:
            if c and c.is_file():
                return str(c)
        except OSError:
            pass
    # 最后在常见下载目录浅层搜索
    for base in (Path.home() / "Downloads", Path.home() / "Desktop"):
        try:
            if base.is_dir():
                for c in base.glob(f"**/{_exe(name)}"):
                    return str(c)
        except OSError:
            pass
    return ""


def resolve_tools():
    for n in ("adb", "fastboot"):
        TOOLS[n] = find_tool(n)
    return TOOLS


def tool(name: str) -> str:
    if not TOOLS.get(name):
        resolve_tools()
    return TOOLS.get(name) or name


# ---------------------------------------------------------------------------
# 进程执行
# ---------------------------------------------------------------------------
def run_capture(cmd, timeout=15, cwd=None):
    """返回 (returncode, 输出)"""
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
            creationflags=CREATE_NO_WINDOW,
            encoding="utf-8",
            errors="replace",
        )
        return p.returncode, p.stdout or ""
    except FileNotFoundError:
        return 127, f"[找不到可执行文件] {cmd[0]}"
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return 124, out + f"\n[超时 {timeout}s]"
    except Exception as e:  # pragma: no cover
        return 1, f"[执行异常] {e}"


def run_stream(cmd, on_line, timeout=600, cwd=None, should_abort=None):
    """流式执行，逐行回调。返回 (returncode, 全量输出)"""
    buf = []
    try:
        p = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(cwd) if cwd else None,
            creationflags=CREATE_NO_WINDOW,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except FileNotFoundError:
        msg = f"[找不到可执行文件] {cmd[0]}"
        on_line(msg)
        return 127, msg
    except Exception as e:  # pragma: no cover
        msg = f"[启动失败] {e}"
        on_line(msg)
        return 1, msg

    start = time.time()
    try:
        for line in p.stdout:
            line = line.rstrip("\r\n")
            buf.append(line)
            on_line(line)
            if should_abort and should_abort():
                p.kill()
                on_line("[已中止]")
                break
            if time.time() - start > timeout:
                p.kill()
                on_line(f"[超时 {timeout}s，已终止]")
                break
    finally:
        try:
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    return p.returncode if p.returncode is not None else -1, "\n".join(buf)


def split_cmd(s: str):
    out, cur, q = [], "", False
    for ch in s:
        if ch == '"':
            q = not q
            continue
        if ch == " " and not q:
            if cur:
                out.append(cur)
                cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def quote(s: str) -> str:
    s = str(s)
    return f'"{s}"' if (" " in s or "\t" in s) else s


def fmt_size(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.2f} GB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


# ---------------------------------------------------------------------------
# 救砖文件类型
# ---------------------------------------------------------------------------
KINDS = [
    # key, 显示名, 匹配优先级模式（先精确后模糊）
    ("boot", "boot.img（内核）", ["boot.img", "boot_a.img", "boot_b.img"]),
    ("dtbo", "dtbo.img", ["dtbo.img", "dtbo_a.img", "dtbo_b.img"]),
    ("vbmeta", "vbmeta.img", ["vbmeta.img", "vbmeta_a.img", "vbmeta_b.img"]),
    ("vendor_boot", "vendor_boot.img", ["vendor_boot.img", "vendor_boot_a.img", "vendor_boot_b.img"]),
    ("recovery", "recovery.img", ["recovery.img"]),
    ("super", "super.img", ["super.img"]),
    ("fullzip", "全量包 (.zip/.tgz/.tar.md5)", ["*.zip", "*.tgz", "*.tar.md5", "*.tar"]),
    ("firehose", "EDL firehose 引导", ["prog_firehose*.elf", "prog_firehose*.mbn", "prog_firehose*"]),
]
KIND_NAME = {k: name for k, name, _ in KINDS}

# 自定义刷写时下拉给的分区名（可自己改写）
COMMON_PARTS = [
    "boot", "dtbo", "vbmeta", "vbmeta_system", "vendor_boot", "recovery", "dtb",
    "super", "system", "system_ext", "vendor", "product", "odm", "mi_ext",
    "modem", "modemst1", "modemst2", "fsg", "fsc", "persist", "misc", "cache",
    "userdata", "metadata", "frp", "spare", "logo", "bootloader", "xbl", "xbl_config",
    "abl", "tz", "hyp", "keymaster", "cmnlib", "cmnlib64", "dsp", "aop", "devcfg",
    "qupfw", "uefisecapp", "imagefv", "multiimgoem", "bluetooth", "devinfo", "oem",
]

USB_DRIVER_URL = "https://dl.google.com/android/repository/usb_driver_r13-windows.zip"


def elevated_install(inf):
    """以管理员身份 pnputil 安装驱动 inf，返回 (rc, 输出)"""
    inf = str(Path(inf))
    ps = ("Start-Process -Wait -Verb RunAs -FilePath 'pnputil.exe' "
          f"-ArgumentList @('/add-driver','{inf}','/install')")
    return run_capture(ps_cmd(ps), timeout=240)


def ps_cmd(body):
    """构造强制 UTF-8 输出的 PowerShell 调用，避免中文乱码"""
    return ["powershell", "-NoProfile", "-Command",
            "$OutputEncoding=[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + body]
SLOTTED = {"boot", "dtbo", "vbmeta", "vendor_boot", "recovery"}

# 可导入的镜像后缀（字库备份里常见这些）
IMG_EXT = {".img", ".bin", ".mbn", ".elf", ".lz4", ".gz", ".zip", ".tgz", ".tar", ".md5", ".tar.md5"}
# 常见别名 -> 分区名
PART_ALIAS = {
    "kernel": "boot",
    "kernel_a": "boot",
    "kernel_b": "boot",
    "dtb": "dtbo",
    "vbmeta.img": "vbmeta",
    "ramdisk": "boot",
    "params": "param",
    "splash": "logo",
}
SLOT_TAIL = re.compile(r"[_-](a|b)$")


def part_of_file(path) -> str:
    """从文件名推断分区名：boot_a.img -> boot；super.img -> super；system.img -> system"""
    name = Path(path).name.lower()
    for ext in (".tar.md5", ".tar", ".md5", ".lz4", ".gz", ".img", ".bin", ".mbn", ".elf",
                ".zip", ".tgz"):
        if name.endswith(ext):
            name = name[: -len(ext)]
            break
    name = SLOT_TAIL.sub("", name)
    return PART_ALIAS.get(name, name)


def kind_of_part(part: str):
    """分区名对应到救砖文件类型，未知返回 None"""
    return part if part in KIND_NAME else None


def scan_library(base: Path = None):
    """扫描救砖文件库，返回 {kind: [Path, ...]}（按优先级排序）"""
    base = base or lib_dir()
    result = {k: [] for k, _, _ in KINDS}
    if not base.is_dir():
        return result
    files = [p for p in base.rglob("*") if p.is_file() and not p.name.startswith(".")]
    for key, _, pats in KINDS:
        hits = []
        for pat in pats:
            for p in files:
                if p.name.lower() == pat.lower() and p not in hits:
                    hits.append(p)
        # 模糊补充
        fuzzy = []
        for p in files:
            low = p.name.lower()
            if key == "boot" and re.fullmatch(r"boot[_-]?[ab]?\.img", low):
                fuzzy.append(p)
            elif key == "dtbo" and low.startswith("dtbo") and low.endswith(".img"):
                fuzzy.append(p)
            elif key == "vbmeta" and low.startswith("vbmeta") and low.endswith(".img"):
                fuzzy.append(p)
            elif key == "vendor_boot" and low.startswith("vendor_boot") and low.endswith(".img"):
                fuzzy.append(p)
            elif key == "recovery" and low.startswith("recovery") and low.endswith(".img"):
                fuzzy.append(p)
            elif key == "super" and low.startswith("super") and low.endswith(".img"):
                fuzzy.append(p)
            elif key == "fullzip" and low.endswith((".zip", ".tgz", ".tar.md5", ".tar")):
                fuzzy.append(p)
            elif key == "firehose" and (low.endswith((".elf", ".mbn")) or "firehose" in low):
                fuzzy.append(p)
        for p in fuzzy:
            if p not in hits:
                hits.append(p)
        hits.sort(key=lambda p: (len(p.name), str(p)))
        result[key] = hits
    return result


# ---------------------------------------------------------------------------
# 症状
# ---------------------------------------------------------------------------
SYMPTOMS = [
    ("bootloop", "无限重启 / 自动重启循环", "亮一下 LOGO 又重启，进不去系统，反复循环"),
    ("stuck_logo", "卡第一屏 / 卡 LOGO 不动", "停在开机第一屏（品牌 LOGO）无进度、无反应"),
    ("stuck_fastboot", "只能进 fastboot / 开机直进 Bootloader", "按键能进 fastboot 界面，系统起不来"),
    ("stuck_recovery", "只能进 recovery / 一直进 recovery", "开机自动进 recovery，选重启还是回去"),
    ("black_9008", "黑屏无反应，电脑识别 9008/EDL", "完全黑屏不亮，设备管理器出现 QDLoader 9008"),
    ("crashdump", "crashdump / ramdump / 红字循环", "屏幕出现 crashdump、Kernel Panic、Qualcomm 报错"),
    ("verity_fail", "vbmeta / dm-verity 校验失败红字", "开机红字 dm-verity corruption / device is corrupt"),
    ("slot_dead", "A/B 分区切换后不开机", "OTA 或切 slot 之后黑屏 / 循环重启"),
    ("boot_to_recovery", "开机自进 recovery（没按键）", "没按任何键却自动进 recovery 界面"),
    ("fastboot_no_resp", "fastboot 能进但命令无响应", "fastboot devices 空、刷写一直 waiting for device"),
    ("kernel_panic", "内核刷完就崩（自编译/第三方内核）", "刷了自制内核后异常、花屏、循环重启"),
    ("battery", "怀疑电池 / 供电导致循环", "低电、插电才亮、充电中反复重启"),
    ("unknown", "说不清，先做只读诊断", "不刷机，先抓机型/slot/内核日志定位问题"),
]
SYMPTOM_NAME = {s[0]: s[1] for s in SYMPTOMS}


# ---------------------------------------------------------------------------
# 方案定义
#   steps: label / tool / cmd / need / opt / note
#     {file:boot}   -> 救砖文件路径
#     {part:boot}   -> boot_a / boot
#     {otherslot}   -> a / b
#     {backup}       -> 备份目录
#     {outdir}       -> 输出目录
#     {ts}           -> 时间戳
# ---------------------------------------------------------------------------
PLANS = [
    dict(
        pid="P09",
        title="◎ 先备份当前分区（任何救砖前都建议先跑）",
        symptoms=[],
        risk="无",
        needs=[],
        desc="用 fastboot fetch 把现在还能读到的 boot / dtbo / vbmeta / vendor_boot 抓回本地。"
             "很多人刷成砖之后才发现原厂 boot 没了，有备份就能随时回滚。",
        pre="fastboot 模式（fastboot 34 以上支持 fetch；本机为 37，可用）",
        steps=[
            dict(label="抓取 boot", tool="fastboot", cmd="fetch {part:boot} {backup}/boot_{ts}.img", opt=True),
            dict(label="抓取 dtbo", tool="fastboot", cmd="fetch {part:dtbo} {backup}/dtbo_{ts}.img", opt=True),
            dict(label="抓取 vbmeta", tool="fastboot", cmd="fetch {part:vbmeta} {backup}/vbmeta_{ts}.img", opt=True),
            dict(label="抓取 vendor_boot", tool="fastboot", cmd="fetch {part:vendor_boot} {backup}/vendor_boot_{ts}.img", opt=True),
            dict(label="说明", tool="note", cmd="若 fetch 报 unknown command，改用 adb(需 root)：adb shell su -c \"dd if=/dev/block/by-name/boot of=/sdcard/boot.img\" 再 adb pull /sdcard/boot.img"),
        ],
    ),
    dict(
        pid="P01",
        title="① 内核回滚（首选，改动最小，保留数据）",
        symptoms=["bootloop", "stuck_logo", "kernel_panic", "verity_fail", "stuck_fastboot"],
        risk="低",
        needs=["boot"],
        desc="只把 boot 分区刷回官方镜像。刷完能正常开机，就确认是内核/ramdisk 的问题，"
             "数据、应用全都不用动。这是救砖的第一手，90% 的内核砖靠它解决。",
        pre="设备能进 fastboot；BL 已解锁；boot.img 与本机型、本系统版本严格对应",
        steps=[
            dict(label="读取当前 slot", tool="fastboot", cmd="getvar current-slot", opt=True,
                 note="拿不到 slot 就走默认分区名，不影响"),
            dict(label="刷写 boot 内核", tool="fastboot", cmd="flash {part:boot} {file:boot}", need=["boot"],
                 note="核心步骤，失败就是 boot.img 与机型不匹配或 BL 未解锁"),
            dict(label="刷写 dtbo（有就刷）", tool="fastboot", cmd="flash {part:dtbo} {file:dtbo}",
                 need=["dtbo"], opt=True, note="dtbo 与内核配套，缺失可能花屏/触摸失效"),
            dict(label="重刷 vbmeta（有就刷）", tool="fastboot", cmd="flash {part:vbmeta} {file:vbmeta}",
                 need=["vbmeta"], opt=True),
            dict(label="重启", tool="fastboot", cmd="reboot"),
        ],
    ),
    dict(
        pid="P02",
        title="② 内核全家桶重刷（boot+dtbo+vendor_boot）",
        symptoms=["stuck_logo", "bootloop", "stuck_fastboot", "crashdump", "kernel_panic", "slot_dead"],
        risk="低",
        needs=["boot", "dtbo", "vendor_boot"],
        desc="四个分区分区成套刷回官方。只刷 boot 仍不开机，通常是 vendor_boot 里的 "
             "first-stage init / ramdisk 与内核不配套，或者 dtbo 版本对不上。",
        pre="fastboot 可操作；四个镜像来自同一个固件包",
        steps=[
            dict(label="读取当前 slot", tool="fastboot", cmd="getvar current-slot", opt=True),
            dict(label="刷写 boot", tool="fastboot", cmd="flash {part:boot} {file:boot}", need=["boot"]),
            dict(label="刷写 dtbo", tool="fastboot", cmd="flash {part:dtbo} {file:dtbo}", need=["dtbo"]),
            dict(label="刷写 vendor_boot", tool="fastboot", cmd="flash {part:vendor_boot} {file:vendor_boot}",
                 need=["vendor_boot"]),
            dict(label="关闭校验并刷 vbmeta", tool="fastboot",
                 cmd="--disable-verity --disable-verification flash {part:vbmeta} {file:vbmeta}",
                 need=["vbmeta"], opt=True,
                 note="自编译内核改了分区布局时必须关校验，否则开机红字"),
            dict(label="重启", tool="fastboot", cmd="reboot"),
        ],
    ),
    dict(
        pid="P03",
        title="③ vbmeta / dm-verity 校验失败修复",
        symptoms=["verity_fail", "boot_to_recovery", "bootloop"],
        risk="低",
        needs=["vbmeta"],
        desc="开机红字 dm-verity corruption 或 device is corrupt，是 vbmeta 校验拒绝了被改过的 boot。"
             "重刷原版 vbmeta 并同时关掉 verity/verification 即可。",
        pre="需要原机 vbmeta.img；部分机型 vbmeta 与 vbmeta_system 成对出现",
        steps=[
            dict(label="读取当前 slot", tool="fastboot", cmd="getvar current-slot", opt=True),
            dict(label="关校验刷 vbmeta", tool="fastboot",
                 cmd="--disable-verity --disable-verification flash {part:vbmeta} {file:vbmeta}",
                 need=["vbmeta"]),
            dict(label="补刷 boot（配合上一步）", tool="fastboot", cmd="flash {part:boot} {file:boot}",
                 need=["boot"], opt=True),
            dict(label="擦除 metadata（部分机型需要）", tool="fastboot", cmd="erase metadata", opt=True,
                 note="只擦元数据，不清用户数据；个别机型刷完仍红字时再用"),
            dict(label="重启", tool="fastboot", cmd="reboot"),
        ],
    ),
    dict(
        pid="P04",
        title="④ A/B 分区切换修复（换 slot 引导）",
        symptoms=["slot_dead", "bootloop", "stuck_fastboot"],
        risk="低",
        needs=[],
        desc="A/B 机型有两个 slot，OTA 失败或刷坏当前 slot 后，可以切到另一个 slot 先把手机救活，"
             "进去之后再补刷坏掉的那个 slot。",
        pre="fastboot 可操作；设备为 A/B 机型",
        steps=[
            dict(label="读取当前 slot", tool="fastboot", cmd="getvar current-slot", opt=True),
            dict(label="检查两个 slot 是否可用", tool="fastboot", cmd="getvar has-slot:boot", opt=True),
            dict(label="切换到另一个 slot", tool="fastboot", cmd="--set-active={otherslot}",
                 note="a/b 互换；切完先别刷，直接重启看能不能进系统"),
            dict(label="重启", tool="fastboot", cmd="reboot"),
            dict(label="说明", tool="note",
                 cmd="切到另一个 slot 能开机后，回到本工具跑【P01 内核回滚】把坏 slot 的 boot 刷好，再切回去"),
        ],
    ),
    dict(
        pid="P05",
        wipes=True,
        title="⑤ 清数据救砖（fastboot -w，会清空全部数据）",
        symptoms=["bootloop", "stuck_logo", "boot_to_recovery", "crashdump"],
        risk="高：清空 userdata / metadata，照片、微信、账号全部丢失",
        needs=[],
        desc="当 boot 刷对了还是循环重启，多半是 /data 或 metadata 加密状态坏了（换了内核但没做对应 "
             "的 data 迁移）。这一步会格式化数据分区，是最后的手段。",
        pre="已经确认 boot 正确；能接受数据丢失；手机电量 > 30%",
        steps=[
            dict(label="先刷回正确内核（有就刷）", tool="fastboot", cmd="flash {part:boot} {file:boot}",
                 need=["boot"], opt=True),
            dict(label="清空 userdata + metadata + cache", tool="fastboot", cmd="-w",
                 note="fastboot -w = erase userdata + erase metadata + erase cache"),
            dict(label="重启（首次开机会慢，等 5~10 分钟）", tool="fastboot", cmd="reboot"),
        ],
    ),
    dict(
        pid="P06",
        title="⑥ 只进 recovery → 侧载全量包（adb sideload）",
        symptoms=["stuck_recovery", "boot_to_recovery", "stuck_logo"],
        risk="中：取决于包内是否含 wipe 脚本",
        needs=["fullzip"],
        desc="recovery 还能用时，直接侧载官方全量 OTA 包，让系统自己把 boot/系统分区全部重写一遍，"
             "比手工刷单个分区更保险。",
        pre="手机停在官方 recovery；选 Apply update from ADB / 从 ADB 更新",
        steps=[
            dict(label="提示：手机上进入 sideload 模式", tool="note",
                 cmd="recovery 界面 → Apply update from ADB（部分机型叫『从 ADB 升级』），屏幕显示 now send the package"),
            dict(label="确认进入 sideload", tool="adb", cmd="devices", opt=True,
                 note="列表里设备状态应为 sideload"),
            dict(label="侧载全量包", tool="adb", cmd="sideload {file:fullzip}", need=["fullzip"],
                 note="1~5 GB 的包要传几分钟，进度到 100% 前别拔线"),
            dict(label="重启", tool="adb", cmd="reboot", opt=True),
        ],
    ),
    dict(
        pid="P07",
        title="⑦ 黑屏 9008 / EDL 深度救砖（生成操作指引）",
        symptoms=["black_9008"],
        risk="中：EDL 刷写范围大，写错分区表可能更严重",
        needs=[],
        desc="完全黑屏、只能被电脑识别成 9008 时，fastboot 已经不可用，必须走 EDL。"
             "本工具会检测 9008 端口，并把 QFIL / MiFlash / edl.py 三套走法整理成文件。",
        pre="设备管理器能看到 Qualcomm HS-USB QDLoader 9008；有该机型的线刷包（含 firehose + rawprogram）",
        steps=[
            dict(label="检测 9008 端口", tool="edl", cmd="scan"),
            dict(label="校验 firehose 引导文件", tool="note", cmd="firehose 一般在官方线刷包 images/ 目录：prog_firehose_ddr.elf 或 .mbn"),
            dict(label="生成 EDL 操作指引", tool="edl", cmd="guide"),
        ],
    ),
    dict(
        pid="P08",
        title="⑧ fastboot 无响应 / 识别不到设备",
        symptoms=["fastboot_no_resp", "stuck_fastboot"],
        risk="无",
        needs=[],
        desc="fastboot 界面能进，但电脑上 fastboot devices 一片空白，或者一直 waiting for device。"
             "先把驱动和 adb server 状态清干净再谈刷机。",
        pre="USB 线直连主板后置口，别用 hub / 延长线",
        steps=[
            dict(label="杀掉 adb/fastboot 服务", tool="adb", cmd="kill-server", opt=True),
            dict(label="重启 adb 服务", tool="adb", cmd="start-server", opt=True),
            dict(label="列出 fastboot 设备", tool="fastboot", cmd="devices"),
            dict(label="确认通信正常", tool="fastboot", cmd="getvar product", opt=True,
                 note="能返回 product 名称说明通道正常"),
            dict(label="强制重启引导器", tool="fastboot", cmd="reboot-bootloader", opt=True),
            dict(label="说明", tool="note",
                 cmd="仍失败：①设备管理器里把 Android Bootloader Interface 驱动更新为 Google USB Driver；"
                     "②换 USB2.0 口；③长按电源 15 秒硬重启后再进 fastboot"),
        ],
    ),
    dict(
        pid="P10",
        wipes=True,
        title="⑨ 全量线刷包刷写（最彻底，清数据）",
        symptoms=["stuck_logo", "bootloop", "crashdump", "slot_dead", "black_9008"],
        risk="高：全盘重写，数据全清",
        needs=["fullzip"],
        desc="单个分区怎么刷都不行，说明问题不只在 boot。用官方线刷包整包重写一遍，"
             "回到出厂状态。本工具生成一个 .bat，在解压后的线刷目录里执行。",
        pre="已下载对应机型/版本的官方线刷包（fastboot 版 tgz 或 flash-all 版 zip）",
        steps=[
            dict(label="检查包类型", tool="note", cmd="tgz/tar.md5 = fastboot 线刷包；zip = recovery 卡刷包（那种请走 P06）"),
            dict(label="生成线刷脚本", tool="pkgzip", cmd="export"),
            dict(label="说明", tool="note",
                 cmd="把生成的 flash_rom.bat 放进解压后的线刷目录，双击执行；脚本会先校验关键 img 再调用 flash-all"),
        ],
    ),
    dict(
        pid="P11",
        title="⑩ 刷回官方 recovery（治『一直进 recovery』）",
        symptoms=["boot_to_recovery", "stuck_recovery"],
        risk="低",
        needs=["recovery"],
        desc="第三方 recovery 刷坏、或者 recovery 分区被写成半截，会导致开机检测失败后直接掉进 recovery。"
             "刷回官方 recovery 即可。",
        pre="fastboot 可操作；recovery.img 为官方原版",
        steps=[
            dict(label="读取当前 slot", tool="fastboot", cmd="getvar current-slot", opt=True),
            dict(label="刷写 recovery", tool="fastboot", cmd="flash {part:recovery} {file:recovery}", need=["recovery"]),
            dict(label="重启", tool="fastboot", cmd="reboot"),
        ],
    ),
    dict(
        pid="P12",
        title="⑪ 只读诊断（不刷机，先定位问题）",
        symptoms=["unknown", "battery", "bootloop"],
        risk="无（只读）",
        needs=[],
        desc="系统还能起来（哪怕是半残），先把机型、版本、slot、内核日志、上次崩溃日志抓下来，"
             "再决定刷什么。避免瞎刷把问题扩大。",
        pre="设备能被 adb 识别（状态 device）",
        steps=[
            dict(label="机型与系统版本", tool="adb", cmd="shell getprop ro.product.device"),
            dict(label="系统指纹", tool="adb", cmd="shell getprop ro.build.fingerprint", opt=True),
            dict(label="当前 slot", tool="adb", cmd="shell getprop ro.boot.slot_suffix", opt=True),
            dict(label="内核版本", tool="adb", cmd="shell cat /proc/version", opt=True),
            dict(label="上次崩溃日志", tool="adb",
                 cmd="shell \"cat /sys/fs/pstore/console-ramoops-0 2>/dev/null | tail -n 120\"", opt=True,
                 note="这就是上次重启的原因，重点看 'Kernel panic' / 'Unable to mount'"),
            dict(label="当前内核日志尾部", tool="adb", cmd="shell \"dmesg | tail -n 120\"", opt=True,
                 note="部分系统需要 root"),
            dict(label="电池与充电状态", tool="adb", cmd="shell dumpsys battery", opt=True),
            dict(label="vbmeta / 校验状态", tool="adb",
                 cmd="shell \"getprop | grep -iE 'verity|verified|slot'\"", opt=True),
        ],
    ),
    dict(
        pid="P13",
        title="⑫ 批量回刷（目录 / 字库备份）",
        symptoms=["stuck_logo", "bootloop", "slot_dead", "crashdump"],
        risk="中：按导入清单逐个分区重写",
        needs=[],
        dynamic="batch",
        desc="把整个备份目录（字库备份、全分区 dump）里的镜像逐个刷回对应分区。"
             "分区名取文件名，A/B 设备自动加 _a/_b，动态分区自动切 FastbootD。"
             "先在 ③ 救砖文件 里点『＋文件夹』把目录导进来。",
        pre="目录里是 boot.img / system.img / vendor.img 这类按分区命名的镜像；设备在 fastboot",
        steps=[],
    ),
    dict(
        pid="P14",
        wipes=True,
        title="⑬ 全量包直刷（fastboot update）",
        symptoms=["stuck_logo", "bootloop", "crashdump", "slot_dead", "black_9008"],
        risk="高：整包重写，通常清数据",
        needs=["fullzip"],
        desc="包里带 android-info.txt 的官方 fastboot 包，直接交给 fastboot update，"
             "由 fastboot 自己按包内清单刷全部分区，比手写命令更稳。",
        pre="zip 内含 android-info.txt / bootloader.img 等；设备 BL 已解锁并在 fastboot",
        steps=[
            dict(label="检查包", tool="note",
                 cmd="把包放进取救砖文件库；zip 里应有 android-info.txt（没有就不是 fastboot 包）"),
            dict(label="整包刷写", tool="fastboot", cmd="update {file:fullzip}", need=["fullzip"],
                 note="会按包内脚本刷 bootloader→分区，过程 5~15 分钟，中途别拔线"),
            dict(label="重启", tool="fastboot", cmd="reboot", opt=True),
        ],
    ),
    dict(
        pid="P15",
        title="⑭ 按官方脚本刷写（flash_all）",
        symptoms=["stuck_logo", "bootloop", "black_9008", "slot_dead"],
        risk="高：脚本内容照原包执行",
        needs=[],
        dynamic="script",
        desc="小米/OPPO 等官方 fastboot 包里有 flash_all.bat，本工具解析出其中的 fastboot 命令行，"
             "在包目录下逐条执行（等价于双击官方的 flash_all）。",
        pre="用『＋文件夹』导入官方 fastboot 包目录（里面有 flash_all.bat 或 flash_all.sh）",
        steps=[],
    ),
    dict(
        pid="P16",
        title="⑮ 自定义刷写（自选 img + 自选分区）",
        symptoms=[],
        risk="按你填的分区名执行：填错分区＝真砖，填对就是最精准的修法",
        needs=[],
        dynamic="custom",
        desc="自己挑一个镜像、自己写分区名（或从常用分区里选），工具负责：查 A/B 槽位、查是不是动态分区、"
             "该切 FastbootD 就切、拼好命令、预检、执行、记日志。刷第三方内核 / 换 recovery / 只修某个分区都用这个。",
        pre="在 ③ 救砖文件 点『＋ 自定义刷写』：选文件 → 填分区名 → 选槽位 → 确定",
        steps=[],
    ),
    dict(
        pid="P17",
        title="⑯ 全字库备份（整机所有分区一次备完）",
        symptoms=["bootloop", "stuck_logo", "crashdump"],
        risk="无（全程只读，不动设备）",
        needs=[],
        dynamic="fullbackup",
        desc="读出设备上的全部分区清单，逐个备份到 backups\\full_时间戳\\，并生成 restore_all.bat 还原脚本。"
             "fastboot 模式用 fastboot fetch；adb 且已 root 时用 dd。备过一次，以后任何砖都能整体刷回去。",
        pre="设备在 fastboot（最快最稳）；或 adb 已 root；磁盘至少留出与机身存储等量的空间",
        steps=[],
    ),
    dict(
        pid="P18",
        title="⑰ 一键识别问题（自动诊断 + 推荐方案）",
        symptoms=["unknown"],
        risk="无（只读，不改设备）",
        needs=[],
        dynamic="diagnose",
        desc="自动抓设备状态、bootloader 变量、分区表、系统属性、内核与 pstore 日志，按规则判断是内核问题 / "
             "vbmeta 校验 / 槽位错乱 / 数据损坏 / 驱动没装 / 9008，直接告诉你下一步该点哪个方案。",
        pre="设备随便在哪个模式；能进 fastboot 或 adb 时结论最准",
        steps=[],
    ),
]
PLAN_BY_ID = {p["pid"]: p for p in PLANS}


# ---------------------------------------------------------------------------
# 渲染与编译
# ---------------------------------------------------------------------------
def part_name(kind: str, ctx) -> str:
    if ctx.get("use_slot") and kind in SLOTTED:
        return kind + ctx.get("slot_suffix", "")
    return kind


def render(tpl: str, ctx) -> str:
    def rep(m):
        key = m.group(1)
        if key.startswith("part:"):
            return part_name(key[5:], ctx)
        if key.startswith("file:"):
            p = ctx["files"].get(key[5:])
            return quote(str(p)) if p else ""
        return str(ctx.get(key, m.group(0)))

    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_:]*)\}", rep, tpl)


def make_ctx(dev, files, ts=None, entries=None, script=None):
    slot = (dev.get("slot") or "").strip()
    slot_suffix = f"_{slot}" if slot in ("a", "b") else ""
    return {
        "ts": ts or datetime.now().strftime("%Y%m%d_%H%M%S"),
        "backup": str(BACKUP_DIR),
        "outdir": str(OUT_DIR),
        "slotname": slot,
        "otherslot": "b" if slot == "a" else "a",
        "slot_suffix": slot_suffix,
        "use_slot": bool(slot_suffix),
        "files": dict(files),
        "entries": list(entries or []),
        "script": script,
    }


def _info_step(label, cmd):
    return dict(label=label, tool="note", cmd=cmd, cmdline="", note="", opt=False,
                state="info", reason="", need=[], tpl="", part="")


def _compile_batch(ctx, keep=False):
    entries = ctx.get("entries") or []
    if not entries:
        return [_info_step("没有导入镜像",
                           "先在 ③ 救砖文件 点『＋文件夹』选备份目录，或『＋文件夹』选整份字库备份")]
    out = [_info_step("批量清单",
                      f"共 {len(entries)} 个镜像，将逐个刷写；分区名取自文件名，"
                      f"A/B 槽位与动态分区按设备实测（getvar has-slot / is-logical）自动处理")]
    suffix = ctx.get("slot_suffix") or ""
    for e in entries:
        part = e["part"]
        path = e["path"]
        show = part + (suffix if part in SLOTTED else "")
        cmd = f"flash {show} {quote(str(path))}"
        out.append(dict(
            label=f"刷 {part}", tool="fastboot", cmd=cmd,
            cmdline=f"{quote(tool('fastboot'))} {cmd}",
            note="", opt=False, state="ready", reason="", need=[], tpl="", part=part,
            batch_part=part, batch_path=str(path),
        ))
    out.append(dict(label="重启", tool="fastboot", cmd="reboot",
                    cmdline=f"{quote(tool('fastboot'))} reboot", note="", opt=True,
                    state="ready", reason="", need=[], tpl="reboot", part=""))
    return out


def _compile_script(ctx, keep=False):
    script = ctx.get("script")
    if not script:
        return [_info_step("没有导入官方脚本",
                           "在 ③ 救砖文件 点『＋文件夹』选官方 fastboot 包目录（内含 flash_all.bat / flash_all.sh）")]
    cmds = parse_flash_all(script)
    if not cmds:
        return [_info_step("脚本里没有 fastboot 命令", f"已读取 {script}，没解析出可执行行")]
    cwd = str(Path(script).parent)
    out = [_info_step("脚本来源", f"{script}（共解析出 {len(cmds)} 条 fastboot 命令）")]
    for i, c in enumerate(cmds, 1):
        shown = re.sub(r"^fastboot", lambda m: os.path.basename(tool("fastboot")), c, flags=re.I)
        run = re.sub(r"^fastboot", lambda m: quote(tool("fastboot")), c, flags=re.I)
        out.append(dict(label=f"脚本第 {i} 条", tool="fastboot", cmd=shown, cmdline=run,
                        note="", opt=False, state="ready", reason="", need=[],
                        tpl="", part="", cwd=cwd))
    return out


def _compile_custom(ctx, keep=False):
    """自定义刷写：用户自己指定 文件 + 分区名 + 槽位"""
    entries = [e for e in (ctx.get("entries") or []) if e.get("custom")]
    if not entries:
        return [_info_step("还没有自定义条目",
                           "在 ③ 救砖文件 点『＋ 自定义刷写』：选一个镜像 → 填分区名 → 选槽位")]
    out = [_info_step("自定义清单",
                      f"共 {len(entries)} 条；分区名以你填的为准，"
                      f"A/B 槽位与动态分区按设备实测（getvar has-slot / is-logical）自动处理")]
    for e in entries:
        part = e["part"]
        slot = e.get("slot", "auto")
        extra = (e.get("extra") or "").strip()
        shown = part
        if slot in ("a", "b"):
            shown = f"{part}_{slot}"
        elif slot == "auto" and ctx.get("use_slot") and part in SLOTTED:
            shown = part + ctx.get("slot_suffix", "")
        cmd = f"{extra + ' ' if extra else ''}flash {shown} {quote(str(e['path']))}"
        out.append(dict(
            label=f"刷 {part}" + (f" [{slot}]" if slot in ("a", "b") else ""),
            tool="fastboot", cmd=cmd,
            cmdline=f"{quote(tool('fastboot'))} {cmd}",
            note="", opt=False, state="ready", reason="", need=[], tpl="", part=part,
            custom_part=part, custom_slot=slot, custom_path=str(e["path"]), custom_extra=extra,
        ))
    out.append(dict(label="重启", tool="fastboot", cmd="reboot",
                    cmdline=f"{quote(tool('fastboot'))} reboot", note="", opt=True,
                    state="ready", reason="", need=[], tpl="reboot", part=""))
    return out


def plan_wipes(plan, steps=None):
    """方案是否会清空用户数据：返回原因，空串表示安全"""
    if plan.get("wipes"):
        return f"{plan['pid']} 本身就会清空用户数据"
    for s in (steps or []):
        why = wipe_reason(s.get("cmdline", ""), s.get("cmd", ""))
        if why:
            return why
    return ""


def apply_keep_data(steps, keep):
    """保留资料模式：把所有会清数据的可执行步骤标成 blocked（永不执行）"""
    if not keep:
        return steps
    for s in steps:
        if s.get("state") in ("info", "missing"):
            continue
        why = wipe_reason(s.get("cmdline", ""), s.get("cmd", ""))
        if why:
            s["state"] = "blocked"
            s["reason"] = "保留资料：" + why
    return steps


def compile_plan(plan, ctx, keep=False):
    """把方案模板编译成可执行步骤列表"""
    if plan.get("dynamic") == "batch":
        return apply_keep_data(_compile_batch(ctx, keep), keep)
    if plan.get("dynamic") == "script":
        return apply_keep_data(_compile_script(ctx, keep), keep)
    if plan.get("dynamic") == "custom":
        return apply_keep_data(_compile_custom(ctx, keep), keep)
    if plan.get("dynamic") in ("fullbackup", "diagnose"):
        return [_info_step("前置条件", plan.get("pre", "")),
                _info_step("说明", "本方案由工具在运行时现场读取设备，步骤表不预先生成，"
                                   "点『开始救砖』后按实际结果执行并输出报告")]

    out = []
    for st in plan["steps"]:
        need = st.get("need") or []
        missing = [k for k in need if not ctx["files"].get(k)]
        rendered = render(st["cmd"], ctx)
        state = "ready"
        reason = ""
        if st["tool"] == "note":
            state = "info"
        elif missing:
            if st.get("opt"):
                state = "skip"
                reason = "缺 " + "、".join(KIND_NAME.get(k, k) for k in missing)
            else:
                state = "missing"
                reason = "缺 " + "、".join(KIND_NAME.get(k, k) for k in missing)
        if st["tool"] in ("adb", "fastboot") and not rendered.strip():
            state, reason = "skip", "模板为空"
        cmdline = ""
        if st["tool"] in ("adb", "fastboot"):
            cmdline = f"{quote(tool(st['tool']))} {rendered}"
        m = re.search(r"\{part:([A-Za-z_][A-Za-z0-9_]*)\}", st["cmd"])
        out.append(dict(
            label=st["label"], tool=st["tool"], cmd=rendered, cmdline=cmdline,
            note=st.get("note", ""), opt=bool(st.get("opt")), state=state, reason=reason,
            need=need, tpl=st["cmd"], part=(m.group(1) if m else ""),
            cwd=st.get("cwd", ""),
        ))
    return apply_keep_data(out, keep)


def match_plans(selected_symptoms):
    if not selected_symptoms:
        return list(PLANS)
    scored = []
    for p in PLANS:
        if not p["symptoms"]:
            score = 0  # 通用方案永远垫底出现
        else:
            score = len(set(p["symptoms"]) & set(selected_symptoms))
        if score > 0 or p["pid"] in ("P09", "P12", "P16", "P17", "P18"):
            scored.append((score, p))
    scored.sort(key=lambda x: (-x[0], x[1]["pid"]))
    return [p for _, p in scored]


def bat_escape(text: str) -> str:
    """cmd.exe 中 echo 用的转义，避免 | & < > ^ % 被解释"""
    out = []
    for ch in str(text):
        if ch in "|&<>^":
            out.append("^" + ch)
        elif ch == "%":
            out.append("%%")
        else:
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# 设备检测
# ---------------------------------------------------------------------------
def adb_shell(arg, timeout=10):
    rc, out = run_capture([tool("adb"), "shell", arg], timeout=timeout)
    return out.strip()


def fastboot_getvar(key, timeout=10):
    rc, out = run_capture([tool("fastboot"), "getvar", key], timeout=timeout, cwd=str(OUT_DIR))
    for line in (out or "").splitlines():
        s = line.strip()
        for pre in ("(bootloader) ", "(bootloader)", "(fastbootd) ", "(fastbootd)"):
            if s.startswith(pre):
                s = s[len(pre):].strip()
        if s.lower().startswith(key.lower() + ":"):
            val = s.split(":", 1)[1].strip()
            if val.lower() in ("variable not found", "unknown variable"):
                return ""
            return val
    return ""


_PROBE = {}


def clear_probe():
    _PROBE.clear()


def probe_partition(part, timeout=8):
    """探测分区的 has-slot / is-logical（参考烟雨的 getvar is-logical 做法）"""
    part = (part or "").strip()
    if not part:
        return {}
    if part in _PROBE:
        return _PROBE[part]
    info = {}
    if TOOLS.get("fastboot"):
        v = fastboot_getvar(f"has-slot:{part}", timeout)
        if v:
            info["has_slot"] = v.strip().lower()
        v = fastboot_getvar(f"is-logical:{part}", timeout)
        if v:
            info["is_logical"] = v.strip().lower()
    _PROBE[part] = info
    return info


def in_fastbootd(timeout=6):
    return fastboot_getvar("is-userspace", timeout).strip().lower() in ("yes", "true", "1")


def parse_flash_all(path):
    """解析小米/高通官方线刷脚本，抽出可直接执行的 fastboot 命令行"""
    p = Path(path)
    if not p.is_file():
        return []
    text = ""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            text = p.read_text(encoding=enc, errors="ignore")
            break
        except Exception:
            continue
    if not text:
        return []
    ok_sub = ("flash", "erase", "format", "reboot", "oem", "update", "flashing",
              "set_active", "boot", "continue", "devices", "getvar", "-w")
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("::", "#", "REM", "rem", "echo", "@echo")):
            continue
        # 管道 / 重定向 / 批处理逻辑都不是能直接跑的 fastboot 命令
        if any(t in line for t in ("|", ">", "<", "findstr", "find ", "goto ", "call ", "set /")):
            continue
        low = line.lower()
        idx = low.find("fastboot")
        if idx < 0:
            continue
        cmd = line[idx:].replace("%*", "").replace("%~dp0", "").replace("%~dp0\\", "")
        cmd = re.sub(r"\s+", " ", cmd).strip()
        tail = cmd[len("fastboot"):].strip()
        if not tail:
            continue
        first = tail.split()[0].lower()
        if first not in ok_sub:
            continue
        if tail.lower().startswith("getvar"):
            continue
        out.append(cmd)
    return out


# -- 分区枚举（全字库备份 / 自定义刷写共用）--------------------------------
def parse_partitions(text):
    """解析 `fastboot getvar all` → [{name,size,has_slot,logical}]，把 boot_a/boot_b 归并成 boot"""
    sizes, has_slot, logical = {}, {}, {}
    for raw in (text or "").splitlines():
        s = raw.strip()
        for pre in ("(bootloader) ", "(bootloader)", "(fastbootd) ", "(fastbootd)"):
            if s.startswith(pre):
                s = s[len(pre):].strip()
        for tag, bucket in (("partition-size:", sizes), ("has-slot:", has_slot),
                            ("is-logical:", logical)):
            if s.startswith(tag):
                rest = s[len(tag):].strip()
                if ":" in rest:
                    name, val = rest.rsplit(":", 1)
                    name, val = name.strip(), val.strip()
                    if tag == "partition-size:":
                        try:
                            bucket[name] = int(val, 16) if val.lower().startswith("0x") else int(val)
                        except Exception:
                            bucket[name] = 0
                    else:
                        bucket[name] = val.lower()
                break
    bases = {}
    for name in sizes:
        base = name
        if len(name) > 2 and name[-2] == "_" and name[-1] in ("a", "b"):
            cand = name[:-2]
            if (has_slot.get(cand) in ("yes", "true")
                    or (cand + "_a" in sizes and cand + "_b" in sizes)):
                base = cand
        bases[base] = bases.get(base, 0) + sizes.get(name, 0)
    for name in logical:          # 逻辑分区可能没有 partition-size
        if name in sizes:
            continue
        base = name[:-2] if (len(name) > 2 and name[-2] == "_" and name[-1] in ("a", "b")) else name
        bases.setdefault(base, 0)
    out = []
    for base in sorted(bases):
        slotted = has_slot.get(base, "no") in ("yes", "true")
        out.append(dict(name=base, size=bases[base], has_slot=slotted,
                        logical=logical.get(base, "no") in ("yes", "true")))
    return out


def list_partitions_fastboot(timeout=40):
    rc, out = run_capture([tool("fastboot"), "getvar", "all"], timeout=timeout, cwd=str(OUT_DIR))
    return parse_partitions(out), out


def list_partitions_adb(timeout=20):
    if not TOOLS.get("adb"):
        return []
    rc, out = run_capture([tool("adb"), "shell", "ls", "/dev/block/by-name/"], timeout=timeout)
    names = []
    for line in (out or "").splitlines():
        n = line.strip()
        if n and " " not in n and not n.lower().startswith("ls:"):
            names.append(n)
    return names


# -- 自动诊断（识别问题）--------------------------------------------------
def _kernel_tail(lines=60):
    if not TOOLS.get("adb"):
        return ""
    rc, out = run_capture([tool("adb"), "shell",
                           f"dmesg | tail -n {lines}"], timeout=20)
    return out or ""


def _pstore_text():
    if not TOOLS.get("adb"):
        return ""
    rc, out = run_capture([tool("adb"), "shell",
                           "cat /sys/fs/pstore/console-ramoops-0 2>/dev/null "
                           "|| cat /sys/fs/pstore/console-ramoops 2>/dev/null "
                           "|| cat /proc/last_kmsg 2>/dev/null"], timeout=20)
    return out or ""


def collect_probe():
    """把能抓的都抓一遍，返回 dict（诊断与报告共用）"""
    dev = detect_device()
    p = {"dev": dev, "getvar": {}, "prop": {}, "parts": [], "parts_raw": "",
         "dmesg": "", "pstore": "", "edl": dev.get("edl") or "", "notes": []}
    st = dev.get("state")
    if st == "fastboot":
        for k in ("product", "variant", "serialno", "version-bootloader",
                  "current-slot", "slot-count", "unlocked", "secure",
                  "is-userspace", "battery-voltage", "max-download-size"):
            p["getvar"][k] = fastboot_getvar(k, 8)
        p["parts"], p["parts_raw"] = list_partitions_fastboot()
    elif st == "adb":
        for k in ("ro.product.model", "ro.product.device", "ro.build.version.release",
                  "ro.build.fingerprint", "ro.boot.slot_suffix", "ro.boot.verifiedbootstate",
                  "ro.boot.veritymode", "ro.boot.flash.locked", "ro.crypto.state",
                  "ro.boot.bootreason", "sys.boot_completed", "ro.boot.hardware"):
            p["prop"][k] = adb_shell(f"getprop {k}", 6)
        p["dmesg"] = _kernel_tail()
        p["pstore"] = _pstore_text()
        p["parts"] = [dict(name=n, size=0, has_slot=False, logical=False)
                      for n in list_partitions_adb()]
    return p


def diag_run(p, emit=None):
    """规则引擎：返回 [{level, text, pid}]，pid 是推荐的方案"""
    out = []

    def add(level, text, pid=""):
        out.append(dict(level=level, text=text, pid=pid))
        if emit:
            emit(f"  {text}", {"ok": "ok", "warn": "warn", "err": "err"}.get(level, "info"))

    dev = p["dev"]
    st = dev.get("state")
    if p["edl"]:
        add("err", f"检测到 EDL/9008 端口：{p['edl']} —— 已经是深度砖，fastboot 救不了", "P07")
        return out
    if st == "none":
        add("err", "没检测到任何设备（adb / fastboot 都没有）")
        add("info", "先确认：数据线是数据线不是充电线、手机在 fastboot 界面或已开机、USB 口换到主板后置口", "")
        add("info", "如果设备管理器里有黄色感叹号，去顶栏『🔧 驱动』装驱动", "")
        return out
    if st == "unauthorized":
        add("warn", "adb 显示 unauthorized：手机上点『允许 USB 调试』后重试", "P12")
        return out
    if st == "sideload":
        add("warn", "设备在 sideload 模式：可以直接推送全量包", "P06")
        return out

    if st == "fastboot":
        g = p["getvar"]
        add("info", f"fastboot 已连通：{g.get('product') or dev.get('model') or '未知机型'}"
                    f"  序列号 {dev.get('serial') or g.get('serialno') or '?'}")
        unlocked = (g.get("unlocked") or "").lower()
        if unlocked in ("no", "false", "0"):
            add("err", "Bootloader 处于锁定状态，任何 flash 都会失败 —— 先在开发者选项里 OEM 解锁，"
                       "再 fastboot flashing unlock", "")
        elif unlocked:
            add("ok", f"Bootloader: {g.get('unlocked')}")
        if not p["parts"]:
            add("warn", "读不到分区表：多为 fastboot 驱动异常或线材问题（不是系统问题）", "P08")
        else:
            add("ok", f"分区表读取正常，共 {len(p['parts'])} 个分区")
            if (g.get("is-userspace") or "").lower() not in ("yes", "true", "1"):
                logical = [x["name"] for x in p["parts"] if x["logical"]]
                if logical:
                    add("info", f"存在动态分区（{', '.join(logical[:4])}…）：刷这些分区时工具会自动切 FastbootD", "")
        cur = g.get("current-slot") or ""
        if not cur and (g.get("slot-count") or "") in ("2",):
            add("warn", "A/B 设备但 current-slot 读不出来：槽位状态可能已损坏", "P04")
        elif cur:
            add("info", f"当前槽位：{cur}（另一槽 {( 'b' if cur == 'a' else 'a' )}）")
        add("info", "fastboot 下看不到内核日志；要判断是不是内核问题，刷 P01 单刷 boot 最快验证", "P01")

    if st == "adb":
        pr = p["prop"]
        vbs = (pr.get("ro.boot.verifiedbootstate") or "").lower()
        vm = (pr.get("ro.boot.veritymode") or "").lower()
        add("ok", f"系统能起来：{pr.get('ro.product.model') or '?'} "
                  f"Android {pr.get('ro.build.version.release') or '?'}")
        if vbs in ("orange", "yellow", "red"):
            add("warn", f"verifiedbootstate={vbs}：boot/vbmeta 与官方校验链不匹配"
                        "（刷过第三方内核或 magisk 常见，能开机就不用管）", "P03")
        if vm and vm != "enforcing":
            add("info", f"veritymode={vm}", "")
        if (pr.get("sys.boot_completed") or "") != "1":
            add("warn", "sys.boot_completed≠1：系统没真正起来，多半卡在 data 解密或 system_server 崩溃",
                "P05")
        br = (pr.get("ro.boot.bootreason") or "").lower()
        if br:
            add("info", f"bootreason={br}")
            if "kernel" in br or "panic" in br:
                add("err", "上次是内核 panic 重启 —— 基本可以确定是 boot.img / vendor_boot 不匹配", "P01")
            elif "watchdog" in br:
                add("warn", "watchdog 重启：可能是内核卡死或驱动 hang", "P01")
            elif "userrequested" in br or "reboot" in br:
                add("info", "上次是你自己重启的，不是崩溃", "")
        d = (p["dmesg"] or "").lower()
        for kw, lvl, msg, pid in (
            ("kernel panic", "err", "内核日志里有 Kernel panic", "P01"),
            ("unable to mount root", "err", "内核找不到可挂载的 rootfs（ramdisk 与内核不配套）", "P02"),
            ("dm-verity", "warn", "内核日志出现 dm-verity 相关报错", "P03"),
            ("failed to mount /data", "err", "/data 挂载失败（加密状态与内核不匹配）", "P05"),
            ("avc: denied", "info", "有 SELinux 拒绝（一般是后果不是原因）", ""),
        ):
            if kw in d:
                add(lvl, msg, pid)
        ps = (p["pstore"] or "").lower()
        if ps:
            if "panic" in ps:
                add("err", "pstore 里留有 panic 记录（上次死机的现场）", "P01")
            elif "watchdog" in ps:
                add("warn", "pstore 里有 watchdog 记录", "P01")
            else:
                add("info", "pstore 有内容但没匹配到已知关键字，可到日志目录翻原始内容", "")
        else:
            add("info", "pstore 为空：说明上次不是内核崩溃重启，更像 system 侧或 bootloader 侧问题", "")

    if not out:
        add("info", "没匹配到明确问题，建议先跑 P12 只读诊断并保存完整日志", "P12")
    return out


def detect_edl():
    """检测 9008 / QDLoader 端口（Windows）"""
    if os.name != "nt":
        return ""
    ps = (
        "Get-CimInstance Win32_PnPEntity | Where-Object { $_.DeviceID -match 'VID_05C6|PID_9008|QDLoader|QUSB_BULK' } "
        "| Select-Object -First 3 -ExpandProperty Name"
    )
    rc, out = run_capture(ps_cmd(ps), timeout=20)
    names = [l.strip() for l in (out or "").splitlines() if l.strip()]
    return " | ".join(names)


def detect_device():
    info = {"state": "none", "serial": "", "model": "", "slot": "",
            "unlocked": "", "edl": "", "android": "", "raw": ""}

    if TOOLS.get("adb"):
        rc, out = run_capture([tool("adb"), "devices"], timeout=10)
        for line in (out or "").splitlines():
            line = line.strip()
            if not line or line.lower().startswith("list of devices"):
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1] in ("device", "recovery", "sideload", "unauthorized", "offline"):
                info["serial"] = parts[0]
                info["raw"] = line
                if parts[1] == "device":
                    info["state"] = "adb"
                    info["model"] = adb_shell("getprop ro.product.model")
                    info["android"] = adb_shell("getprop ro.build.version.release")
                    info["slot"] = adb_shell("getprop ro.boot.slot_suffix").lstrip("_")
                    return info
                if parts[1] == "sideload":
                    info["state"] = "sideload"
                    return info
                if parts[1] == "unauthorized":
                    info["state"] = "unauthorized"
                    return info

    if TOOLS.get("fastboot"):
        rc, out = run_capture([tool("fastboot"), "devices"], timeout=12, cwd=str(OUT_DIR))
        lines = [l.strip() for l in (out or "").splitlines() if l.strip() and "fastboot" in l.lower()]
        if lines:
            info["state"] = "fastboot"
            info["serial"] = lines[0].split()[0]
            info["raw"] = lines[0]
            info["slot"] = fastboot_getvar("current-slot")
            info["unlocked"] = fastboot_getvar("unlocked")
            if not info["model"]:
                info["model"] = fastboot_getvar("product")
            return info

    edl = detect_edl()
    if edl:
        info["state"] = "edl"
        info["edl"] = edl
        return info
    return info


STATE_LABEL = {
    "adb": "已连接系统 (adb)",
    "fastboot": "Fastboot / Bootloader",
    "sideload": "Recovery 侧载 (sideload)",
    "unauthorized": "未授权（手机需点允许 USB 调试）",
    "edl": "EDL / 9008 深度刷机模式",
    "none": "未检测到设备",
}


# ---------------------------------------------------------------------------
# 执行引擎
# ---------------------------------------------------------------------------
class Runner:
    def __init__(self, on_log, on_done=None, keep=None, on_plan_hint=None):
        self.on_log = on_log
        self.on_done = on_done
        self.on_plan_hint = on_plan_hint
        self.keep = keep_data() if keep is None else bool(keep)
        self.abort_flag = False
        self.thread = None
        self.ok = True

    def abort(self):
        self.abort_flag = True

    def should_abort(self):
        return self.abort_flag

    def log(self, msg, level="info"):
        self.on_log(msg, level)

    # -- 预检 -------------------------------------------------------------
    def preflight(self, plan, steps, dev):
        warn = []
        block = []
        keep = self.keep
        if plan["pid"] not in ("P07", "P12", "P18"):
            if dev["state"] not in ("fastboot", "adb", "sideload"):
                block.append(f"当前设备状态为『{STATE_LABEL.get(dev['state'], dev['state'])}』，"
                             f"本方案需要 fastboot 或 adb 可用")
        if dev["state"] == "fastboot" and dev.get("unlocked"):
            u = dev["unlocked"].lower()
            if u in ("no", "false", "0"):
                block.append("BL 未解锁（unlocked: no），fastboot 会拒绝刷写")
            elif u not in ("yes", "true", "1"):
                warn.append(f"解锁状态读到『{dev['unlocked']}』，确认一下是否已解锁 BL")
        miss = [s["label"] for s in steps if s["state"] == "missing"]
        if miss:
            block.append("缺少必需救砖文件：" + "；".join(miss))
        if keep:
            why = plan_wipes(plan, steps)
            if why and not [s for s in steps if s["state"] in ("ready", "skip")]:
                block.append(f"【保留资料模式】{why}，该方案在本模式下不可执行")
            blocked = [s for s in steps if s["state"] == "blocked"]
            if blocked:
                warn.append("【保留资料模式】已自动屏蔽 " + str(len(blocked)) +
                            " 个会清数据的步骤：" + "、".join(s["label"] for s in blocked[:4]))
        if plan.get("dynamic") in ("batch", "script", "custom") and not \
                [s for s in steps if s["state"] in ("ready", "skip")] and not keep:
            block.append("这个方案没有可执行步骤：先到 ③ 救砖文件 导入镜像或整个目录")
        if plan["pid"] in ("P05", "P10") and not keep:
            warn.append("高风险步骤：会清空全部用户数据（照片/聊天记录/账号）")
        return block, warn

    # -- 主流程 -----------------------------------------------------------
    def run(self, plan, steps, dev, ctx=None):
        self.abort_flag = False
        self.ok = True
        self.ctx = ctx or make_ctx(dev, {})
        self.fastbootd = False
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        logfile = log_dir() / f"rescue_{plan['pid']}_{ts}.log"
        self.logfile = logfile
        lines = []

        def emit(msg, level="info"):
            lines.append(msg)
            self.on_log(msg, level)

        emit("=" * 72)
        emit(f"{APP_NAME} v{VERSION}  方案 {plan['pid']} {plan['title']}")
        emit(f"时间 {datetime.now():%Y-%m-%d %H:%M:%S}")
        if self.keep:
            emit("模式 保留资料（禁止任何清空用户数据的操作）", "warn")
        emit(f"设备 状态={STATE_LABEL.get(dev['state'], dev['state'])} 序列号={dev['serial'] or '-'} "
             f"机型={dev['model'] or '-'} slot={dev['slot'] or '-'} 解锁={dev['unlocked'] or '-'}")
        emit(f"adb={TOOLS.get('adb') or '未找到'}  fastboot={TOOLS.get('fastboot') or '未找到'}")
        emit("=" * 72)

        block, warn = self.preflight(plan, steps, dev)
        for w in warn:
            emit(f"[警告] {w}", "warn")
        if block:
            for b in block:
                emit(f"[阻断] {b}", "err")
            emit("预检未通过，已停止。", "err")
            self.ok = False
            return self._finish(plan, lines, logfile)

        if plan["pid"] in ("P07", "P10", "P17", "P18"):
            self._special(plan, steps, emit)
            return self._finish(plan, lines, logfile)

        # 需要 fastboot 但设备在 adb：先切过去
        needs_fb = any(s["tool"] == "fastboot" and s["state"] in ("ready", "skip") for s in steps)
        if needs_fb and dev["state"] in ("adb", "sideload"):
            emit("设备当前在系统内，正在重启到 bootloader …", "warn")
            run_capture([tool("adb"), "reboot", "bootloader"], timeout=15)
            ok = self._wait_for("fastboot", 90, emit)
            if not ok:
                emit("等待 fastboot 超时，已停止。请手动按住 音量下+电源 进 bootloader 后重试。", "err")
                self.ok = False
                return self._finish(plan, lines, logfile)

        total = len([s for s in steps if s["state"] in ("ready", "skip")])
        idx = 0
        for s in steps:
            if self.abort_flag:
                emit("已被用户中止。", "warn")
                self.ok = False
                break
            if s["state"] == "info":
                emit(f"— {s['label']}")
                if s["cmd"]:
                    emit(f"   {s['cmd']}")
                continue
            if s["state"] == "skip":
                emit(f"↷ 跳过 {s['label']}（{s['reason']}）", "dim")
                continue
            if s["state"] == "blocked":
                emit(f"⛔ 已屏蔽 {s['label']}（{s['reason']}）", "warn")
                continue
            idx += 1
            emit("")
            emit(f"[{idx}/{total}] ▶ {s['label']}")
            cmdline = s["cmdline"]
            try:
                cmdline = self._prepare_step(s, emit)
            except Exception as e:  # 探测失败不影响原命令
                emit(f"    设备探测跳过: {e}", "dim")
            if self.keep:
                why = wipe_reason(cmdline, s.get("cmd", ""))
                if why:
                    emit(f"⛔ 一条会清数据的命令被拦下：{why}", "err")
                    emit("    保留资料模式已开启，这条不执行。", "warn")
                    self.ok = False
                    continue
            if cmdline != s["cmdline"] and cmdline:
                emit(f"    按设备实际分区信息调整: {cmdline}", "cmd")
            elif s["cmdline"]:
                emit(f"    $ {s['cmdline']}", "cmd")
            if s["note"]:
                emit(f"    说明: {s['note']}", "dim")
            t0 = time.time()
            rc, out = run_stream(split_cmd(cmdline), lambda l: emit("    " + l), timeout=900,
                                 cwd=s.get("cwd") or str(OUT_DIR), should_abort=self.should_abort)
            dt = time.time() - t0
            if self.abort_flag:
                emit("已被用户中止。", "warn")
                self.ok = False
                break
            if rc == 0:
                emit(f"    ✔ 完成 ({dt:.1f}s)", "ok")
            else:
                emit(f"    ✘ 返回码 {rc} ({dt:.1f}s)", "err")
                low = (out or "").lower()
                hint = None
                if "waiting for" in low:
                    hint = "设备掉线：换线/换 USB2.0 口，确认 fastboot 界面还在"
                elif "not allowed" in low or "permission" in low and "denied" in low:
                    hint = "BL 未解锁，或该分区在当前固件下锁定"
                elif "too large" in low or "size" in low and "fail" in low:
                    hint = "镜像大小与分区不符：boot.img 机型/版本不对"
                elif "unknown command" in low:
                    hint = "本机 fastboot 版本不支持该子命令，升级 platform-tools"
                elif "no such file" in low or "cannot open" in low:
                    hint = "文件路径问题或镜像损坏"
                if hint:
                    emit(f"    处理建议: {hint}", "warn")
                if s["opt"]:
                    emit("    可选步骤失败，继续后续步骤", "dim")
                    self.ok = False
                    continue
                emit("    必需步骤失败，已停止。", "err")
                self.ok = False
                break

        return self._finish(plan, lines, logfile)

    # -- 按设备实际情况调整命令 -------------------------------------------
    def _ensure_fastbootd(self, emit, part):
        """动态分区（is-logical: yes）只能在 FastbootD 里刷"""
        if getattr(self, "fastbootd", False):
            return True
        emit(f"    分区 {part} 是动态分区（is-logical: yes），切到 FastbootD 再刷 …", "warn")
        run_capture([tool("fastboot"), "reboot", "fastboot"], timeout=30, cwd=str(OUT_DIR))
        if not self._wait_for("fastboot", 90, emit):
            emit("    没能进入 FastbootD，仍按 bootloader 模式尝试", "warn")
            return False
        self.fastbootd = in_fastbootd()
        if self.fastbootd:
            emit("    已在 FastbootD，继续", "ok")
        return self.fastbootd

    def _prepare_step(self, s, emit):
        """执行前用 getvar 实测 has-slot / is-logical，再决定真实分区名"""
        if s.get("custom_part"):
            part = s["custom_part"]
            mode = s.get("custom_slot", "auto")
            info = probe_partition(part)
            if (info.get("is_logical") or "no").lower() not in ("no", "false", "0"):
                self._ensure_fastbootd(emit, part)
            name = part
            if mode in ("a", "b"):
                name = f"{part}_{mode}"
            elif (info.get("has_slot") or "no").lower() in ("yes", "true"):
                slot = self.ctx.get("slotname") or fastboot_getvar("current-slot", 8)
                if slot in ("a", "b"):
                    self.ctx["slotname"] = slot
                    name = f"{part}_{slot}"
                emit(f"    实测 has-slot:{part} = yes，按当前槽位 {slot or '?'} 拼分区名", "dim")
            extra = (s.get("custom_extra") or "").strip()
            cmd = f"{extra + ' ' if extra else ''}flash {name} {quote(s['custom_path'])}"
            s["cmd"] = cmd
            return f"{quote(tool('fastboot'))} {cmd}"

        if s.get("batch_part"):
            part = s["batch_part"]
            info = probe_partition(part)
            name = part
            if (info.get("has_slot") or "").lower() == "yes":
                slot = self.ctx.get("slotname") or fastboot_getvar("current-slot", 8)
                if slot in ("a", "b"):
                    self.ctx["slotname"] = slot
                    name = f"{part}_{slot}"
            if (info.get("is_logical") or "no").lower() not in ("no", "false", "0"):
                self._ensure_fastbootd(emit, part)
            cmd = f"flash {name} {quote(s['batch_path'])}"
            s["cmd"] = cmd
            return f"{quote(tool('fastboot'))} {cmd}"

        part = (s.get("part") or "").strip()
        if not part or s.get("tool") != "fastboot":
            return s["cmdline"]
        info = probe_partition(part)
        if not info:
            return s["cmdline"]
        if (info.get("is_logical") or "no").lower() not in ("no", "false", "0"):
            self._ensure_fastbootd(emit, part)
        hs = (info.get("has_slot") or "").lower()
        if hs in ("yes", "no"):
            want = hs == "yes"
            if want != bool(self.ctx.get("use_slot")):
                self.ctx["use_slot"] = want
                emit(f"    实测 has-slot:{part} = {hs}，按真实槽位决定分区名", "dim")
                rendered = render(s["tpl"], self.ctx)
                s["cmd"] = rendered
                s["cmdline"] = f"{quote(tool(s['tool']))} {rendered}"
                return s["cmdline"]
        return s["cmdline"]

    def _wait_for(self, state, timeout, emit):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.abort_flag:
                return False
            d = detect_device()
            if d["state"] == state:
                emit(f"已进入 {state}（序列号 {d['serial']}）", "ok")
                return True
            time.sleep(1.5)
        return False

    def _special(self, plan, steps, emit):
        if plan["pid"] == "P07":
            self._edl_guide(emit)
        elif plan["pid"] == "P10":
            self._rom_script(emit)
        elif plan["pid"] == "P17":
            self._full_backup(emit)
        elif plan["pid"] == "P18":
            self._diagnose(emit)

    # -- 全字库备份 -------------------------------------------------------
    def _full_backup(self, emit):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = Path(BACKUP_DIR) / f"full_{ts}"
        dst.mkdir(parents=True, exist_ok=True)
        emit("")
        emit("—— 全字库备份 ——")
        dev = detect_device()
        mode = ""
        parts = []
        if dev["state"] == "fastboot":
            mode = "fetch"
            emit("设备在 fastboot，读取分区表 …")
            parts, raw = list_partitions_fastboot()
            if not parts:
                emit("getvar all 没返回分区表，可能是 fastboot 版本太旧（需 34+）", "warn")
        elif dev["state"] == "adb":
            mode = "dd"
            emit("设备在 adb，读取 /dev/block/by-name/ …（需要 root）")
            names = list_partitions_adb()
            parts = [dict(name=n, size=0, has_slot=False, logical=False) for n in names]
        else:
            emit("设备既不在 fastboot 也不在 adb，无法备份。", "err")
            self.ok = False
            return

        if not parts:
            emit("没有读到任何分区，备份中止。", "err")
            self.ok = False
            return
        emit(f"共 {len(parts)} 个分区，保存到 {dst}", "ok")
        slot = fastboot_getvar("current-slot", 8) if mode == "fetch" else ""
        manifest = []
        okc = failc = 0
        total = len(parts)
        for i, p in enumerate(parts, 1):
            if self.abort_flag:
                emit("已被用户中止备份。", "warn")
                self.ok = False
                break
            base = p["name"]
            real = f"{base}_{slot}" if (slot in ("a", "b") and p.get("has_slot")) else base
            out = dst / f"{base}.img"
            emit(f"[{i}/{total}] 备份 {base}"
                 + (f"  ({fmt_size(p['size'])})" if p.get("size") else ""))
            if mode == "fetch":
                rc, o = run_capture([tool("fastboot"), "fetch", real, str(out)],
                                    timeout=1800, cwd=str(dst))
                good = out.exists() and out.stat().st_size > 0
                if not good:
                    # 可能不支持 fetch 或分区名不对，试一次原始名
                    rc, o = run_capture([tool("fastboot"), "fetch", base, str(out)],
                                        timeout=1800, cwd=str(dst))
                    good = out.exists() and out.stat().st_size > 0
            else:
                tmp = f"/sdcard/br_{base}.img"
                run_capture([tool("adb"), "shell",
                             f"su -c 'dd if=/dev/block/by-name/{base} of={tmp} bs=1M'"],
                            timeout=1800)
                rc, o = run_capture([tool("adb"), "pull", tmp, str(out)], timeout=1800)
                run_capture([tool("adb"), "shell", f"rm -f {tmp}"], timeout=30)
                good = out.exists() and out.stat().st_size > 0
            if good:
                okc += 1
                size = out.stat().st_size
                manifest.append(dict(name=base, part=real, file=out.name, size=size))
                emit(f"     ok {out.name} ({fmt_size(size)})", "ok")
            else:
                failc += 1
                emit(f"     失败：{(o or '').strip().splitlines()[-1:] or ['无输出']}", "warn")
                try:
                    if out.exists():
                        out.unlink()
                except Exception:
                    pass

        (dst / "manifest.json").write_text(
            json.dumps(dict(device=dev.get("model"), serial=dev.get("serial"),
                            mode=mode, time=ts, partitions=manifest),
                       ensure_ascii=False, indent=2), encoding="utf-8")

        lines = ["@echo off", "chcp 65001 >nul", f"rem 由 {APP_NAME} v{VERSION} 生成的全字库还原脚本",
                 f"rem 备份时间 {ts}   设备 {dev.get('model') or '?'}", "cd /d \"%~dp0\"",
                 "echo 设备需在 fastboot 模式，按任意键开始", "pause"]
        for m in manifest:
            lines.append(f"fastboot flash {m['part']} \"%~dp0{m['file']}\"")
        lines += ["echo 全部写完，重启", "fastboot reboot", "pause"]
        (dst / "restore_all.bat").write_text("\r\n".join(lines), encoding="utf-8")

        emit("")
        emit(f"备份完成：成功 {okc} 个，失败 {failc} 个", "ok" if not failc else "warn")
        emit(f"目录: {dst}")
        emit("还原: 双击该目录下的 restore_all.bat（设备需在 fastboot）", "info")
        if failc:
            self.ok = False

    # -- 一键诊断 ---------------------------------------------------------
    def _diagnose(self, emit):
        emit("")
        emit("—— 开始采集设备信息 ——")
        p = collect_probe()
        emit(f"设备状态: {STATE_LABEL.get(p['dev']['state'], p['dev']['state'])}")
        if p["parts"]:
            emit(f"分区数量: {len(p['parts'])}")
        emit("")
        emit("—— 判定结果 ——")
        verdicts = diag_run(p, emit)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        rpt = Path(OUT_DIR) / f"诊断报告_{ts}.txt"
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        body = [f"{APP_NAME} v{VERSION} — 诊断报告",
                f"时间: {datetime.now():%Y-%m-%d %H:%M:%S}",
                f"设备: {p['dev'].get('model') or '?'}  序列号: {p['dev'].get('serial') or '?'}",
                f"状态: {STATE_LABEL.get(p['dev']['state'], p['dev']['state'])}", ""]
        if p["getvar"]:
            body.append("[bootloader 变量]")
            body += [f"  {k} = {v}" for k, v in p["getvar"].items() if v]
        if p["prop"]:
            body.append("[系统属性]")
            body += [f"  {k} = {v}" for k, v in p["prop"].items() if v]
        if p["parts"]:
            body.append(f"[分区表] 共 {len(p['parts'])} 个")
            body += [f"  {x['name']}\t{fmt_size(x['size']) if x['size'] else '-'}"
                     f"\t{'有槽位' if x['has_slot'] else ''}"
                     f"\t{'动态' if x['logical'] else ''}" for x in p["parts"]]
        body.append("")
        body.append("[判定]")
        body += [f"  [{v['level']}] {v['text']}" + (f"  → 建议 {v['pid']}" if v["pid"] else "")
                 for v in verdicts]
        if p["dmesg"]:
            body += ["", "[dmesg 尾部]", p["dmesg"][-4000:]]
        if p["pstore"]:
            body += ["", "[pstore]", p["pstore"][-4000:]]
        rpt.write_text("\n".join(body), encoding="utf-8")

        best = [v["pid"] for v in verdicts if v["pid"]]
        emit("")
        if best:
            uniq = []
            for x in best:
                if x not in uniq:
                    uniq.append(x)
            emit(f"推荐方案（按顺序试）: {' → '.join(uniq)}", "ok")
            if self.on_plan_hint:
                self.on_plan_hint(uniq[0])
        else:
            emit("没有匹配到明确问题，先跑 P12 只读诊断并保存完整日志。", "warn")
        emit(f"报告已保存: {rpt}", "ok")

    def _edl_guide(self, emit):
        port = detect_edl()
        emit("")
        emit("—— EDL / 9008 检测 ——")
        emit(f"端口: {port or '未检测到 QDLoader 9008（请检查驱动或换 USB 口）'}",
             "ok" if port else "warn")
        fires = (scan_library().get("firehose") or [])
        emit(f"firehose 文件: {fires[0].name if fires else '未放入 rescue_files/edl/'}",
             "ok" if fires else "warn")
        guide = Path(OUT_DIR) / "EDL_救砖指引.txt"
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        text = f"""{APP_NAME} — EDL(9008) 救砖指引
生成时间: {datetime.now():%Y-%m-%d %H:%M:%S}
检测端口: {port or '未检测到'}

【1】确认进入 EDL
  设备管理器 → 端口(COM 和 LPT) 出现 "Qualcomm HS-USB QDLoader 9008 (COMx)"
  没出现时: 关机 → 按住 音量上下 + 插线；或拆机短接主板测试点

【2】需要的文件（官方线刷包内）
  prog_firehose_ddr.elf / .mbn     引导程序
  rawprogram0.xml ~ rawprogram5.xml 分区表
  patch0.xml ~ patch5.xml           补丁脚本

【3】方案 A — MiFlash（小米/部分高通机型）
  1. 打开 MiFlash，选解压后的线刷包目录
  2. 右下角选 "clean all"（保留数据选 save user data）
  3. 点 flash，等待全绿

【4】方案 B — QFIL（通用高通）
  1. QFIL → Flat Build
  2. Programmer path 选 prog_firehose_ddr.elf
  3. Load XML 选 rawprogram*.xml 与 patch*.xml
  4. Download

【5】方案 C — edl.py（跨平台/可脚本化）
  pip install edlclient
  edl printgpt --loader=prog_firehose_ddr.elf
  edl w boot boot.img --loader=prog_firehose_ddr.elf
  edl w super super.img --loader=prog_firehose_ddr.elf
  edl reset

【6】常见坑
  * 9008 只能停留几分钟，超时需重新进入
  * 换 USB2.0 口、原装线，避免 hub
  * Sahara 报错多为 firehose 与机型不匹配
  * 刷完仍黑屏: 先 edl reset，再插电充 10 分钟
"""
        guide.write_text(text, encoding="utf-8")
        emit(f"已生成指引: {guide}", "ok")

    def _rom_script(self, emit):
        zips = (scan_library().get("fullzip") or [])
        emit("")
        if not zips:
            emit("未在全量包目录找到 .zip/.tgz，先把线刷包放进去。", "warn")
            return
        src = zips[0]
        emit(f"检测到包: {src.name}  ({fmt_size(src.stat().st_size)})")
        script = OUT_DIR / "flash_rom.bat"
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        body = f"""@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo ============================================
echo  {APP_NAME} - 全量线刷脚本
echo  当前目录: %CD%
echo ============================================
if not exist "flash-all.bat" if not exist "flash-all.sh" (
  echo [错误] 当前目录没有 flash-all.bat
  echo        请把本脚本放进解压后的线刷包目录再运行
  pause & exit /b 1
)
if exist "boot.img" (
  echo [校验] boot.img 存在
) else (
  echo [警告] 没有找到 boot.img，确认包是否完整
)
echo.
echo 即将开始刷写，设备需在 fastboot 模式
pause
if exist "flash-all.bat" (
  call flash-all.bat
) else (
  bash flash-all.sh
)
echo.
echo 刷写流程结束，检查设备是否正常开机
pause
"""
        script.write_text(body, encoding="utf-8")
        emit(f"已生成线刷脚本: {script}", "ok")
        emit("用法: 解压线刷包 → 把 flash_rom.bat 复制进去 → 双击", "dim")

    def _finish(self, plan, lines, logfile):
        tail = ["", "-" * 72,
                "结果: " + ("成功" if self.ok else "未完成/有失败步骤"),
                f"日志: {logfile}"]
        try:
            logfile.write_text("\n".join(lines + tail), encoding="utf-8")
        except Exception:
            pass
        for t in tail:
            self.on_log(t, "ok" if self.ok else "err")
        if self.on_done:
            self.on_done(self.ok, logfile)
        return self.ok


# ---------------------------------------------------------------------------
# 图形界面
# ---------------------------------------------------------------------------
BG = "#0f131a"
PANEL = "#171d28"
PANEL2 = "#1e2634"
FG = "#dde5f2"
DIM = "#7c8aa3"
ACC = "#3b82f6"
OKC = "#3ddc84"
WARNC = "#ffb020"
ERRC = "#ff5c5c"
BORD = "#2b3547"


def build_ui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog
    from tkinter import font as tkfont
    try:
        from PIL import Image, ImageDraw, ImageFilter, ImageChops, ImageTk
        HAS_PIL = True
    except Exception:
        HAS_PIL = False

    # =====================================================================
    # 视觉系统：液态玻璃
    # =====================================================================
    C = {
        "bg0": "#070A11", "bg1": "#0B1220", "bg2": "#141A33",
        "panel": "#0C131F", "panel2": "#111A28", "log": "#080D15",
        "line": "#1D2839",
        "fg": "#E9EFFA", "fg2": "#B7C4DA", "dim": "#8292AC", "dim2": "#586A82",
        "acc": "#4F8DFF", "acc_d": "#2F6BD6", "vio": "#8B5CF6",
        "ok": "#3DDC97", "warn": "#FFC24B", "err": "#FF6B7A", "teal": "#22D3EE",
    }
    FUI = "Microsoft YaHei UI"
    FMONO = "Consolas"

    def hx(s):
        s = s.lstrip("#")
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))

    def mix(a, b, t):
        a, b = hx(a), hx(b)
        return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

    # -- 背景：深空渐变 + 光斑 + 噪点 -------------------------------------
    def make_backdrop(w, h):
        w, h = max(w, 32), max(h, 32)
        img = Image.new("RGB", (w, h))
        d = ImageDraw.Draw(img)
        top, mid, bot = hx(C["bg0"]), hx(C["bg1"]), hx(C["bg2"])
        for y in range(h):
            t = y / max(1, h - 1)
            if t < 0.55:
                k = t / 0.55
                col = tuple(int(top[i] + (mid[i] - top[i]) * k) for i in range(3))
            else:
                k = (t - 0.55) / 0.45
                col = tuple(int(mid[i] + (bot[i] - mid[i]) * k) for i in range(3))
            d.line([(0, y), (w, y)], fill=col)
        # 光斑
        orbs = [(0.16, 0.06, 0.55, C["acc"], 46), (0.86, 0.14, 0.48, C["vio"], 40),
                (0.62, 0.96, 0.62, C["teal"], 20), (0.05, 0.92, 0.50, C["acc"], 22)]
        for cx, cy, sc, col, al in orbs:
            size = int(min(w, h) * sc * 2)
            if size < 8:
                continue
            g = Image.radial_gradient("L").resize((size, size), Image.BILINEAR)
            g = Image.eval(g, lambda v: int((255 - v) * al / 255))
            layer = Image.new("RGB", (size, size), hx(col))
            img.paste(layer, (int(cx * w - size / 2), int(cy * h - size / 2)), g)
        img = img.filter(ImageFilter.GaussianBlur(1.2))
        return img

    # -- 玻璃面板 ---------------------------------------------------------
    PAD = 22   # 阴影预留

    def panel_image(w, h, r=18, tint=None, border=42, gloss=True):
        w, h = max(w, 8), max(h, 8)
        r = min(r, w // 2, h // 2)
        W, H = w + PAD * 2, h + PAD * 2
        if not HAS_PIL:
            return None, PAD
        # 阴影
        canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(sh).rounded_rectangle(
            [PAD, PAD + 7, PAD + w, PAD + h + 7], r, fill=(0, 0, 0, 150))
        canvas.alpha_composite(sh.filter(ImageFilter.GaussianBlur(14)))
        # 主体
        body = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        bd = ImageDraw.Draw(body)
        bd.rounded_rectangle([0, 0, w - 1, h - 1], r, fill=hx(C["panel"]) + (255,))
        if tint:
            bd.rounded_rectangle([0, 0, w - 1, h - 1], r, fill=hx(tint) + (26,))
        canvas.alpha_composite(body, (PAD, PAD))
        # 顶部高光 + 描边
        if gloss:
            edge = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            ImageDraw.Draw(edge).rounded_rectangle(
                [0, 0, w - 1, h - 1], r, outline=(255, 255, 255, 255), width=1)
            ramp = Image.new("L", (1, h))
            for y in range(h):
                t = y / max(1, h - 1)
                ramp.putpixel((0, y), int(border * (1.0 - 0.72 * t)))
            edge.putalpha(ImageChops.multiply(edge.getchannel("A"), ramp.resize((w, h))))
            canvas.alpha_composite(edge, (PAD, PAD))
            hi = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            ImageDraw.Draw(hi).line([(r, 1), (w - r, 1)], fill=(255, 255, 255, 34), width=1)
            canvas.alpha_composite(hi.filter(ImageFilter.GaussianBlur(0.6)), (PAD, PAD))
        return canvas, PAD

    class UI(tk.Tk):
        # -----------------------------------------------------------------
        def __init__(self):
            super().__init__()
            self.title(f"{APP_NAME} v{VERSION}")
            self.configure(bg=C["bg0"])
            for ico in (ROOT / "BrickRescue.ico", Path(__file__).with_name("BrickRescue.ico")):
                try:
                    if ico.is_file():
                        self.iconbitmap(str(ico))
                        break
                except Exception:
                    pass
            self.minsize(1180, 720)
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            w, h = min(1560, sw - 30), min(980, sh - 60)
            self.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 2 - 24)}")
            try:
                self.state("zoomed")
            except Exception:
                pass

            self.dev = {"state": "none", "serial": "", "model": "", "slot": "",
                        "unlocked": "", "edl": "", "android": "", "raw": ""}
            self.lib = {}
            self.override = {}
            self.entries = []
            self.script = None
            self.bundle_dir = None
            self.cur_plan = None
            self.cur_steps = []
            self._plans = []
            self.runner = None
            self.ctx = None
            self.logbuf = []
            self.sel_sym = set()
            self.sel_plan = 0
            self.off = {"sym": 0, "plan": 0, "file": 0, "step": 0}
            self.hover = {"sym": -1, "plan": -1, "file": -1}
            self._imgs = {}
            self._fonts = {}
            self._geom = {}
            self.prog = ""
            self._backdrop = None
            self._bksize = (0, 0)
            self._resize_job = None

            self.cv = tk.Canvas(self, bg=C["bg0"], highlightthickness=0, bd=0)
            self.cv.pack(fill="both", expand=True)
            self.log_widget = tk.Text(self, bg=C["log"], fg="#C6D3E8", bd=0,
                                      highlightthickness=0, font=(FMONO, 9),
                                      wrap="word", padx=12, pady=9, insertbackground=C["fg"])
            for tag, col in (("info", "#C6D3E8"), ("cmd", "#7FB8FF"), ("ok", C["ok"]),
                             ("warn", C["warn"]), ("err", C["err"]), ("dim", "#5E6D86"),
                             ("keep", "#8FE7B6")):
                self.log_widget.tag_configure(tag, foreground=col)
            self.log_widget.place_forget()

            self.cv.bind("<Configure>", self._on_resize)
            self.cv.bind("<Button-1>", self._on_click)
            self.cv.bind("<Double-Button-1>", self._on_dbl)
            self.cv.bind("<Motion>", self._on_motion)
            self.cv.bind("<Leave>", lambda e: self._hover_clear())
            self.bind_all("<MouseWheel>", self._on_wheel)

            self.keep_var = tk.BooleanVar(value=keep_data())
            self.auto_ref = tk.BooleanVar(value=False)
            self.btn_state = {}

            ensure_dirs()
            resolve_tools()
            self.rescan_library()
            self.on_symptom_change(log=False)
            self.after(120, self._first_paint)

        # =================================================================
        # 图像缓存 / 绘制基元
        # =================================================================
        def _img(self, key, maker):
            if key in self._imgs:
                return self._imgs[key]
            v = maker()
            if len(self._imgs) > 400:
                self._imgs.clear()
            self._imgs[key] = v
            return v

        def _photo(self, key, maker):
            v = self._img(key, maker)
            if v is None:
                return None
            return ImageTk.PhotoImage(v)

        def _panel(self, x, y, w, h, r=18, tint=None, border=42, tag="ui"):
            if not HAS_PIL:
                self.cv.create_rectangle(x, y, x + w, y + h, fill=C["panel"],
                                         outline=C["line"], tags=tag)
                return
            key = ("panel", w, h, r, tint, border)
            ph = self._img(key, lambda: ImageTk.PhotoImage(panel_image(w, h, r, tint, border)[0]))
            if ph is None:
                return
            self.cv.create_image(x - PAD, y - PAD, image=ph, anchor="nw", tags=tag)
            self.cv._ph_cache = getattr(self.cv, "_ph_cache", {})
            self.cv._ph_cache[key] = ph   # 防 GC

        def _text(self, x, y, s, fg=None, fnt=None, anchor="nw", tag="ui", w=None):
            return self.cv.create_text(x, y, text=s, fill=fg or C["fg"],
                                       font=fnt or (FUI, 9), anchor=anchor, tags=tag,
                                       width=w or 0)

        def _measure(self, s, fnt):
            f = self._fonts.get(fnt)
            if f is None:
                f = tkfont.Font(font=fnt)
                self._fonts[fnt] = f
            try:
                return f.measure(s)
            except Exception:
                return len(s) * 9

        # -- 圆角按钮 -----------------------------------------------------
        def _btn_img(self, w, h, kind, state):
            r = min(h // 2, 13)
            key = ("btn", w, h, kind, state)
            def mk():
                img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
                d = ImageDraw.Draw(img)
                if kind == "hero":
                    a, b = (70, 110, 235), (140, 92, 246)
                    if state == "hover":
                        a, b = (86, 128, 255), (156, 110, 255)
                    if state == "press":
                        a, b = (54, 90, 205), (120, 76, 220)
                    for y in range(h):
                        t = y / max(1, h - 1)
                        d.line([(0, y), (w, y)],
                               fill=tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3)) + (255,))
                elif kind == "primary":
                    a = (56, 100, 214) if state == "idle" else (72, 122, 240)
                    if state == "press":
                        a = (42, 78, 176)
                    if state == "off":
                        a = (34, 48, 70)
                    d.rounded_rectangle([0, 0, w - 1, h - 1], r, fill=a + (255,))
                elif kind == "danger":
                    a = (150, 46, 58) if state == "idle" else (176, 58, 70)
                    if state == "off":
                        a = (52, 32, 40)
                    d.rounded_rectangle([0, 0, w - 1, h - 1], r, fill=a + (255,))
                else:  # ghost
                    a = (255, 255, 255, 12) if state == "idle" else (255, 255, 255, 26)
                    if state == "press":
                        a = (255, 255, 255, 40)
                    if state == "off":
                        a = (255, 255, 255, 6)
                    d.rounded_rectangle([0, 0, w - 1, h - 1], r, fill=a)
                    d.rounded_rectangle([0, 0, w - 1, h - 1], r,
                                        outline=(255, 255, 255, 30), width=1)
                m = Image.new("L", (w, h), 0)
                ImageDraw.Draw(m).rounded_rectangle([0, 0, w - 1, h - 1], r, fill=255)
                img.putalpha(ImageChops.multiply(img.getchannel("A"), m))
                if kind in ("hero", "primary"):
                    hi = Image.new("RGBA", (w, h), (0, 0, 0, 0))
                    ImageDraw.Draw(hi).line([(r, 1), (w - r, 1)], fill=(255, 255, 255, 70))
                    img.alpha_composite(hi.filter(ImageFilter.GaussianBlur(0.7)))
                return ImageTk.PhotoImage(img)
            return self._img(key, mk)

        def _button(self, x, y, w, h, label, cmd, kind="ghost", key=None,
                    sub="", icon="", enabled=True):
            key = key or f"b{len(self.btn_state)}"
            self.btn_state[key] = dict(x=x, y=y, w=w, h=h, kind=kind,
                                       cmd=cmd, label=label, sub=sub, icon=icon,
                                       enabled=enabled, hov=False)
            self._draw_button(key)

        def _draw_button(self, key):
            b = self.btn_state.get(key)
            if not b:
                return
            st = "off" if not b["enabled"] else ("hover" if b["hov"] else "idle")
            ph = self._btn_img(b["w"], b["h"], b["kind"], st)
            tag = "btn:" + key
            self.cv.create_image(b["x"], b["y"], image=ph, anchor="nw", tags=("ui", tag))
            self.cv._ph_cache = getattr(self.cv, "_ph_cache", {})
            self.cv._ph_cache[(key, st)] = ph
            fg = C["fg"] if b["enabled"] else "#5C6B84"
            if b["kind"] in ("hero", "primary") and b["enabled"]:
                fg = "#FFFFFF"
            cx = b["x"] + (18 if b["icon"] else 0)
            if b["icon"]:
                self.cv.create_text(b["x"] + 18, b["y"] + b["h"] / 2, text=b["icon"],
                                    fill=fg, font=(FUI, 13), anchor="w", tags=("ui", tag))
            if b["sub"]:
                self.cv.create_text(cx + 6, b["y"] + b["h"] / 2 - 8, text=b["label"],
                                    fill=fg, font=(FUI, 10, "bold"), anchor="w", tags=("ui", tag))
                self.cv.create_text(cx + 6, b["y"] + b["h"] / 2 + 8, text=b["sub"],
                                    fill=mix(C["fg"], C["panel"], 0.42) if b["enabled"] else "#4A576B",
                                    font=(FUI, 8), anchor="w", tags=("ui", tag))
            else:
                self.cv.create_text(b["x"] + b["w"] / 2 + (8 if b["icon"] else 0),
                                    b["y"] + b["h"] / 2, text=b["label"], fill=fg,
                                    font=(FUI, 9, "bold") if b["kind"] != "ghost" else (FUI, 9),
                                    anchor="center", tags=("ui", tag))
            self.cv.tag_bind(tag, "<Button-1>", lambda e, k=key: self._btn_click(k))
            self.cv.tag_bind(tag, "<Enter>", lambda e, k=key: self._btn_hover(k, True))
            self.cv.tag_bind(tag, "<Leave>", lambda e, k=key: self._btn_hover(k, False))

        def _btn_hover(self, key, on):
            b = self.btn_state.get(key)
            if not b or b["hov"] == on:
                return
            b["hov"] = on
            self.cv.delete("btn:" + key)
            self._draw_button(key)

        def _btn_click(self, key):
            b = self.btn_state.get(key)
            if not b or not b["enabled"]:
                return
            self.cv.configure(cursor="")
            try:
                b["cmd"]()
            except Exception as e:
                self.logln(f"操作失败: {e}", "err")

        # =================================================================
        # 布局
        # =================================================================
        def _on_resize(self, ev=None):
            if self._resize_job:
                try:
                    self.after_cancel(self._resize_job)
                except Exception:
                    pass
            self._resize_job = self.after(160, self._first_paint)

        def _first_paint(self):
            self._resize_job = None
            self._backdrop_img = None
            self._imgs.clear()
            self._layout()
            if not getattr(self, "_booted", False):
                self._booted = True
                self.logln(f"{APP_NAME} v{VERSION} 已就绪", "ok")
                self.logln("① 选症状 → ② 选方案 → ③ 确认救砖文件 → ④ 开始救砖", "info")
                self.logln(f"文件库 {LIB_DIR}", "dim")
                self.logln(f"adb {tool('adb') or '未找到'}   fastboot {tool('fastboot') or '未找到'}", "dim")
                self.refresh_device()
            self._sync_log()

        def _layout(self):
            self.cv.delete("ui")
            self.btn_state.clear()
            W = max(self.winfo_width(), 900)
            H = max(self.winfo_height(), 620)
            g = self._geom
            g["W"], g["H"] = W, H
            pad, gap = 14, 12
            g["top"] = (pad, pad, W - pad * 2, 58)
            ty = pad + 58 + gap
            g["tools"] = (pad, ty, W - pad * 2, 88)
            my = ty + 88 + gap
            barh = 30
            mh = H - my - barh - pad - 4
            lw = max(430, min(560, int(W * 0.335)))
            g["left"] = (pad, my, lw, mh)
            g["right"] = (pad + lw + gap, my, W - pad * 2 - lw - gap, mh)
            g["status"] = (pad, H - barh - pad + 4, W - pad * 2, barh)
            self._paint()
            self._place_log()
            self._sync_log()

        # -- 主绘制 -------------------------------------------------------
        def _paint(self):
            if "top" not in self._geom:
                return
            self.cv.delete("ui")
            self.btn_state.clear()
            W, H = self._geom.get("W", 1200), self._geom.get("H", 800)
            cv = self.cv
            # 背景
            if self._bksize != (W, H):
                self._backdrop = make_backdrop(W, H)
                self._bksize = (W, H)
                self._bk_photo = ImageTk.PhotoImage(self._backdrop) if HAS_PIL else None
            if HAS_PIL and getattr(self, "_bk_photo", None):
                cv.create_image(0, 0, image=self._bk_photo, anchor="nw", tags=("ui", "bk"))
            else:
                cv.create_rectangle(0, 0, W, H, fill=C["bg1"], outline="", tags=("ui", "bk"))

            self.btn_state.clear()
            self._paint_top()
            self._paint_tools()
            self._paint_left()
            self._paint_right()
            self._paint_status()

        # -- 顶栏 ---------------------------------------------------------
        def _paint_top(self):
            x, y, w, h = self._geom["top"]
            self._panel(x, y, w, h, 20, tint=C["acc"])
            cv = self.cv
            # logo 徽标
            bx = x + 20
            self._panel(bx, y + 14, 30, 30, 10, tint=C["acc"], border=90)
            cv.create_text(bx + 15, y + 29, text="⛨", fill="#CFE0FF", font=(FUI, 13), tags="ui")
            cv.create_text(bx + 44, y + 20, text="救砖工具", fill=C["fg"],
                           font=(FUI, 14, "bold"), anchor="w", tags="ui")
            cv.create_text(bx + 44, y + 42, text=f"BRICKRESCUE  v{VERSION}", fill=C["dim2"],
                           font=(FMONO, 8), anchor="w", tags="ui")

            # 设备胶囊
            d = self.dev
            st = d.get("state", "none")
            col = {"adb": C["ok"], "fastboot": C["ok"], "sideload": C["ok"],
                   "edl": C["warn"], "unauthorized": C["warn"]}.get(st, C["err"])
            bits = []
            if d.get("model"):
                bits.append(d["model"])
            if d.get("slot"):
                bits.append("slot " + d["slot"])
            if d.get("serial"):
                bits.append(d["serial"])
            info = "  ".join(bits) or "连接手机后点右侧刷新"
            px = x + 230
            pw = min(430, max(240, self._measure(info, (FUI, 8)) + 96))
            self._panel(px, y + 12, pw, 34, 17, border=26)
            cv.create_oval(px + 14, y + 25, px + 22, y + 33, fill=col, outline="", tags="ui")
            cv.create_text(px + 30, y + 21, text=STATE_LABEL.get(st, st), fill=col,
                           font=(FUI, 9, "bold"), anchor="w", tags="ui")
            cv.create_text(px + 30, y + 37, text=info, fill=C["dim2"], font=(FUI, 8),
                           anchor="w", tags="ui")

            # 右侧按钮组
            bw, bh, by = 96, 32, y + 13
            rx = x + w - 16
            rx -= bw
            self._button(rx, by, bw, bh, "🔧  驱动", self.driver_panel, "ghost", "drv")
            rx -= bw + 8
            self._button(rx, by, bw, bh, "↻  重新检测", lambda: self.refresh_device(True),
                         "ghost", "ref")
            rx -= 118
            keep = self.keep_var.get()
            self._button(rx, by, 110, bh,
                         ("🔒  保留资料" if keep else "🔓  保留资料"),
                         self.toggle_keep, "primary" if keep else "ghost", "keep")
            if keep:
                cv.create_text(rx + 55, by + bh + 12, text="已禁止所有清数据操作",
                               fill=C["ok"], font=(FUI, 8), anchor="n", tags="ui")
            rx -= 92
            self._button(rx, by, 84, bh,
                         ("自动刷新 ✓" if self.auto_ref.get() else "自动刷新"),
                         self.toggle_auto, "ghost", "auto")

        # -- 工具条（全字库备份 突出） --------------------------------------
        def _paint_tools(self):
            x, y, w, h = self._geom["tools"]
            cv = self.cv
            hero_w = int(min(max(360, w * 0.40), 520))
            self._button(x, y, hero_w, h, "全字库备份", self.run_backup_plan,
                         "hero", "hero", sub="把整机全部分区一次备份下来 · 不属于救砖流程",
                         icon="⛁")
            rx = x + hero_w + 12
            rest = w - hero_w - 12
            bw = max(150, min(210, rest // 3 - 8))
            self._button(rx, y, bw, h, "一键诊断", self.run_diag_plan, "ghost", "g2",
                         sub="自动判断砖在哪，推荐方案", icon="⌖")
            rx += bw + 10
            self._button(rx, y, bw, h, "自定义刷写", self.custom_flash, "ghost", "g3",
                         sub="自选镜像 + 自定分区", icon="✎")
            rx += bw + 10
            if rx + bw <= x + w:
                self._button(rx, y, bw, h, "清理导入", self.clear_imports, "ghost", "g4",
                             sub="清空手动加入的镜像", icon="⌫")

        # -- 左栏 ---------------------------------------------------------
        def _paint_left(self):
            x, y, w, h = self._geom["left"]
            cv = self.cv
            gap = 10
            h1 = int(h * 0.33)
            h2 = int(h * 0.28)
            h3 = h - h1 - h2 - gap * 2

            # ① 症状
            self._panel(x, y, w, h1, 18)
            self._section(x + 18, y + 16, "①", "选择症状", "可多选")
            rows_h = h1 - 34 - 48
            self._list_region("sym", x + 12, y + 44, w - 24, rows_h,
                              [(sid, name) for sid, name, _ in SYMPTOMS], multi=True)
            cur = sorted(self.sel_sym)
            desc = ""
            if cur:
                desc = SYMPTOMS[cur[-1]][2]
            cv.create_text(x + 18, y + h1 - 20, text=desc or "选中症状后这里显示判断依据",
                           fill=C["dim2"] if not desc else C["dim"], font=(FUI, 8),
                           anchor="w", tags="ui", width=w - 36)

            # ② 方案
            y2 = y + h1 + gap
            self._panel(x, y2, w, h2, 18)
            self._section(x + 18, y2 + 16, "②", "匹配方案", "按匹配度排序")
            self._plan_region(x + 12, y2 + 44, w - 24, h2 - 52)

            # ③ 救砖文件
            y3 = y2 + h2 + gap
            self._panel(x, y3, w, h3, 18)
            files = self.current_files()
            n = sum(1 for k, _, _ in KINDS if files.get(k))
            if self.entries:
                sub = f"已载入 {len(self.entries)} 个镜像"
            else:
                sub = "单文件 / 整个文件夹"
            self._section(x + 18, y3 + 16, "③", "救砖文件", sub)
            bh = 27
            bx = x + 14
            for lbl, fn, kd, kk in (("＋ 文件", self.import_files, "primary", "f1"),
                                    ("＋ 文件夹", self.import_folder, "primary", "f2"),
                                    ("＋ 自定义", self.custom_flash, "primary", "f3"),
                                    ("重扫", self.rescan_library, "ghost", "f4"),
                                    ("清空", self.clear_imports, "ghost", "f5")):
                bw = self._measure(lbl, (FUI, 9, "bold")) + 24
                if bx + bw > x + w - 14:
                    break
                self._button(bx, y3 + 42, bw, bh, lbl, fn, kd, kk)
                bx += bw + 6
            self._file_region(x + 12, y3 + 78, w - 24, h3 - 78 - 12)

        def _section(self, x, y, num, title, sub=""):
            cv = self.cv
            self._text(x, y, num, C["acc"], (FUI, 10, "bold"))
            self._text(x + 22, y, title, C["fg"], (FUI, 10, "bold"))
            if sub:
                self._text(x + 26 + self._measure(title, (FUI, 10, "bold")), y + 1,
                           sub, C["dim2"], (FUI, 8))

        # -- 右栏 ---------------------------------------------------------
        def _paint_right(self):
            x, y, w, h = self._geom["right"]
            cv = self.cv
            gap = 10
            hh = 118
            logh = max(150, int(h * 0.225))
            hstep = h - hh - logh - gap * 2 - 46

            # 方案头
            self._panel(x, y, w, hh, 18)
            p = self.cur_plan
            if not p:
                cv.create_text(x + 22, y + 30, text="选择左侧症状以开始",
                               fill=C["fg"], font=(FUI, 14, "bold"), anchor="w", tags="ui")
                cv.create_text(x + 22, y + 58,
                               text="选一个症状 → 右侧会列出匹配的救砖方案与逐步命令",
                               fill=C["dim"], font=(FUI, 9), anchor="w", tags="ui")
            else:
                risk = p["risk"][:1]
                rcol = {"低": C["ok"], "中": C["warn"], "高": C["err"]}.get(risk, C["dim"])
                cv.create_text(x + 22, y + 26, text=p["title"], fill=C["fg"],
                               font=(FUI, 14, "bold"), anchor="w", tags="ui")
                # 徽章
                bxx = x + 22
                for txt, col in ((p["pid"], C["acc"]),
                                 (f"风险 {p['risk']}", rcol),
                                 ("需要文件 " + (", ".join(KIND_NAME.get(k, k) for k in p["needs"]) or "无"),
                                  C["dim"])):
                    bw = self._measure(txt, (FUI, 8)) + 18
                    self._panel(bxx, y + 44, bw, 21, 10, tint=col, border=40)
                    cv.create_text(bxx + bw / 2, y + 54, text=txt, fill=col,
                                   font=(FUI, 8, "bold"), anchor="center", tags="ui")
                    bxx += bw + 7
                cv.create_text(x + 22, y + 74, text=p["desc"], fill=C["fg2"],
                               font=(FUI, 9), anchor="nw", justify="left",
                               width=w - 44, tags="ui")
                keep = keep_data()
                why = plan_wipes(p, self.cur_steps)
                if keep and why and p.get("wipes"):
                    pre, pc = f"⛔ 保留资料模式已拦截：{why}", C["err"]
                else:
                    pre, pc = "前置条件：" + p["pre"], C["warn"]
                cv.create_text(x + 22, y + hh - 16, text=pre, fill=pc, font=(FUI, 8),
                               anchor="w", tags="ui", width=w - 44)

            # 步骤
            ys = y + hh + gap
            hs = hstep
            self._panel(x, ys, w, hs, 18)
            self._section(x + 18, ys + 16, "▤", "执行步骤预览",
                          f"{len(self.cur_steps)} 步 · 命令可复制")
            self._step_region(x + 12, ys + 44, w - 24, hs - 52)

            # 按钮条
            yb = ys + hs + gap
            bh = 38
            bx = x
            run_off = bool(self.cur_plan and keep_data() and self.cur_plan.get("wipes"))
            busy = bool(self.runner and getattr(self.runner, "thread", None)
                        and self.runner.thread.is_alive())
            self._button(bx, yb, 152, bh, "▶  开始救砖", self.start_run,
                         "primary", "run", enabled=not (run_off or busy))
            bx += 160
            self._button(bx, yb, 96, bh, "■ 中止", self.stop_run, "danger", "stop",
                         enabled=busy)
            bx += 104
            for lbl, fn, kk in (("导出 .bat", self.export_bat, "x1"),
                                ("复制命令", self.copy_cmds, "x2"),
                                ("方案报告", self.export_report, "x3"),
                                ("清空日志", self.clear_log, "x4")):
                bw = self._measure(lbl, (FUI, 9)) + 26
                if bx + bw > x + w - 8:
                    break
                self._button(bx, yb, bw, bh, lbl, fn, "ghost", kk)
                bx += bw + 6

            # 日志
            yl = yb + bh + gap
            self._panel(x, yl, w, logh, 18)
            self._section(x + 18, yl + 14, "▸", "运行日志", "")
            self._geom["log"] = (x + 16, yl + 40, w - 32, logh - 52)
            lx = x + 46 + self._measure("运行日志", (FUI, 10, "bold")) + 16
            cv.create_text(lx, yl + 15, text="打开目录", fill=C["dim2"], font=(FUI, 8),
                           anchor="nw", tags=("ui", "lnk_open"))
            cv.tag_bind("lnk_open", "<Button-1>", lambda e: self.open_log_dir())
            cv.create_text(lx + 62, yl + 15, text="改目录", fill=C["dim2"], font=(FUI, 8),
                           anchor="nw", tags=("ui", "lnk_chg"))
            cv.tag_bind("lnk_chg", "<Button-1>", lambda e: self.choose_log_dir())
            if self.prog:
                cv.create_text(x + w - 20, yl + 15, text=self.prog, fill=C["acc"],
                               font=(FMONO, 8), anchor="ne", tags="ui")

        def _paint_status(self):
            x, y, w, h = self._geom["status"]
            cv = self.cv
            at, ft = TOOLS.get("adb"), TOOLS.get("fastboot")
            cv.create_text(x + 4, y + 8, text=f"adb {at or '✗'}", fill=C["dim2"],
                           font=(FMONO, 8), anchor="w", tags="ui")
            cv.create_text(x + 4, y + 22, text=f"fastboot {ft or '✗'}", fill=C["dim2"],
                           font=(FMONO, 8), anchor="w", tags="ui")
            cv.tag_bind(cv.create_text(x + w - 4, y + 14, text="定位 platform-tools",
                                       fill=C["dim2"], font=(FUI, 8), anchor="e",
                                       tags=("ui", "lnk3")),
                        "<Button-1>", lambda e: self.pick_platform_tools())
            cv.create_text(x + w / 2, y + 14, text=f"①  ②  ③  ④   四步走完，砖就活了",
                           fill="#243247", font=(FUI, 8), anchor="center", tags="ui")

        # =================================================================
        # 可交互列表（Canvas 自绘）
        # =================================================================
        def _list_region(self, key, x, y, w, h, items, multi=False, rowh=25):
            self._geom["r_" + key] = dict(x=x, y=y, w=w, h=h, items=items, multi=multi,
                                          rowh=rowh)
            self._draw_list(key)

        def _plan_region(self, x, y, w, h):
            items = [(str(i), p) for i, p in enumerate(self._plans)]
            self._geom["r_plan"] = dict(x=x, y=y, w=w, h=h, items=items, multi=False,
                                        rowh=25)
            self._draw_list("plan")

        def _draw_list(self, key):
            r = self._geom.get("r_" + key)
            if not r:
                return
            cv = self.cv
            cv.delete("lst_" + key)
            tag = ("ui", "lst_" + key)
            x, y, w, h = r["x"], r["y"], r["w"], r["h"]
            items = r["items"]
            rowh = r["rowh"]
            visible = max(1, h // rowh)
            off = max(0, min(self.off[key], max(0, len(items) - visible)))
            self.off[key] = off
            for i in range(visible):
                idx = off + i
                if idx >= len(items):
                    break
                ry = y + i * rowh
                sel = self._is_sel(key, idx)
                hov = self.hover.get(key) == idx
                if sel or hov:
                    col = C["acc"] if sel else "#16202F"
                    self.cv.create_rectangle(x + 2, ry + 1, x + w - 6, ry + rowh - 2,
                                             fill=col if sel else col, outline="",
                                             tags=tag)
                    if sel:
                        self.cv.create_rectangle(x + 2, ry + 5, x + 5, ry + rowh - 6,
                                                 fill="#FFFFFF", outline="", tags=tag)
                if key == "sym":
                    label = "·  " + items[idx][1]
                    fg = "#FFFFFF" if sel else (C["fg2"] if hov else "#A9B8CE")
                elif key == "plan":
                    p = items[idx][1]
                    keep = keep_data()
                    flag = "⛔ " if (keep and p.get("wipes")) else ""
                    label = f"{flag}{p['title']}"
                    rk = p["risk"][:1]
                    fg = "#FFFFFF" if sel else C["fg2"]
                    if rk == "高" and not sel:
                        fg = C["err"]
                    elif rk == "中" and not sel:
                        fg = C["warn"]
                else:
                    label, fg = str(items[idx]), C["fg2"]
                self.cv.create_text(x + 14, ry + rowh / 2, text=label, fill=fg,
                                    font=(FUI, 9), anchor="w", tags=tag)
            # 滚动条
            if len(items) > visible:
                bh = max(20, int(h * visible / len(items)))
                by = y + int((h - bh) * off / max(1, len(items) - visible))
                self.cv.create_rectangle(x + w - 4, y, x + w - 1, y + h,
                                         fill="#0E1826", outline="", tags=tag)
                self.cv.create_rectangle(x + w - 4, by, x + w - 1, by + bh,
                                         fill="#31435F", outline="", tags=tag)

        def _is_sel(self, key, idx):
            if key == "sym":
                items = self._geom["r_sym"]["items"]
                return items[idx][0] in self.sel_sym
            if key == "plan":
                return idx == self.sel_plan
            return False

        def _file_region(self, x, y, w, h):
            self._geom["r_file"] = dict(x=x, y=y, w=w, h=h, rowh=25)
            self._draw_files()

        def _file_rows(self):
            files = self.current_files()
            rows = []
            for k, name, _ in KINDS:
                p = files.get(k)
                if p:
                    src = "手动" if k in self.override else (self._src_of(k) or "自动")
                    rows.append(("k:" + k, name.split("（")[0], p.name, src, True))
                else:
                    rows.append(("k:" + k, name.split("（")[0], "— 未找到 —", "", False))
            extras = [e for e in self.entries if not e["kind"]]
            for i, e in enumerate(extras):
                src = e["src"]
                if e.get("custom"):
                    sl = e.get("slot", "auto")
                    src = "自定" + (f"/{sl}" if sl in ("a", "b") else "")
                rows.append((f"x:{i}", e["part"], e["path"].name, src, True))
            return rows

        def _draw_files(self):
            r = self._geom.get("r_file")
            if not r:
                return
            cv = self.cv
            cv.delete("fl")
            tag = ("ui", "fl")
            x, y, w, h = r["x"], r["y"], r["w"], r["h"]
            rows = self._file_rows()
            rowh = r["rowh"]
            self._file_rows_cache = rows
            # 表头
            cv.create_text(x + 12, y + 8, text="分区 / 类型", fill=C["dim2"],
                           font=(FUI, 8, "bold"), anchor="w", tags=tag)
            cv.create_text(x + int(w * 0.40), y + 8, text="文件", fill=C["dim2"],
                           font=(FUI, 8, "bold"), anchor="w", tags=tag)
            cv.create_text(x + w - 6, y + 8, text="来源", fill=C["dim2"],
                           font=(FUI, 8, "bold"), anchor="e", tags=tag)
            top = y + 20
            visible = max(1, (h - 20) // rowh)
            off = max(0, min(self.off["file"], max(0, len(rows) - visible)))
            self.off["file"] = off
            for i in range(visible):
                idx = off + i
                if idx >= len(rows):
                    break
                iid, kind, fname, src, ok = rows[idx]
                ry = top + i * rowh
                hov = self.hover.get("file") == idx
                if hov:
                    cv.create_rectangle(x + 2, ry + 1, x + w - 6, ry + rowh - 2,
                                        fill="#16202F", outline="", tags=tag)
                cv.create_text(x + 12, ry + rowh / 2, text=kind,
                               fill=C["fg2"] if ok else C["dim2"], font=(FUI, 9),
                               anchor="w", tags=tag)
                fn = fname
                maxw = int(w * 0.42) - 20
                while self._measure(fn, (FUI, 9)) > maxw and len(fn) > 6:
                    fn = fn[:-4] + "…"
                cv.create_text(x + int(w * 0.40), ry + rowh / 2, text=fn,
                               fill=C["ok"] if ok else C["dim2"], font=(FMONO, 8),
                               anchor="w", tags=tag)
                if src:
                    cv.create_text(x + w - 6, ry + rowh / 2, text=src,
                                   fill=C["acc"] if src in ("文件", "目录", "自定") else C["dim2"],
                                   font=(FUI, 8), anchor="e", tags=tag)
            if len(rows) > visible:
                bh = max(18, int((h - 20) * visible / len(rows)))
                by = top + int((h - 20 - bh) * off / max(1, len(rows) - visible))
                cv.create_rectangle(x + w - 4, top, x + w - 1, y + h,
                                    fill="#0E1826", outline="", tags=tag)
                cv.create_rectangle(x + w - 4, by, x + w - 1, by + bh,
                                    fill="#31435F", outline="", tags=tag)

        def _step_region(self, x, y, w, h):
            self._geom["r_step"] = dict(x=x, y=y, w=w, h=h, rowh=28)
            self._draw_steps()

        def _draw_steps(self):
            r = self._geom.get("r_step")
            if not r:
                return
            cv = self.cv
            cv.delete("st")
            tag = ("ui", "st")
            x, y, w, h = r["x"], r["y"], r["w"], r["h"]
            steps = self.cur_steps
            rowh = r["rowh"]
            if not steps:
                cv.create_text(x + 14, y + 16, text="选好方案后，这里显示每一步要执行的命令",
                               fill=C["dim2"], font=(FUI, 9), anchor="w", tags=tag)
                return
            cnum, clab, cst = x + 12, x + 46, x + w - 78
            cv.create_text(cnum, y + 6, text="#", fill=C["dim2"], font=(FUI, 8, "bold"), anchor="w", tags=tag)
            cv.create_text(clab, y + 6, text="步骤", fill=C["dim2"], font=(FUI, 8, "bold"), anchor="w", tags=tag)
            cv.create_text(cst, y + 6, text="状态", fill=C["dim2"], font=(FUI, 8, "bold"), anchor="w", tags=tag)
            cv.create_line(x + 10, y + 18, x + w - 10, y + 18, fill=C["line"], tags=tag)
            top = y + 24
            visible = max(1, (h - 24) // rowh)
            off = max(0, min(self.off["step"], max(0, len(steps) - visible)))
            self.off["step"] = off
            n = 0
            for si, s in enumerate(steps):
                if s["state"] in ("ready", "skip"):
                    n += 1
            num = 0
            for i in range(visible):
                idx = off + i
                if idx >= len(steps):
                    break
                s = steps[idx]
                ry = top + i * rowh
                if i % 2 == 1:
                    cv.create_rectangle(x + 6, ry, x + w - 8, ry + rowh - 2,
                                        fill="#0A111C", outline="", tags=tag)
                if s["state"] in ("ready", "skip"):
                    num += 1
                    numtxt = str(num)
                else:
                    numtxt = "·"
                col = {"ready": C["fg2"], "info": "#7E93B4", "skip": C["warn"],
                       "missing": C["err"], "blocked": C["err"]}[s["state"]]
                cv.create_text(cnum, ry + rowh / 2, text=numtxt, fill=C["dim2"],
                               font=(FMONO, 8), anchor="w", tags=tag)
                lab = s["label"]
                labw = 172
                maxl = labw - 14
                while self._measure(lab, (FUI, 9)) > maxl and len(lab) > 4:
                    lab = lab[:-3] + "…"
                cv.create_text(clab, ry + rowh / 2, text=lab, fill=col, font=(FUI, 9),
                               anchor="w", tags=tag)
                disp = s["cmdline"] if s["cmdline"] else s["cmd"]
                if s["reason"]:
                    disp = f"[{s['reason']}] " + disp
                dmax = max(60, (cst - 12) - (clab + labw))
                dtxt = disp
                while self._measure(dtxt, (FMONO, 8)) > dmax and len(dtxt) > 8:
                    dtxt = dtxt[:-4] + "…"
                cv.create_text(clab + labw, ry + rowh / 2, text=dtxt,
                               fill="#6E86A6" if s["state"] in ("ready", "skip") else col,
                               font=(FMONO, 8), anchor="w", tags=tag)
                stt = {"ready": "就绪", "info": "提示", "skip": "可跳过",
                       "missing": "缺文件", "blocked": "⛔拦下"}[s["state"]]
                cv.create_text(cst, ry + rowh / 2, text=stt, fill=col, font=(FUI, 8),
                               anchor="w", tags=tag)
            if len(steps) > visible:
                bh = max(18, int((h - 24) * visible / len(steps)))
                by = top + int((h - 24 - bh) * off / max(1, len(steps) - visible))
                cv.create_rectangle(x + w - 4, top, x + w - 1, y + h,
                                    fill="#0E1826", outline="", tags=tag)
                cv.create_rectangle(x + w - 4, by, x + w - 1, by + bh,
                                    fill="#31435F", outline="", tags=tag)

        # =================================================================
        # 事件路由
        # =================================================================
        def _region_at(self, px, py):
            for key in ("sym", "plan", "file", "step"):
                r = self._geom.get("r_" + key)
                if not r:
                    continue
                if r["x"] <= px <= r["x"] + r["w"] and r["y"] <= py <= r["y"] + r["h"]:
                    return key, r
            return None, None

        def _on_click(self, ev):
            px, py = ev.x, ev.y
            key, r = self._region_at(px, py)
            if not key:
                return
            if key == "step":
                idx = self._idx_at(key, r, px, py)
                if idx is not None:
                    self.copy_step(idx)
                return
            idx = self._idx_at(key, r, px, py)
            if idx is None:
                return
            if key == "sym":
                sid = r["items"][idx][0]
                if sid in self.sel_sym:
                    self.sel_sym.discard(sid)
                else:
                    self.sel_sym.add(sid)
                self.on_symptom_change()
            elif key == "plan":
                self.sel_plan = idx
                self.on_plan_change()

        def _on_dbl(self, ev):
            key, r = self._region_at(ev.x, ev.y)
            if key == "file":
                idx = self._idx_at(key, r, ev.x, ev.y)
                if idx is not None:
                    self.pick_file_for_kind(idx)

        def _idx_at(self, key, r, px, py):
            if key == "file":
                top = r["y"] + 20
                rowh = r["rowh"]
                i = int((py - top) // rowh)
                idx = self.off["file"] + i
                rows = getattr(self, "_file_rows_cache", [])
                return idx if 0 <= idx < len(rows) else None
            if key == "step":
                top = r["y"] + 24
                i = int((py - top) // r["rowh"])
                idx = self.off["step"] + i
                return idx if 0 <= idx < len(self.cur_steps) else None
            rowh = r["rowh"]
            i = int((py - r["y"]) // rowh)
            idx = self.off[key] + i
            return idx if 0 <= idx < len(r["items"]) else None

        def _on_motion(self, ev):
            key, r = self._region_at(ev.x, ev.y)
            if key in ("file", "sym", "plan"):
                idx = self._idx_at(key, r, ev.x, ev.y)
                if idx != self.hover.get(key):
                    self.hover[key] = idx
                    if key == "file":
                        self._draw_files()
                    else:
                        self._draw_list(key)
                self.cv.configure(cursor="hand2" if idx is not None else "")
            elif key == "step":
                self.cv.configure(cursor="hand2")
            else:
                self.cv.configure(cursor="")

        def _hover_clear(self):
            for k in self.hover:
                self.hover[k] = -1
            self._redraw_lists()

        def _redraw_lists(self):
            for k in ("sym", "plan"):
                if "r_" + k in self._geom:
                    self._draw_list(k)
            if "r_file" in self._geom:
                self._draw_files()
            if "r_step" in self._geom:
                self._draw_steps()

        def _on_wheel(self, ev):
            try:
                px, py = self.cv.winfo_pointerxy()
                px -= self.cv.winfo_rootx()
                py -= self.cv.winfo_rooty()
            except Exception:
                return
            key, r = self._region_at(px, py)
            if not key:
                return
            delta = -1 if ev.delta > 0 else 1
            if key == "step":
                total = len(self.cur_steps)
                vis = max(1, (r["h"] - 24) // r["rowh"])
            elif key == "file":
                total = len(getattr(self, "_file_rows_cache", []))
                vis = max(1, (r["h"] - 20) // r["rowh"])
            else:
                total = len(r["items"])
                vis = max(1, r["h"] // r["rowh"])
            mx = max(0, total - vis)
            self.off[key] = max(0, min(mx, self.off[key] + delta))
            if key == "step":
                self._draw_steps()
            elif key == "file":
                self._draw_files()
            else:
                self._draw_list(key)

        # =================================================================
        # 日志
        # =================================================================
        def _place_log(self):
            g = self._geom.get("log")
            if not g:
                return
            x, y, w, h = g
            self.log_widget.place(x=x, y=y, width=w, height=h)

        def _sync_log(self):
            try:
                self.log_widget.delete("1.0", "end")
                for msg, lvl in self.logbuf[-400:]:
                    self.log_widget.insert("end", msg + "\n", lvl)
                self.log_widget.see("end")
            except Exception:
                pass

        def logln(self, msg, level="info"):
            self.logbuf.append((msg, level))
            if len(self.logbuf) > 2000:
                del self.logbuf[:500]
            try:
                self.log_widget.insert("end", msg + "\n", level)
                self.log_widget.see("end")
            except Exception:
                pass

        def clear_log(self):
            self.logbuf.clear()
            try:
                self.log_widget.delete("1.0", "end")
            except Exception:
                pass

        def set_progress(self, text):
            self.prog = text
            self._paint()

        # =================================================================
        # 状态与动作
        # =================================================================
        def toggle_keep(self):
            self.keep_var.set(not self.keep_var.get())
            CONFIG["keep_data"] = bool(self.keep_var.get())
            save_config()
            if self.keep_var.get():
                self.logln("🔒 保留资料模式已开启：任何清空 userdata / metadata / cache 的操作都会被拦下", "ok")
            else:
                self.logln("已关闭保留资料模式", "warn")
            self._paint()
            self.on_symptom_change(log=False)

        def toggle_auto(self):
            self.auto_ref.set(not self.auto_ref.get())
            self._paint()

        def choose_log_dir(self):
            d = filedialog.askdirectory(title="选择日志保存目录", initialdir=str(log_dir()))
            if d:
                CONFIG["log_dir"] = d
                save_config()
                self.logln(f"日志目录改为: {d}", "ok")

        def open_log_dir(self):
            try:
                os.startfile(str(log_dir()))
            except Exception as e:
                self.logln(f"打开失败: {e}", "warn")

        def pick_platform_tools(self):
            d = filedialog.askdirectory(title="选择 platform-tools 目录（含 adb.exe / fastboot.exe）")
            if d:
                CONFIG["platform_tools"] = d
                save_config()
                resolve_tools()
                self.logln(f"platform-tools: {d}", "ok")
                self.refresh_device()

        def refresh_device(self, async_run=False):
            def work():
                d = detect_device()
                self.after(0, lambda: self._apply_dev(d))
            threading.Thread(target=work, daemon=True).start()

        def _apply_dev(self, d):
            self.dev = d
            if self.cur_plan:
                self.render_plan()
            else:
                self._paint()

        def _tick_auto(self):
            try:
                if self.auto_ref.get() and not (self.runner and getattr(self.runner, "thread", None)
                                                and self.runner.thread.is_alive()):
                    self.refresh_device(True)
            except Exception:
                pass
            self.after(5000, self._tick_auto)

        def rescan_library(self):
            self.lib = scan_library()
            if "r_file" in self._geom:
                self._draw_files()

        def open_lib_folder(self):
            ensure_dirs()
            try:
                os.startfile(str(lib_dir()))
            except Exception as e:
                self.logln(f"打开目录失败: {e}", "err")

        def clear_imports(self):
            self.entries = [e for e in self.entries if e["src"] == "文件库"]
            self.override.clear()
            self.script = None
            self.bundle_dir = None
            clear_probe()
            self.logln("已清空手动导入的文件与脚本", "dim")
            self._paint()
            self.render_plan()

        def _add_paths(self, paths, src):
            added = 0
            for p in paths:
                p = Path(p)
                if not p.is_file():
                    continue
                if p.suffix.lower() in (".bat", ".sh"):
                    continue
                part = part_of_file(p)
                if not part:
                    continue
                if any(e["path"] == p for e in self.entries):
                    continue
                self.entries.append(dict(part=part, path=p, kind=kind_of_part(part), src=src))
                added += 1
            return added

        def import_files(self):
            ensure_dirs()
            paths = filedialog.askopenfilenames(
                title="选择镜像（可多选：boot.img / super.img / 全量包）",
                initialdir=str(self.bundle_dir or lib_dir()),
                filetypes=[("镜像 / 刷机包", "*.img *.bin *.mbn *.elf *.zip *.tgz *.tar *.md5"),
                           ("全部文件", "*.*")])
            if not paths:
                return
            n = self._add_paths([Path(p) for p in paths], "文件")
            self.logln(f"已添加 {n} 个文件（共 {len(self.entries)} 个）", "ok")
            if self.script is None:
                self._detect_script(Path(paths[0]).parent)
            self._paint()
            self.render_plan()

        def import_folder(self):
            ensure_dirs()
            d = filedialog.askdirectory(
                title="选择目录（整份字库备份 / 全分区 dump / 官方线刷包）",
                initialdir=str(self.bundle_dir or lib_dir()))
            if not d:
                return
            root = Path(d)
            self.bundle_dir = root
            try:
                files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in IMG_EXT]
            except Exception as e:
                self.logln(f"扫描目录失败: {e}", "err")
                return
            if len(files) > 600:
                files = files[:600]
                self.logln("镜像太多，只取前 600 个", "warn")
            n = self._add_paths(files, "目录")
            self._detect_script(root)
            self.logln(f"从 {root} 导入 {n} 个镜像（共 {len(self.entries)} 个）", "ok")
            if n == 0:
                self.logln("目录里没找到 .img/.bin/.mbn/.zip", "warn")
            self._paint()
            self.render_plan()

        def _detect_script(self, root):
            names = ("flash_all.bat", "flash_all.sh", "flash-all.bat", "flash-all.sh",
                     "flash_all_except_storage.bat", "flash_all_except_storage.sh")
            if keep_data():
                names = ("flash_all_except_storage.bat", "flash_all_except_storage.sh") + names
            for nm in names:
                cand = Path(root) / nm
                if cand.is_file():
                    self.script = cand
                    self.logln(f"检测到官方脚本 {nm}", "ok")
                    return True
            return False

        def batch_entries(self):
            out = []
            for e in self.entries:
                if e["path"].suffix.lower() not in (".img", ".bin", ".mbn", ".elf"):
                    continue
                if e["kind"] == "firehose" or e.get("custom"):
                    continue
                out.append(e)
            return out

        def clear_override(self):
            self.override.clear()
            self._paint()
            self.render_plan()

        def current_files(self):
            out = {k: None for k, _, _ in KINDS}
            for e in self.entries:
                if e["kind"]:
                    out[e["kind"]] = e["path"]
            for k, _, _ in KINDS:
                if self.lib.get(k) and out.get(k) is None:
                    out[k] = self.lib[k][0]
            for k, p in self.override.items():
                if p and Path(p).is_file():
                    out[k] = Path(p)
            return out

        def _src_of(self, kind):
            for e in reversed(self.entries):
                if e["kind"] == kind:
                    return e["src"]
            if self.lib.get(kind):
                return "文件库"
            return ""

        def pick_file_for_kind(self, idx):
            rows = getattr(self, "_file_rows_cache", [])
            if idx >= len(rows):
                return
            iid, kind, fname, src, ok = rows[idx]
            if iid.startswith("x:"):
                ei = int(iid.split(":")[1])
                extras = [e for e in self.entries if not e["kind"]]
                if ei >= len(extras):
                    return
                ent = extras[ei]
                name = simpledialog.askstring(
                    "改分区名", f"文件 {ent['path'].name}\n刷到哪个分区？",
                    initialvalue=ent["part"], parent=self)
                if name:
                    ent["part"] = name.strip()
                    ent["kind"] = kind_of_part(ent["part"])
                    self._paint()
                    self.render_plan()
                return
            k = iid.split(":", 1)[1]
            p = filedialog.askopenfilename(title=f"为 {KIND_NAME.get(k, k)} 指定文件",
                                           initialdir=str(self.bundle_dir or lib_dir()))
            if p:
                self.override[k] = p
                self._paint()
                self.render_plan()

        # -- 症状 / 方案 ---------------------------------------------------
        def on_symptom_change(self, log=True):
            ids = [s for s in self.sel_sym]
            self._plans = match_plans(ids)
            self.sel_plan = 0
            self.off["plan"] = 0
            if self._plans:
                self.cur_plan = self._plans[0]
            else:
                self.cur_plan = None
                self.cur_steps = []
            self._paint()
            self.render_plan()

        def on_plan_change(self):
            if 0 <= self.sel_plan < len(self._plans):
                self.cur_plan = self._plans[self.sel_plan]
                self.off["step"] = 0
                self.render_plan()

        def select_plan_pid(self, pid):
            for i, p in enumerate(self._plans):
                if p["pid"] == pid:
                    self.sel_plan = i
                    self.on_plan_change()
                    self._paint()
                    return True
            return False

        def render_plan(self):
            p = self.cur_plan
            if not p:
                self.cur_steps = []
                self._paint()
                return
            keep = keep_data()
            self.ctx = make_ctx(self.dev, self.current_files(),
                               entries=self.batch_entries(), script=self.script)
            self.cur_steps = compile_plan(p, self.ctx, keep)
            self._paint()

        # -- 按钮动作 -----------------------------------------------------
        def custom_flash(self):
            self._dialog_custom()

        def driver_panel(self):
            self._dialog_driver()

        def copy_cmds(self):
            p, steps = self.cur_plan, self.cur_steps
            if not p or not steps:
                return
            lines = [f":: {p['title']}"]
            for s in steps:
                if s["state"] == "info":
                    lines.append(f":: {s['cmd']}")
                elif s["state"] == "blocked":
                    lines.append(f":: [保留资料已屏蔽] {s['label']}")
                elif s["cmdline"]:
                    lines.append(s["cmdline"])
            self.clipboard_clear()
            self.clipboard_append("\n".join(lines))
            self.logln(f"已复制 {len(lines)} 行命令", "ok")

        def copy_step(self, idx):
            if 0 <= idx < len(self.cur_steps):
                s = self.cur_steps[idx]
                txt = s["cmdline"] or s["cmd"]
                self.clipboard_clear()
                self.clipboard_append(txt)
                self.logln("已复制: " + txt, "cmd")

        def export_bat(self):
            p, steps = self.cur_plan, self.cur_steps
            if not p or not steps:
                return
            dst = filedialog.asksaveasfilename(
                title="导出为 .bat", defaultextension=".bat", initialdir=str(OUT_DIR),
                initialfile=f"{p['pid']}_{datetime.now():%Y%m%d_%H%M}.bat",
                filetypes=[("批处理", "*.bat")])
            if not dst:
                return
            body = ["@echo off", "chcp 65001 >nul", "setlocal",
                    f"title {bat_escape(p['pid'] + ' ' + p['title'])}",
                    f"echo ===== {APP_NAME} v{VERSION} =====",
                    f"echo 方案: {bat_escape(p['title'])}", "echo.", ""]
            for s in steps:
                if s["state"] == "info":
                    body.append(f"echo [提示] {bat_escape(s['cmd'])}")
                    continue
                if s["state"] in ("skip", "blocked"):
                    body.append(f"echo [跳过] {bat_escape(s['label'])}")
                    continue
                if not s["cmdline"]:
                    continue
                body += ["echo.", f"echo [ ] {bat_escape(s['label'])}",
                         f"echo $ {bat_escape(s['cmdline'])}"]
                body.append(s["cmdline"] + (" || echo 可选步骤失败" if s.get("opt")
                                            else " || (echo 失败 & pause & exit /b 1)"))
            body += ["", "echo.", "echo 完成", "pause"]
            Path(dst).write_text("\n".join(body), encoding="utf-8")
            self.logln(f"已导出: {dst}", "ok")

        def export_report(self):
            p, steps = self.cur_plan, self.cur_steps
            if not p:
                return
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            dst = OUT_DIR / f"plan_{p['pid']}.md"
            L = [f"# {p['title']}", "", f"- 编号: {p['pid']}", f"- 风险: {p['risk']}",
                 f"- 前置条件: {p['pre']}", "", "## 说明", p["desc"], "", "## 步骤", ""]
            for i, s in enumerate(steps, 1):
                L.append(f"{i}. **{s['label']}** [{s['state']}]")
                if s["cmdline"]:
                    L += ["   ```", "   " + s["cmdline"], "   ```"]
                elif s["cmd"]:
                    L.append(f"   > {s['cmd']}")
            dst.write_text("\n".join(L), encoding="utf-8")
            self.logln(f"已导出报告: {dst}", "ok")

        def run_backup_plan(self):
            self.select_plan_pid("P17") or self._force_plan("P17")

        def run_diag_plan(self):
            self.select_plan_pid("P18") or self._force_plan("P18")

        def _force_plan(self, pid):
            self._plans = match_plans([s for s in self.sel_sym])
            self.select_plan_pid(pid)

        def start_run(self):
            p, steps = self.cur_plan, self.cur_steps
            if not p:
                messagebox.showinfo(APP_NAME, "先选一个症状，再选方案。")
                return
            if self.runner and getattr(self.runner, "thread", None) and self.runner.thread.is_alive():
                return
            keep = keep_data()
            block, warn = Runner(lambda m, l: None, keep=keep).preflight(p, steps, self.dev)
            if block:
                messagebox.showerror("预检未通过", "\n".join(block))
                for b in block:
                    self.logln("[阻断] " + b, "err")
                return
            if keep and plan_wipes(p, steps) and p.get("wipes"):
                messagebox.showerror("保留资料模式",
                                     f"『{p['title']}』会清空用户数据。\n请先取消顶部『保留资料』。")
                return
            msg = f"方案：{p['title']}\n风险：{p['risk']}\n\n"
            if keep:
                msg += "🔒 保留资料：清数据步骤会被自动屏蔽\n\n"
            if warn:
                msg += "注意事项：\n" + "\n".join("• " + w for w in warn) + "\n\n"
            msg += "确认开始执行？"
            if not messagebox.askyesno("确认执行", msg):
                return
            if p["risk"].startswith("高") and not keep:
                if not messagebox.askyesno("二次确认（高风险）", "该方案会清空数据且不可撤销。继续？"):
                    return
            self.clear_log()
            self.logln(f"开始执行 {p['pid']} {p['title']}", "ok")

            def on_log(m, level):
                self.after(0, lambda: self.logln(m, level))

            def on_done(ok, logfile):
                def fin():
                    self.set_progress("")
                    if ok:
                        messagebox.showinfo(APP_NAME, f"执行完成。\n日志：{logfile}")
                    else:
                        messagebox.showwarning(APP_NAME, f"有失败或未完成步骤，看日志。\n{logfile}")
                self.after(0, fin)

            self.runner = Runner(on_log, on_done, keep=keep,
                                 on_plan_hint=lambda pid: self.after(0, lambda: self.select_plan_pid(pid)))
            self.set_progress("● 执行中…")
            self.runner.thread = threading.Thread(
                target=self.runner.run,
                args=(p, steps, dict(self.dev), self.ctx), daemon=True)
            self.runner.thread.start()
            self._paint()

        def stop_run(self):
            if self.runner:
                self.runner.abort()
                self.logln("已请求中止…", "warn")

        # =================================================================
        # 对话框（同样走玻璃风格）
        # =================================================================
        def _toplevel(self, title, w, h):
            win = tk.Toplevel(self)
            win.title(title)
            win.configure(bg=C["bg1"])
            win.transient(self)
            win.grab_set()
            win.resizable(False, False)
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            win.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 3)}")
            return win

        def _dialog_custom(self):
            win = self._toplevel("自定义刷写", 680, 452)
            fpath = tk.StringVar()
            fpart = tk.StringVar(value="boot")
            fslot = tk.StringVar(value="auto")
            fver = tk.BooleanVar(value=False)
            tk.Label(win, text="自定义刷写", bg=C["bg1"], fg=C["fg"],
                     font=(FUI, 15, "bold")).place(x=24, y=20)
            tk.Label(win, text="填错分区名＝真砖 · 刷前建议先跑『全字库备份』",
                     bg=C["bg1"], fg=C["warn"], font=(FUI, 8)).place(x=24, y=52)

            def lab(y, t):
                tk.Label(win, text=t, bg=C["bg1"], fg=C["dim"],
                         font=(FUI, 9)).place(x=24, y=y)

            lab(92, "镜像文件")
            tk.Entry(win, textvariable=fpath, bg=C["panel2"], fg=C["fg"], bd=0,
                     insertbackground=C["fg"], font=(FMONO, 9)).place(
                x=124, y=88, width=420, height=30)

            def browse():
                p = filedialog.askopenfilename(
                    title="选择要刷入的镜像", initialdir=str(self.bundle_dir or lib_dir()),
                    filetypes=[("镜像", "*.img *.bin *.mbn *.elf"), ("全部", "*.*")])
                if p:
                    fpath.set(p)
                    g = part_of_file(Path(p))
                    if g:
                        fpart.set(g)

            tk.Button(win, text="浏览…", command=browse, bg=C["panel2"], fg=C["fg"], bd=0,
                      padx=10, pady=2, cursor="hand2",
                      activebackground="#2A3547").place(x=552, y=86, width=96, height=34)

            lab(140, "分区名")
            from tkinter import ttk as _ttk
            stl = _ttk.Style(win)
            stl.configure("G.TCombobox", fieldbackground=C["panel2"], background=C["panel2"],
                          foreground=C["fg"], arrowcolor=C["fg"], bordercolor=C["line"],
                          selectbackground=C["panel2"], selectforeground=C["fg"],
                          insertcolor=C["fg"], padding=5)
            stl.map("G.TCombobox", fieldbackground=[("readonly", C["panel2"])],
                    foreground=[("readonly", C["fg"])])
            cb = _ttk.Combobox(win, textvariable=fpart, values=COMMON_PARTS, width=28,
                               style="G.TCombobox")
            cb.place(x=124, y=136, width=240, height=30)

            lab(188, "槽位")
            for i, (txt, val) in enumerate((("自动（按设备实测）", "auto"), ("无槽位", "none"),
                                            ("_a", "a"), ("_b", "b"))):
                tk.Radiobutton(win, text=txt, variable=fslot, value=val, bg=C["bg1"],
                               fg=C["fg"], selectcolor=C["panel2"], activebackground=C["bg1"],
                               activeforeground=C["fg"], bd=0, highlightthickness=0,
                               font=(FUI, 9)).place(x=124 + i * 138, y=186)

            tk.Checkbutton(win, text="顺带关闭 vbmeta 校验（--disable-verity --disable-verification）",
                           variable=fver, bg=C["bg1"], fg=C["fg"], selectcolor=C["panel2"],
                           activebackground=C["bg1"], activeforeground=C["fg"], bd=0,
                           highlightthickness=0, font=(FUI, 9)).place(x=124, y=222)

            prev = tk.Label(win, text="", bg=C["log"], fg=C["ok"], anchor="w", justify="left",
                            font=(FMONO, 9), padx=12, pady=10)
            prev.place(x=24, y=258, width=632, height=56)

            def refresh_prev(*_):
                extra = "--disable-verity --disable-verification " if fver.get() else ""
                part = (fpart.get() or "boot").strip()
                s = fslot.get()
                name = f"{part}_{s}" if s in ("a", "b") else part
                p = fpath.get().strip()
                prev.config(text=f"fastboot {extra}flash {name} {p or '<未选择文件>'}")

            for v in (fpath, fpart, fslot, fver):
                v.trace_add("write", refresh_prev)
            refresh_prev()

            def do_add():
                p = Path(fpath.get().strip().strip('"'))
                if not p.is_file():
                    messagebox.showwarning("没选文件", "先选一个存在的镜像", parent=win)
                    return
                part = (fpart.get() or "").strip()
                if not part or " " in part:
                    messagebox.showwarning("分区名无效", "分区名不能为空、不能带空格", parent=win)
                    return
                extra = "--disable-verity --disable-verification" if fver.get() else ""
                self.entries.append(dict(part=part, path=p, kind=None, src="自定义",
                                         custom=True, slot=fslot.get(), extra=extra))
                self.logln(f"已加入：{part} ← {p.name}", "ok")
                self._paint()
                self.select_plan_pid("P16")
                win.destroy()

            def mk(x, w, text, fn, primary):
                tk.Button(win, text=text, command=fn,
                          bg=C["acc"] if primary else C["panel2"],
                          fg="#fff" if primary else C["fg"], bd=0, padx=14, pady=8,
                          cursor="hand2", font=(FUI, 9, "bold") if primary else (FUI, 9),
                          activebackground=C["acc_d"] if primary else "#2A3547").place(
                    x=x, y=332, width=w, height=38)

            mk(24, 300, "加入并切到 自定义刷写", do_add, True)
            mk(340, 100, "取消", win.destroy, False)
            tk.Label(win, text="动态分区（super/system/vendor）会自动切 FastbootD 再刷",
                     bg=C["bg1"], fg=C["dim2"], font=(FUI, 8)).place(x=24, y=386)

        def _dialog_driver(self):
            win = self._toplevel("驱动与设备识别", 860, 580)
            tk.Label(win, text="驱动与设备识别", bg=C["bg1"], fg=C["fg"],
                     font=(FUI, 15, "bold")).place(x=22, y=18)
            tk.Label(win, text="fastboot 连不上 / 设备管理器有黄色感叹号时，先在这里看一眼",
                     bg=C["bg1"], fg=C["dim"], font=(FUI, 8)).place(x=22, y=50)
            txt = tk.Text(win, bg=C["log"], fg=C["fg"], bd=0, insertbackground=C["fg"],
                          font=(FMONO, 9), wrap="none", padx=12, pady=10)
            txt.place(x=22, y=78, width=816, height=420)
            sb = tk.Scrollbar(win, command=txt.yview, bd=0, width=10, bg=C["panel2"],
                              troughcolor=C["bg1"], highlightthickness=0)
            sb.place(x=838, y=78, height=420)
            txt.configure(yscrollcommand=sb.set)

            def say(s, clear=False):
                if clear:
                    txt.delete("1.0", "end")
                txt.insert("end", s + "\n")
                txt.see("end")
                win.update_idletasks()

            PS_LIST = ("Get-PnpDevice -PresentOnly | Where-Object {"
                       " $_.FriendlyName -match 'Android|ADB|Fastboot|QDLoader|Qualcomm|Google|Bootloader|MTP'"
                       " -or $_.InstanceId -match 'VID_18D1|VID_05C6|VID_0E8D|VID_2717|VID_2A70'"
                       " } | Select-Object Status,Class,FriendlyName | Format-Table -AutoSize | Out-String -Width 160")

            def scan():
                say("正在扫描设备 …", clear=True)
                win.update()
                rc, out = run_capture(ps_cmd(PS_LIST), timeout=40)
                body = (out or "").strip()
                if not body:
                    say("没有发现 Android / ADB / QDLoader 相关设备。")
                    say("")
                    say("fastboot 连不上时常见原因：")
                    say("  · 数据线是充电线（换原装线）")
                    say("  · USB 口是前置，换主板后置或 USB 2.0 口")
                    say("  · 手机没在 bootloader 界面")
                    say("  · 驱动没装 → 点下面『安装 Google USB Driver』")
                else:
                    say(body)
                rc, out2 = run_capture(
                    ps_cmd("Get-PnpDevice -PresentOnly | Where-Object { $_.Status -ne 'OK' } "
                           "| Select-Object Status,Class,FriendlyName "
                           "| Format-Table -AutoSize | Out-String -Width 160"), timeout=40)
                bad = (out2 or "").strip()
                if bad and len(bad.splitlines()) > 3:
                    say("")
                    say("【状态异常的设备】")
                    say(bad)

            def install_google():
                import urllib.request
                import tempfile
                import zipfile
                say("")
                say("开始下载 Google USB Driver（约 8.6MB）…")
                win.update()
                tmp = Path(tempfile.gettempdir()) / "google_usb_driver.zip"
                try:
                    urllib.request.urlretrieve(USB_DRIVER_URL, str(tmp))
                except Exception as e:
                    say(f"下载失败：{e}")
                    say(f"可手动下载：{USB_DRIVER_URL}")
                    return
                outdir = ROOT / "drivers" / "google_usb"
                outdir.mkdir(parents=True, exist_ok=True)
                try:
                    with zipfile.ZipFile(tmp) as z:
                        z.extractall(outdir)
                except Exception as e:
                    say(f"解压失败：{e}")
                    return
                infs = list(outdir.rglob("*.inf"))
                if not infs:
                    say("压缩包里没找到 .inf")
                    return
                say(f"找到驱动 {len(infs)} 个，开始安装（会弹 UAC）…")
                for inf in infs:
                    rc, o = elevated_install(inf)
                    say(f"  {inf.name}: {'OK' if rc == 0 else '失败'}")
                say("")
                say("完成。拔插数据线，再点『重新扫描』。")
                scan()

            def install_inf():
                p = filedialog.askopenfilename(title="选择驱动 .inf",
                                               filetypes=[("驱动信息", "*.inf"), ("全部", "*.*")])
                if p:
                    rc, o = elevated_install(Path(p))
                    say("OK" if rc == 0 else (o or "失败"))
                    scan()

            def rescan_hw():
                say("让 Windows 重新扫描硬件 …")
                run_capture(ps_cmd("Start-Process -Wait -Verb RunAs 'pnputil.exe' "
                                   "-ArgumentList '/scan-devices'"), timeout=60)
                scan()

            def devmgmt():
                run_capture(ps_cmd("Start-Process devmgmt.msc"), timeout=15)

            def restart_adb():
                if not TOOLS.get("adb"):
                    say("没找到 adb")
                    return
                run_capture([tool("adb"), "kill-server"], timeout=15)
                rc, out = run_capture([tool("adb"), "start-server"], timeout=30)
                say((out or "adb 已重启").strip())
                self.refresh_device(True)

            bx = 22
            for lbl, fn, pr in (("重新扫描", scan, True),
                                ("安装 Google USB Driver", install_google, True),
                                ("从 .inf 安装…", install_inf, False),
                                ("重新扫描硬件", rescan_hw, False),
                                ("设备管理器", devmgmt, False),
                                ("重启 adb", restart_adb, False)):
                wpx = self._measure(lbl, (FUI, 9)) + 30
                tk.Button(win, text=lbl, command=fn, bd=0, padx=12, pady=8, cursor="hand2",
                          bg=C["acc"] if pr else C["panel2"],
                          fg="#fff" if pr else C["fg"],
                          activebackground=C["acc_d"] if pr else "#2A3547",
                          font=(FUI, 9)).place(x=bx, y=514, width=wpx, height=36)
                bx += wpx + 8
            scan()

    return UI

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def cli():
    ensure_dirs()
    resolve_tools()
    dev = detect_device()
    print(f"{APP_NAME} v{VERSION}")
    print(f"adb={TOOLS.get('adb') or '未找到'}  fastboot={TOOLS.get('fastboot') or '未找到'}")
    print(f"设备: {STATE_LABEL.get(dev['state'], dev['state'])}  {dev['serial']}  {dev['model']} "
          f"slot={dev['slot']}  unlocked={dev['unlocked']}")
    print()
    print("症状：")
    for i, (sid, name, desc) in enumerate(SYMPTOMS, 1):
        print(f"  {i:>2}. {name}")
    try:
        raw = input("\n输入症状编号（可多个，用空格分隔）: ").strip()
    except (EOFError, KeyboardInterrupt):
        return 1
    ids = []
    for tok in raw.split():
        if tok.isdigit() and 1 <= int(tok) <= len(SYMPTOMS):
            ids.append(SYMPTOMS[int(tok) - 1][0])
    plans = match_plans(ids)
    if not plans:
        print("没有匹配方案")
        return 1
    print("\n匹配方案：")
    for i, p in enumerate(plans, 1):
        print(f"  {i:>2}. [{p['risk']}] {p['title']}")
    try:
        pick = input("\n选择方案编号: ").strip()
        plan = plans[int(pick) - 1]
    except Exception:
        print("输入无效")
        return 1

    files = {}
    lib = scan_library()
    for k, _, _ in KINDS:
        files[k] = lib[k][0] if lib[k] else None
    ctx = make_ctx(dev, files)
    steps = compile_plan(plan, ctx, keep_data())
    print(f"\n=== {plan['title']} ===")
    print(plan["desc"])
    print(f"前置条件: {plan['pre']}\n")
    for i, s in enumerate(steps, 1):
        flag = {"ready": " ", "info": "i", "skip": "-", "missing": "!", "blocked": "×"}[s["state"]]
        print(f"[{flag}] {i}. {s['label']}")
        if s["cmdline"]:
            print(f"      $ {s['cmdline']}")
        elif s["cmd"]:
            print(f"      {s['cmd']}")
        if s["reason"]:
            print(f"      ({s['reason']})")
    try:
        if input("\n执行? (y/N): ").strip().lower() != "y":
            print("已取消")
            return 0
    except (EOFError, KeyboardInterrupt):
        return 1

    r = Runner(lambda m, l="info": print(m))
    ok = r.run(plan, steps, dev)
    return 0 if ok else 2


def list_all():
    print(f"{APP_NAME} v{VERSION}\n")
    print("症状：")
    for sid, name, desc in SYMPTOMS:
        print(f"  {sid:<18} {name}")
        print(f"  {'':<18} {desc}")
    print("\n方案：")
    for p in PLANS:
        print(f"  {p['pid']}  [{p['risk']}] {p['title']}")
        print(f"        症状: {', '.join(p['symptoms']) or '通用'}")
        print(f"        需要: {', '.join(p['needs']) or '无'}")
    return 0


def dry_run(pid):
    ensure_dirs()
    resolve_tools()
    p = PLAN_BY_ID.get(pid)
    if not p:
        print(f"没有方案 {pid}，用 --list 查看")
        return 1
    dev = {"state": "fastboot", "serial": "SAMPLE0000", "model": "SAMPLE_MODEL",
           "slot": "a", "unlocked": "yes", "edl": "", "android": ""}
    lib = scan_library()
    files = {k: (lib[k][0] if lib[k] else None) for k, _, _ in KINDS}
    ctx = make_ctx(dev, files)
    steps = compile_plan(p, ctx, keep_data())
    print(f"=== {p['pid']} {p['title']} ===")
    print(f"风险: {p['risk']}    前置: {p['pre']}")
    print(p["desc"])
    print()
    if keep_data():
        why = plan_wipes(p, steps)
        if why:
            print(f"[保留资料模式] 会拦下：{why}")
            print()
    for i, s in enumerate(steps, 1):
        flag = {"ready": " ", "info": "i", "skip": "-", "missing": "!", "blocked": "×"}[s["state"]]
        print(f"[{flag}] {i:>2}. {s['label']}")
        if s["cmdline"]:
            print(f"       $ {s['cmdline']}")
        elif s["cmd"]:
            print(f"       {s['cmd']}")
        if s["note"]:
            print(f"       注: {s['note']}")
        if s["reason"]:
            print(f"       ({s['reason']})")
    return 0


def selftest():
    fails = []

    def check(cond, msg):
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    print(f"{APP_NAME} v{VERSION} selftest")
    ensure_dirs()
    check(LIB_DIR.is_dir(), "救砖文件库目录已创建")
    for sub in ("boot", "dtbo", "vbmeta", "vendor_boot", "recovery", "super", "fullpackage", "edl"):
        check((LIB_DIR / sub).is_dir(), f"子目录 {sub}/")

    lib = scan_library()
    check(set(lib.keys()) == {k for k, _, _ in KINDS}, "文件库扫描返回全部类型")

    # 渲染测试
    ctx = make_ctx({"slot": "b"}, {"boot": Path("C:/x/boot.img"), "dtbo": None})
    check(part_name("boot", ctx) == "boot_b", "A/B slot 分区名 boot_b")
    check(part_name("super", ctx) == "super", "非 slotted 分区不加后缀")
    r = render("flash {part:boot} {file:boot}", ctx)
    check("boot_b" in r and "boot.img" in r, f"模板渲染: {r}")
    check(render("{otherslot}", ctx) == "a", "otherslot 计算")
    check(split_cmd('a "b c" d') == ["a", "b c", "d"], "命令行切分")

    # 每个方案都能编译
    dev = {"state": "fastboot", "serial": "X", "model": "M", "slot": "a",
           "unlocked": "yes", "edl": "", "android": ""}
    files = {k: None for k, _, _ in KINDS}
    files["boot"] = Path("C:/lib/boot.img")
    ctx = make_ctx(dev, files)
    for p in PLANS:
        try:
            steps = compile_plan(p, ctx, keep_data())
            check(len(steps) > 0, f"{p['pid']} 编译出 {len(steps)} 步")
            check(all("state" in s for s in steps), f"{p['pid']} 步骤状态完整")
        except Exception as e:
            check(False, f"{p['pid']} 编译异常 {e}")

    # 症状匹配
    check(len(match_plans(["bootloop"])) >= 3, "bootloop 匹配到多个方案")
    check(match_plans(["black_9008"])[0]["pid"] == "P07", "9008 首选 P07")
    check(match_plans(["verity_fail"])[0]["pid"] in ("P03", "P01"), "verity 红字命中校验修复")
    check(len(set(SYMPTOM_NAME)) == len(SYMPTOMS), "症状 id 唯一")
    check(len(PLAN_BY_ID) == len(PLANS), "方案 id 唯一")
    for p in PLANS:
        for s in p["symptoms"]:
            if s not in SYMPTOM_NAME:
                check(False, f"{p['pid']} 引用了未知症状 {s}")

    # GUI 模块可用性
    try:
        import tkinter  # noqa
        check(True, "tkinter 可用")
    except Exception as e:
        check(False, f"tkinter 不可用 {e}")

    print()
    if fails:
        print(f"自检失败 {len(fails)} 项")
        return 1
    print("自检全部通过")
    return 0


def _enable_dpi_awareness():
    """让 Tk 按真实像素渲染，避免 125%/150% 缩放下界面发虚"""
    if os.name != "nt":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _force_utf8_console():
    """Windows 控制台默认 GBK，中文输出会乱码，这里强制 UTF-8"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass


def main():
    _force_utf8_console()
    load_config()
    ap = argparse.ArgumentParser(description=f"{APP_NAME} v{VERSION}")
    ap.add_argument("--cli", action="store_true", help="命令行交互模式")
    ap.add_argument("--list", action="store_true", help="列出症状与方案")
    ap.add_argument("--dry-run", metavar="PID", help="只渲染方案命令")
    ap.add_argument("--selftest", action="store_true", help="自检")
    ap.add_argument("--lib", metavar="DIR", help="指定救砖文件库目录")
    ap.add_argument("--keep-data", action="store_true", help="保留资料模式（禁止任何清数据操作）")
    ap.add_argument("--allow-wipe", action="store_true", help="关闭保留资料模式")
    ap.add_argument("--log-dir", metavar="DIR", help="指定日志目录")
    args = ap.parse_args()

    if args.lib:
        CONFIG["library"] = args.lib
        save_config()
    if args.keep_data:
        CONFIG["keep_data"] = True
        save_config()
    if args.allow_wipe:
        CONFIG["keep_data"] = False
        save_config()
    if args.log_dir:
        CONFIG["log_dir"] = args.log_dir
        save_config()
    if args.selftest:
        return selftest()
    if args.list:
        return list_all()
    if args.dry_run:
        return dry_run(args.dry_run)
    if args.cli:
        return cli()

    ensure_dirs()
    resolve_tools()
    _enable_dpi_awareness()
    try:
        App = build_ui()
        app = App()
        app.mainloop()
    except Exception:
        traceback.print_exc()
        print("\n[GUI 启动失败] 可改用命令行模式: python brick_rescue.py --cli")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
