#!/usr/bin/env python3
# ===========================
# HL2DM AUTO MAPS DOWNLOADER by Peter Brev
#
# Cleaned, fixed, and hardened:
# - Fix summary thread count accuracy
# - Fix disk space required calculation (accounts only for files that will be downloaded)
# - Central Config (no accidental global leakage)
# - Safer cancellation; atomic downloads (.part then rename)
# - Better Steam library discovery (libraryfolders.vdf)
# - Optional recursion into subfolders under /maps/
# - Robust size probing (HEAD with fallback); excludes existing files
# - Clear logs & summary written once at the end
# - Concurrent, visible enumeration of FastDL sources (no more "hang" feeling)
# ===========================

import os
import sys
import subprocess
import threading
import multiprocessing
import time
import signal
import bz2
import shutil
import platform
import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Set, Tuple, Optional
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- Dependency bootstrap (quiet, no clutter) ---
required_modules = {
    'requests': 'requests',
    'beautifulsoup4': 'bs4',
    'tqdm': 'tqdm'
}

def ensure_deps():
    missing = []
    for pkg, imp in required_modules.items():
        try:
            __import__(imp)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"[!] Missing modules: {', '.join(missing)}")
        choice = input(f"Install now via pip? (y/n): ").strip().lower()
        if choice != 'y':
            print("[!] Cannot continue without required modules. Exiting.")
            sys.exit(1)
        for pkg in missing:
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])

ensure_deps()
import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

# ---------------- Config & State ----------------

@dataclass
class Config:
    # User choices
    hl2mp_folder: Path = Path()
    download_folder: Path = Path()
    include_filters: List[str] = field(default_factory=list)
    exclude_filters: List[str] = field(default_factory=list)
    skip_size_check: bool = False
    decompress_choice: bool = False
    delete_bz2_choice: bool = False
    max_workers: int = max(1, multiprocessing.cpu_count() // 2)
    recurse_subdirs: bool = False

    # Derived
    start_time: datetime.datetime = field(default_factory=datetime.datetime.now)
    log_file_name: str = ""
    user_agent: str = "hl2mp-auto-maps-downloader/1.1 (+https://github.com/)"
    max_retries: int = 3
    per_request_timeout: int = 30  # you can lower to 15 if mirrors are very slow

@dataclass
class State:
    existing_files: Set[str] = field(default_factory=set)
    downloaded_files: List[str] = field(default_factory=list)
    skipped_files: List[str] = field(default_factory=list)
    failed_downloads: List[str] = field(default_factory=list)
    extracted_files: List[str] = field(default_factory=list)
    failed_extractions: List[str] = field(default_factory=list)
    deleted_bz2_files: List[str] = field(default_factory=list)
    log_entries: List[str] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event)

# ---------------- Utilities ----------------

def log(state: State, msg: str):
    print(msg)
    state.log_entries.append(msg)

def save_log(cfg: Config, state: State):
    if not cfg.log_file_name:
        cfg.log_file_name = f"download_summary_{cfg.start_time.strftime('%Y%m%d_%H%M%S')}.txt"
    with open(cfg.log_file_name, 'w', encoding="utf-8") as f:
        f.write("\n".join(state.log_entries))
    print(f"[i] Summary log saved as: {cfg.log_file_name}")

def format_size(bytes_size: Optional[int]) -> str:
    if bytes_size is None:
        return "unknown"
    size = float(bytes_size)
    for unit in ['B', 'KB', 'MB', 'GB', 'TB', 'PB']:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} EB"

def format_eta(seconds: int) -> str:
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{int(days):02}:{int(hours):02}:{int(minutes):02}:{int(seconds):02}"

def colorize_warning(text: str) -> str:
    return f"\033[1;33;41m{text}\033[0m"

# -------------- Steam path discovery --------------

