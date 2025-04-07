# ===========================
# HL2DM AUTO MAPS DOWNLOADER by Peter Brev
#
# Description:
# This script automates the downloading of all HL2:DM maps from multiple FastDL sources.
# It scans your existing HL2DM 'maps', 'download/maps' folders to skip maps you already have.
# It supports multi-threaded downloading and decompression with per-file and total progress bars and ETA.
#
# Features:
# - Automatic detection of HL2DM installation folder (Windows and Linux Steam libraries).
# - Linux permissions warning if the destination folder is not writable.
# - Scans existing map files to avoid redundant downloads.
# - Multi-FastDL source support: define URLs in the 'fastdl_sources.txt' file.
# - Interactive prompt to add extra FastDL URLs at runtime.
# - Multi-threaded downloads (default: half of your CPU threads) and decompression.
# - Per-file and total progress bars with ETA (formatted DD:HH:MM:SS).
# - Detects incomplete downloads and retries up to 3 times.
# - Option to decompress downloaded .bz2 files.
# - Option to delete .bz2 files after extraction.
# - Early user prompts for all preferences (path, decompression, deletion, filters, threads).
# - Cancel anytime by pressing Enter during the download or decompression.
# - Type 'q' during prompts to exit immediately.
# - Summary at the end showing downloads, skips, decompressions, failures, etc.
# - Final summary automatically logged to a text file.
# - Warnings if downloading 100 or more maps, or over 10 GB total size.
# - Disk space check to warn if not enough space or less than 100 GB would remain.
# - Filtering system: include or exclude specific maps by name keyword (case insensitive).
# - Option to skip download size checking for faster operation on large downloads.
# - URL validation to ensure FastDL sources are reachable.
# - Auto-install dependencies (requests, beautifulsoup4, tqdm).
#
# How to update or customize:
# - Add or remove FastDL URLs in the 'fastdl_sources.txt' file in the script directory.
# - Change the number of threads at runtime prompt. Higher values = faster (network/CPU dependent).
# - Modify 'max_retries' in the script to change how many times the script retries failed downloads.
#
# Requirements:
# - Python 3.6+
# - Modules: requests, beautifulsoup4, tqdm
# - The script checks and installs missing modules automatically.
#
# Usage:
# - Run the script (using a command prompt, PowerShell, or terminal from the directory the file is in): python hl2mp_maps_downloader.py
# - Follow the prompts to configure path, decompression, deletion options, optional map filtering, and thread count.
# - Enjoy automatic downloads, progress tracking, and final summary logs!
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
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed

# Module Check and Install
required_modules = {
    'requests': 'requests',
    'beautifulsoup4': 'bs4',
    'tqdm': 'tqdm'
}

for package_name, import_name in required_modules.items():
    try:
        __import__(import_name)
    except ImportError:
        print(f"[!] Missing module: {package_name}")
        choice = input(f"Do you want to install '{package_name}' now? (y/n): ").strip().lower()
        if choice == 'y':
            subprocess.check_call([sys.executable, "-m", "pip", "install", package_name])
        else:
            print(f"[!] Cannot continue without '{package_name}'. Exiting.")
            sys.exit(1)

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

# Globals

