![hl2mp_downloader](https://github.com/user-attachments/assets/f2fbfc35-a79e-489e-adec-36deaeb4bd5d)

# HL2DM Auto Maps Downloader

### Author: Peter Brev  
### Version: 1.0.2  
### Last updated: 2026-01-12

# Changelog

## [1.0.2] - 2026-01-12

- Fix summary thread count accuracy
- Fix disk space required calculation (accounts only for files that will be downloaded)
- Central Config (no accidental global leakage)
- Safer cancellation; atomic downloads (.part then rename)
- Better Steam library discovery (libraryfolders.vdf)
- Optional recursion into subfolders under /maps/
- Robust size probing (HEAD with fallback); excludes existing files
- Clear logs & summary written once at the end
- Concurrent, visible enumeration of FastDL sources (no more "hang" feeling)

## [1.0.1] - 2025-04-29

### Added
- **IPv6/IPv4 Failover Support**: 
  - If a download attempt hangs or fails due to unreachable IPv6 endpoints, the script retries using IPv4-only resolution.
  - Ensures successful downloads even if FastDL servers advertise broken IPv6 AAAA records.
- **Address Resolution Control**:
  - Added `enforce_address_family(family)` and `restore_address_family()` to manage forced IPv4 or IPv6 during download attempts.
- **Virtual Environment Check** (Linux only):
  - Script now detects if it's running outside a virtual environment.
  - Offers to automatically create and activate a `venv` to prevent Python 3.12+ "externally managed" pip installation errors.
- **Terminal Clear and Welcome Banner**:
  - Clears the terminal window on script startup for a cleaner experience.
  - Displays an ASCII art welcome banner for user friendliness.

### Changed
- **Download Logic Rewrite**:
  - `download_file()` now intelligently retries over different IP families.
  - Cleans up partial/incomplete downloads between retries to avoid file corruption.
- **Existing Map Detection Improvement**:
  - Now checks for both `.bsp` and `.bz2` versions of a map.
  - Prevents re-downloading compressed maps if the uncompressed `.bsp` already exists.
- **General Code Improvements**:
  - Additional minor bug fixes, better error logging, more consistent behavior across systems.
  - Improved cancellation handling mid-download.

### Fixed
- Partial `.bsp.bz2` files left after failed downloads.
- Unwanted re-downloads when `.bsp` existed but `.bz2` did not.
- Inconsistent behavior when IPv6 addresses were unreachable.

## [1.0] - 2025-04-06
- **Initial Release**

---

## Overview

The **HL2DM Auto Maps Downloader** is a complete automation tool designed for Half-Life 2 Deathmatch server administrators and players.  
This script automates the tedious process of downloading map files from multiple FastDL sources, checks your existing game folders to skip already downloaded maps, and offers features like decompression, automatic logging, and real-time progress indicators.

It's designed to be cross-platform (Windows and Linux) and user-friendly, with built-in safety checks and customization options.

---

## Features

- ✨ **Automatic detection** of your HL2DM game installation (Windows & Linux Steam library).
- 🔗 **Load FastDL URLs from a text file**, and add more interactively at startup.
- 🧰 **Multi-threaded** map downloading for high speed.
- ✅ **Per-file & total progress bars** with ETA.
- ⚠️ **Warnings** for large downloads (100+ maps or 10GB+).
- 💾 **Disk space check** to warn of low space (<100GB free), and abort if insufficient space.
- 🗃️ **Decompression** of .bz2 files with optional deletion after extraction.
- 🗄️ **Skip existing maps** to save bandwidth and time.
- 📝 **Filtering**: include or exclude maps by keyword (case insensitive).
- ⏱️ **Cancel anytime** by pressing [Enter] while downloading.
- 📄 **Auto-generated log file** with complete session summary.
- 🛠️ **Configurable:** max workers (threads), retries, FastDL URLs.
- 🛠️ **URL validation**: verifies all FastDL URLs before starting.
- ⚙️ **Auto-install dependencies** (requests, beautifulsoup4, tqdm).

---

## Requirements

- Python 3.6+
- Internet connection

Required Python modules:
- requests
- beautifulsoup4
- tqdm

> ✅ The script checks and installs missing modules automatically at startup.

---

## Usage

1. **Prepare your FastDL URLs**

   - Add your URLs to `fastdl_sources.txt`. If this file is missing, it will be created automatically by the script and populated with a default community fastDL link.
   - Each URL on its own line, ending with `/`.
   - Example:

       https://files.everythingfps.com/hl2mp/maps/
     
       https://fastdl.hl2dm.community/

2. **Run the script**

       python hl2mp_maps_downloader.py

3. **Follow the prompts**

   - Confirm or set your HL2DM game directory.
   - Choose your download directory (default: `download/maps` inside HL2DM).
     - **NOTE**: You can use your `maps` folder from your `hl2mp` directory, but for organizational purposes, it is best to use the one in the `download` folder.
   - Apply map filters (include/exclude keywords, case insensitive).
   - Skip download size checking for faster starts on large map packs.
   - Decide whether to decompress `.bz2` files after download.
   - Decide whether to delete `.bz2` files after successful decompression.
   - Enter number of threads to use for downloads (leave blank to auto-detect).

4. **Monitor progress**

   - Per-file download progress.
   - Total download progress.
   - Decompression progress (if selected).

5. **Cancel anytime**

   - Press [Enter] during downloads or decompression to cancel the process safely.

6. **Check final log**

   - Summary is automatically saved to `download_summary_YYYYMMDD_HHMMSS.txt`.

---

## Customization

### Add FastDL sources:

Edit `fastdl_sources.txt`:

    https://files.everythingfps.com/hl2mp/maps/
    https://fastdl.hl2dm.community/

You will also be prompted at startup if you want to add extra FastDL URLs interactively!

### Change the number of threads:

At runtime, you will be asked:

    Enter number of threads to use (leave blank for default: X):

Higher thread count speeds up downloads (network and CPU dependent). If left blank, it will use half of your CPU threads.

---

## Examples

### Download only "booty" maps:

    Enter keywords to include (comma separated, leave blank for all): booty
    Enter keywords to exclude (comma separated, leave blank for none):

### Download everything except "test" and "beta" maps:

    Enter keywords to include (comma separated, leave blank for all):
    Enter keywords to exclude (comma separated, leave blank for none): test, beta

### Skip download size checking for faster start:

    Skip total download size checking? (y/n): y

---

## Credits

- **Script developed by Peter Brev**

---

## Notes

- Always ensure you have sufficient disk space before downloading large map packs. The script will do its best to detect the final download size, compare it to your free disk space left and warn you if you are about to download a large number of maps, if the total size exceeds 10 GB or if your disk would otherwise end up with less than 100 GB. If one of those 3 situations happen, a user confirmation is required before the download begins.
- FastDL sources are public; availability and speed depend on the host servers.
- You can cancel safely anytime; incomplete downloads will not be kept.
- Summary logs help you track what was downloaded, skipped, and processed!
- Ideal for server owners to bulk update map repositories.

---

Enjoy your automated HL2DM map downloading experience!