def find_hl2dm_dir() -> Optional[Path]:
    # Direct known installs
    candidates = []
    sysname = platform.system().lower()

    if sysname == "windows":
        # Common Steam paths
        candidates += [
            Path(os.path.expandvars(r"%ProgramFiles(x86)%\Steam\steamapps\common\Half-Life 2 Deathmatch\hl2mp")),
            Path(os.path.expandvars(r"%ProgramFiles%\Steam\steamapps\common\Half-Life 2 Deathmatch\hl2mp")),
        ]
        # Read libraryfolders.vdf (default steamapps)
        appdata = Path(os.path.expandvars(r"%ProgramFiles(x86)%\Steam\steamapps"))
        candidates += find_in_libraryfolders(appdata)
    else:
        # Linux/macOS default Steam locations
        linux_paths = [
            Path.home() / ".steam/steam/steamapps",
            Path.home() / ".local/share/Steam/steamapps",
        ]
        mac_paths = [
            Path.home() / "Library/Application Support/Steam/steamapps"
        ]
        roots = linux_paths + mac_paths
        for root in roots:
            candidates += find_in_libraryfolders(root)

        # Also check default app dir
        candidates += [
            Path.home() / ".steam/steam/steamapps/common/Half-Life 2 Deathmatch/hl2mp",
            Path.home() / ".local/share/Steam/steamapps/common/Half-Life 2 Deathmatch/hl2mp",
        ]

    for c in candidates:
        if (c / "maps").exists() or (c / "download").exists():
            return c.resolve()

    return None

def find_in_libraryfolders(steamapps_root: Path) -> List[Path]:
    out = []
    try:
        vdf = steamapps_root / "libraryfolders.vdf"
        if not vdf.exists():
            return out
        text = vdf.read_text(encoding="utf-8", errors="ignore")
        # Minimal parse: look for "path" "C:\\SteamLibrary"
        import re
        paths = re.findall(r'"\d+"\s*\{\s*"path"\s*"([^"]+)"', text, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL)
        for p in paths:
            lib = Path(p).expanduser().resolve() / "steamapps" / "common" / "Half-Life 2 Deathmatch" / "hl2mp"
            out.append(lib)
    except Exception:
        pass
    return out

# -------------- FastDL source handling --------------

def load_fastdl_urls(cfg: Config, state: State) -> List[str]:
    urls = []
    sources_file = Path("fastdl_sources.txt")

    if not sources_file.exists():
        sources_file.write_text("# Add one FastDL URL per line\n# Example:\nhttps://fastdl.hl2dm.community/\n", encoding="utf-8")
        log(state, f"[i] Created '{sources_file.name}' with a sample FastDL source.")

    for line in sources_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.endswith("/"):
            line += "/"
        urls.append(line)

    log(state, f"\nLoaded {len(urls)} FastDL URL(s) from {sources_file.name}.")

    # Session extra
    choice = input("Add extra FastDL URLs for this session? (y/n): ").strip().lower()
    if choice == 'q':
        sys.exit(0)
    if choice == 'y':
        while True:
            extra = input("Enter FastDL URL (blank to finish): ").strip()
            if not extra:
                break
            if not extra.endswith("/"):
                extra += "/"
            urls.append(extra)

    if not urls:
        log(state, "[!] No FastDL URLs provided. Exiting.")
        sys.exit(1)

    # Validate
    log(state, "\nValidating FastDL URLs...")
    valid = []
    for u in urls:
        if validate_url(cfg, u):
            valid.append(u)
        else:
            log(state, f"[!] Rejected: {u}")

    if not valid:
        log(state, "[!] No valid FastDL URLs. Exiting.")
        sys.exit(1)

    log(state, f"Validated {len(valid)} FastDL URL(s).")
    return valid

def validate_url(cfg: Config, url: str) -> bool:
    try:
        r = requests.get(url, timeout=cfg.per_request_timeout, allow_redirects=True, headers={"User-Agent": cfg.user_agent})
        if r.status_code >= 400:
            return False
        # Accept either HTML index or direct file
        ctype = r.headers.get("Content-Type", "")
        return ("text/html" in ctype) or ("application/octet-stream" in ctype) or ("application/x-bzip2" in ctype)
    except requests.RequestException:
        return False

# -------------- Existing file scan --------------

def scan_existing_maps(state: State, base_folder: Path):
    log(state, "\nScanning existing map files...")
    for sub in ["maps", "download/maps"]:
        root = base_folder / sub
        if root.exists():
            for p in root.rglob("*"):
                if p.is_file() and p.suffix.lower() in (".bsp", ".bz2"):
                    state.existing_files.add(p.name)
    log(state, f"Found {len(state.existing_files)} existing map file(s).")

# -------------- Map link discovery --------------

def is_dir_link(href: str) -> bool:
    return href.endswith('/')