def load_fastdl_urls():
    urls = []

    sources_file = "fastdl_sources.txt"

    # Auto-create file if it doesn't exist
    if not os.path.exists(sources_file):
        with open(sources_file, "w") as f:
            f.write("# Add one FastDL URL per line\n")
            f.write("# Example:\n")
            f.write("https://fastdl.hl2dm.community/\n")
        log(f"[i] Created 'fastdl_sources.txt' with default FastDL source.")

    # Load URLs from file
    with open(sources_file, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                if line.endswith("/"):
                    urls.append(line)
                else:
                    urls.append(line + "/")

    log(f"\nLoaded {len(urls)} FastDL URL(s) from {sources_file}.")

    # Ask user if they want to add more URLs for this session
    choice = input("Would you like to add extra FastDL URLs for this session? (y/n): ").strip().lower()
    if choice == 'y':
        while True:
            extra_url = input("Enter FastDL URL (or leave blank to finish): ").strip()
            if not extra_url:
                break
            if not extra_url.endswith("/"):
                extra_url += "/"
            urls.append(extra_url)

    if not urls:
        log("[!] No FastDL URLs provided. Exiting.")
        sys.exit(1)

    # Validate URLs
    log("\nValidating FastDL URLs, please wait...")
    valid_urls = []
    for url in urls:
        if validate_url(url):
            valid_urls.append(url)

    if not valid_urls:
        log("[!] No valid FastDL URLs found. Exiting.")
        sys.exit(1)

    log(f"Validated {len(valid_urls)} FastDL URL(s). Proceeding with download.")
    return valid_urls

def validate_url(url):
    try:
        response = requests.get(url, timeout=5, stream=True, allow_redirects=True)
        if response.status_code in (200, 301, 302):
            return True
        else:
            log(f"[!] URL check failed: {url} (HTTP {response.status_code})")
            return False
    except requests.RequestException as e:
        log(f"[!] URL check failed: {url} ({e})")
        return False

existing_files = set()
downloaded_files = []
skipped_files = []
failed_downloads = []
extracted_files = []
failed_extractions = []
deleted_bz2_files = []

cpu_threads = multiprocessing.cpu_count()
default_threads = max(1, cpu_threads // 2)
max_workers = 8
max_retries = 3
cancel_event = threading.Event()

skip_size_check = False
decompress_choice = False
delete_bz2_choice = False
include_filters = []
exclude_filters = []
log_entries = []

# Utility functions
def log(message):
    print(message)
    log_entries.append(message)

def save_log(log_file_name):
    with open(log_file_name, 'w') as log_file:
        log_file.write("\n".join(log_entries))

def format_size(bytes_size):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_size < 1024:
            return f"{bytes_size:.2f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.2f} PB"

def format_eta(seconds):
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{int(days):02}:{int(hours):02}:{int(minutes):02}:{int(seconds):02}"

def get_steam_library():
    if sys.platform.startswith('linux'):
        base_path = os.path.expanduser('~/.steam/steam/steamapps/common/Half-Life 2 Deathmatch/hl2mp')
        return base_path if os.path.exists(base_path) else None
    elif sys.platform.startswith('win'):
        possible_paths = [
            os.path.expandvars(r"%ProgramFiles(x86)%\Steam\steamapps\common\Half-Life 2 Deathmatch\hl2mp"),
            os.path.expandvars(r"%ProgramFiles%\Steam\steamapps\common\Half-Life 2 Deathmatch\hl2mp")
        ]
        for path in possible_paths:
            if os.path.exists(path):
                return path
    return None

def disk_space_warning(path, required_space):
    try:
        total, used, free = shutil.disk_usage(path)
        log(f"Disk space available: {format_size(free)}")
        log(f"Total required space for download: {format_size(required_space)}")
        # Don't want to cause space issues
        if free < required_space:
            log(f"[!] ERROR: Not enough free space! Required: {format_size(required_space)}, Available: {format_size(free)}")
            return True
        elif free < 100 * (1024 ** 3):
            log(f"[!] WARNING: You have less than 100GB free space remaining: {format_size(free)}")
            return False  # Warn but do not exit
        return False
    except Exception as e:
        log(f"[!] Disk space check failed: {e}")
        return False

def scan_existing_maps(base_folder):
    log("\nScanning existing map files...")
    folders = ['maps', 'download/maps']
    for folder in folders:
        full_path = os.path.join(base_folder, folder)
        if os.path.exists(full_path):
            for root, _, files in os.walk(full_path):
                for file in files:
                    if file.endswith(('.bsp', '.bz2')):
                        existing_files.add(file)
    log(f"Found {len(existing_files)} existing map files.")

def get_map_links(base_url):
    try:
        response = requests.get(base_url, timeout=10)
        response.raise_for_status()
    except Exception as e:
        log(f"[!] Failed to fetch {base_url}: {e}")
        return []

    soup = BeautifulSoup(response.text, 'html.parser')
    links = []

    for link in soup.find_all('a'):
        href = link.get('href')
        if href and (href.endswith('.bsp') or href.endswith('.bz2')):
            full_url = urljoin(base_url, href)
            links.append(full_url)

    log(f"[+] Found {len(links)} map(s) at {base_url}")
    return links

def apply_filters(map_links):
    filtered = []
    include_lower = [term.lower() for term in include_filters]
    exclude_lower = [term.lower() for term in exclude_filters]

    matched_includes = 0
    matched_excludes = 0

    for link in map_links:
        name = link.split('/')[-1].lower()

        # Include filter check
        if include_lower and not any(term in name for term in include_lower):
            continue
        matched_includes += 1

        # Exclude filter check
        if exclude_lower and any(term in name for term in exclude_lower):
            matched_excludes += 1
            continue

        filtered.append(link)

    log("\nFilter summary:")
    if include_lower:
        log(f"Included matches: {matched_includes}")
    else:
        log("Included matches: (all maps)")
    if exclude_lower:
        log(f"Excluded matches: {matched_excludes}")
    else:
        log("Excluded matches: (none)")
    log(f"Final map count after filtering: {len(filtered)}\n")

    return filtered

def calculate_total_download_size(map_links):
    if skip_size_check:
        log("\nSkipping total download size calculation as per user choice.")
        return 0
    log("\nCalculating total download size (this may take a moment)...")
    total_size = 0
    with tqdm(total=len(map_links), desc="Checking file sizes", unit="file") as progress_bar:
        for url in map_links:
            try:
                response = requests.head(url, timeout=10)
                if 'Content-Length' in response.headers:
                    total_size += int(response.headers['Content-Length'])
            except Exception as e:
                log(f"[!] Failed to get size for {url}: {e}")
            progress_bar.update(1)
    log(f"Total download size: {format_size(total_size)}")
    return total_size

def confirm_large_download(map_count, total_size_bytes):        
    warnings = []
    if map_count >= 100:
        warnings.append(f"You are about to download {map_count} maps.")
    if total_size_bytes >= 10 * (1024 ** 3):
        warnings.append(f"Total download size exceeds 10GB: {format_size(total_size_bytes)}.")
    if warnings:
        log("\nWARNING:")
        for warn in warnings:
            log(f"- {warn}")
        choice = input("Do you wish to continue? (y/n): ").strip().lower()
        if choice != 'y':
            sys.exit("Aborted by user.")

# Download function

def download_file(url, output_folder, progress_bar):
    file_name = url.split('/')[-1]
    output_path = os.path.join(output_folder, file_name)

    if file_name in existing_files:
        skipped_files.append(file_name)
        progress_bar.update(1)
        return

    attempt = 0
    while attempt < max_retries and not cancel_event.is_set():
        try:
            with requests.get(url, stream=True, timeout=30) as r:
                r.raise_for_status()
                total_size = int(r.headers.get('content-length', 0))

                with open(output_path, 'wb') as f, tqdm(
                    desc=file_name,
                    total=total_size,
                    unit='B',
                    unit_scale=True,
                    unit_divisor=1024,
                    leave=False
                ) as file_bar:
                    for chunk in r.iter_content(chunk_size=8192):
                        if cancel_event.is_set():
                            return
                        if chunk:
                            f.write(chunk)
                            file_bar.update(len(chunk))

            actual_size = os.path.getsize(output_path)
            if actual_size != total_size:
                log(f"[!] Incomplete download detected: {file_name}")
                failed_downloads.append(file_name)
                os.remove(output_path)
                progress_bar.update(1)
                return

            downloaded_files.append(file_name)
            progress_bar.update(1)
            return

        except Exception as e:
            attempt += 1
            if attempt >= max_retries:
                log(f"[!] Failed to download {file_name} after {max_retries} attempts: {e}")
                failed_downloads.append(file_name)
            else:
                log(f"[Retry {attempt}/{max_retries}] {file_name} due to error: {e}")

# Decompression function

def extract_bz2(file_path, output_folder, progress_bar):
    output_file = os.path.splitext(file_path)[0]
    attempt = 0
    while attempt < max_retries:
        try:
            with bz2.BZ2File(file_path, 'rb') as fr, open(output_file, 'wb') as fw:
                shutil.copyfileobj(fr, fw)
            extracted_files.append(os.path.basename(output_file))
            progress_bar.update(1)
            return True
        except Exception as e:
            attempt += 1
            if attempt >= max_retries:
                log(f"[!] Failed to extract {file_path}: {e}")
                failed_extractions.append(os.path.basename(file_path))
                progress_bar.update(1)
                return False

# Cancel listener

def listen_for_cancel():
    input("\nPress [Enter] at any time to cancel...\n")
    cancel_event.set()

# Final summary

def print_summary():
    log("\n========= SUMMARY =========")
    log(f"Downloaded successfully: {len(downloaded_files)}")
    log(f"Skipped (already exists): {len(skipped_files)}")
    log(f"Failed downloads: {len(failed_downloads)}")
    log(f"Extracted .bz2 files: {len(extracted_files)}")
    log(f"Failed extractions: {len(failed_extractions)}")
    log(f"Deleted .bz2 files: {len(deleted_bz2_files)}")
    log(f"CPU threads used: {max_workers}")
    log("==========================")

def colorize_warning(text):
    # ANSI escape codes for bright yellow text on red background
    return f"\033[1;33;41m{text}\033[0m"
    
# Main driver

def main():
    start_time = datetime.datetime.now()
    log_file_name = f"download_summary_{start_time.strftime('%Y%m%d_%H%M%S')}.txt"

    steam_path = get_steam_library()
    if steam_path:
        log(f"Detected HL2DM installation: {steam_path}") # TODO: Extend this beyond HL2DM?
        use_default = input("Use this path? (y/n): ").strip().lower()
        if use_default == 'q':
            sys.exit(0)
        hl2mp_folder = steam_path if use_default == 'y' else input("Enter full path to your 'hl2mp' folder: ").strip()
    else:
        hl2mp_folder = input("Enter full path to your 'hl2mp' folder: ").strip()

    if not os.path.exists(hl2mp_folder):
        log("[!] Provided path does not exist. Exiting.")
        save_log(log_file_name)
        return

    if sys.platform.startswith('linux') and not os.access(hl2mp_folder, os.W_OK):
        log("[!] WARNING: You might not have write permissions for this folder on Linux.")

    scan_existing_maps(hl2mp_folder)

    default_download_folder = os.path.join(hl2mp_folder, 'download', 'maps')
    log(f"\nDefault download path: {default_download_folder}")
    use_default = input("Use this path? (y/n): ").strip().lower()
    if use_default == 'q':
        sys.exit(0)
    download_folder = default_download_folder if use_default == 'y' else input("Enter custom download folder: ").strip()
    os.makedirs(download_folder, exist_ok=True)
    
    # Ensure 'download' and 'download/maps' folders exist inside hl2mp_folder
    download_base_folder = os.path.join(hl2mp_folder, 'download')
    download_maps_folder = os.path.join(download_base_folder, 'maps')

    for folder in [download_base_folder, download_maps_folder]:
        if not os.path.exists(folder):
            try:
                os.makedirs(folder, exist_ok=True)
                log(f"[i] Created missing folder: {folder}")
            except Exception as e:
                log(f"[!] Failed to create folder {folder}: {e}")
                sys.exit(1)

    include_input = input("Enter keywords to include (comma separated, leave blank for all): ").strip()
    exclude_input = input("Enter keywords to exclude (comma separated, leave blank for none): ").strip()
    global include_filters, exclude_filters
    include_filters = [x.strip() for x in include_input.split(',') if x.strip()]
    exclude_filters = [x.strip() for x in exclude_input.split(',') if x.strip()]

    warning_message = (
        "Python can determine the final download size and check this against the total free space left on your system.\n"
        "[!] WARNING: Very large downloads can make this verification process last a while.\n"
        "Only do this if you want to play it safe, you have no idea what you are doing, do not know the space left on your system,\n"
        "or if you have time to waste and can take a coffee break.\n"
    )

    print(colorize_warning(warning_message))

    # Don't want this to take an eternity for larger downloads
    skip_size = input("Skip total download size checking? (y/n): ").strip().lower()

    if skip_size == 'q':
        sys.exit(0)
    skip_size_check = (skip_size == 'y')

    decompress = input("Decompress downloaded .bz2 files after download? (y/n): ").strip().lower()
    if decompress == 'q':
        sys.exit(0)
    global decompress_choice, delete_bz2_choice
    decompress_choice = decompress == 'y'

    if decompress_choice:
        remove_bz2 = input("Delete .bz2 files after extraction? (y/n): ").strip().lower()
        if remove_bz2 == 'q':
            sys.exit(0)
        delete_bz2_choice = remove_bz2 == 'y'

    urls = load_fastdl_urls()
    
    # Collect all map links
    all_map_links = []
    for url in urls:
        all_map_links.extend(get_map_links(url))

    filtered_links = apply_filters(all_map_links)
    map_count = len(filtered_links)

    if map_count == 0:
        log("No maps to download after applying filters.")
        save_log(log_file_name)
        return

    if not skip_size_check:
        total_size_bytes = calculate_total_download_size(filtered_links)
    if disk_space_warning(download_folder, total_size_bytes):
        save_log(log_file_name)
        sys.exit("[!] Insufficient disk space. Aborting to prevent issues.")
    confirm_large_download(map_count, total_size_bytes)


    try:
        user_threads = input(f"Enter number of CPU threads to use (leave blank for default: {default_threads}): ").strip()
        if user_threads == 'q':
            sys.exit(0)
        elif user_threads:
            max_workers = max(1, int(user_threads))
        else:
            max_workers = default_threads
    except ValueError:
        print(f"[!] Invalid input, using default: {default_threads} threads.")
        max_workers = default_threads

    print(f"[i] Using {max_workers} thread(s) for downloads and decompression.")
    
    # Start cancel listener thread (hitting Enter)
    threading.Thread(target=listen_for_cancel, daemon=True).start()
    
    # Download maps
    log("\nStarting downloads...")
    with tqdm(total=map_count, desc="Total Download Progress", unit="file") as progress_bar:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(download_file, url, download_folder, progress_bar) for url in filtered_links]
            for future in as_completed(futures):
                if cancel_event.is_set():
                    break

    # Decompress bz2 files if selected
    if decompress_choice:
        bz2_files = [os.path.join(download_folder, f) for f in os.listdir(download_folder) if f.endswith('.bz2')]
        if bz2_files:
            log("\nStarting decompression...")
            with tqdm(total=len(bz2_files), desc="Decompression Progress", unit="file") as progress_bar:
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = [executor.submit(extract_bz2, bz2_file, download_folder, progress_bar) for bz2_file in bz2_files]
                    for future in as_completed(futures):
                        if cancel_event.is_set():
                            break

            if delete_bz2_choice:
                for bz2_file in bz2_files:
                    try:
                        os.remove(bz2_file)
                        deleted_bz2_files.append(os.path.basename(bz2_file))
                    except Exception as e:
                        log(f"[!] Failed to delete {bz2_file}: {e}")

    # Print summary
    print_summary()
    save_log(log_file_name)
    
    try:
        total, used, free = shutil.disk_usage(download_folder)
        log(f"Disk space remaining after process: {format_size(free)}")
    except Exception as e:
        log(f"[!] Disk space retrieval failed at end of process: {e}")

    log("\nProcess completed!")
    log(f"Summary log saved as: {log_file_name}")

if __name__ == "__main__":
    main()   

# Weeeeeeeeeeeeee