# Kaguya Manga Automation

A customized fork of [Kaguya](https://github.com/wotakumoe/kaguya) designed for streamlined, unattended manga releases to **Cubari** and **ImageChest**.

## About This Fork
This project builds upon the original Kaguya JSON generator. While the core functionality for generating Cubari-compatible JSON remains consistent, this version includes an **Automation Layer** to handle batch processing and scheduled releases without manual intervention.

**Key Enhancements:**
* **Automation Wrapper (`auto_kaguya.py`)**: Enables command-line scheduling and bypasses interactive confirmation prompts.
* **Configurable Workflow**: Control upload behavior via `auto.txt` (bypass prompts, force re-uploads, and toggle automatic GitHub sync).
* **Release Ready**: Optimized for directory-based release pipelines, parsing chapter metadata directly from folder names.

## Documentation & Guides
* **Original Project**: [wotakumoe/kaguya](https://github.com/wotakumoe/kaguya)
* **Official Cubari Setup Guide**: [Wotaku.wiki Cubari Guide](https://wotaku.wiki/guides/manga/cubari)

## Quick Start (Automation)
To trigger an automated upload for a specific chapter:

```bash
python auto_kaguya.py --base_folder "[PATH_TO_SERIES]" --number [INDEX] --schedule "YYYY-MM-DD HH:MM:SS"