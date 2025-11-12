import os
import requests
import json
from pathlib import Path
import time # For time.monotonic() in Rich's columns and manga.json last_updated
from datetime import datetime
import re
import sys # For sys.exit
from typing import List, Dict, Tuple, Optional, Any, NamedTuple, Set, Callable
import base64 # For GitHubJSONUploader

import concurrent.futures

from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor

from rich.console import Console
from rich.progress import (
    Progress,
    BarColumn,
    TextColumn,
    TimeRemainingColumn as RichTimeRemainingColumn,
    TimeElapsedColumn as RichTimeElapsedColumn,
    TransferSpeedColumn as RichTransferSpeedColumn,
    FileSizeColumn as RichFileSizeColumn,
    SpinnerColumn,
    TaskID,
    Task,
    ProgressColumn
)
from rich.live import Live
from rich.panel import Panel
from rich.text import Text as RichText

# --- Rich Console (Global) ---
console = Console()

# --- Constants ---
# Image Hosting (ImgChest) Constants
API_KEY_FILE = Path("api_key.txt")
UPLOAD_RECORD_FILE = "imgchest_upload_record.txt"
MANGA_INFO_FILE = "info.txt"
IMAGE_EXTENSIONS: Set[str] = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff"}
IMGCHEST_API_BASE_URL = "https://api.imgchest.com/v1"
MAX_IMAGES_PER_BATCH = 20

# Chapter Processing Status Constants
CHAPTER_PROC_UPLOAD_SUCCESS = "UPLOAD_SUCCESS"
CHAPTER_PROC_SKIPPED_EXISTING_USER_CONFIRMED = "SKIPPED_EXISTING_USER_CONFIRMED"
CHAPTER_PROC_ERROR_NO_IMAGES = "ERROR_NO_IMAGES"
CHAPTER_PROC_ERROR_UPLOAD_FAILED = "ERROR_UPLOAD_FAILED"

# GitHub Constants
GITHUB_CONFIG_FILE = "github.txt"
CUBARI_URLS_FILE = "cubari_urls.txt"


# --- NamedTuples for structured data ---
class ChapterInfo(NamedTuple):
    volume: str
    chapter: str
    title: str

class FolderDetails(NamedTuple):
    path: Path
    name: str
    image_count: int

# --- Custom Progress Columns ---
class ConditionalFileSizeColumn(RichFileSizeColumn):
    def render(self, task: "Task") -> RichText:
        if task.fields.get("is_byte_task"):
            return super().render(task)
        return RichText("")

class ConditionalTransferSpeedColumn(RichTransferSpeedColumn):
    def render(self, task: "Task") -> RichText:
        if task.fields.get("is_byte_task"):
            return super().render(task)
        return RichText("")

class CustomTimeDisplayColumn(ProgressColumn):
    def __init__(self):
        super().__init__()
        self._time_remaining_col = RichTimeRemainingColumn()
        self._time_elapsed_col = RichTimeElapsedColumn()

    def render(self, task: "Task") -> RichText:
        if task.fields.get("is_byte_task"):
            if task.finished:
                return self._time_elapsed_col.render(task)
            else:
                return self._time_remaining_col.render(task)
        else:
            return self._time_elapsed_col.render(task)


# --- ImgChest Helper Functions ---
def load_api_key(file_path: Path = API_KEY_FILE) -> Optional[str]:
    try:
        with open(file_path, 'r') as f:
            return f.read().strip()
    except FileNotFoundError:
        console.print(f"[red]Error: API key file for ImgChest ('{file_path}') not found.[/red]")
        return None
    except IOError as e:
        console.print(f"[red]Error reading API key from {file_path}: {e}[/red]")
        return None

def create_sample_api_key_file(file_path: Path = API_KEY_FILE):
    """Creates a sample api_key.txt file if it doesn't exist."""
    if file_path.exists():
        console.print(f"ℹ️ [yellow]{file_path.name} already exists. Please ensure it's correctly filled out.[/yellow]")
        return
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write("your_imgchest_api_key_here")
        console.print(f"✅ Sample [cyan]{file_path.name}[/cyan] file created.")
        console.print(f"👉 [bold]Please get your key from https://imgchest.com/profile/api and paste it into the file.[/bold]")
    except Exception as e:
        console.print(f"[red]❌ Error creating sample API key file [cyan]{file_path.name}[/cyan]: {str(e)}[/red]")
    finally:
        console.line()

def parse_folder_name(folder_name: str) -> ChapterInfo:
    volume_pattern = r'V(\d+)\s+Ch(\d+(?:\.\d+)?)\s*(.*)?'
    volume_match = re.match(volume_pattern, folder_name, re.IGNORECASE)
    if volume_match:
        volume, chapter_num, title = volume_match.group(1), volume_match.group(2), (volume_match.group(3) or "").strip()
        return ChapterInfo(volume, chapter_num, title)

    chapter_pattern = r'Ch(?:apter)?\s*(\d+(?:\.\d+)?)\s*(.*)?'
    chapter_match = re.match(chapter_pattern, folder_name, re.IGNORECASE)
    if chapter_match:
        chapter_num, title = chapter_match.group(1), (chapter_match.group(2) or "").strip()
        return ChapterInfo("", chapter_num, title)

    numbers = re.findall(r'\d+(?:\.\d+)?', folder_name)
    if len(numbers) >= 2: return ChapterInfo(numbers[0], numbers[1], "")
    if len(numbers) == 1: return ChapterInfo("", numbers[0], "")

    console.print(f"[yellow]Warning: Could not parse volume/chapter from '{folder_name}'. Defaulting to Ch 1, title='{folder_name}'.[/yellow]")
    return ChapterInfo("", "1", folder_name)