def same_origin_and_prefix(root: str, child: str) -> bool:
    ru, cu = urlparse(root), urlparse(child)
    if (ru.scheme, ru.netloc) != (cu.scheme, cu.netloc):
        return False
    return cu.path.startswith(ru.path)

def get_map_links_from_index(cfg: Config, base_url: str, recurse: bool, visited: Optional[Set[str]] = None) -> List[str]:
    """
    Parse a typical directory listing for .bsp / .bz2 files.
    If base_url points directly to a file, return it immediately.
    Optionally recurse into subdirectories.
    """
    visited = visited or set()
    out: List[str] = []

    # If the seed is itself a file URL, accept it directly
    name = Path(urlparse(base_url).path).name.lower()
    if name.endswith(".bsp") or name.endswith(".bz2"):
        return [base_url]

    # Avoid re-visiting the same index
    if base_url in visited:
        return out
    visited.add(base_url)

    try:
        r = requests.get(
            base_url,
            timeout=cfg.per_request_timeout,
            headers={"User-Agent": cfg.user_agent},
        )
        r.raise_for_status()
    except Exception:
        return out

    soup = BeautifulSoup(r.text, 'html.parser')
    anchors = soup.find_all('a')
    for a in anchors:
        href = a.get('href')
        if not href:
            continue
        abs_url = urljoin(base_url, href)
        item_name = Path(urlparse(abs_url).path).name

        # Directory?
        if href.endswith('/'):
            if recurse and same_origin_and_prefix(base_url, abs_url):
                out.extend(get_map_links_from_index(cfg, abs_url, recurse, visited))
            continue

        # File?
        low = item_name.lower()
        if low.endswith(".bsp") or low.endswith(".bz2"):
            out.append(abs_url)

    return out