def load_manga_info_from_txt(base_folder_path: Path) -> Dict[str, str]:
    """Loads manga metadata from MANGA_INFO_FILE in the base_folder_path."""
    info_file = base_folder_path / MANGA_INFO_FILE
    info: Dict[str, str] = {'title': '', 'description': '', 'artist': '', 'author': '', 'cover': '', 'groups': ''}
    if info_file.exists():
        try:
            with open(info_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if ':' in line and not line.startswith('#'):
                        key, value = line.split(':', 1)
                        key = key.strip().lower()
                        if key in info: info[key] = value.strip()
        except IOError as e:
            console.print(f"[yellow]Warning: Could not read {MANGA_INFO_FILE}: {e}[/yellow]")
        except Exception as e:
            console.print(f"[yellow]Warning: An unexpected error occurred while reading {MANGA_INFO_FILE}: {e}[/yellow]")
    else:
        console.print(f"[yellow]{MANGA_INFO_FILE} not found in {base_folder_path}. Manga metadata will be minimal.[/yellow]")

    if not info.get('title'):
        info['title'] = base_folder_path.name
    return info

def load_upload_record(base_folder_path: Path) -> Dict[str, Dict[str, str]]:
    record_file = base_folder_path / UPLOAD_RECORD_FILE
    uploaded_folders: Dict[str, Dict[str, str]] = {}
    if record_file.exists():
        try:
            with open(record_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '|' in line:
                        parts = line.split('|', 4)
                        if len(parts) >= 4:
                            folder_name, album_url, timestamp, image_count_str = parts[:4]
                            post_id = parts[4] if len(parts) > 4 else album_url.split('/')[-1]
                            uploaded_folders[folder_name] = {'album_url': album_url, 'timestamp': timestamp, 'image_count': image_count_str, 'post_id': post_id}
                        else:
                            console.print(f"[yellow]Warning: Skipping malformed line in {record_file.name}: {line}[/yellow]")
        except Exception as e:
            console.print(f"[yellow]Warning: Error reading {record_file.name}: {e}[/yellow]")
    return uploaded_folders

def save_upload_record(base_folder_path: Path, uploaded_folders: Dict[str, Dict[str, str]], live: Optional[Live] = None):
    record_file = base_folder_path / UPLOAD_RECORD_FILE
    output_func = live.console.print if live else console.print
    try:
        with open(record_file, 'w', encoding='utf-8') as f:
            f.write(f"# Manga Upload Record for {base_folder_path.name}\n")
            f.write("# Format: folder_name|album_url|timestamp|image_count|post_id\n")
            f.write(f"# Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            for folder_name, data in uploaded_folders.items():
                f.write(f"{folder_name}|{data['album_url']}|{data['timestamp']}|"
                        f"{data.get('image_count', 'unknown')}|{data.get('post_id', data['album_url'].split('/')[-1])}\n")
        output_func(f"[green]Upload record ({record_file.name}) saved to: {record_file}[/green]")
    except IOError as e:
        output_func(f"[red]Error: Could not save upload record to {record_file}: {e}[/red]")

def sanitize_filename(name: str) -> str:
    name = re.sub(r'[^\w\s-]', '', name).strip()
    name = re.sub(r'[-\s]+', '_', name)
    return name

def load_manga_json(base_folder_path: Path, manga_title: str, live: Optional[Live] = None) -> Tuple[Dict[str, Any], Path]:
    output_func = live.console.print if live else console.print
    sanitized_title = sanitize_filename(manga_title if manga_title else base_folder_path.name)
    json_file = base_folder_path / f"{sanitized_title}.json"

    if json_file.exists():
        try:
            with open(json_file, 'r', encoding='utf-8') as f:
                manga_json_data = json.load(f)
            output_func(f"[dim]Loaded existing manga data from {json_file}[/dim]")
            return manga_json_data, json_file
        except (json.JSONDecodeError, IOError) as e:
            output_func(f"[yellow]Warning: Could not read existing JSON {json_file}: {e}. Creating a new one.[/yellow]")

    manga_json_data = {"title": manga_title, "description": "", "artist": "", "author": "", "cover": "", "chapters": {}}
    return manga_json_data, json_file

def save_manga_json(json_file_path: Path, manga_json_data: Dict[str, Any], live: Optional[Live] = None):
    output_func = live.console.print if live else console.print
    try:
        with open(json_file_path, 'w', encoding='utf-8') as f:
            json.dump(manga_json_data, f, indent=2, ensure_ascii=False)
        output_func(f"[green]Manga JSON saved to: {json_file_path}[/green]")
    except IOError as e:
        output_func(f"[red]Error: Could not save manga JSON to {json_file_path}: {e}[/red]")

def get_image_files(folder_path: Path, live: Optional[Live] = None) -> List[Path]:
    output_func = live.console.print if live else console.print
    if not folder_path.is_dir():
        output_func(f"[red]Error: Folder '{folder_path}' does not exist.[/red]")
        return []
    return sorted([p for p in folder_path.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS], key=lambda x: x.name.lower())

def find_subfolders_with_images(base_path: Path) -> List[FolderDetails]:
    if not base_path.is_dir():
        console.print(f"[red]Error: Base path '{base_path}' does not exist.[/red]")
        return []
    subfolders = []
    for item in base_path.iterdir():
        if item.is_dir():
            image_count = sum(1 for f in item.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS)
            if image_count > 0:
                subfolders.append(FolderDetails(item, item.name, image_count))
    return sorted(subfolders, key=lambda x: x.name.lower())


# --- Image Hosting Core Upload Functions ---
def _perform_image_upload_to_host(
    url: str, api_key: str, image_files_batch: List[Path],
    progress: Progress, task_description: str
) -> Dict[str, Any]:
    files_to_upload_fields: List[Tuple[str, Tuple[str, Any, str]]] = []
    opened_files: List[Any] = []
    upload_task_id: Optional[TaskID] = None
    try:
        for file_path in image_files_batch:
            try:
                file_handle = open(file_path, 'rb')
                opened_files.append(file_handle)
                files_to_upload_fields.append(('images[]', (file_path.name, file_handle, f'image/{file_path.suffix[1:]}')))
            except IOError as e:
                if upload_task_id is not None and any(t.id == upload_task_id for t in progress.tasks):
                    progress.remove_task(upload_task_id)
                return {'success': False, 'error': f"Error opening file {file_path.name}: {e}"}

        if not files_to_upload_fields:
             return {'success': False, 'error': "No valid image files to upload in this batch."}

        encoder = MultipartEncoder(fields=files_to_upload_fields)
        upload_task_id = progress.add_task(task_description, total=encoder.len, fields={"is_byte_task": True})

        monitor = MultipartEncoderMonitor(encoder, lambda m: progress.update(upload_task_id, completed=m.bytes_read) if upload_task_id and any(t.id == upload_task_id for t in progress.tasks) else None)
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": monitor.content_type}

        response = requests.post(url, data=monitor, headers=headers, timeout=300)

        if response.status_code == 200:
            try:
                data = response.json()
                if 'error' in data or ('status' in data and data['status'] == 'error'):
                    return {'success': False, 'error': data.get('error', data.get('message', 'Unknown API error from host'))}
                return {'success': True, 'data': data}
            except json.JSONDecodeError:
                return {'success': False, 'error': "Invalid JSON response from image hosting API."}
        else:
            return {'success': False, 'error': f"Image hosting API HTTP {response.status_code}: {response.text}"}
    except requests.exceptions.RequestException as e:
        return {'success': False, 'error': f"Image hosting request failed: {e}"}
    except Exception as e:
        return {'success': False, 'error': f"Unexpected error during image upload: {e}"}
    finally:
        if upload_task_id is not None:
            task_exists = any(t.id == upload_task_id for t in progress.tasks)
            if task_exists:
                 current_task_idx = progress.task_ids.index(upload_task_id)
                 current_task = progress.tasks[current_task_idx]
                 if not current_task.finished:
                     progress.update(upload_task_id, completed=current_task.total)
                 progress.remove_task(upload_task_id)
        for fh in opened_files: fh.close()

def upload_initial_batch_to_host(image_files_batch: List[Path], api_key: str, chapter_name: str, batch_idx_info: str, progress: Progress) -> Dict[str, Any]:
    url = f"{IMGCHEST_API_BASE_URL}/post"
    task_description = f"[cyan]ImgChest Batch (Create Album)[/cyan]: {chapter_name} ({batch_idx_info})"
    result = _perform_image_upload_to_host(url, api_key, image_files_batch, progress, task_description)
    if result['success'] and 'data' in result:
        api_data = result['data'].get('data', {})
        if 'id' in api_data:
            return {'success': True, 'album_url': f"https://imgchest.com/p/{api_data['id']}",
                    'post_id': api_data['id'], 'total_images': len(api_data.get('images', []))}
        return {'success': False, 'error': "ImgChest API response missing post ID."}
    return result

def add_images_to_existing_album_on_host(image_files_batch: List[Path], post_id: str, api_key: str, chapter_name: str, batch_idx_info: str, progress: Progress) -> Dict[str, Any]:
    url = f"{IMGCHEST_API_BASE_URL}/post/{post_id}/add"
    task_description = f"[cyan]ImgChest Batch (Add Images)[/cyan]: {chapter_name} ({batch_idx_info})"
    result = _perform_image_upload_to_host(url, api_key, image_files_batch, progress, task_description)
    if result['success']:
        return {'success': True, 'added_images': len(image_files_batch)}
    return result

def chunk_list(lst: List[Any], chunk_size: int) -> List[List[Any]]:
    return [lst[i:i + chunk_size] for i in range(0, len(lst), chunk_size)]

def upload_all_images_for_chapter_to_host(
    image_files: List[Path], api_key: str, chapter_name_for_desc: str,
    progress: Progress, live: Live
) -> Dict[str, Any]:
    if not image_files:
        live.console.print(f"[dim]Info: No image files for '{chapter_name_for_desc}'.[/dim]")
        return {'success': False, 'error': "No image files for upload.", 'total_uploaded': 0}

    image_chunks = chunk_list(image_files, MAX_IMAGES_PER_BATCH)
    total_chunks, total_uploaded_count = len(image_chunks), 0
    post_id: Optional[str] = None; album_url: Optional[str] = None
    chapter_batch_task_id: Optional[TaskID] = None

    try:
        chapter_batch_task_id = progress.add_task(
            f"[blue]ImgChest Upload Batches '{chapter_name_for_desc}'[/blue]",
            total=total_chunks, fields={"is_byte_task": False}
        )
        for i, chunk in enumerate(image_chunks):
            batch_info_str = f"Batch {i+1}/{total_chunks}"
            current_op_desc = "Create Album" if i == 0 else "Add Images"
            if chapter_batch_task_id and any(t.id == chapter_batch_task_id for t in progress.tasks):
                progress.update(chapter_batch_task_id, description=f"[blue]ImgChest '{chapter_name_for_desc}'[/blue] ({batch_info_str} - {current_op_desc})")

            if i == 0:
                res = upload_initial_batch_to_host(chunk, api_key, chapter_name_for_desc, batch_info_str, progress)
                if not res['success']:
                    live.console.print(f"[red]❌ Error creating ImgChest album for '{chapter_name_for_desc}': {res.get('error', 'Unknown')}[/red]")
                    return {'success': False, 'error': f"Failed to create album: {res.get('error', 'Unknown')}", 'total_uploaded': 0}
                post_id, album_url = res['post_id'], res['album_url']
                total_uploaded_count += res['total_images']
                live.console.line()
                live.console.print(f"[green]✓ Album created for '{chapter_name_for_desc}': {album_url} ({res['total_images']} images).[/green]")
                live.console.line()
            else:
                if not post_id:
                    live.console.print(f"[red]❌ Critical: Album post_id missing for '{chapter_name_for_desc}'.[/red]")
                    return {'success': False, 'error': "post_id missing for adding images", 'total_uploaded': total_uploaded_count}
                time.sleep(1) # Small delay before adding images
                res = add_images_to_existing_album_on_host(chunk, post_id, api_key, chapter_name_for_desc, batch_info_str, progress)
                if res['success']:
                    total_uploaded_count += res['added_images']
                    live.console.line()
                    live.console.print(f"[green]✓ Added {res['added_images']} images to album '{chapter_name_for_desc}'.[/green]")
                    live.console.line()
                else:
                    live.console.print(f"[red]❌ Error adding batch {i+1} to album '{chapter_name_for_desc}': {res.get('error', 'Unknown')}[/red]")
                    return {'success': False, 'error': f"Failed image upload batch {i+1}: {res.get('error', 'Unknown')}",
                            'total_uploaded': total_uploaded_count, 'album_url': album_url, 'post_id': post_id}

            if chapter_batch_task_id and any(t.id == chapter_batch_task_id for t in progress.tasks): progress.update(chapter_batch_task_id, advance=1)
    finally:
        if chapter_batch_task_id:
            task_exists = any(t.id == chapter_batch_task_id for t in progress.tasks)
            if task_exists:
                current_task_idx = progress.task_ids.index(chapter_batch_task_id)
                current_task = progress.tasks[current_task_idx]
                if not current_task.finished:
                    progress.update(chapter_batch_task_id, completed=current_task.total)
                progress.remove_task(chapter_batch_task_id)

    return {'success': True, 'album_url': album_url, 'post_id': post_id, 'total_uploaded': total_uploaded_count}

# --- GitHub Uploader Class and Helper Functions ---
class GitHubJSONUploader:
    def __init__(self, token: str, owner: str, repo: str, console_instance: Console):
        self.token = token
        self.owner = owner
        self.repo = repo
        self.repo_api_url = f"https://api.github.com/repos/{owner}/{repo}"
        self.contents_api_url = f"https://api.github.com/repos/{owner}/{repo}/contents"
        self.headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
        self.console = console_instance
        self._default_branch: Optional[str] = None

    def _fetch_default_branch(self) -> str:
        if self._default_branch:
            return self._default_branch

        self.console.print(f"[dim]GitHub: Fetching default branch for [cyan]{self.owner}/{self.repo}[/cyan]...[/dim]")
        try:
            response = requests.get(self.repo_api_url, headers=self.headers, timeout=30)
            response.raise_for_status()
            repo_info = response.json()
            default_branch = repo_info.get("default_branch")

            if default_branch:
                self._default_branch = default_branch
                self.console.print(f"[dim]GitHub: Default branch for [cyan]{self.owner}/{self.repo}[/cyan] is [bold cyan]{default_branch}[/bold cyan].[/dim]")
                return default_branch
            else:
                self.console.print(f"[yellow]⚠️ GitHub: Could not determine default branch for [cyan]{self.owner}/{self.repo}[/cyan]. 'default_branch' field missing. Falling back to 'main'.[/yellow]")
                self._default_branch = "main"
                return "main"
        except requests.exceptions.HTTPError as e:
            self.console.print(f"\n[yellow]⚠️ GitHub: HTTP error fetching repository details for [cyan]{self.owner}/{self.repo}[/cyan]: {e}. Falling back to 'main'.[/yellow]")
        except requests.exceptions.RequestException as e:
            self.console.print(f"\n[yellow]⚠️ GitHub: Network error fetching repository details for [cyan]{self.owner}/{self.repo}[/cyan]: {e}. Falling back to 'main'.[/yellow]")
        except json.JSONDecodeError:
            self.console.print(f"\n[yellow]⚠️ GitHub: Invalid JSON response when fetching repository details for [cyan]{self.owner}/{self.repo}[/cyan]. Falling back to 'main'.[/yellow]")
        except Exception as e:
            self.console.print(f"\n[yellow]⚠️ GitHub: An unexpected error occurred while fetching default branch for [cyan]{self.owner}/{self.repo}[/cyan]: {e}. Falling back to 'main'.[/yellow]")

        self._default_branch = "main"
        return "main"

    def get_file_sha(self, file_path: str) -> Optional[str]:
        url = f"{self.contents_api_url}/{file_path.replace(os.sep, '/')}"
        try:
            response = requests.get(url, headers=self.headers, params={"ref": self._fetch_default_branch()}, timeout=30)
            if response.status_code == 200:
                return response.json().get("sha")
            elif response.status_code == 404:
                return None
            else:
                self.console.line()
                self.console.print(f"[yellow]⚠️ GitHub API error getting SHA for [cyan]{file_path}[/cyan]: {response.status_code} - {response.text}[/yellow]")
                self.console.line()
                return None
        except requests.exceptions.RequestException as e:
            self.console.line()
            self.console.print(f"[red]❌ Network error getting SHA for [cyan]{file_path}[/cyan]: {e}[/red]")
            self.console.line()
            return None

    def get_raw_url(self, repo_file_path: str, branch: Optional[str] = None) -> str:
        target_branch = branch if branch is not None else self._fetch_default_branch()
        normalized_repo_file_path = repo_file_path.replace(os.sep, '/')
        return f"https://raw.githubusercontent.com/{self.owner}/{self.repo}/{target_branch}/{normalized_repo_file_path}"

    def get_cubari_url(self, repo_file_path: str, branch: Optional[str] = None) -> str:
        target_branch = branch if branch is not None else self._fetch_default_branch()
        normalized_repo_file_path = repo_file_path.replace(os.sep, '/')
        raw_path_for_cubari_gist = f"raw/{self.owner}/{self.repo}/{target_branch}/{normalized_repo_file_path}"
        b64_encoded = base64.b64encode(raw_path_for_cubari_gist.encode('utf-8')).decode('utf-8')
        return f"https://cubari.moe/read/gist/{b64_encoded}/"

    def read_info_txt_for_github(self, folder_path: Path) -> Dict[str, str]:
        info_file = folder_path / MANGA_INFO_FILE
        info_data = {"title": folder_path.name}
        if info_file.exists():
            try:
                with open(info_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if ':' in line and not line.startswith('#'):
                            parts = line.split(':', 1)
                            if len(parts) == 2:
                                key, value = parts[0].strip().lower(), parts[1].strip()
                                if key and value: info_data[key] = value
                if not info_data.get('title'): info_data['title'] = folder_path.name
            except Exception as e:
                self.console.line()
                self.console.print(f"[yellow]⚠️ Error reading {info_file} for GitHub: {e}. Using folder name '{folder_path.name}' as title.[/yellow]")
                self.console.line()
                info_data = {"title": folder_path.name}
        if not info_data.get("title"): info_data["title"] = folder_path.name
        return info_data

    def _get_previous_last_modified(self, repo_file_path: str) -> Optional[str]:
        """
        Reads cubari_urls.txt and tries to find the last modified timestamp
        for the given repo_file_path. Returns the most recent one found in the log.
        """
        urls_file_path = Path(CUBARI_URLS_FILE)
        if not urls_file_path.exists():
            return None

        last_found_timestamp = None
        current_repo_path_in_entry = None # Stores the repo path for the current entry block

        try:
            with open(urls_file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("GitHub Repo Path:"):
                        current_repo_path_in_entry = line.split("GitHub Repo Path:", 1)[1].strip()
                    elif line.startswith("Logged Action:") and current_repo_path_in_entry == repo_file_path:
                        match = re.search(r'at\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})$', line)
                        if match:
                            last_found_timestamp = match.group(1)
                            # Keep overwriting; the last one for this path will be the latest from the file
                    elif not line.strip(): # Blank line often separates entries
                        current_repo_path_in_entry = None # Reset for the next potential entry block
            return last_found_timestamp
        except Exception as e:
            self.console.print(f"[yellow]⚠️ Could not read or parse {urls_file_path.name} to get previous modified time: {e}[/yellow]")
            return None

    def upload_file(self, local_file_path: str, repo_file_path: str, commit_message: Optional[str] = None) -> Dict[str, Any]:
        normalized_repo_file_path = repo_file_path.replace(os.sep, '/')
        local_p_path = Path(local_file_path)

        try:
            with open(local_p_path, 'r', encoding='utf-8') as f:
                content = f.read()

            content_encoded = base64.b64encode(content.encode('utf-8')).decode('utf-8')

            default_branch = self._fetch_default_branch()
            existing_sha = self.get_file_sha(normalized_repo_file_path)

            action_desc = "Update" if existing_sha else "Add"

            if commit_message is None:
                commit_message = f"{action_desc} {local_p_path.name}"

            payload: Dict[str, Any] = {"message": commit_message, "content": content_encoded, "branch": default_branch}
            if existing_sha: payload["sha"] = existing_sha

            url = f"{self.contents_api_url}/{normalized_repo_file_path}"
            response = requests.put(url, headers=self.headers, json=payload, timeout=60)

            if response.status_code in [200, 201]:
                action_taken = "Updated" if existing_sha else "Uploaded"
                raw_url = self.get_raw_url(normalized_repo_file_path, branch=default_branch)
                cubari_url = self.get_cubari_url(normalized_repo_file_path, branch=default_branch)

                previous_last_modified = self._get_previous_last_modified(normalized_repo_file_path)

                self.console.line()
                self.console.print(f"✅ GitHub: [green]{action_taken} [cyan]{normalized_repo_file_path}[/cyan] (branch: {default_branch})[/green]")
                self.console.print(f"🔗 Raw URL: {raw_url}")
                self.console.print(f"📚 Cubari URL: {cubari_url}")
                if previous_last_modified:
                    self.console.print(f"   Last Updated (from log): {previous_last_modified}")
                self.console.line()

                return_data = {
                    "success": True,
                    "raw_url": raw_url,
                    "cubari_url": cubari_url,
                    "action": action_taken
                }
                if previous_last_modified:
                    return_data["previous_last_modified"] = previous_last_modified
                return return_data
            else:
                self.console.line()
                self.console.print(f"❌ GitHub: Failed to upload [cyan]{normalized_repo_file_path}[/cyan]: {response.status_code} - {response.text}")
                self.console.line()
                return {"success": False, "error": response.text}
        except FileNotFoundError:
            self.console.line()
            self.console.print(f"❌ GitHub: Local file not found [cyan]{local_file_path}[/cyan]")
            self.console.line()
            return {"success": False, "error": "Local file not found"}
        except requests.exceptions.RequestException as e:
            self.console.line()
            self.console.print(f"❌ GitHub: Network error uploading [cyan]{local_file_path}[/cyan]: {e}")
            self.console.line()
            return {"success": False, "error": str(e)}
        except Exception as e:
            self.console.line()
            self.console.print(f"❌ GitHub: Error uploading [cyan]{local_file_path}[/cyan]: {str(e)}")
            self.console.line()
            return {"success": False, "error": str(e)}

    def upload_json_files_from_folder(self, folder_path: str, repo_subfolder: str ="", recursive: bool =True) -> Dict[str, Any]:
        source_folder = Path(folder_path)
        self.console.line()
        if not source_folder.is_dir():
            self.console.print(f"❌ GitHub: Source folder does not exist: [cyan]{source_folder}[/cyan]")
            self.console.line()
            return {"success_count": 0, "failed_count": 0, "cubari_items": []}

        json_files = list(source_folder.rglob("*.json")) if recursive else list(source_folder.glob("*.json"))
        if not json_files:
            self.console.print(f"📁 GitHub: No JSON files found in [cyan]{source_folder}[/cyan]" + (" (recursively)" if recursive else ""))
            self.console.line()
            return {"success_count": 0, "failed_count": 0, "cubari_items": []}

        self.console.print(f"📁 GitHub: Found {len(json_files)} JSON file(s) to process from [cyan]{source_folder}[/cyan]")
        self.console.line()
        success_count, failed_count = 0, 0
        processed_cubari_items = []

        self._fetch_default_branch() # Fetch once for all uploads in this call

        for json_file_p_path in json_files:
            json_parent_dir = json_file_p_path.parent
            info_data = self.read_info_txt_for_github(json_parent_dir)

            try:
                relative_json_path = json_file_p_path.relative_to(source_folder)
            except ValueError: # If json_file_p_path is not under source_folder (e.g. source_folder is just a file)
                relative_json_path = Path(json_file_p_path.name)

            repo_path_parts = [p.strip('/') for p in [repo_subfolder, str(relative_json_path)] if p.strip('/')]
            repo_file_path_str = "/".join(repo_path_parts).replace("\\", "/")

            commit_title = info_data.get("title", json_parent_dir.name)
            # Make commit message more concise for common JSON names
            commit_msg_suffix = f" ({json_file_p_path.name})" if json_file_p_path.name.lower() not in ["data.json", "index.json", f"{sanitize_filename(commit_title)}.json"] else ""
            commit_message = f"Sync: {commit_title}{commit_msg_suffix}"

            self.console.print(f"Processing: [cyan]{json_file_p_path.name}[/cyan] for repo path [cyan]{repo_file_path_str}[/cyan]")
            result = self.upload_file(str(json_file_p_path), repo_file_path_str, commit_message=commit_message)

            if result.get("success"):
                success_count += 1
                item_to_log = {
                    "title": info_data.get("title", json_parent_dir.name),
                    "folder_path": str(json_parent_dir.resolve()),
                    "file": json_file_p_path.name,
                    "repo_path": repo_file_path_str,
                    "raw_url": result["raw_url"],
                    "cubari_url": result["cubari_url"],
                    "action": result["action"],
                    "last_modified": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }
                if "previous_last_modified" in result:
                    item_to_log["previous_last_modified_in_log"] = result["previous_last_modified"]
                processed_cubari_items.append(item_to_log)
            else:
                failed_count += 1
            self.console.line()

        self.console.print(f"📊 GitHub Upload Summary (for folder '{source_folder.name}'):")
        self.console.print(f"   Processed: {len(json_files)}")
        self.console.print(f"   ✅ Successful: {success_count}")
        self.console.print(f"   ❌ Failed: {failed_count}")
        self.console.line()
        return {"success_count": success_count, "failed_count": failed_count, "cubari_items": processed_cubari_items}


def load_github_config(config_file: str = GITHUB_CONFIG_FILE) -> Optional[Dict[str,str]]:
    config = {}
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    config[key.strip().lower()] = value.strip()
        required_fields = ['token', 'owner', 'repo']
        missing = [field for field in required_fields if not config.get(field)]
        if missing:
            console.line()
            console.print(f"[red]❌ Missing or empty required fields in [cyan]{config_file}[/cyan]: {', '.join(missing)}[/red]")
            console.line()
            return None
        return config
    except FileNotFoundError:
        console.line()
        console.print(f"[red]❌ GitHub configuration file '[cyan]{config_file}[/cyan]' not found.[/red]")
        create_sample_github_config()
        return None
    except Exception as e:
        console.line()
        console.print(f"[red]❌ Error reading GitHub configuration file: {str(e)}[/red]")
        console.line()
        return None

def create_sample_github_config(config_filename: str = GITHUB_CONFIG_FILE):
    sample_content = """# GitHub Configuration File
# Remove the # from the beginning of each line and replace with your actual values.
# Ensure there are no spaces around the = sign for your actual values.

#token=your_github_personal_access_token_here
#owner=your_github_username_or_organization_name
#repo=your_repository_name

# Example:
# token=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
# owner=MyUserName
# repo=MyCubariComics
"""
    if Path(config_filename).exists():
        console.print(f"ℹ️ [yellow]{config_filename} already exists. Please ensure it's correctly filled out.[/yellow]")
        console.line()
        return

    try:
        with open(config_filename, 'w', encoding='utf-8') as f:
            f.write(sample_content)
        console.print(f"✅ Sample [cyan]{config_filename}[/cyan] file created.")
        console.print(f"👉 [bold]Please edit it with your actual GitHub token, owner, and repository name.[/bold]")
        console.line()
    except Exception as e:
        console.print(f"[red]❌ Error creating sample config file [cyan]{config_filename}[/cyan]: {str(e)}[/red]")
        console.line()

def save_cubari_urls(cubari_items_list: List[Dict[str, Any]], mode: str = "append"):
    if not cubari_items_list: return

    urls_file_path = Path(CUBARI_URLS_FILE)
    try:
        write_main_header = (mode == "overwrite" or not urls_file_path.exists() or urls_file_path.stat().st_size == 0)
        file_open_mode = 'w' if write_main_header else 'a'

        console.line()
        with open(urls_file_path, file_open_mode, encoding='utf-8') as f:
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            if write_main_header:
                f.write(f"# Cubari URLs Log\n# File created/overwritten: {timestamp}\n" + "=" * 60 + "\n\n")
            elif mode == "append":
                f.write(f"\n# Entries appended on: {timestamp}\n" + "-" * 40 + "\n\n")

            for item in cubari_items_list:
                action_emoji = "🆕" if item.get('action') == "Uploaded" else "🔄"
                f.write(f"{action_emoji} Title: {item.get('title', 'N/A')}\n")
                f.write(f"   Local Source Folder: {item.get('folder_path', 'N/A')}\n")
                f.write(f"   JSON File: {item.get('file', 'N/A')}\n")
                f.write(f"   GitHub Repo Path: {item.get('repo_path', 'N/A')}\n")
                f.write(f"   Cubari URL: {item.get('cubari_url', 'N/A')}\n")
                f.write(f"   GitHub Raw URL: {item.get('raw_url', 'N/A')}\n")
                if "previous_last_modified_in_log" in item: # Log previous update time if available
                   f.write(f"   Previous Logged Action At: {item['previous_last_modified_in_log']}\n")
                f.write(f"   Logged Action: {item.get('action', 'N/A')} at {item.get('last_modified', 'N/A')}\n\n")
        console.print(f"💾 GitHub upload log saved/appended to: [cyan]{CUBARI_URLS_FILE}[/cyan]")
        console.line()
    except Exception as e:
        console.print(f"[red]⚠️ Could not save Cubari URLs to [cyan]{CUBARI_URLS_FILE}[/cyan]: {str(e)}[/red]")
        console.line()


# --- Combined Workflow Functions ---
def process_single_chapter_folder(
    folder_details: FolderDetails,
    base_folder_path: Path,
    uploaded_folders_record: Dict[str, Dict[str, str]],
    progress: Progress,
    live: Live,
    manga_json_data_to_update: Dict[str, Any],
    manga_main_groups_info: str,
    manga_json_file_path: Path,
    imgchest_api_key: str
) -> str:
    live.console.print(Panel(RichText(f"Processing Chapter Folder: {folder_details.name}", justify="center"), style="bold yellow", border_style="yellow"))
    image_files = get_image_files(folder_details.path, live)
    if not image_files:
        live.console.print(f"[yellow]Warning: No images in {folder_details.name}. Skipping folder processing.[/yellow]")
        return CHAPTER_PROC_ERROR_NO_IMAGES

    chapter_info = parse_folder_name(folder_details.name)

    if 'chapters' not in manga_json_data_to_update:
        manga_json_data_to_update['chapters'] = {}

    if folder_details.name in uploaded_folders_record:
        existing_record = uploaded_folders_record[folder_details.name]
        current_live_status = live.is_started
        if current_live_status: live.stop()
        console.line()
        console.print(f"[yellow]⚠️ Chapter folder '{folder_details.name}' found in upload record ({UPLOAD_RECORD_FILE})![/yellow]")
        console.print(f"   URL: {existing_record['album_url']} Date: {existing_record['timestamp']} Images: {existing_record.get('image_count', 'N/A')}")
        skip_choice = console.input(f"\n[bold yellow]Skip re-uploading '{folder_details.name}'? (Y/n):[/bold yellow] ").strip().lower()
        console.line()
        if current_live_status: live.start(refresh=True)

        if skip_choice != 'n':
            live.console.print(f"[dim]Skipped re-upload for '{folder_details.name}'. The corresponding entry in manga.json (if any) will not be modified.[/dim]")
            return CHAPTER_PROC_SKIPPED_EXISTING_USER_CONFIRMED

    live.console.print(f"\n[bold]📂 Chapter Info:[/bold] V: {chapter_info.volume or 'N/A'}, Ch: {chapter_info.chapter}, Title: {chapter_info.title or 'N/A'}")
    live.console.print(f"[bold]📸 Found {len(image_files)} image(s).[/bold]", highlight= False)

    upload_res = upload_all_images_for_chapter_to_host(image_files, imgchest_api_key, folder_details.name, progress, live)

    if upload_res['success']:
        live.console.line()
        live.console.print(f"[bold green]🎉 Chapter Image Upload SUCCESS! {upload_res['total_uploaded']} images for '{folder_details.name}'.[/bold green]")
        live.console.print(f"Album URL: {upload_res['album_url']}")
        live.console.line()

        uploaded_folders_record[folder_details.name] = {
            'album_url': upload_res['album_url'],
            'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'image_count': str(upload_res['total_uploaded']),
            'post_id': upload_res['post_id']
        }

        final_chapter_key = chapter_info.chapter
        if final_chapter_key in manga_json_data_to_update['chapters']:
            live.console.print(f"[yellow]Note: Chapter key '{final_chapter_key}' already exists in manga.json. It will be overwritten.[/yellow]")

        proxy_path = f"/proxy/api/imgchest/chapter/{upload_res['post_id']}"

        ch_data: Dict[str, Any] = {
            "title": chapter_info.title,
            "last_updated": str(int(time.time())),
            "groups": {manga_main_groups_info: proxy_path}
        }
        if chapter_info.volume:
            ch_data["volume"] = chapter_info.volume

        manga_json_data_to_update['chapters'][final_chapter_key] = ch_data

        # Persist manga.json after each successful chapter update
        save_manga_json(manga_json_file_path, manga_json_data_to_update, live)

        live.console.print(f"[green]Chapter '{folder_details.name}' successfully added/updated in manga.json with key '{final_chapter_key}'.[/green]")
        return CHAPTER_PROC_UPLOAD_SUCCESS

    else:
        live.console.line()
        live.console.print(f"[bold red]❌ Chapter Upload FAILED for '{folder_details.name}': {upload_res.get('error', 'Unknown')}[/bold red]")
        if upload_res.get('total_uploaded', 0) > 0 and 'album_url' in upload_res and upload_res['album_url']:
            live.console.print(f"[yellow]Partial chapter upload success: {upload_res['total_uploaded']} images. Album URL: {upload_res['album_url']}[/yellow]")
        live.console.line()
        return CHAPTER_PROC_ERROR_UPLOAD_FAILED

def parse_folder_selection(selection_str: str, num_folders: int) -> Optional[List[int]]:
    selected_indices: Set[int] = set()
    if not selection_str: console.print("[red]Error: No selection provided.[/red]"); return None
    try:
        for part in selection_str.split(','):
            part = part.strip()
            if '-' in part:
                s, e = map(int, part.split('-', 1))
                if not (1 <= s <= e <= num_folders):
                    console.print(f"[red]Invalid range: {s}-{e}. Max: {num_folders}.[/red]"); return None
                selected_indices.update(range(s - 1, e))
            else:
                idx = int(part)
                if not (1 <= idx <= num_folders):
                    console.print(f"[red]Invalid folder number: {idx}. Max: {num_folders}.[/red]"); return None
                selected_indices.add(idx - 1)
        return sorted(list(selected_indices))
    except ValueError:
        console.print("[red]Invalid format. Use numbers or ranges like 1,3,5-7.[/red]"); return None

def regenerate_manga_json_from_folders(
    base_folder_path: Path,
    subfolders_with_images: List[FolderDetails],
    uploaded_chapter_record: Dict[str, Dict[str, str]],
    manga_json_data: Dict[str, Any],
    manga_main_groups: str,
    manga_json_file_path: Path,
    live: Optional[Live] = None
) -> None:
    """Rebuilds the manga_json_data['chapters'] from the current subfolders and the upload record, then saves it."""
    output_func = live.console.print if live else console.print
    output_func(f"[dim]Regenerating manga.json for: {base_folder_path}[/dim]")
    if 'chapters' not in manga_json_data:
        manga_json_data['chapters'] = {}
    # Start fresh
    manga_json_data['chapters'].clear()

    for fd in subfolders_with_images:
        chapter_info = parse_folder_name(fd.name)
        final_chapter_key = chapter_info.chapter

        proxy_groups: Dict[str, str] = {}
        post_id = None
        if fd.name in uploaded_chapter_record:
            post_id = uploaded_chapter_record[fd.name].get('post_id')
            if post_id:
                proxy_groups = {manga_main_groups: f"/proxy/api/imgchest/chapter/{post_id}"}

            ts_str = uploaded_chapter_record[fd.name].get('timestamp')
            try:
                if ts_str:
                    dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                    last_updated = str(int(time.mktime(dt.timetuple())))
                else:
                    last_updated = str(int(time.time()))
            except Exception:
                last_updated = str(int(time.time()))
        else:
            last_updated = str(int(time.time()))

        ch_data: Dict[str, Any] = {
            "title": chapter_info.title,
            "last_updated": last_updated,
            "groups": proxy_groups
        }
        if chapter_info.volume:
            ch_data["volume"] = chapter_info.volume

        manga_json_data['chapters'][final_chapter_key] = ch_data
        output_func(f"[dim]Added chapter {final_chapter_key}: {fd.name} (uploaded={'yes' if post_id else 'no'})[/dim]")

    save_manga_json(manga_json_file_path, manga_json_data, live)
    output_func(f"[green]Manga JSON regenerated and saved to: {manga_json_file_path}[/green]")

def run_chapter_upload_processing() -> Optional[Dict[str, Any]]:
    """Handles chapter image upload and/or prepares for GitHub update for a selected manga folder."""
    imgchest_api_key = load_api_key()
    console.line()

    if not imgchest_api_key:
        if not console.input("[yellow]ImgChest API key not configured. Continue with GitHub-only options? (y/N): [/yellow]").strip().lower() == 'y':
            console.print("[red]ImgChest API key is required for uploads. Exiting.[/red]")
            return None
        console.print("[yellow]Proceeding without API key. Image upload options will be disabled.[/yellow]")
    else:
        console.print("[green]ImgChest API key loaded successfully.[/green]")
    console.line()


    while True:
        base_path_str = console.input("[bold cyan]Enter the base manga folder path (contains chapter subfolders and info.txt):[/bold cyan] ").strip()
        base_folder_path = Path(base_path_str)
        if base_folder_path.is_dir(): break
        console.print(f"[red]Error: '{base_path_str}' is not a valid directory.[/red]")
        console.line()

    subfolders_with_images = find_subfolders_with_images(base_folder_path)
    if not subfolders_with_images:
        console.line()
        console.print(f"[yellow]No chapter subfolders with images found in '{base_folder_path}'. Image upload options will be limited.[/yellow]")
        console.line()

    manga_overall_info = load_manga_info_from_txt(base_folder_path)
    uploaded_chapter_record = load_upload_record(base_folder_path)

    manga_title_for_json = manga_overall_info.get('title') or base_folder_path.name
    manga_json_data, manga_json_file_path = load_manga_json(base_folder_path, manga_title_for_json)

    for key in ['title', 'description', 'artist', 'author', 'cover']:
        if manga_overall_info.get(key):
            manga_json_data[key] = manga_overall_info[key]
        elif not manga_json_data.get(key) and key == 'title':
             manga_json_data[key] = base_folder_path.name

    if 'chapters' not in manga_json_data:
        manga_json_data['chapters'] = {}

    manga_main_groups = manga_overall_info.get('groups', 'UnknownGroup')

    console.line()
    console.print("[bold underline]📖 Manga Info:[/bold underline]")
    has_manga_info_values = any(manga_overall_info.get(k) for k in ['title', 'description', 'artist', 'author', 'cover', 'groups'])
    if has_manga_info_values:
        for k, v in manga_overall_info.items():
            if v: console.print(f"   [dim]{k.capitalize()}:[/dim] {v}")
    else:
        console.print(f"   [dim]No {MANGA_INFO_FILE} found or it's empty. Using folder name '{base_folder_path.name}' as title if not already in JSON.[/dim]")
    console.print(f"   [dim]Manga JSON will be named: '{manga_json_file_path.name}'[/dim]")


    console.print(f"\n[bold underline]📁 Found {len(subfolders_with_images)} folder(s) with images:[/bold underline]", highlight= False)
    if subfolders_with_images:
        for i, fd in enumerate(subfolders_with_images, 1):
            status = f"✓ Uploaded" if fd.name in uploaded_chapter_record else "○ New"
            color = "green" if fd.name in uploaded_chapter_record else "yellow"
            console.print(f"{i:3d}. {fd.name} ({fd.image_count} images) [[{color}]{status}[/{color}]]")
    else:
        console.print("   [dim]None suitable for image upload.[/dim]")

    console.print("\n[bold underline]⬆️ Process Options:[/bold underline]\n"
                  "1. Upload all folders\n"
                  "2. Upload only new folders (skip already uploaded)\n"
                  "3. Select specific folder(s) to upload/re-upload\n"
                  "4. Update GitHub only (uses existing manga.json for this manga)\n"
                  "5. Regenerate manga.json from current folders (uses upload records)\n"
                  "6. Cancel", highlight=False)

    folders_to_process: List[FolderDetails] = []
    choice = ''
    is_github_only_choice = False

    while True:
        console.line()
        choice_input = console.input("[bold cyan]Choose an option (1-6):[/bold cyan] ").strip()
        if choice_input in ['1', '2', '3']:
            if not imgchest_api_key: console.print("[red]ImgChest API key is not configured. Cannot perform image uploads.[/red]"); continue
            if not subfolders_with_images: console.print("[yellow]No folders with images available for this option.[/yellow]"); continue

            if choice_input == '1':
                folders_to_process = subfolders_with_images; choice = choice_input; break
            elif choice_input == '2':
                folders_to_process = [f for f in subfolders_with_images if f.name not in uploaded_chapter_record]
                if not folders_to_process: console.print("[yellow]No new/unrecorded folders to process. Try another option.[/yellow]"); continue
                choice = choice_input; break
            elif choice_input == '3':
                sel_str = console.input("[cyan]Enter folder numbers (e.g., 1,3,5-7):[/cyan] ").strip()
                indices = parse_folder_selection(sel_str, len(subfolders_with_images))
                if indices is not None:
                    folders_to_process = [subfolders_with_images[i] for i in indices]
                    if folders_to_process: choice = choice_input; break
                    else: console.print("[yellow]No valid folders selected from your input. Try again.[/yellow]")

        elif choice_input == '4':
            if not manga_json_file_path.exists():
                console.print(f"[red]Error: Manga JSON file '{manga_json_file_path.name}' does not exist in '{base_folder_path}'.[/red]")
                console.print("[yellow]This option requires an existing manga.json. Please run an image upload option first or ensure the file exists.[/yellow]")
                continue
            console.print(f"[green]Selected 'Update GitHub only'. Will use existing '{manga_json_file_path.name}'.[/green]")
            folders_to_process = []
            is_github_only_choice = True
            choice = choice_input; break
        elif choice_input == '5':
            # Regenerate manga.json from current folders and existing upload record, then quit
            console.line()
            console.print("[bold underline]Regenerate manga.json from current folders and upload records[/bold underline]")
            regenerate_manga_json_from_folders(base_folder_path, subfolders_with_images, uploaded_chapter_record, manga_json_data, manga_main_groups, manga_json_file_path)
            console.line()
            console.print("[green]Regeneration complete. Exiting.[/green]")
            console.line()
            sys.exit(0)
        elif choice_input == '6':
            console.print("[yellow]Processing canceled by user.[/yellow]"); console.line(); return None
        else: console.print("[red]Invalid choice. Please enter a number between 1 and 6.[/red]")

    newly_uploaded_or_reuploaded_count = 0
    user_confirmed_skipped_count = 0

    if not is_github_only_choice:
        if not folders_to_process:
            console.line()
            console.print("[yellow]No chapter folders were identified for image upload based on your selection.[/yellow]")
            save_manga_json(manga_json_file_path, manga_json_data)
            console.line()
            return { "manga_json_path": manga_json_file_path, "base_folder_path": base_folder_path, "manga_title": manga_title_for_json, "is_github_only_mode": False }

        console.line()
        console.print(f"[bold underline]Will process using [cyan]ImgChest[/cyan]:[/bold underline]")
        for fd in folders_to_process: console.print(f"  - {fd.name}")
        console.line()
        if console.input("[bold yellow]Proceed with chapter image uploads? (y/N):[/bold yellow] ").strip().lower() != 'y':
            console.print("[yellow]Chapter image upload processing canceled by user.[/yellow]")
            save_manga_json(manga_json_file_path, manga_json_data)
            console.line()
            return { "manga_json_path": manga_json_file_path, "base_folder_path": base_folder_path, "manga_title": manga_title_for_json, "is_github_only_mode": False }

        console.line()
        console.print("[bold underline]🚀 Starting chapter image uploads via ImgChest...[/bold underline]")
        progress_columns = [ SpinnerColumn(finished_text="[green]✓[/green]"), TextColumn("[progress.description]{task.description}", justify="left"), BarColumn(bar_width=None), TextColumn("[progress.percentage]{task.percentage:>3.1f}%"), TextColumn("• {task.completed} of {task.total} •"), ConditionalTransferSpeedColumn(), ConditionalFileSizeColumn(), CustomTimeDisplayColumn()]
        progress_bar_manager = Progress(*progress_columns, console=console, transient=False, expand=True)
 
        with Live(progress_bar_manager, console=console, refresh_per_second=10, vertical_overflow="visible") as live:
            overall_task_id = progress_bar_manager.add_task("[bold #AAAAFF]Overall ImgChest Upload Progress[/bold #AAAAFF]", total=len(folders_to_process), fields={"is_byte_task": False})
 
            # Partition folders into those already recorded (may require user prompt) and new ones we can upload concurrently.
            pending_to_upload = []
            recorded_to_handle = []
            for folder_item in folders_to_process:
                if folder_item.name in uploaded_chapter_record:
                    recorded_to_handle.append(folder_item)
                else:
                    pending_to_upload.append(folder_item)
 
            # Preserve existing behavior for folders already present in the upload record (keeps user prompt)
            for folder_item in recorded_to_handle:
                chapter_processing_status = process_single_chapter_folder(
                    folder_item, base_folder_path, uploaded_chapter_record,
                    progress_bar_manager, live, manga_json_data, manga_main_groups,
                    manga_json_file_path, imgchest_api_key
                )
 
                if chapter_processing_status == CHAPTER_PROC_UPLOAD_SUCCESS:
                    newly_uploaded_or_reuploaded_count += 1
                elif chapter_processing_status == CHAPTER_PROC_SKIPPED_EXISTING_USER_CONFIRMED:
                    user_confirmed_skipped_count += 1
 
                save_upload_record(base_folder_path, uploaded_chapter_record, live)
                if any(t.id == overall_task_id for t in progress_bar_manager.tasks):
                    progress_bar_manager.update(overall_task_id, advance=1)
                live.console.line()
 
            # Upload new/unrecorded folders concurrently
            if pending_to_upload:
                live.console.print(f"[dim]Uploading {len(pending_to_upload)} new folder(s) concurrently...[/dim]")
                max_workers = min(4, len(pending_to_upload))
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                    future_to_folder = {
                        executor.submit(
                            upload_all_images_for_chapter_to_host,
                            get_image_files(folder.path, live),
                            imgchest_api_key,
                            folder.name,
                            progress_bar_manager,
                            live,
                        ): folder
                        for folder in pending_to_upload
                    }
 
                    for fut in concurrent.futures.as_completed(future_to_folder):
                        folder = future_to_folder[fut]
                        try:
                            res = fut.result()
                        except Exception as e:
                            live.console.print(f"[red]Upload raised exception for '{folder.name}': {e}[/red]")
                            res = {"success": False, "error": str(e)}
 
                        if res.get("success"):
                            post_id = res.get("post_id")
                            album_url = res.get("album_url")
                            total_uploaded = res.get("total_uploaded", 0)
 
                            # Update upload record
                            uploaded_chapter_record[folder.name] = {
                                "album_url": album_url,
                                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "image_count": str(total_uploaded),
                                "post_id": post_id
                            }
 
                            # Update manga_json_data with proxy entry and chapter meta (same as sequential path)
                            chapter_info = parse_folder_name(folder.name)
                            final_chapter_key = chapter_info.chapter
                            proxy_path = f"/proxy/api/imgchest/chapter/{post_id}" if post_id else None
                            ch_data: Dict[str, Any] = {
                                "title": chapter_info.title,
                                "last_updated": str(int(time.time())),
                                "groups": {manga_main_groups: proxy_path}
                            }
                            if chapter_info.volume:
                                ch_data["volume"] = chapter_info.volume
                            manga_json_data['chapters'][final_chapter_key] = ch_data
 
                            # Persist after each successful chapter update
                            save_manga_json(manga_json_file_path, manga_json_data, live)
 
                            live.console.print(f"[green]Uploaded '{folder.name}' -> {album_url} ({total_uploaded} images).[/green]")
                            newly_uploaded_or_reuploaded_count += 1
                        else:
                            live.console.print(f"[red]Upload failed for '{folder.name}': {res.get('error', 'Unknown')}[/red]")
 
                        # Persist upload record and advance overall progress
                        save_upload_record(base_folder_path, uploaded_chapter_record, live)
                        if any(t.id == overall_task_id for t in progress_bar_manager.tasks):
                            progress_bar_manager.update(overall_task_id, advance=1)
                        live.console.line()
 
            # Finalize overall progress bar
            if any(t.id == overall_task_id for t in progress_bar_manager.tasks):
                progress_bar_manager.update(overall_task_id, completed=len(folders_to_process), description="[bold green]Overall ImgChest Upload Progress Complete[/bold green]")
            live.console.line()

    save_manga_json(manga_json_file_path, manga_json_data)
    console.line()
    total_selected_for_upload = len(folders_to_process) if not is_github_only_choice else 0

    if is_github_only_choice:
        console.print(f"[green]Prepared to use existing '{manga_json_file_path.name}' for GitHub update.[/green]")
    elif total_selected_for_upload > 0:
        total_accounted_for_positively = newly_uploaded_or_reuploaded_count + user_confirmed_skipped_count
        failures_during_processing = total_selected_for_upload - total_accounted_for_positively

        summary_details = []
        if newly_uploaded_or_reuploaded_count > 0:
            summary_details.append(f"{newly_uploaded_or_reuploaded_count} uploaded/re-uploaded successfully")
        if user_confirmed_skipped_count > 0:
            summary_details.append(f"{user_confirmed_skipped_count} skipped (already existed)")
        if failures_during_processing > 0:
            summary_details.append(f"{failures_during_processing} failed")

        detail_str = f" ({'; '.join(summary_details)})" if summary_details else ""

        if failures_during_processing == 0 and total_accounted_for_positively == total_selected_for_upload :
            console.print(f"[bold green]🎉 All {total_accounted_for_positively}/{total_selected_for_upload} selected chapter folders processed successfully{detail_str}.[/bold green]")
        else:
            console.print(f"[bold yellow]⚠️ Chapter processing for {total_selected_for_upload} selected folders complete with issues{detail_str}.[/bold yellow]")
    else:
         if not is_github_only_choice:
            console.print("[yellow]No chapter image uploads were performed or selected.[/yellow]")

    console.print(f"Manga JSON reference: [cyan]{manga_json_file_path}[/cyan]")
    console.line()

    return {
        "manga_json_path": manga_json_file_path,
        "base_folder_path": base_folder_path,
        "manga_title": manga_title_for_json,
        "is_github_only_mode": is_github_only_choice
    }

# --- Main Application Logic ---
def main():
    console.print(Panel(RichText("Welcome to Kaguya!", justify="center", style="bold hot_pink"), border_style="hot_pink"))
    console.line()

    # --- Config file checks ---
    if not Path(API_KEY_FILE).exists():
        console.print(f"[yellow]ImgChest API key file ([cyan]{API_KEY_FILE}[/cyan]) not found.[/yellow]")
        create_sample_api_key_file()

    if not Path(GITHUB_CONFIG_FILE).exists():
        console.print(f"[yellow]GitHub configuration file ([cyan]{GITHUB_CONFIG_FILE}[/cyan]) not found.[/yellow]")
        create_sample_github_config()
    else:
        if not load_github_config():
            console.print(f"[yellow]Warning: GitHub configuration ([cyan]{GITHUB_CONFIG_FILE}[/cyan]) is invalid. GitHub uploads will not be possible until fixed.[/yellow]")
            console.line()

    upload_result_summary = run_chapter_upload_processing()

    if not upload_result_summary or not upload_result_summary.get("manga_json_path"):
        console.print("[yellow]Processing did not complete or no manga JSON file was specified. Exiting.[/yellow]")
        console.line()
        sys.exit(0)

    manga_json_local_path = Path(upload_result_summary["manga_json_path"])
    if not manga_json_local_path.is_file():
        console.print(f"[red]Error: Manga JSON file '{manga_json_local_path}' not found or is not a file. Cannot proceed.[/red]")
        console.line()
        sys.exit(1)

    manga_title_for_commit = upload_result_summary.get("manga_title", manga_json_local_path.stem)
    is_github_only_mode = upload_result_summary.get("is_github_only_mode", False)

    console.print("="*50)
    console.line()

    proceed_with_github = False
    if is_github_only_mode:
        console.print(f"[info]GitHub-only mode selected. Will attempt to upload '{manga_json_local_path.name}'.[/info]")
        proceed_with_github = True
    elif console.input(f"[bold cyan]Upload/Update manga JSON '[white]{manga_json_local_path.name}[/white]' on GitHub? (y/N):[/bold cyan] ").strip().lower() == 'y':
        proceed_with_github = True

    if proceed_with_github:
        console.line()
        github_config = load_github_config()
        if not github_config:
            console.print(f"[red]GitHub configuration ([cyan]{GITHUB_CONFIG_FILE}[/cyan]) is missing or invalid. Cannot upload to GitHub.[/red]")
            console.line()
            sys.exit(1)

        uploader = GitHubJSONUploader(
            token=github_config['token'],
            owner=github_config['owner'],
            repo=github_config['repo'],
            console_instance=console
        )

        repo_subfolder_prompt = (
            f"[cyan]Enter target subfolder in GitHub repo for '[white]{manga_json_local_path.name}[/white]' "
            f"(e.g., 'manga/seriesX').\nPress Enter for default (repository root): [/cyan]"
        )
        repo_subfolder = console.input(repo_subfolder_prompt).strip()
        console.line()

        repo_file_path_parts = [p.strip('/') for p in [repo_subfolder, manga_json_local_path.name] if p.strip('/')]
        repo_file_path_str = "/".join(repo_file_path_parts).replace("\\", "/")
        commit_message = f"Update: {manga_title_for_commit} ({manga_json_local_path.name})"

        console.print("[bold underline]🚀 Starting GitHub Upload...[/bold underline]")
        progress_columns_github = [
            SpinnerColumn(finished_text="[green]✓[/green]"),
            TextColumn("[progress.description]{task.description}", justify="left"),
            BarColumn(bar_width=None),
            RichTimeElapsedColumn()
        ]
        github_progress_manager = Progress(*progress_columns_github, console=console, transient=False, expand=True)
        github_upload_op_result = {}

        with Live(github_progress_manager, console=console, refresh_per_second=10, vertical_overflow="visible") as live_gh:
            gh_task_desc = f"Uploading [cyan]{manga_json_local_path.name}[/cyan] to GitHub"
            gh_task_id = github_progress_manager.add_task(gh_task_desc, total=1, fields={"is_byte_task": False})

            live_gh.console.print(f"Commit message: [dim]'{commit_message}'[/dim]")
            live_gh.console.line()

            github_upload_op_result = uploader.upload_file(
                local_file_path=str(manga_json_local_path),
                repo_file_path=repo_file_path_str,
                commit_message=commit_message
            )

            if github_upload_op_result.get("success"):
                if any(t.id == gh_task_id for t in github_progress_manager.tasks):
                    github_progress_manager.update(gh_task_id, advance=1, description=f"[green]Successfully uploaded [cyan]{manga_json_local_path.name}[/cyan][/green]")
            else:
                if any(t.id == gh_task_id for t in github_progress_manager.tasks):
                    github_progress_manager.update(gh_task_id, completed=1, description=f"[red]Upload FAILED for [cyan]{manga_json_local_path.name}[/cyan][/red]")

            live_gh.console.line()

        if github_upload_op_result.get("success"):
            cubari_item_for_log = {
                "title": manga_title_for_commit,
                "folder_path": str(manga_json_local_path.parent.resolve()),
                "file": manga_json_local_path.name,
                "repo_path": repo_file_path_str,
                "raw_url": github_upload_op_result["raw_url"],
                "cubari_url": github_upload_op_result["cubari_url"],
                "action": github_upload_op_result["action"],
                "last_modified": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            if "previous_last_modified" in github_upload_op_result:
                cubari_item_for_log["previous_last_modified_in_log"] = github_upload_op_result["previous_last_modified"]
            save_cubari_urls([cubari_item_for_log], mode="append")
        else:
            console.print(f"[bold red]GitHub upload for [cyan]{manga_json_local_path.name}[/cyan] was unsuccessful. See details above.[/bold red]")
            console.line()
    else:
        console.line()
        console.print("[dim]Skipped GitHub upload step.[/dim]")
        console.line()

    console.print("[bold magenta]All operations complete. Goodbye![/bold magenta]")
    console.line()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.line()
        console.print("[yellow]Process interrupted by user. Exiting.[/yellow]")
        console.line()
        sys.exit(1)
    except Exception as e:
        console.line()
        console.print(f"[bold red]An unexpected critical error occurred:[/bold red]")
        console.print_exception(show_locals=False)
        console.line()
        sys.exit(1)