def enumerate_all_links(cfg: Config, state: State, seeds: List[str]) -> List[str]:
    """
    Concurrently enumerate all seed URLs with visible progress.
    """
    results: List[str] = []
    visited: Set[str] = set()

    print("\nEnumerating FastDL sources...")
    with ThreadPoolExecutor(max_workers=min(8, max(2, cfg.max_workers // 2))) as ex:
        futures = {
            ex.submit(get_map_links_from_index, cfg, seed, cfg.recurse_subdirs, visited): seed
            for seed in seeds
        }
        with tqdm(total=len(futures), desc="Indexing sources", unit="src") as bar:
            for fut in as_completed(futures):
                seed = futures[fut]
                try:
                    links = fut.result() or []
                    results.extend(links)
                    print(f"[+] {seed} -> {len(links)} file(s)")
                except Exception as e:
                    print(f"[!] {seed} failed: {e}")
                bar.update(1)

    # De-dup while preserving order
    results = list(dict.fromkeys(results))
    print(f"[i] Total files discovered: {len(results)}")
    return results

# -------------- Filtering --------------

def apply_filters(state: State, links: List[str], include_terms: List[str], exclude_terms: List[str]) -> List[str]:
    include_lower = [t.lower() for t in include_terms]
    exclude_lower = [t.lower() for t in exclude_terms]
    filtered: List[str] = []
    matched_includes = 0
    matched_excludes = 0

    for url in links:
        name = Path(urlparse(url).path).name.lower()
        if include_lower and not any(term in name for term in include_lower):
            continue
        matched_includes += 1
        if exclude_lower and any(term in name for term in exclude_lower):
            matched_excludes += 1
            continue
        filtered.append(url)

    log(state, "\nFilter summary:")
    log(state, f"Included matches: {matched_includes if include_lower else '(all maps)'}")
    log(state, f"Excluded matches: {matched_excludes if exclude_lower else '(none)'}")
    log(state, f"Final map count after filtering: {len(filtered)}\n")
    return filtered

# -------------- Size probing & disk checks --------------

def head_size(cfg: Config, url: str) -> Optional[int]:
    # Try HEAD; fallback to GET with stream (without downloading body fully)
    try:
        h = requests.head(url, timeout=cfg.per_request_timeout, allow_redirects=True, headers={"User-Agent": cfg.user_agent})
        if h.status_code < 400:
            cl = h.headers.get("Content-Length")
            if cl is not None and cl.isdigit():
                return int(cl)
    except requests.RequestException:
        pass
    # Fallback quick GET to peek at length if server omits HEAD info
    try:
        g = requests.get(url, timeout=cfg.per_request_timeout, stream=True, headers={"User-Agent": cfg.user_agent})
        if g.status_code < 400:
            cl = g.headers.get("Content-Length")
            if cl and cl.isdigit():
                return int(cl)
    except requests.RequestException:
        pass
    return None

def calculate_total_download_size(cfg: Config, state: State, links: List[str]) -> Tuple[int, int]:
    """
    Returns (total_bytes, unknown_count) for files that are NOT present locally.
    """
    if cfg.skip_size_check:
        log(state, "\nSkipping total download size calculation (per user choice).")
        return 0, 0

    log(state, "\nCalculating total download size (skipping files already present)...")
    total = 0
    unknown = 0
    to_probe = [u for u in links if Path(urlparse(u).path).name not in state.existing_files]
    with tqdm(total=len(to_probe), desc="Checking file sizes", unit="file") as bar:
        for u in to_probe:
            sz = head_size(cfg, u)
            if sz is None:
                unknown += 1
            else:
                total += sz
            bar.update(1)
    log(state, f"Total download size (known): {format_size(total)} ({unknown} file(s) unknown size)")
    return total, unknown

def disk_space_warning(state: State, path: Path, required_space: int):
    try:
        total, used, free = shutil.disk_usage(path)
        log(state, f"Disk space available: {format_size(free)}")
        log(state, f"Required (known) to download: {format_size(required_space)}")
        if required_space > 0 and free < required_space:
            log(state, f"[!] ERROR: Not enough free space! Required: {format_size(required_space)}, Available: {format_size(free)}")
            return True
        elif free < 100 * (1024 ** 3):
            log(state, f"[!] WARNING: Less than 100 GB free remains: {format_size(free)}")
            return False
        return False
    except Exception as e:
        log(state, f"[!] Disk space check failed: {e}")
        return False

def confirm_large_download(state: State, map_count: int, total_size_bytes: int):
    warns = []
    if map_count >= 100:
        warns.append(f"You are about to download {map_count} maps.")
    if total_size_bytes >= 10 * (1024 ** 3):
        warns.append(f"Total (known) size exceeds 10 GB: {format_size(total_size_bytes)}.")
    if warns:
        log(state, "\nWARNING:")
        for w in warns:
            log(state, f"- {w}")
        choice = input("Do you wish to continue? (y/n): ").strip().lower()
        if choice != 'y':
            sys.exit("Aborted by user.")

# -------------- Downloading --------------

def download_one(cfg: Config, state: State, url: str, out_dir: Path, total_bar: tqdm):
    name = Path(urlparse(url).path).name
    dest = out_dir / name
    tmp = out_dir / (name + ".part")

    if name in state.existing_files:
        state.skipped_files.append(name)
        total_bar.update(1)
        return

    attempt = 0
    while attempt < cfg.max_retries and not state.cancel_event.is_set():
        attempt += 1
        try:
            with requests.get(url, stream=True, timeout=cfg.per_request_timeout, headers={"User-Agent": cfg.user_agent}) as r:
                r.raise_for_status()
                total_size = r.headers.get('Content-Length')
                total_size = int(total_size) if total_size and total_size.isdigit() else None

                out_dir.mkdir(parents=True, exist_ok=True)
                with open(tmp, "wb") as f, tqdm(
                    desc=name,
                    total=total_size,
                    unit='B',
                    unit_scale=True,
                    unit_divisor=1024,
                    leave=False
                ) as file_bar:
                    for chunk in r.iter_content(chunk_size=8192):
                        if state.cancel_event.is_set():
                            return
                        if chunk:
                            f.write(chunk)
                            file_bar.update(len(chunk))

            # Atomic finalize
            os.replace(tmp, dest)

            # If server gave size, verify
            if total_size is not None and dest.stat().st_size != total_size:
                raise IOError(f"Incomplete download (size mismatch)")

            state.downloaded_files.append(name)
            total_bar.update(1)
            return

        except Exception as e:
            # Clean temp file on failure
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            if attempt >= cfg.max_retries:
                state.failed_downloads.append(name)
            else:
                log(state, f"[Retry {attempt}/{cfg.max_retries}] {name}: {e}")

# -------------- Decompression --------------

def extract_bz2_one(cfg: Config, state: State, bz2_file: Path, bar: tqdm) -> bool:
    out_file = bz2_file.with_suffix("")  # strip .bz2
    attempt = 0
    while attempt < cfg.max_retries:
        attempt += 1
        try:
            with bz2.BZ2File(bz2_file, 'rb') as fr, open(out_file, 'wb') as fw:
                shutil.copyfileobj(fr, fw)
            state.extracted_files.append(out_file.name)
            bar.update(1)
            return True
        except Exception as e:
            if attempt >= cfg.max_retries:
                state.failed_extractions.append(bz2_file.name)
                bar.update(1)
                return False

# -------------- Summary --------------

def print_summary(cfg: Config, state: State):
    log(state, "\n========= SUMMARY =========")
    log(state, f"Downloaded successfully: {len(state.downloaded_files)}")
    log(state, f"Skipped (already exists): {len(state.skipped_files)}")
    log(state, f"Failed downloads: {len(state.failed_downloads)}")
    log(state, f"Extracted .bz2 files: {len(state.extracted_files)}")
    log(state, f"Failed extractions: {len(state.failed_extractions)}")
    log(state, f"Deleted .bz2 files: {len(state.deleted_bz2_files)}")
    log(state, f"CPU threads used: {cfg.max_workers}")
    log(state, "==========================")

# -------------- Cancel handling --------------

def listen_for_cancel(state: State):
    try:
        input("\nPress [Enter] at any time to cancel...\n")
        state.cancel_event.set()
    except EOFError:
        pass

def setup_signals(state: State):
    def handler(sig, frame):
        state.cancel_event.set()
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, handler)
        except Exception:
            pass

# ------------------- Main -------------------

def main():
    cfg = Config()
    state = State()
    setup_signals(state)
    cfg.log_file_name = f"download_summary_{cfg.start_time.strftime('%Y%m%d_%H%M%S')}.txt"

    # Locate HL2DM
    found = find_hl2dm_dir()
    if found:
        print(f"Detected HL2DM installation: {found}")
        ch = input("Use this path? (y/n): ").strip().lower()
        if ch == 'q': sys.exit(0)
        if ch == 'y':
            cfg.hl2mp_folder = found
        else:
            cfg.hl2mp_folder = Path(input("Enter full path to your 'hl2mp' folder: ").strip()).expanduser()
    else:
        cfg.hl2mp_folder = Path(input("Enter full path to your 'hl2mp' folder: ").strip()).expanduser()

    if not cfg.hl2mp_folder.exists():
        print("[!] Provided path does not exist. Exiting.")
        return

    if (platform.system().lower() != "windows") and (not os.access(cfg.hl2mp_folder, os.W_OK)):
        print("[!] WARNING: You might not have write permissions for this folder.")

    scan_existing_maps(state, cfg.hl2mp_folder)

    # Ensure download structure
    default_download = cfg.hl2mp_folder / "download" / "maps"
    print(f"\nDefault download path: {default_download}")
    ch = input("Use this path? (y/n): ").strip().lower()
    if ch == 'q': sys.exit(0)
    cfg.download_folder = (default_download if ch == 'y' else Path(input("Enter custom download folder: ").strip()).expanduser())
    cfg.download_folder.mkdir(parents=True, exist_ok=True)

    # Ensure expected subfolders exist
    for p in [cfg.hl2mp_folder / "download", cfg.hl2mp_folder / "download" / "maps"]:
        if not p.exists():
            try:
                p.mkdir(parents=True, exist_ok=True)
                log(state, f"[i] Created missing folder: {p}")
            except Exception as e:
                log(state, f"[!] Failed to create folder {p}: {e}")
                sys.exit(1)

    # Filters
    include_input = input("Enter keywords to include (comma separated, blank = all): ").strip()
    exclude_input = input("Enter keywords to exclude (comma separated, blank = none): ").strip()
    cfg.include_filters = [t.strip() for t in include_input.split(",") if t.strip()]
    cfg.exclude_filters = [t.strip() for t in exclude_input.split(",") if t.strip()]

    # Size check choice
    print(colorize_warning(
        "Python can estimate final download size and check free disk space.\n"
        "[!] WARNING: On very large sets, this can take a while.\n"
        "Choose this if you want a safety check or aren’t sure of your free space.\n"
    ))
    sk = input("Skip total download size checking? (y/n): ").strip().lower()
    if sk == 'q': sys.exit(0)
    cfg.skip_size_check = (sk == 'y')

    # Decompression options
    de = input("Decompress downloaded .bz2 files after download? (y/n): ").strip().lower()
    if de == 'q': sys.exit(0)
    cfg.decompress_choice = (de == 'y')
    if cfg.decompress_choice:
        rm = input("Delete .bz2 files after extraction? (y/n): ").strip().lower()
        if rm == 'q': sys.exit(0)
        cfg.delete_bz2_choice = (rm == 'y')

    # Recursion option
    rc = input("Recurse into subfolders under /maps/? (y/n): ").strip().lower()
    if rc == 'q': sys.exit(0)
    cfg.recurse_subdirs = (rc == 'y')

    # FastDL sources
    urls = load_fastdl_urls(cfg, state)

    # Gather links (visible progress + concurrency)
    all_links = enumerate_all_links(cfg, state, urls)

    filtered_links = apply_filters(state, all_links, cfg.include_filters, cfg.exclude_filters)
    map_count = len(filtered_links)
    if map_count == 0:
        log(state, "No maps to download after filtering.")
        save_log(cfg, state)
        return

    # Size & disk space checks
    total_bytes, unknown_count = calculate_total_download_size(cfg, state, filtered_links)
    # Disk warning uses known bytes only; unknown files are extra caution
    if disk_space_warning(state, cfg.download_folder, total_bytes):
        save_log(cfg, state)
        sys.exit("[!] Insufficient disk space for known sizes. Aborting.")
    confirm_large_download(state, map_count, total_bytes)

    # Threads
    default_threads = cfg.max_workers
    try:
        ut = input(f"Enter number of CPU threads to use (blank for default: {default_threads}): ").strip()
        if ut == 'q':
            sys.exit(0)
        elif ut:
            cfg.max_workers = max(1, int(ut))
    except ValueError:
        print(f"[!] Invalid input, using default: {default_threads} threads.")
        cfg.max_workers = default_threads

    print(f"[i] Using {cfg.max_workers} thread(s) for downloads/decompression).")

    # Cancel listener (Enter) + SIGINT
    threading.Thread(target=listen_for_cancel, args=(state,), daemon=True).start()

    # Downloads
    log(state, "\nStarting downloads...")
    with tqdm(total=map_count, desc="Total Download Progress", unit="file") as total_bar:
        with ThreadPoolExecutor(max_workers=cfg.max_workers) as ex:
            futures = [ex.submit(download_one, cfg, state, url, cfg.download_folder, total_bar) for url in filtered_links]
            try:
                for _ in as_completed(futures):
                    if state.cancel_event.is_set():
                        break
            except KeyboardInterrupt:
                state.cancel_event.set()

    # Decompress if requested
    if cfg.decompress_choice and not state.cancel_event.is_set():
        bz2_files = [p for p in cfg.download_folder.iterdir() if p.is_file() and p.suffix.lower() == ".bz2"]
        if bz2_files:
            log(state, "\nStarting decompression...")
            with tqdm(total=len(bz2_files), desc="Decompression Progress", unit="file") as bar:
                with ThreadPoolExecutor(max_workers=cfg.max_workers) as ex:
                    futures = [ex.submit(extract_bz2_one, cfg, state, f, bar) for f in bz2_files]
                    try:
                        for _ in as_completed(futures):
                            if state.cancel_event.is_set():
                                break
                    except KeyboardInterrupt:
                        state.cancel_event.set()

            if cfg.delete_bz2_choice and not state.cancel_event.is_set():
                for f in bz2_files:
                    try:
                        f.unlink(missing_ok=True)
                        state.deleted_bz2_files.append(f.name)
                    except Exception as e:
                        log(state, f"[!] Failed to delete {f.name}: {e}")

    # Summary
    print_summary(cfg, state)
    save_log(cfg, state)

    try:
        total, used, free = shutil.disk_usage(cfg.download_folder)
        log(state, f"Disk space remaining after process: {format_size(free)}")
    except Exception as e:
        log(state, f"[!] Disk space retrieval failed at end of process: {e}")

    log(state, "\nProcess completed!")

if __name__ == "__main__":
    main()
