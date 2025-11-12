import argparse
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from pathlib import Path as _Path
from typing import Optional
import concurrent.futures

# --- ImgChest upload implementation duplicated locally from kaguya.py ---
# This keeps process_cbz self-contained for image uploads and volume-aware records
import requests
from requests_toolbelt.multipart.encoder import (
    MultipartEncoder,
    MultipartEncoderMonitor,
)
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.progress import Task as _RichTask
from rich.text import Text as RichText

# Reuse helpers from kaguya.py
from kaguya import (
    ChapterInfo,
    ConditionalFileSizeColumn,
    # Progress helper columns from main script
    ConditionalTransferSpeedColumn,
    CustomTimeDisplayColumn,
    GitHubJSONUploader,
    console,
    find_subfolders_with_images,
    get_image_files,
    load_api_key,
    load_github_config,
    load_manga_info_from_txt,
    load_manga_json,
    load_upload_record,
    parse_folder_name,
    parse_folder_selection,
    regenerate_manga_json_from_folders,
    sanitize_filename,
    save_cubari_urls,
    save_manga_json,
    save_upload_record,
)

# Constants (local copy)
API_KEY_FILE_LOCAL = _Path("api_key.txt")
IMGCHEST_API_BASE_URL_LOCAL = "https://api.imgchest.com/v1"
MAX_IMAGES_PER_BATCH_LOCAL = 20
IMAGE_EXTENSIONS_LOCAL = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff"}


def load_api_key_local(file_path: _Path = API_KEY_FILE_LOCAL) -> Optional[str]:
    """Load ImgChest API key from a local file (duplicate of kaguya.load_api_key)."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        console.print(
            f"[red]Warning: ImgChest API key file '{file_path}' not found. Image uploads will be disabled unless you provide a key.[/red]"
        )
        return None
    except Exception as e:
        console.print(
            f"[red]Warning: Error reading ImgChest API key from {file_path}: {e}[/red]"
        )
        return None


# --- Mangabaka interactive lookup helpers ---
def mangabaka_search(query: str):
    """Search Mangabaka series API and return a list of results (best-effort parsing).
    If the query looks like a series ID (numeric, or prefixed with 'id:', or a mangabaka series URL),
    try the series-by-id endpoint first and return a single-item list on success.
    Otherwise fall back to the normal search endpoint.
    """
    q = (query or "").strip()
    if not q:
        return []

    # Helper to attempt a series-by-id fetch and normalize the result into a list
    def try_fetch_by_id(series_id: str):
        try:
            resp = requests.get(
                f"https://api.mangabaka.dev/v1/series/{series_id}", timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
            # Many APIs wrap the single item under 'data' or return the item directly
            if isinstance(data, dict):
                if "data" in data and isinstance(data["data"], dict):
                    return [data["data"]]
                return [data]
            elif isinstance(data, list):
                return data
        except Exception:
            return None

    # Normalize queries that look like explicit id forms:
    # - "id:12345"
    # - direct numeric ID "12345"
    # - a URL containing '/series/<id>'
    series_id_candidate = None
    lower_q = q.lower()
    if lower_q.startswith("id:"):
        series_id_candidate = q.split(":", 1)[1].strip()
    else:
        # Try to extract an ID from a Mangabaka series URL
        m = re.search(r"/series/([^/?#\s]+)", q, flags=re.IGNORECASE)
        if m:
            series_id_candidate = m.group(1).strip()
        else:
            # If the query is a single token without spaces and looks like an ID (digits or short alnum id),
            # consider trying it as an id first. Be conservative: require either all digits or
            # an alnum/[-_] token of reasonable length (2-64 chars).
            if " " not in q and re.match(r"^[0-9]+$", q):
                series_id_candidate = q
            elif " " not in q and re.match(r"^[A-Za-z0-9_-]{2,64}$", q):
                series_id_candidate = q

    # If we have a candidate, try the id endpoint first and fall back to search on failure.
    if series_id_candidate:
        try_result = try_fetch_by_id(series_id_candidate)
        if try_result:
            return try_result
        # if id lookup failed, fall through to search

    # Fallback: perform a text search using the search endpoint
    try:
        resp = requests.get(
            "https://api.mangabaka.dev/v1/series/search", params={"q": q}, timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            for key in ("data", "results", "series", "items"):
                if key in data and isinstance(data[key], list):
                    return data[key]
            for v in data.values():
                if isinstance(v, list):
                    return v
        elif isinstance(data, list):
            return data
    except Exception:
        return []
    return []


def _extract_manga_field(item: dict, *keys):
    """Helper to get first non-empty value from item for provided keys."""
    for k in keys:
        v = item.get(k)
        if isinstance(v, (list, tuple)) and v:
            # join lists like authors/artists if present
            try:
                return ", ".join([str(x) for x in v])
            except Exception:
                return str(v)
        if v:
            return str(v)
    return ""


def _clean_description(raw_html: str) -> str:
    """Convert basic HTML in descriptions to plain text and unescape entities."""
    if not raw_html:
        return ""
    import html as _html

    # Normalize common HTML line breaks to newlines
    s = raw_html.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    # Strip any remaining tags
    s = re.sub(r"<[^>]+>", "", s)
    # Unescape HTML entities
    s = _html.unescape(s)
    # Collapse multiple blank lines and trim
    s = re.sub(r"\n\s*\n+", "\n\n", s).strip()
    return s


def _pick_raw_cover(item: dict) -> str:
    """
    Prefer the API 'raw' cover URL when available.
    Accepts various shapes: cover may be string or dict with multiple sizes.
    """
    # Common keys that might contain cover metadata
    for key in ("cover", "image", "thumbnail", "thumbnail_url", "cover_url"):
        v = item.get(key)
        if not v:
            continue
        if isinstance(v, str):
            return v
        if isinstance(v, dict):
            # Try 'raw' first, then 'default', then common size keys
            for sub in ("raw", "default", "small", "large", "x350", "x250", "x150"):
                subval = v.get(sub)
                if subval:
                    # If subval is dict (like x350:{x1:...,x2:...}), try x1 first
                    if isinstance(subval, dict):
                        for inner in ("x1", "1", "url"):
                            if subval.get(inner):
                                return subval.get(inner)
                        # fallback to any value
                        try:
                            return next(iter(subval.values()))
                        except Exception:
                            continue
                    else:
                        return subval
            # fallback: any string-like value inside dict
            for vv in v.values():
                if isinstance(vv, str):
                    return vv
                if isinstance(vv, dict):
                    # deeper fallback
                    for inner in vv.values():
                        if isinstance(inner, str):
                            return inner
    return ""


def prompt_mangabaka_and_create_info(target_folder: Path) -> bool:
    """
    Interactive prompt to let the user search Mangabaka and write an info.txt in target_folder.
    Returns True if info.txt was created, False otherwise.
    """
    console.line()
    console.print("[bold cyan]Mangabaka interactive lookup[/bold cyan]")
    console.print(
        "[dim]Enter an empty search to cancel and continue without creating info.txt.[/dim]"
    )
    while True:
        query = console.input(
            "[cyan]Search Mangabaka for series (title/keyword/id):[/cyan] "
        ).strip()
        if not query:
            console.print("[yellow]Mangabaka lookup canceled by user.[/yellow]")
            return False
        results = mangabaka_search(query)
        if not results:
            console.print(
                f"[yellow]No results found for '{query}'. Try another search.[/yellow]"
            )
            continue

        # Limit displayed results to first 10 for readability
        display_results = results[:10]
        console.line()
        console.print(
            f"[bold]Found {len(results)} result(s). Showing first {len(display_results)}:[/bold]"
        )
        for idx, item in enumerate(display_results, start=1):
            title = _extract_manga_field(
                item, "title", "name", "enTitle", "romajiTitle"
            )
            authors = _extract_manga_field(item, "author", "authors", "author_string")
            artists = _extract_manga_field(item, "artist", "artists")
            synopsis = _extract_manga_field(
                item, "synopsis", "description", "detail", "overview"
            )
            short_syn = (
                (synopsis[:180] + "...")
                if synopsis and len(synopsis) > 180
                else synopsis
            )
            console.print(
                f"{idx:2d}. [cyan]{title}[/cyan] • Author(s): {authors or 'N/A'} • Artist(s): {artists or 'N/A'}"
            )
            if short_syn:
                console.print(f"     [dim]{short_syn}[/dim]")
        console.line()
        sel = (
            console.input(
                "[cyan]Select number to use (or 's' to search again, blank to cancel):[/cyan] "
            )
            .strip()
            .lower()
        )
        if sel == "":
            console.print("[yellow]Mangabaka lookup canceled by user.[/yellow]")
            return False
        if sel == "s":
            continue
        try:
            choice = int(sel)
            if not (1 <= choice <= len(display_results)):
                console.print("[red]Invalid selection number. Try again.[/red]")
                continue
            chosen = display_results[choice - 1]
            # Map fields sensibly into info.txt structure
            title = (
                _extract_manga_field(chosen, "title", "name", "enTitle", "romajiTitle")
                or target_folder.name
            )
            raw_description = _extract_manga_field(
                chosen, "synopsis", "description", "detail", "overview"
            )
            description = _clean_description(raw_description)
            author = _extract_manga_field(chosen, "author", "authors", "author_string")
            artist = _extract_manga_field(chosen, "artist", "artists")
            cover = _pick_raw_cover(chosen) or ""

            # Prompt the user for group name(s) because Mangabaka has no group endpoint.
            console.line()
            console.print(
                "[dim]Mangabaka doesn't provide group info. You can enter the scanlation group(s) now (comma-separated), or leave blank to fill later.[/dim]"
            )
            groups_input = console.input(
                "[cyan]Enter group name(s) (separated by commas) or leave blank:[/cyan] "
            ).strip()
            # Normalize groups: join with ', ' and strip extra whitespace
            if groups_input:
                groups_value = ", ".join(
                    [g.strip() for g in groups_input.split(",") if g.strip()]
                )
            else:
                groups_value = ""

            info_file = Path(target_folder) / "info.txt"
            try:
                with open(info_file, "w", encoding="utf-8") as f:
                    f.write(f"title: {title}\n")
                    if description:
                        # write description preserving newlines as a readable block
                        # collapse multiple consecutive blank lines to two for readability
                        desc_norm = re.sub(r"\n\s*\n+", "\n\n", description).rstrip()
                        f.write("description: |\n")
                        for line in desc_norm.split("\n"):
                            f.write(f"  {line}\n")
                    if artist:
                        f.write(f"artist: {artist}\n")
                    if author:
                        f.write(f"author: {author}\n")
                    if cover:
                        f.write(f"cover: {cover}\n")
                    # Write groups (either empty or the user-provided value)
                    f.write(f"groups: {groups_value}\n")
                console.print(f"[green]info.txt created at: {info_file}[/green]")
                return True
            except Exception as e:
                console.print(f"[red]Failed to write info.txt: {e}[/red]")
                return False
        except ValueError:
            console.print(
                "[red]Invalid input. Enter a number, 's' to search again, or blank to cancel.[/red]"
            )
            continue


def _perform_image_upload_to_host_local(
    url: str, api_key: str, image_files_batch, progress: Progress, task_description: str
):
    """Perform a multipart upload of a list of image Paths to the given URL.
    Returns a dict with success/data or success/error.
    """
    files_to_upload_fields = []
    opened_files = []
    upload_task_id = None
    try:
        for file_path in image_files_batch:
            try:
                fh = open(file_path, "rb")
                opened_files.append(fh)
                files_to_upload_fields.append(
                    ("images[]", (file_path.name, fh, f"image/{file_path.suffix[1:]}"))
                )
            except IOError as e:
                if upload_task_id is not None:
                    try:
                        progress.remove_task(upload_task_id)
                    except Exception:
                        pass
                return {
                    "success": False,
                    "error": f"Error opening file {file_path.name}: {e}",
                }

        if not files_to_upload_fields:
            return {
                "success": False,
                "error": "No valid image files to upload in this batch.",
            }

        encoder = MultipartEncoder(fields=files_to_upload_fields)
        try:
            upload_task_id = progress.add_task(
                task_description, total=encoder.len, fields={"is_byte_task": True}
            )
        except Exception:
            # If adding a task fails, continue without progress tracking
            upload_task_id = None

        monitor = MultipartEncoderMonitor(
            encoder,
            lambda m: progress.update(upload_task_id, completed=m.bytes_read)
            if upload_task_id and any(t.id == upload_task_id for t in progress.tasks)
            else None,
        )
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": monitor.content_type,
        }

        response = requests.post(url, data=monitor, headers=headers, timeout=300)
        if response.status_code == 200:
            try:
                data = response.json()
                if "error" in data or ("status" in data and data["status"] == "error"):
                    return {
                        "success": False,
                        "error": data.get(
                            "error", data.get("message", "Unknown API error from host")
                        ),
                    }
                return {"success": True, "data": data}
            except ValueError:
                return {
                    "success": False,
                    "error": "Invalid JSON response from image hosting API.",
                }
        else:
            return {
                "success": False,
                "error": f"Image hosting API HTTP {response.status_code}: {response.text}",
            }
    except requests.exceptions.RequestException as e:
        return {"success": False, "error": f"Image hosting request failed: {e}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error during image upload: {e}"}
    finally:
        if upload_task_id is not None:
            try:
                task_exists = any(t.id == upload_task_id for t in progress.tasks)
                if task_exists:
                    current_task_idx = progress.task_ids.index(upload_task_id)
                    current_task = progress.tasks[current_task_idx]
                    if not current_task.finished:
                        progress.update(upload_task_id, completed=current_task.total)
                    progress.remove_task(upload_task_id)
            except Exception:
                pass
        for fh in opened_files:
            try:
                fh.close()
            except Exception:
                pass


def upload_initial_batch_to_host_local(
    image_files_batch,
    api_key: str,
    chapter_name: str,
    batch_idx_info: str,
    progress: Progress,
):
    url = f"{IMGCHEST_API_BASE_URL_LOCAL}/post"
    task_description = (
        f"[cyan]ImgChest Batch (Create Album)[/cyan]: {chapter_name} ({batch_idx_info})"
    )
    result = _perform_image_upload_to_host_local(
        url, api_key, image_files_batch, progress, task_description
    )
    if result.get("success") and "data" in result:
        api_data = result["data"].get("data", {})
        if "id" in api_data:
            return {
                "success": True,
                "album_url": f"https://imgchest.com/p/{api_data['id']}",
                "post_id": api_data["id"],
                "total_images": len(api_data.get("images", [])),
            }
        return {"success": False, "error": "ImgChest API response missing post ID."}
    return result


def add_images_to_existing_album_on_host_local(
    image_files_batch,
    post_id: str,
    api_key: str,
    chapter_name: str,
    batch_idx_info: str,
    progress: Progress,
):
    url = f"{IMGCHEST_API_BASE_URL_LOCAL}/post/{post_id}/add"
    task_description = (
        f"[cyan]ImgChest Batch (Add Images)[/cyan]: {chapter_name} ({batch_idx_info})"
    )
    result = _perform_image_upload_to_host_local(
        url, api_key, image_files_batch, progress, task_description
    )
    if result.get("success"):
        return {"success": True, "added_images": len(image_files_batch)}
    return result


def chunk_list_local(lst, chunk_size):
    return [lst[i : i + chunk_size] for i in range(0, len(lst), chunk_size)]


def upload_all_images_for_chapter_to_host_local(
    image_files, api_key, chapter_name_for_desc, progress: Progress, live: Live
):
    """Uploads image files in batches, creating an album then adding images in subsequent batches."""
    if not image_files:
        live.console.print(
            f"[dim]Info: No image files for '{chapter_name_for_desc}'.[/dim]"
        )
        return {
            "success": False,
            "error": "No image files for upload.",
            "total_uploaded": 0,
        }

    image_chunks = chunk_list_local(image_files, MAX_IMAGES_PER_BATCH_LOCAL)
    total_chunks, total_uploaded_count = len(image_chunks), 0
    post_id = None
    album_url = None
    chapter_batch_task_id = None

    try:
        chapter_batch_task_id = progress.add_task(
            f"[blue]ImgChest Upload Batches '{chapter_name_for_desc}'[/blue]",
            total=total_chunks,
            fields={"is_byte_task": False},
        )
        for i, chunk in enumerate(image_chunks):
            batch_info_str = f"Batch {i + 1}/{total_chunks}"
            current_op_desc = "Create Album" if i == 0 else "Add Images"
            if chapter_batch_task_id and any(
                t.id == chapter_batch_task_id for t in progress.tasks
            ):
                progress.update(
                    chapter_batch_task_id,
                    description=f"[blue]ImgChest '{chapter_name_for_desc}'[/blue] ({batch_info_str} - {current_op_desc})",
                )

            if i == 0:
                res = upload_initial_batch_to_host_local(
                    chunk, api_key, chapter_name_for_desc, batch_info_str, progress
                )
                if not res.get("success"):
                    live.console.print(
                        f"[red]❌ Error creating ImgChest album for '{chapter_name_for_desc}': {res.get('error', 'Unknown')}[/red]"
                    )
                    return {
                        "success": False,
                        "error": f"Failed to create album: {res.get('error', 'Unknown')}",
                        "total_uploaded": 0,
                    }
                post_id, album_url = res["post_id"], res["album_url"]
                total_uploaded_count += res.get("total_images", len(chunk))
                live.console.line()
                live.console.print(
                    f"[green]✓ Album created for '{chapter_name_for_desc}': {album_url} ({res.get('total_images', len(chunk))} images).[/green]"
                )
                live.console.line()
            else:
                if not post_id:
                    live.console.print(
                        f"[red]❌ Critical: Album post_id missing for '{chapter_name_for_desc}'.[/red]"
                    )
                    return {
                        "success": False,
                        "error": "post_id missing for adding images",
                        "total_uploaded": total_uploaded_count,
                    }
                time.sleep(1)
                res = add_images_to_existing_album_on_host_local(
                    chunk,
                    post_id,
                    api_key,
                    chapter_name_for_desc,
                    batch_info_str,
                    progress,
                )
                if res.get("success"):
                    total_uploaded_count += res.get("added_images", len(chunk))
                    live.console.line()
                    live.console.print(
                        f"[green]✓ Added {res.get('added_images', len(chunk))} images to album '{chapter_name_for_desc}'.[/green]"
                    )
                    live.console.line()
                else:
                    live.console.print(
                        f"[red]❌ Error adding batch {i + 1} to album '{chapter_name_for_desc}': {res.get('error', 'Unknown')}[/red]"
                    )
                    return {
                        "success": False,
                        "error": f"Failed image upload batch {i + 1}: {res.get('error', 'Unknown')}",
                        "total_uploaded": total_uploaded_count,
                        "album_url": album_url,
                        "post_id": post_id,
                    }

            if chapter_batch_task_id and any(
                t.id == chapter_batch_task_id for t in progress.tasks
            ):
                progress.update(chapter_batch_task_id, advance=1)
    finally:
        if chapter_batch_task_id:
            try:
                task_exists = any(t.id == chapter_batch_task_id for t in progress.tasks)
                if task_exists:
                    current_task_idx = progress.task_ids.index(chapter_batch_task_id)
                    current_task = progress.tasks[current_task_idx]
                    if not current_task.finished:
                        progress.update(
                            chapter_batch_task_id, completed=current_task.total
                        )
                    progress.remove_task(chapter_batch_task_id)
            except Exception:
                pass

    return {
        "success": True,
        "album_url": album_url,
        "post_id": post_id,
        "total_uploaded": total_uploaded_count,
    }


# --- end of local ImgChest upload implementation ---


# Helper: robust lookup in upload record
def find_matching_upload_record_key(uploaded_record: dict, name: str):
    """Return the key in uploaded_record that best matches 'name', or None if not found.

    Matching strategy (in order):
    - exact key match
    - case-insensitive key match
    - sanitized filename match (both sides), case-insensitive
    """
    if not uploaded_record:
        return None
    # Exact match
    if name in uploaded_record:
        return name
    # Case-insensitive exact match
    name_lower = name.lower()
    for k in uploaded_record.keys():
        if k.lower() == name_lower:
            return k
    # Sanitized match
    try:
        name_san = sanitize_filename(name).lower()
    except Exception:
        name_san = name.lower()
    for k in uploaded_record.keys():
        try:
            k_san = sanitize_filename(k).lower()
        except Exception:
            k_san = k.lower()
        if k_san == name_san:
            return k
    return None


def find_record_by_volume(uploaded_record: dict, volume: str):
    """Return a key from uploaded_record that has a matching explicit 'volume' value or a volume
    extractable from the key string. Returns the key or None if not found.
    Normalizes numeric volumes (e.g., "01" -> "1") for comparison.
    """
    if not uploaded_record or not volume:
        return None
    try:
        vol_norm = str(int(str(volume)))
    except Exception:
        vol_norm = str(volume).strip()
    for key, rec in uploaded_record.items():
        try:
            # Prefer explicit volume field in the record entry
            if isinstance(rec, dict):
                v = rec.get("volume")
                if v is not None and str(v).strip() != "":
                    try:
                        kv = str(int(str(v)))
                    except Exception:
                        kv = str(v).strip()
                else:
                    # Fallback: try extracting from the key string itself
                    kv_match = re.search(
                        r"(?i)\b(?:v|vol|volume)[\s._-]*0*([0-9]+)\b", key
                    )
                    kv = kv_match.group(1) if kv_match else None
                if kv and vol_norm and str(kv) == str(vol_norm):
                    return key
        except Exception:
            continue
    return None


# Helper: determine if a .cbz file likely corresponds to an existing upload record
def cbz_file_appears_uploaded(
    uploaded_record: dict, cbz_name: str, record_folder_name: str
) -> bool:
    """Check if cbz_name corresponds to an existing upload record entry.

    The upload record can contain:
    1. Aggregate CBZ entries (e.g., "manga_vol1.cbz") added after processing
    2. Individual chapter entries (e.g., "Ch1", "Ch2", "V1 Ch3")

    Strategy:
    - First check for exact CBZ filename match (aggregate entry)
    - Then check for volume-specific matches requiring exact volume numbers.
      Additionally, treat a CBZ as uploaded if any existing record entry has a matching
      explicit 'volume' value (conservative: same numeric volume => uploaded).
    """
    if not uploaded_record:
        return False

    # Check for exact filename match first (most reliable)
    if cbz_name in uploaded_record:
        return True

    # Remove extension and get base name
    base = re.sub(r"\.cbz$", "", cbz_name, flags=re.IGNORECASE)

    # Extract volume number if present
    vol_match = re.search(r"(?i)\b(?:v|vol|volume)[\s._-]*0*([0-9]+)\b", base)
    cbz_vol_num = vol_match.group(1) if vol_match else None

    # If CBZ has no volume number, can't reliably match against chapters
    if not cbz_vol_num:
        return False

    # First, conservative fast-path: if any recorded entry has an explicit 'volume'
    # that equals this CBZ's volume, treat the CBZ as uploaded.
    try:
        for key, rec in uploaded_record.items():
            try:
                if isinstance(rec, dict):
                    v = rec.get("volume")
                    if v is not None and str(v).strip() != "":
                        try:
                            if str(int(str(v))) == str(int(cbz_vol_num)):
                                return True
                        except Exception:
                            if str(v).strip() == str(cbz_vol_num):
                                return True
            except Exception:
                continue
    except Exception:
        # Fall back to slower matching below on any error
        pass

    # Normalize CBZ name
    try:
        cbz_san = sanitize_filename(base).lower()
    except Exception:
        cbz_san = base.lower()

    # Create base name without volume markers for comparison
    cbz_base_no_vol = re.sub(
        r"(?i)\b(?:v|vol|volume)[\s._-]*0*[0-9]+\b", "", cbz_san
    ).strip()

    # Check each key in the upload record using the existing heuristic (name similarity + volume)
    for key in uploaded_record.keys():
        try:
            key_san = sanitize_filename(key).lower()
        except Exception:
            key_san = key.lower()

        # Prefer an explicit 'volume' stored in the upload record entry when available.
        # This allows more reliable matching when detecting whether a CBZ (volume)
        # has already been uploaded.
        key_vol_num = None
        try:
            rec = uploaded_record.get(key, {})
            if isinstance(rec, dict):
                v = rec.get("volume")
                if v is not None and str(v).strip() != "":
                    # Normalize numeric-looking volumes to plain ints-as-strings (e.g., "01" -> "1")
                    try:
                        key_vol_num = str(int(str(v)))
                    except Exception:
                        key_vol_num = str(v).strip()
        except Exception:
            key_vol_num = None

        # If no stored volume was present in the record, fall back to extracting from the key string
        if not key_vol_num:
            key_vol_match = re.search(
                r"(?i)\b(?:v|vol|volume)[\s._-]*0*([0-9]+)\b", key_san
            )
            key_vol_num = key_vol_match.group(1) if key_vol_match else None

        # Only match if both have volume numbers AND they're the same
        if cbz_vol_num and key_vol_num and cbz_vol_num == key_vol_num:
            # Same volume - check if base names are similar enough
            key_base_no_vol = re.sub(
                r"(?i)\b(?:v|vol|volume)[\s._-]*0*[0-9]+\b", "", key_san
            ).strip()
            # Require substantial overlap (at least 3 chars to avoid false positives)
            if (
                cbz_base_no_vol
                and key_base_no_vol
                and len(cbz_base_no_vol) > 2
                and len(key_base_no_vol) > 2
                and (
                    cbz_base_no_vol in key_base_no_vol
                    or key_base_no_vol in cbz_base_no_vol
                )
            ):
                return True

    return False


def add_cbz_aggregate_to_record(record_base: Path, cbz_filename: str) -> bool:
    """Add an aggregate entry for a CBZ file to the upload record.

    This helps track that a volume has been processed even if only chapter
    entries exist in the record.

    Additionally, when a volume number can be inferred from the CBZ filename,
    propagate that volume number into existing chapter records that are missing
    a 'volume' field so later heuristics and matching can use the volume info.

    Returns True if entry was added, False otherwise.
    """

    def _propagate_volume_to_chapter_records(
        uploaded_record_local: dict, volume_str: str
    ) -> None:
        """Set 'volume' on chapter records that lack it (in-place)."""
        if not volume_str or not isinstance(uploaded_record_local, dict):
            return
        try:
            for k, rec in uploaded_record_local.items():
                if isinstance(rec, dict):
                    if not rec.get("volume"):
                        rec["volume"] = str(volume_str)
        except Exception:
            # Non-fatal; propagation is best-effort
            pass

    try:
        uploaded_record = (
            load_upload_record_local(record_base) if record_base.exists() else {}
        )
        if not uploaded_record:
            return False

        # Don't add if already exists
        if cbz_filename in uploaded_record:
            return False

        # Sum up images from all chapters to create aggregate
        total_images = 0
        first_album = None
        for rec in uploaded_record.values():
            try:
                total_images += int(rec.get("image_count", 0) or 0)
            except Exception:
                pass
            if not first_album and rec.get("album_url"):
                first_album = rec.get("album_url")

        if total_images > 0 or first_album:
            # Try to detect volume number from the provided CBZ filename and store it in the aggregate entry
            try:
                base = re.sub(r"\.cbz$", "", cbz_filename, flags=re.IGNORECASE)
                vol_match = re.search(
                    r"(?i)\b(?:v|vol|volume)[\s._-]*0*([0-9]+)\b", base
                )
                cbz_volume = vol_match.group(1) if vol_match else ""
            except Exception:
                cbz_volume = ""

            # If we inferred a volume, propagate it to existing chapter entries (best-effort).
            try:
                _propagate_volume_to_chapter_records(uploaded_record, cbz_volume)
            except Exception:
                pass

            uploaded_record[cbz_filename] = {
                "album_url": first_album or "",
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "image_count": str(total_images),
                "post_id": "",
                "volume": cbz_volume,
            }
            save_upload_record(record_base, uploaded_record)
            return True
        return False
    except Exception:
        return False


def save_upload_record(
    base_folder_path: Path, uploaded_folders: dict, live: Optional[Live] = None
):
    record_file = base_folder_path / "imgchest_upload_record.txt"
    output_func = live.console.print if live else console.print
    try:
        with open(record_file, "w", encoding="utf-8") as f:
            f.write(f"# Manga Upload Record for {base_folder_path.name}\n")
            f.write(
                "# Format: folder_name|album_url|timestamp|image_count|post_id|volume\n"
            )
            f.write(f"# Generated on: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            for folder_name, data in uploaded_folders.items():
                album_url = data.get("album_url", "")
                timestamp = data.get("timestamp", "")
                image_count = data.get("image_count", "unknown")
                post_id = data.get(
                    "post_id", album_url.split("/")[-1] if album_url else ""
                )
                volume = data.get("volume", "")

                # If volume missing, attempt to infer it heuristically from:
                # 1) the folder/entry name itself (e.g., "V1 Ch3", "Vol01", "v02")
                # 2) the base folder name (likely the CBZ filename when processing .cbz)
                # 3) the parent folder name
                if not volume:
                    try:
                        m = re.search(
                            r"(?i)\b(?:v|vol|volume)[\s._-]*0*([0-9]+)\b", folder_name
                        )
                        if m:
                            volume = str(int(m.group(1)))
                    except Exception:
                        volume = volume or ""
                if not volume:
                    try:
                        m = re.search(
                            r"(?i)\b(?:v|vol|volume)[\s._-]*0*([0-9]+)\b",
                            base_folder_path.name,
                        )
                        if m:
                            volume = str(int(m.group(1)))
                    except Exception:
                        volume = volume or ""
                if not volume:
                    try:
                        if base_folder_path.parent:
                            m = re.search(
                                r"(?i)\b(?:v|vol|volume)[\s._-]*0*([0-9]+)\b",
                                base_folder_path.parent.name,
                            )
                            if m:
                                volume = str(int(m.group(1)))
                    except Exception:
                        volume = volume or ""

                f.write(
                    f"{folder_name}|{album_url}|{timestamp}|{image_count}|{post_id}|{volume}\n"
                )
        output_func(
            f"[green]Upload record ({record_file.name}) saved to: {record_file}[/green]"
        )
    except IOError as e:
        output_func(
            f"[red]Error: Could not save upload record to {record_file}: {e}[/red]"
        )


def load_upload_record_local(base_folder_path: Path) -> dict:
    """Load imgchest_upload_record.txt allowing either 5- or 6-column formats.

    Expected format (backwards compatible):
      folder_name|album_url|timestamp|image_count|post_id
    New extended format:
      folder_name|album_url|timestamp|image_count|post_id|volume

    Returns a dict mapping folder_name -> { album_url, timestamp, image_count, post_id, volume }
    """
    record_file = base_folder_path / "imgchest_upload_record.txt"
    uploaded = {}
    if not record_file.exists():
        return {}
    try:
        with open(record_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|")
                if len(parts) < 5:
                    # Malformed/old line - skip
                    continue
                folder_name = parts[0]
                album_url = parts[1] if len(parts) > 1 else ""
                timestamp = parts[2] if len(parts) > 2 else ""
                image_count = parts[3] if len(parts) > 3 else ""
                post_id = (
                    parts[4]
                    if len(parts) > 4
                    else (album_url.split("/")[-1] if album_url else "")
                )
                volume = parts[5] if len(parts) > 5 else ""
                uploaded[folder_name] = {
                    "album_url": album_url,
                    "timestamp": timestamp,
                    "image_count": image_count,
                    "post_id": post_id,
                    "volume": volume,
                }
    except Exception:
        # Best-effort: return whatever was parsed so far
        return uploaded
    return uploaded


# Helper: determine if an item (folder or .cbz file) should be considered uploaded
def is_item_uploaded(uploaded_record: dict, name: str, path: Path) -> bool:
    """Return True if uploaded_record indicates 'name' is uploaded.

    Safe matching: first use find_matching_upload_record_key (exact or sanitized).
    For .cbz files, also apply a conservative CBZ-to-record heuristic (cbz_file_appears_uploaded)
    which compares the sanitized CBZ base name against the record folder name and keys.
    """
    try:
        if not uploaded_record:
            return False
        # Robust direct/sanitized match
        if find_matching_upload_record_key(uploaded_record, name):
            return True
        # For .cbz files, try the CBZ-specific heuristic (does this CBZ map to existing record entries?)
        try:
            if isinstance(path, Path) and path.suffix and path.suffix.lower() == ".cbz":
                record_folder_name = path.parent.name if path.parent else ""
                if cbz_file_appears_uploaded(uploaded_record, name, record_folder_name):
                    return True
        except Exception:
            # Heuristic failure should not crash; fall back to conservative behavior
            pass
        return False
    except Exception:
        return False


def extract_cbz(cbz_path: Path, dest_dir: Path) -> Path:
    """Extract a .cbz (zip) into dest_dir and return path to extracted root."""
    with zipfile.ZipFile(cbz_path, "r") as zf:
        zf.extractall(dest_dir)
    # If extraction created a single top-level folder, return it, else return dest_dir
    entries = [p for p in dest_dir.iterdir()]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return dest_dir


def build_chapter_entry_from_folder(folder: Path, manga_main_group: str) -> (str, dict):
    """Create a chapter key and data dict from a folder or filename.

    This function improves on the generic parse by explicitly extracting
    'c' (chapter) and 'v' (volume) markers commonly found in filenames like:
      CITY - c001 (v01) - p000 [Cover] ...
    It falls back to the original parse_folder_name() behavior when markers
    are not present.
    """
    chapter_name = folder.name
    # Try to find explicit chapter and volume markers first (c### and v##).
    # Some filenames use forms like "c152x1" (part notation) where a \b word-boundary
    # after the digits fails; use a negative lookahead to ensure we capture the
    # numeric portion even when followed by letters (e.g. 'x1').
    c_match = re.search(r"(?i)c0*([0-9]+)(?!\d)", chapter_name)
    v_match = re.search(r"(?i)v0*([0-9]+)(?!\d)", chapter_name)

    if c_match:
        chapter_str = str(int(c_match.group(1)))
    else:
        chapter_str = ""

    if v_match:
        volume_str = str(int(v_match.group(1)))
    else:
        volume_str = ""

    if chapter_str:
        # Build ChapterInfo using extracted values and preserve title from parse_folder_name
        parsed = parse_folder_name(chapter_name)
        title = parsed.title or ""
        # If parse_folder_name mistakenly swapped volume/chapter, prefer explicit markers
        chapter_info = ChapterInfo(
            volume_str if volume_str else parsed.volume, chapter_str, title
        )
    else:
        # No explicit chapter marker found; fall back to original parsing
        chapter_info = parse_folder_name(chapter_name)

    key = chapter_info.chapter
    proxy_groups = {}
    # Ensure chapter title is present; fallback to "Ch.<number>" when missing
    title_val = (
        chapter_info.title
        if chapter_info.title
        else f"Ch.{chapter_info.chapter or '1'}"
    )
    ch_data = {
        "title": title_val,
        "last_updated": str(int(time.time())),
        "groups": proxy_groups,
    }
    if chapter_info.volume:
        ch_data["volume"] = chapter_info.volume
    return key, ch_data


def normalize_chapters(mj: dict):
    """Ensure each chapter entry has title, last_updated and groups keys."""
    chapters = mj.setdefault("chapters", {})
    for ch_key, ch_val in list(chapters.items()):
        if not isinstance(ch_val, dict):
            chapters[ch_key] = {
                "title": f"Ch.{ch_key if ch_key else '1'}",
                "last_updated": str(int(time.time())),
                "groups": {},
            }
            continue
        if not ch_val.get("title"):
            ch_val["title"] = f"Ch.{ch_key if ch_key else '1'}"
        if not ch_val.get("last_updated"):
            ch_val["last_updated"] = str(int(time.time()))
        if "groups" not in ch_val or not isinstance(ch_val["groups"], dict):
            ch_val["groups"] = {}

def populate_chapter_groups_from_root(mj: dict, manga_info: dict, overwrite: bool = False) -> None:
    """
    Ensure each chapter has group entries populated from the root manga_info 'groups' line.

    Behavior:
    - By default (overwrite=False) this will only add group keys when a chapter's "groups"
      mapping is missing or empty (non-destructive).
    - When overwrite=True the chapter's "groups" mapping will be replaced with the
      groups derived from manga_info (useful for regeneration where the root info.txt
      has changed and you want to apply the new groups to all chapters).
    """
    if not mj or not isinstance(mj, dict):
        return
    groups_str = manga_info.get("groups", "") if isinstance(manga_info, dict) else ""
    groups_list = [g.strip() for g in groups_str.split(",") if g.strip()] if groups_str else []
    if not groups_list:
        return
    chapters = mj.setdefault("chapters", {})

    # Track which chapters were actually modified for debugging visibility
    modified_chapters = []

    for ch_key, ch_val in chapters.items():
        try:
            if not isinstance(ch_val, dict):
                continue
            before = None
            try:
                before = dict(ch_val.get("groups", {}))
            except Exception:
                before = None

            if overwrite:
                # Replace existing groups mapping with the new list (preserve order, empty values)
                ch_val["groups"] = {grp: {} for grp in groups_list}
            else:
                ch_groups = ch_val.setdefault("groups", {})
                # Only populate if groups mapping is empty to avoid clobbering explicit values
                if not ch_groups:
                    for grp in groups_list:
                        ch_groups.setdefault(grp, {})

            after = None
            try:
                after = dict(ch_val.get("groups", {}))
            except Exception:
                after = None

            # Consider it modified if the mapping changed (including key differences)
            if before != after:
                modified_chapters.append(ch_key)
        except Exception:
            # Best-effort: ignore failures per-chapter
            continue

    # If running interactively, show a concise debug summary so users understand what changed.
    try:
        if modified_chapters:
            console.print(
                f"[cyan]populate_chapter_groups_from_root:[/cyan] applied groups {groups_list} "
                f"to chapters: {', '.join(map(str, modified_chapters))} (overwrite={overwrite})"
            )
    except Exception:
        # Don't fail on logging
        pass

def load_manga_info_from_txt_with_block_support(base_folder_path: Path) -> dict:
    """
    Wrapper around the original load_manga_info_from_txt that understands
    YAML-like block-style description fields (e.g.:
      description: |
        line1
        line2

    Behavior:
    - Calls the original loader to get basic single-line key parsing.
    - If the raw info.txt contains a block-style description, it will parse and
      replace the 'description' value with the multi-line text (dedented).
    - Also converts literal "\n" sequences into actual newlines.
    This function is best-effort and will fall back to the original loader on any error.
    """
    try:
        info = load_manga_info_from_txt(base_folder_path)
    except Exception:
        # If the underlying loader fails for any reason, return a minimal dict.
        info = {"title": base_folder_path.name, "description": "", "artist": "", "author": "", "cover": "", "groups": ""}

    info_file = Path(base_folder_path) / "info.txt"
    if not info_file.exists():
        # Still normalize any escaped newlines in the single-line value
        try:
            if isinstance(info.get("description"), str):
                info["description"] = info["description"].replace("\\n", "\n")
        except Exception:
            pass
        return info

    try:
        with open(info_file, "r", encoding="utf-8") as f:
            raw_lines = f.readlines()
    except Exception:
        # If we can't read the file, fall back to the already-parsed info
        try:
            if isinstance(info.get("description"), str):
                info["description"] = info["description"].replace("\\n", "\n")
        except Exception:
            pass
        return info

    # Look for a top-level 'description:' line and detect block marker '|' or '>'
    desc_block = None
    i = 0
    while i < len(raw_lines):
        line = raw_lines[i].rstrip("\n")
        m = re.match(r'^\s*description\s*:\s*(.*)$', line, flags=re.IGNORECASE)
        if m:
            after_colon = m.group(1).strip()
            # If the value is a block indicator (| or >) or empty, collect the following indented lines
            if after_colon in ("|", ">") or after_colon == "":
                i += 1
                collected = []
                while i < len(raw_lines):
                    ln = raw_lines[i].rstrip("\n")
                    # Stop when we encounter another top-level key like "artist:" (no leading indent)
                    if re.match(r'^[A-Za-z0-9 _-]+\s*:', ln) and not re.match(r'^[ \t]+', ln):
                        break
                    # Accept indented lines (remove a single leading level of indentation)
                    if re.match(r'^[ \t]+', ln):
                        collected.append(re.sub(r'^[ \t]{1,4}', '', ln))
                    else:
                        # blank lines or softly-indented text should be preserved
                        collected.append(ln)
                    i += 1
                desc_block = "\n".join(collected).rstrip("\n")
            else:
                # Inline value (e.g., description: This is a short description)
                desc_block = after_colon
            break
        i += 1

    if desc_block is not None:
        try:
            # Normalize escaped sequences like "\n" into real newlines, and strip trailing/leading extra newlines
            desc_block = desc_block.replace("\\n", "\n")
        except Exception:
            pass
        info["description"] = desc_block
    else:
        # No block found; still ensure escaped newlines are normalized
        try:
            if isinstance(info.get("description"), str):
                info["description"] = info["description"].replace("\\n", "\n")
        except Exception:
            pass

    return info


def normalize_description_in_info(manga_info: dict) -> None:
    """
    Keep a small helper for backward-compatible normalization: convert literal
    backslash+n sequences into actual newline characters. This is intentionally
    best-effort and will not raise on errors.
    """
    if not manga_info or not isinstance(manga_info, dict):
        return
    desc = manga_info.get("description")
    if not isinstance(desc, str):
        return
    try:
        manga_info["description"] = desc.replace("\\n", "\n")
    except Exception:
        pass


def restore_album_urls_from_upload_record(
    manga_json_data: dict, record_base: Path, manga_info: dict
) -> None:
    """
    Best-effort pass to restore ImgChest album URLs (as proxy entries) from the
    on-disk upload record (imgchest_upload_record.txt) into the provided
    manga_json_data['chapters'] mapping.

    This function is intentionally forgiving and attempts several matching
    strategies:
      1. Derive chapter number via parse_folder_name(record_key) and match exact.
      2. Compare sanitized record key against chapter keys and chapter titles.
      3. If record contains a 'volume', match chapters by volume when possible.

    For each matched chapter we will populate the chapter's 'groups' mapping with
    the proxy URL: "/proxy/api/imgchest/chapter/<post_id>" so regeneration will
    re-add previously uploaded album links into manga.json.
    """
    try:
        if not isinstance(manga_json_data, dict):
            return
        existing_record = (
            load_upload_record_local(record_base) if record_base and record_base.exists() else {}
        )
        if not existing_record:
            return
        chapters = manga_json_data.setdefault("chapters", {})
        for rec_key, rec in existing_record.items():
            try:
                album_url = rec.get("album_url", "") or ""
                post_id = rec.get("post_id") or (
                    album_url.split("/")[-1] if album_url else ""
                )
                if not album_url and not post_id:
                    # Nothing useful to restore
                    continue
                proxy = f"/proxy/api/imgchest/chapter/{post_id}" if post_id else ""
                # Strategy 1: use parse_folder_name to extract a chapter key
                parsed = None
                try:
                    parsed = parse_folder_name(rec_key)
                except Exception:
                    parsed = None
                candidate_ch_key = None
                if parsed and getattr(parsed, "chapter", None):
                    candidate_ch_key = parsed.chapter
                    if candidate_ch_key in chapters:
                        matched_key = candidate_ch_key
                    else:
                        matched_key = None
                else:
                    matched_key = None
                # Strategy 2: sanitized name/title matching
                if not matched_key:
                    try:
                        rec_san = sanitize_filename(rec_key).lower()
                    except Exception:
                        rec_san = rec_key.lower()
                    for ck, ch in chapters.items():
                        try:
                            ck_san = sanitize_filename(str(ck)).lower()
                        except Exception:
                            ck_san = str(ck).lower()
                        title = ch.get("title", "") if isinstance(ch, dict) else ""
                        try:
                            title_san = sanitize_filename(title).lower() if title else ""
                        except Exception:
                            title_san = title.lower() if title else ""
                        if (
                            rec_san == ck_san
                            or rec_san in ck_san
                            or ck_san in rec_san
                            or (title_san and (rec_san in title_san or title_san in rec_san))
                        ):
                            matched_key = ck
                            break
                # Strategy 3: volume-based fallback
                if not matched_key:
                    rec_vol = rec.get("volume") or ""
                    if rec_vol:
                        for ck, ch in chapters.items():
                            try:
                                ch_vol = ch.get("volume") or ""
                                if ch_vol:
                                    try:
                                        if str(int(str(ch_vol))) == str(int(str(rec_vol))):
                                            matched_key = ck
                                            break
                                    except Exception:
                                        if str(ch_vol).strip() == str(rec_vol).strip():
                                            matched_key = ck
                                            break
                            except Exception:
                                continue
                # If we found a match, attach the proxy to all groups derived from root info
                if matched_key:
                    groups_str = manga_info.get("groups", "") or ""
                    groups_list = [g.strip() for g in groups_str.split(",") if g.strip()] or ["UnknownGroup"]
                    ch_entry = chapters.setdefault(matched_key, {})
                    ch_entry.setdefault("groups", {})
                    for grp in groups_list:
                        # If a group already has a mapping, do not overwrite non-empty mappings;
                        # prefer to set only when empty to avoid clobbering more specific data.
                        existing_val = ch_entry["groups"].get(grp)
                        if not existing_val:
                            ch_entry["groups"][grp] = proxy if proxy else {}
                    # Restore title/last_updated when missing
                    if not ch_entry.get("title"):
                        ch_entry["title"] = parsed.title if parsed and getattr(parsed, "title", None) else f"Ch.{matched_key if matched_key else '1'}"
                    if not ch_entry.get("last_updated") and rec.get("timestamp"):
                        try:
                            dt = time.strptime(rec.get("timestamp"), "%Y-%m-%d %H:%M:%S")
                            ch_entry["last_updated"] = str(int(time.mktime(dt)))
                        except Exception:
                            ch_entry["last_updated"] = ch_entry.get("last_updated", str(int(time.time())))
            except Exception:
                # Per-record failures should not abort restoration
                continue
    except Exception:
        # Swallow all errors; this is best-effort recovery logic only
        return

def process_input_path(
    input_path: Path,
    output_json_base: Path,
    github_subfolder: str = "",
    upload_to_github: bool = False,
    upload_images: bool = False,
    imgchest_api_key: Optional[str] = None,
    save_json: bool = True,
):
    """Process either a folder or a .cbz file and update/generate manga.json.
    When upload_images is True, images will be uploaded to ImgChest using imgchest_api_key
    (or the key will be loaded from file if not provided).

    If save_json is False the function will NOT persist the manga_json to disk and will
    instead return a dict with the generated manga_json_data and manga_json_file so the
    caller can merge multiple results into one combined JSON.
    """
    temp_dir = None
    try:
        if input_path.is_file() and input_path.suffix.lower() == ".cbz":
            temp_dir_obj = tempfile.TemporaryDirectory(prefix="kaguya_cbz_")
            temp_dir = Path(temp_dir_obj.name)
            console.print(f"[dim]Extracting CBZ {input_path} -> {temp_dir}[/dim]")
            extracted_root = extract_cbz(input_path, temp_dir)
            base_folder = extracted_root
        elif input_path.is_dir():
            base_folder = input_path
        else:
            err_msg = f"Error: {input_path} is not a folder or a .cbz file"
            console.print(f"[red]{err_msg}[/red]")
            if save_json:
                return False
            return {"success": False, "error": err_msg}

        # Determine which folder's info.txt to use.
        # Prefer the provided output_json_base (manga root) when it contains an info.txt
        # so regeneration/combined runs will refresh metadata from the shared root.
        # Otherwise fall back to the input folder (or the parent folder when input is a file).
        try:
            if output_json_base and (Path(output_json_base) / "info.txt").exists():
                info_folder = Path(output_json_base)
            else:
                if input_path.is_file():
                    info_folder = input_path.parent
                else:
                    info_folder = input_path
        except Exception:
            # Defensive fallback
            info_folder = (
                Path(output_json_base)
                if output_json_base
                else (input_path.parent if input_path.is_file() else input_path)
            )
        manga_info = load_manga_info_from_txt_with_block_support(info_folder)
        # Normalize the description string here to ensure process_cbz writes/uses human-readable newlines.
        try:
            if manga_info and isinstance(manga_info, dict) and isinstance(manga_info.get("description"), str):
                manga_info["description"] = manga_info["description"].replace("\\n", "\n")
        except Exception:
            pass

        manga_title = manga_info.get("title") or base_folder.name
        manga_json_data, manga_json_file = load_manga_json(
            output_json_base, manga_title
        )

        # Use module-level normalize_chapters to ensure chapter defaults before saving.

        # Ensure meta fields
        for k in ("title", "description", "artist", "author", "cover"):
            if manga_info.get(k):
                manga_json_data[k] = manga_info[k]
            elif not manga_json_data.get(k) and k == "title":
                manga_json_data[k] = base_folder.name

        if "chapters" not in manga_json_data:
            manga_json_data["chapters"] = {}

        # If the input is a folder that contains chapter subfolders, iterate them.
        subfolders = find_subfolders_with_images(base_folder)
        # If no immediate subfolders contain images, try searching recursively.
        # Some CBZ files extract into a single top-level "volume" folder which
        # itself contains chapter folders; account for that case so chapters
        # inside nested folders are discovered and processed/uploaded.
        if not subfolders:
            nested_folders = []

            # Simple local lightweight container so code below can use .path/.name/.image_count like FolderDetails
            class _SimpleFolder:
                def __init__(self, p, n, c):
                    self.path = p
                    self.name = n
                    self.image_count = c

            # Look for any directory under the extracted root that directly contains image files.
            for d in base_folder.rglob("*"):
                if d.is_dir():
                    try:
                        image_count = sum(
                            1
                            for f in d.iterdir()
                            if f.is_file()
                            and f.suffix.lower()
                            in {
                                ".jpg",
                                ".jpeg",
                                ".png",
                                ".gif",
                                ".bmp",
                                ".webp",
                                ".tiff",
                            }
                        )
                    except Exception:
                        image_count = 0
                    if image_count > 0:
                        nested_folders.append(_SimpleFolder(d, d.name, image_count))
            if nested_folders:
                # Prefer alphabetical ordering by folder name to keep behaviour deterministic.
                subfolders = sorted(nested_folders, key=lambda x: x.name.lower())

        # Load upload record early so we can add proxy entries from previous uploads
        record_base = Path(output_json_base) if output_json_base else base_folder
        uploaded_record = (
            load_upload_record_local(record_base) if record_base.exists() else {}
        )

        # If subfolders are present and we are NOT performing image uploads here,
        # build manga.json chapter entries now using folder names and any existing upload record.
        # If upload_images is True we'll defer creating/updating entries until after uploading
        # (the upload loop will populate manga_json_data with proxy info).
        if subfolders:
            if not upload_images:
                for fd in subfolders:
                    key, ch = build_chapter_entry_from_folder(
                        fd.path, manga_info.get("groups", "UnknownGroup")
                    )
                    # If already uploaded, attach proxy info (use robust lookup for record keys)
                    match_key = find_matching_upload_record_key(
                        uploaded_record, fd.name
                    )
                    if match_key:
                        rec = uploaded_record[match_key]
                        post_id = rec.get("post_id") or (
                            rec.get("album_url", "").split("/")[-1]
                            if rec.get("album_url")
                            else None
                        )
                        if post_id:
                            # Support multiple comma-separated groups from info.txt.
                            groups_str = manga_info.get("groups", "") or ""
                            groups_list = [
                                g.strip() for g in groups_str.split(",") if g.strip()
                            ] or ["UnknownGroup"]
                            for grp in groups_list:
                                ch.setdefault("groups", {})[grp] = (
                                    f"/proxy/api/imgchest/chapter/{post_id}"
                                )
                        # Try to set last_updated based on record timestamp
                        ts_str = rec.get("timestamp")
                        try:
                            if ts_str:
                                dt = time.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                                ch["last_updated"] = str(int(time.mktime(dt)))
                        except Exception:
                            ch["last_updated"] = ch.get(
                                "last_updated", str(int(time.time()))
                            )
                    # Ensure chapter has group keys derived from the root info.txt 'groups' line.
                    groups_str = manga_info.get("groups", "") or ""
                    groups_list = [
                        g.strip() for g in groups_str.split(",") if g.strip()
                    ] or ["UnknownGroup"]
                    for grp in groups_list:
                        ch.setdefault("groups", {})[grp] = {}
                    manga_json_data["chapters"][key] = ch
                    console.print(f"[green]Added chapter {key}: {fd.name}[/green]")
            else:
                # Defer to upload section which will populate manga_json_data for each chapter
                pass
        else:
            # Treat base_folder as a single chapter (images directly inside) OR group by filename chapter markers like c001/c002
            images = get_image_files(base_folder)
            if not images:
                console.print(
                    f"[yellow]No images found in {base_folder}. Nothing to add to manga.json[/yellow]"
                )
            else:
                # Group images by chapter marker in filename, e.g., 'c001', 'c002'
                chapter_groups = {}
                # Match 'c001', 'C001', 'c1', etc. Use (?!\d) so patterns match cases like
                # 'c152x1' (we capture '152' even though it's followed by 'x1').
                chap_pattern = re.compile(r"(?i)(?:^|[^A-Za-z0-9])c0*([0-9]+)(?!\d)")
                vol_pattern = re.compile(r"(?i)v0*([0-9]+)(?!\d)")
                for img in images:
                    name = img.name
                    # Skip obvious cover images from grouping (any appearance of the word 'cover')
                    if "cover" in name.lower():
                        continue
                    m = chap_pattern.search(name)
                    if m:
                        chap_num = str(int(m.group(1)))  # normalize e.g. "001" -> "1"
                    else:
                        chap_num = "1"
                    vm = vol_pattern.search(name)
                    vol_num = str(int(vm.group(1))) if vm else ""
                    # Store tuple of (Path, volume_str) so volume info is available per-file
                    chapter_groups.setdefault(chap_num, []).append((img, vol_num))

                # Create manga.json entries per detected chapter
                for chap_num, items_in_group in sorted(
                    chapter_groups.items(), key=lambda x: int(x[0])
                ):
                    key = chap_num
                    # Determine a representative volume for this chapter (first non-empty vol)
                    vol_for_group = next((v for (_, v) in items_in_group if v), "")
                    # Use a pseudo-folder name to reuse parsing logic for volume/title extraction
                    if vol_for_group:
                        fake_folder = Path(f"V{vol_for_group} Ch{int(chap_num)}")
                    else:
                        fake_folder = Path(f"Ch{int(chap_num)}")

                    # If uploading images, extract the Path list; else just count images
                    img_paths = [t[0] for t in items_in_group]

                    # Build chapter data (parse volume from fake_folder name)
                    k, ch = build_chapter_entry_from_folder(
                        fake_folder, manga_info.get("groups", "UnknownGroup")
                    )
                    # Ensure volume is kept in the chapter data if detected
                    if vol_for_group:
                        ch["volume"] = vol_for_group
                    # Populate chapter-level group keys from the root info.txt 'groups' line.
                    groups_str = manga_info.get("groups", "") or ""
                    groups_list = [
                        g.strip() for g in groups_str.split(",") if g.strip()
                    ] or ["UnknownGroup"]
                    for grp in groups_list:
                        ch.setdefault("groups", {})[grp] = {}
                    manga_json_data["chapters"][key] = ch
                    console.print(
                        f"[green]Added chapter {key} from filenames ({len(img_paths)} images){' (V' + vol_for_group + ')' if vol_for_group else ''}[/green]"
                    )

        # Save JSON to the output base folder (usually same as base_folder)
        # If we're not going to upload images right now, persist the JSON immediately.
        if save_json and not upload_images:
            populate_chapter_groups_from_root(manga_json_data, manga_info)
            populate_chapter_groups_from_root(manga_json_data, refreshed_info if 'refreshed_info' in locals() else manga_info)
            # Ensure no root-level "groups" key remains; groups should live per-chapter only.
            manga_json_data.pop("groups", None)
            # Best-effort: restore album URLs from the on-disk upload record so regenerated JSON preserves proxies.
            try:
                restore_album_urls_from_upload_record(manga_json_data, record_base, manga_info)
            except Exception:
                # Non-fatal: continue even if restoration fails
                pass
            normalize_chapters(manga_json_data)
            save_manga_json(manga_json_file, manga_json_data)

        # If requested, upload images to ImgChest and update manga_json accordingly.
        # When processing a .cbz that extracts into a volume containing chapter subfolders,
        # the desired behaviour is to upload each chapter first and then write the final
        # manga.json with all chapter entries updated to point to the uploaded albums.
        if upload_images:
            api_key = imgchest_api_key or load_api_key_local()
            if not api_key:
                console.print(
                    "[yellow]ImgChest API key missing; skipping image uploads.[/yellow]"
                )
            else:
                # Determine where to store the upload record (use output_json_base when available)
                record_base = (
                    Path(output_json_base) if output_json_base else base_folder
                )
                uploaded_record = (
                    load_upload_record_local(record_base)
                    if record_base.exists()
                    else {}
                )

                # Determine source volume (best-effort) so uploaded chapter records record
                # the volume they were uploaded from. Prefer the CBZ filename (if input was a .cbz),
                # then the extracted root folder name, then the parent folder name.
                source_volume = ""
                try:
                    if input_path.is_file() and input_path.suffix.lower() == ".cbz":
                        base_cbz = re.sub(
                            r"\.cbz$", "", input_path.name, flags=re.IGNORECASE
                        )
                        m = re.search(
                            r"(?i)\b(?:v|vol|volume)[\s._-]*0*([0-9]+)\b", base_cbz
                        )
                        if m:
                            source_volume = str(int(m.group(1)))
                    else:
                        # Check the extracted root folder and its parent for volume markers
                        candidates = [base_folder.name]
                        try:
                            if base_folder.parent:
                                candidates.append(base_folder.parent.name)
                        except Exception:
                            pass
                        for candidate in candidates:
                            if not candidate:
                                continue
                            m = re.search(
                                r"(?i)\b(?:v|vol|volume)[\s._-]*0*([0-9]+)\b", candidate
                            )
                            if m:
                                source_volume = str(int(m.group(1)))
                                break
                except Exception:
                    source_volume = ""

                # Build chapters->image lists to upload
                chapters_to_upload = []
                if subfolders:
                    for fd in subfolders:
                        imgs = get_image_files(fd.path)
                        if imgs:
                            chapters_to_upload.append((fd.name, imgs, fd.path))
                else:
                    # Reconstruct chapter grouping from files in base_folder (same logic as above)
                    img_files = get_image_files(base_folder)
                    chap_pattern = re.compile(
                        r"(?i)(?:^|[^A-Za-z0-9])c0*([0-9]+)(?!\d)"
                    )
                    vol_pattern = re.compile(r"(?i)v0*([0-9]+)(?!\d)")
                    chapter_groups = {}
                    for img in img_files:
                        name = img.name
                        if "cover" in name.lower():
                            continue
                        m = chap_pattern.search(name)
                        if m:
                            chap_num = str(int(m.group(1)))
                        else:
                            chap_num = "1"
                        chapter_groups.setdefault(chap_num, []).append(img)
                    for chap_num, imgs in sorted(
                        chapter_groups.items(), key=lambda x: int(x[0])
                    ):
                        name_for_desc = f"Ch{int(chap_num)}"
                        chapters_to_upload.append((name_for_desc, imgs, base_folder))

                # Only create the progress manager when there's actual work to do.
                if chapters_to_upload:
                    progress_columns = [
                        SpinnerColumn(finished_text="[green]✓[/green]"),
                        TextColumn(
                            "[progress.description]{task.description}", justify="left"
                        ),
                        BarColumn(bar_width=None),
                        TextColumn("[progress.percentage]{task.percentage:>3.1f}%"),
                        TextColumn("• {task.completed} of {task.total} •"),
                        ConditionalTransferSpeedColumn(),
                        ConditionalFileSizeColumn(),
                        CustomTimeDisplayColumn(),
                    ]
                    progress_manager = Progress(
                        *progress_columns, console=console, transient=False, expand=True
                    )

                    with Live(
                        progress_manager,
                        console=console,
                        refresh_per_second=10,
                        vertical_overflow="visible",
                    ) as live:
                        overall_task_id = progress_manager.add_task(
                            "[bold #AAAAFF]ImgChest Upload Progress[/bold #AAAAFF]",
                            total=len(chapters_to_upload),
                            fields={"is_byte_task": False},
                        )
 
                        # First, apply existing-record shortcuts and build a list of chapters that actually need uploading.
                        pending_to_upload = []
                        for chap_name, imgs, chap_path in chapters_to_upload:
                            match_key = find_matching_upload_record_key(
                                uploaded_record, chap_name
                            )
                            if match_key:
                                # Existing record — attach proxy info to manga_json and advance overall progress.
                                rec = uploaded_record[match_key]
                                post_id = rec.get("post_id") or (
                                    rec.get("album_url", "").split("/")[-1]
                                    if rec.get("album_url")
                                    else None
                                )
                                album_url = rec.get("album_url")
                                try:
                                    total_uploaded = (
                                        int(rec.get("image_count"))
                                        if rec.get("image_count")
                                        and str(rec.get("image_count")).isdigit()
                                        else 0
                                    )
                                except Exception:
                                    total_uploaded = 0
                                parsed = parse_folder_name(chap_name)
                                ch_key = parsed.chapter or chap_name
                                manga_json_data.setdefault("chapters", {})
                                proxy = (
                                    f"/proxy/api/imgchest/chapter/{post_id}"
                                    if post_id
                                    else ""
                                )
                                manga_json_data["chapters"].setdefault(ch_key, {})
                                groups_str = manga_info.get("groups", "") or ""
                                groups_list = [
                                    g.strip() for g in groups_str.split(",") if g.strip()
                                ] or ["UnknownGroup"]
                                groups_map = {}
                                for grp in groups_list:
                                    groups_map[grp] = proxy if proxy else {}
                                manga_json_data["chapters"][ch_key]["groups"] = groups_map
                                manga_json_data["chapters"][ch_key]["title"] = (
                                    parsed.title
                                    if parsed.title
                                    else f"Ch.{ch_key if ch_key else '1'}"
                                )
                                ts_str = rec.get("timestamp")
                                try:
                                    if ts_str:
                                        dt = time.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                                        manga_json_data["chapters"][ch_key][
                                            "last_updated"
                                        ] = str(int(time.mktime(dt)))
                                except Exception:
                                    manga_json_data["chapters"][ch_key][
                                        "last_updated"
                                    ] = manga_json_data["chapters"][ch_key].get(
                                        "last_updated", str(int(time.time()))
                                    )
                                live.console.print(
                                    f"[yellow]Using existing upload record for '{chap_name}'. Added proxy -> {album_url}[/yellow]"
                                )
                                if any(t.id == overall_task_id for t in progress_manager.tasks):
                                    progress_manager.update(overall_task_id, advance=1)
                                continue
                            # Defer actual uploads to the thread pool
                            pending_to_upload.append((chap_name, imgs, chap_path))
 
                        # Upload pending chapters concurrently, prioritizing small chapters (<= MAX_IMAGES_PER_BATCH_LOCAL) first.
                        if pending_to_upload:
                            # Sort so small chapters go first; tie-breaker by size ascending
                            pending_to_upload.sort(
                                key=lambda t: (len(t[1]) > MAX_IMAGES_PER_BATCH_LOCAL, len(t[1]))
                            )
                            max_workers = min(4, len(pending_to_upload))
                            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                                future_to_meta = {
                                    executor.submit(
                                        upload_all_images_for_chapter_to_host_local,
                                        imgs,
                                        api_key,
                                        chap_name,
                                        progress_manager,
                                        live,
                                    ): (chap_name, imgs)
                                    for chap_name, imgs, _ in pending_to_upload
                                }
 
                                for fut in concurrent.futures.as_completed(future_to_meta):
                                    chap_name, imgs = future_to_meta[fut]
                                    try:
                                        res = fut.result()
                                    except Exception as e:
                                        live.console.print(
                                            f"[red]Upload raised exception for '{chap_name}': {e}[/red]"
                                        )
                                        res = {"success": False, "error": str(e)}
 
                                    if res.get("success"):
                                        post_id = res.get("post_id")
                                        album_url = res.get("album_url")
                                        total_uploaded = res.get("total_uploaded", 0)
 
                                        parsed = parse_folder_name(chap_name)
                                        ch_key = parsed.chapter or chap_name
                                        manga_json_data.setdefault("chapters", {})
                                        proxy = (
                                            f"/proxy/api/imgchest/chapter/{post_id}"
                                            if post_id
                                            else ""
                                        )
                                        manga_json_data["chapters"].setdefault(ch_key, {})
                                        groups_str = manga_info.get("groups", "") or ""
                                        groups_list = [
                                            g.strip()
                                            for g in groups_str.split(",")
                                            if g.strip()
                                        ] or ["UnknownGroup"]
                                        groups_map = {}
                                        for grp in groups_list:
                                            groups_map[grp] = proxy if proxy else {}
                                        manga_json_data["chapters"][ch_key]["groups"] = (
                                            groups_map
                                        )
                                        manga_json_data["chapters"][ch_key]["title"] = (
                                            parsed.title
                                            if parsed.title
                                            else f"Ch.{ch_key if ch_key else '1'}"
                                        )
 
                                        vol_val = (
                                            parsed.volume
                                            if parsed.volume
                                            else (source_volume if source_volume else "")
                                        )
                                        uploaded_record[chap_name] = {
                                            "album_url": album_url,
                                            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                                            "image_count": str(total_uploaded),
                                            "post_id": post_id,
                                            "volume": vol_val,
                                        }
                                        # Merge with any existing record on disk to avoid overwriting older entries
                                        try:
                                            existing_record = (
                                                load_upload_record_local(record_base)
                                                if record_base.exists()
                                                else {}
                                            )
                                            if uploaded_record:
                                                existing_record.update(uploaded_record)
                                            save_upload_record(record_base, existing_record, live)
                                        except Exception:
                                            # Fallback: attempt to save what we have
                                            save_upload_record(record_base, uploaded_record, live)
 
                                        live.console.print(
                                            f"[green]Uploaded chapter '{chap_name}' -> {album_url} ({total_uploaded} images).[/green]"
                                        )
                                    else:
                                        live.console.print(
                                            f"[red]Upload failed for '{chap_name}': {res.get('error', 'Unknown')}[/red]"
                                        )
 
                                    if any(t.id == overall_task_id for t in progress_manager.tasks):
                                        progress_manager.update(overall_task_id, advance=1)
 

                        # Best-effort: ensure any entries recorded in uploaded_record are merged into manga_json_data
                        # This helps when concurrent uploads finished but the in-memory manga_json_data missed some entries.
                        try:
                            for rec_key, rec in (uploaded_record or {}).items():
                                try:
                                    parsed = parse_folder_name(rec_key)
                                    ch_key = parsed.chapter or rec_key
                                    manga_json_data.setdefault("chapters", {})
                                    manga_json_data["chapters"].setdefault(ch_key, {})
                                    post_id = rec.get("post_id") or (rec.get("album_url", "").split("/")[-1] if rec.get("album_url") else "")
                                    proxy = f"/proxy/api/imgchest/chapter/{post_id}" if post_id else ""
                                    groups_str = manga_info.get("groups", "") or ""
                                    groups_list = [g.strip() for g in groups_str.split(",") if g.strip()] or ["UnknownGroup"]
                                    # Ensure groups mapping exists and attach proxy for each group
                                    for grp in groups_list:
                                        manga_json_data["chapters"][ch_key].setdefault("groups", {})[grp] = proxy if proxy else {}
                                    # Ensure title and last_updated are present
                                    manga_json_data["chapters"][ch_key]["title"] = parsed.title if parsed and parsed.title else f"Ch.{ch_key if ch_key else '1'}"
                                    ts_str = rec.get("timestamp")
                                    try:
                                        if ts_str:
                                            dt = time.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                                            manga_json_data["chapters"][ch_key]["last_updated"] = str(int(time.mktime(dt)))
                                    except Exception:
                                        manga_json_data["chapters"][ch_key]["last_updated"] = manga_json_data["chapters"][ch_key].get("last_updated", str(int(time.time())))
                                except Exception:
                                    # Skip problematic record entries but continue merging others
                                    continue
                        except Exception:
                            pass

                        # Immediately persist the in-memory manga_json_data so we don't lose merged chapter proxies.
                        try:
                            populate_chapter_groups_from_root(manga_json_data, manga_info)
                            populate_chapter_groups_from_root(manga_json_data, refreshed_info if 'refreshed_info' in locals() else manga_info)
                            manga_json_data.pop("groups", None)
                            # Restore album URLs from upload record (best-effort) so concurrent uploads and existing on-disk records
                            # are reflected in the saved JSON.
                            try:
                                restore_album_urls_from_upload_record(manga_json_data, record_base, manga_info)
                            except Exception:
                                pass
                            normalize_chapters(manga_json_data)
                            save_manga_json(manga_json_file, manga_json_data)
                        except Exception:
                            # Non-fatal: best-effort save
                            pass

                        # Persist merged upload record to disk (merge with existing on-disk record to avoid overwriting)
                        try:
                            existing_record = load_upload_record_local(record_base) if record_base.exists() else {}
                            if uploaded_record:
                                existing_record.update(uploaded_record)
                                save_upload_record(record_base, existing_record, live)
                        except Exception:
                            try:
                                save_upload_record(record_base, uploaded_record, live)
                            except Exception:
                                pass
                # After processing all chapter uploads, persist the manga JSON once with the updated chapter entries.
                if save_json:
                    populate_chapter_groups_from_root(manga_json_data, manga_info)
                    populate_chapter_groups_from_root(manga_json_data, refreshed_info if 'refreshed_info' in locals() else manga_info)
                    # Ensure no root-level "groups" key remains; groups should live per-chapter only.
                    manga_json_data.pop("groups", None)
                    normalize_chapters(manga_json_data)
                    save_manga_json(manga_json_file, manga_json_data)

                    # If the input was a .cbz file, create an aggregate entry in the upload record
                    # keyed by the .cbz filename so subsequent script runs will treat that volume
                    # as already uploaded. We keep this conservative and only sum existing chapter
                    # entries discovered during this run (do not invent album URLs).
                    try:
                        if input_path.is_file() and input_path.suffix.lower() == ".cbz":
                            cbz_key = input_path.name
                            # Only add the aggregate if there are chapter entries recorded
                            total_images_sum = 0
                            first_album = None
                            for rec_key, rec in uploaded_record.items():
                                try:
                                    total_images_sum += int(
                                        rec.get("image_count", 0) or 0
                                    )
                                except Exception:
                                    pass
                                if not first_album and rec.get("album_url"):
                                    first_album = rec.get("album_url")
                            # Only add an aggregate record if we found at least one chapter record
                            if total_images_sum > 0 or first_album:
                                # Attempt to extract a volume number from the CBZ filename for the aggregate record
                                try:
                                    base_cbz = re.sub(
                                        r"\.cbz$", "", cbz_key, flags=re.IGNORECASE
                                    )
                                    vol_match = re.search(
                                        r"(?i)\b(?:v|vol|volume)[\s._-]*0*([0-9]+)\b",
                                        base_cbz,
                                    )
                                    cbz_volume = vol_match.group(1) if vol_match else ""
                                except Exception:
                                    cbz_volume = ""

                                uploaded_record[cbz_key] = {
                                    "album_url": first_album or "",
                                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "image_count": str(total_images_sum),
                                    "post_id": "",
                                    "volume": cbz_volume,
                                }
                                # Merge with any existing on-disk record to avoid accidental overwrite
                                try:
                                    existing_record = (
                                        load_upload_record_local(record_base)
                                        if record_base.exists()
                                        else {}
                                    )
                                    existing_record.update(uploaded_record)
                                    save_upload_record(
                                        record_base, existing_record, live
                                    )
                                except Exception:
                                    # Best-effort fallback: attempt to save what we have
                                    try:
                                        save_upload_record(
                                            record_base, uploaded_record, live
                                        )
                                    except Exception:
                                        pass
                    except Exception:
                        pass

        # If upload_to_github requested, perform upload now (caller might have requested this)
        gh_result = None
        if upload_to_github:
            gh_config = load_github_config()
            if not gh_config:
                console.print(
                    f"[red]GitHub configuration missing or invalid. Skipping GitHub upload.[/red]"
                )
                if save_json:
                    return True
                return {
                    "success": False,
                    "error": "invalid_github_config",
                    "manga_json_data": manga_json_data,
                    "manga_json_file": manga_json_file,
                }

            uploader = GitHubJSONUploader(
                token=gh_config["token"],
                owner=gh_config["owner"],
                repo=gh_config["repo"],
                console_instance=console,
            )
            repo_subfolder = github_subfolder.strip()
            repo_path_parts = [
                p.strip("/")
                for p in [repo_subfolder, manga_json_file.name]
                if p.strip("/")
            ]
            repo_file_path_str = "/".join(repo_path_parts).replace("\\", "/")
            commit_msg = f"Sync: {manga_title} ({manga_json_file.name})"
            console.print(
                f"[dim]Uploading {manga_json_file} to GitHub as {repo_file_path_str}[/dim]"
            )
            res = uploader.upload_file(
                str(manga_json_file), repo_file_path_str, commit_message=commit_msg
            )
            gh_result = res
            if res.get("success"):
                console.print(
                    f"[green]GitHub upload succeeded. Raw URL: {res.get('raw_url')}[/green]"
                )
                # Log to cubari_urls.txt
                item = {
                    "title": manga_title,
                    "folder_path": str(manga_json_file.parent.resolve()),
                    "file": manga_json_file.name,
                    "repo_path": repo_file_path_str,
                    "raw_url": res.get("raw_url"),
                    "cubari_url": res.get("cubari_url"),
                    "action": res.get("action"),
                    "last_modified": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                # Call save_cubari_urls if available
                try:
                    from kaguya import save_cubari_urls

                    save_cubari_urls([item], mode="append")
                except Exception:
                    pass
            else:
                console.print(f"[red]GitHub upload failed: {res.get('error')}[/red]")

        # Return structure if caller requested to merge results themselves
        if not save_json:
            result = {
                "success": True,
                "manga_json_data": manga_json_data,
                "manga_json_file": manga_json_file,
            }
            if gh_result is not None:
                result["github_result"] = gh_result
            return result

        return True
    finally:
        # cleanup temp dir if used
        if temp_dir and temp_dir.exists():
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass


def main():
    console.print(
        Panel(
            RichText("Welcome to Kaguya!", justify="center", style="bold hot_pink"),
            border_style="hot_pink",
        )
    )
    console.line()

    # Ask user for a path: either a folder containing .cbz files or a single .cbz file or a folder of extracted chapter folders
    while True:
        base_path_str = console.input(
            "[bold cyan]Enter the path to a folder of .cbz files, a single .cbz, or a folder containing chapter subfolders:[/bold cyan] "
        ).strip()
        base_path = Path(base_path_str).expanduser().resolve()
        if base_path.exists():
            break
        console.print(f"[red]Error: '{base_path_str}' does not exist.[/red]")
        console.line()

    # Determine items to operate on
    cbz_items = []
    subfolders_with_images = []
    if base_path.is_file() and base_path.suffix.lower() == ".cbz":
        cbz_items = [base_path]
    elif base_path.is_dir():
        cbz_items = sorted(
            [
                p
                for p in base_path.iterdir()
                if p.is_file() and p.suffix.lower() == ".cbz"
            ],
            key=lambda p: p.name.lower(),
        )
        # Also detect extracted subfolders with images (in case user pointed at already-extracted folder)
        subfolders_with_images = find_subfolders_with_images(base_path)
    else:
        console.print(f"[red]Unsupported path type: {base_path}[/red]")
        sys.exit(1)

    # Determine a canonical folder to read/save records and manga JSON from.
    # If the user provided a single .cbz file, use its parent folder as the record/base.
    record_lookup_path = base_path if base_path.is_dir() else base_path.parent
    uploaded_record = (
        load_upload_record_local(record_lookup_path)
        if record_lookup_path.exists()
        else {}
    )

    # If info.txt isn't present, offer an interactive Mangabaka lookup to auto-create one.
    try:
        info_file_candidate = Path(record_lookup_path) / "info.txt"
        if not info_file_candidate.exists():
            try:
                choice = (
                    console.input(
                        "[cyan]No info.txt found. Search Mangabaka to auto-generate info.txt? (y/N):[/cyan] "
                    )
                    .strip()
                    .lower()
                )
                if choice == "y":
                    prompt_mangabaka_and_create_info(record_lookup_path)
            except Exception as e:
                console.print(
                    f"[yellow]Mangabaka lookup failed or was canceled: {e}[/yellow]"
                )
    except Exception:
        # Defensive: if any path error occurs, fall back to normal behavior
        pass

    manga_info = load_manga_info_from_txt_with_block_support(record_lookup_path)
    # Normalize description here so the CLI/main flow uses real newlines (human-readable)
    try:
        if manga_info and isinstance(manga_info, dict) and isinstance(manga_info.get("description"), str):
            manga_info["description"] = manga_info["description"].replace("\\n", "\n")
    except Exception:
        pass
    manga_title = manga_info.get("title") or base_path.name
    manga_json_data, manga_json_file = load_manga_json(record_lookup_path, manga_title)

    console.line()
    console.print("[bold underline]Found items:[/bold underline]")
    items = []
    if cbz_items:
        for p in cbz_items:
            name = p.name
            uploaded = False
            try:
                uploaded = is_item_uploaded(uploaded_record, name, p)
            except Exception:
                uploaded = False
            status = "✓ Uploaded" if uploaded else "○ New"
            items.append((name, p))
            color = "green" if uploaded else "yellow"
            console.print(f"{len(items):3d}. {name} [[{color}]{status}[/{color}]]")
    if subfolders_with_images:
        for fd in subfolders_with_images:
            name = fd.name
            uploaded = False
            try:
                uploaded = is_item_uploaded(uploaded_record, name, fd.path)
            except Exception:
                uploaded = False
            status = "✓ Uploaded" if uploaded else "○ New"
            items.append((name, fd.path))
            color = "green" if uploaded else "yellow"
            console.print(
                f"{len(items):3d}. {name} ({fd.image_count} images) [[{color}]{status}[/{color}]]"
            )

    if not items:
        console.print(
            "[yellow]No .cbz files or chapter subfolders found in the provided path.[/yellow]"
        )

    console.line()
    console.print(
        "\n[bold underline]Process Options:[/bold underline]\n"
        "1. Upload all items\n"
        "2. Upload only new items (skip already uploaded)\n"
        "3. Select specific item(s) to process\n"
        "4. Update GitHub only (uses existing manga.json for this folder)\n"
        "5. Regenerate manga.json from current items (uses found items)\n"
        "6. Cancel",
        highlight=False,
    )
    choice = ""
    folders_to_process = []
    is_github_only_choice = False

    while True:
        console.line()
        choice_input = console.input(
            "[bold cyan]Choose an option (1-6):[/bold cyan] "
        ).strip()
        if choice_input in ["1", "2", "3"]:
            if not items:
                console.print(
                    "[yellow]No items available to process for this option.[/yellow]"
                )
                continue

            if choice_input == "1":
                folders_to_process = [p for (_, p) in items]
                choice = choice_input
                break
            elif choice_input == "2":
                # Use robust matching & CBZ heuristic to decide which items are considered "already uploaded"
                folders_to_process = [
                    p for (n, p) in items if not is_item_uploaded(uploaded_record, n, p)
                ]
                if not folders_to_process:
                    console.print(
                        "[yellow]No new/unrecorded items to process. Try another option.[/yellow]"
                    )
                    continue
                choice = choice_input
                break
            elif choice_input == "3":
                sel_str = console.input(
                    "[cyan]Enter item numbers (e.g., 1,3,5-7):[/cyan] "
                ).strip()
                indices = parse_folder_selection(sel_str, len(items))
                if indices is not None:
                    folders_to_process = [items[i][1] for i in indices]
                    if folders_to_process:
                        choice = choice_input
                        break
                    else:
                        console.print(
                            "[yellow]No valid items selected from your input. Try again.[/yellow]"
                        )

        elif choice_input == "4":
            if not manga_json_file.exists():
                console.print(
                    f"[red]Error: Manga JSON file '{manga_json_file.name}' does not exist in '{manga_json_file.parent}'.[/red]"
                )
                console.print(
                    "[yellow]This option requires an existing manga.json. Please run an upload option first or ensure the file exists.[/yellow]"
                )
                continue
            console.print(
                f"[green]Selected 'Update GitHub only'. Will use existing '{manga_json_file.name}'.[/green]"
            )
            folders_to_process = []
            is_github_only_choice = True
            choice = choice_input
            break
        elif choice_input == "5":
            console.line()
            console.print(
                "[bold underline]Regenerate manga.json from current items[/bold underline]"
            )
            # Regenerate from cbz items and/or subfolders. We'll reuse existing utilities: process each item without uploading images.
            # Clear chapters and refresh root metadata from the manga JSON parent (the canonical root for this manga)
            manga_json_data["chapters"] = {}
            # Refresh manga metadata so the regenerated JSON includes updated description/artist/author/cover/groups
            try:
                refreshed_info = load_manga_info_from_txt_with_block_support(manga_json_file.parent)
                # Normalize description so regenerated JSON uses real newlines
                try:
                    if refreshed_info and isinstance(refreshed_info, dict) and isinstance(refreshed_info.get("description"), str):
                        refreshed_info["description"] = refreshed_info["description"].replace("\\n", "\n")
                except Exception:
                    pass
                for mk in [
                    "title",
                    "description",
                    "artist",
                    "author",
                    "cover",
                ]:
                    # Intentionally do not copy 'groups' into the root of manga.json here;
                    # groups belong on a per-chapter basis (inside each chapter entry).
                    if refreshed_info.get(mk):
                        manga_json_data[mk] = refreshed_info[mk]
                    elif not manga_json_data.get(mk) and mk == "title":
                        manga_json_data[mk] = manga_json_file.parent.name
            except Exception:
                # If refresh fails, proceed with existing manga_json_data (best-effort)
                pass

            if subfolders_with_images:
                for fd in subfolders_with_images:
                    key, ch = (
                        (parse_folder_selection, None) if False else (None, None)
                    )  # placeholder to satisfy static analysis
                # We'll instead process each found subfolder by calling build logic in process_input_path via a dry-run.
            # Simpler approach: process every item but do not upload to GitHub (process_input_path will only build chapters)
            for p in [it[1] for it in items]:
                # Ensure process_input_path reads info.txt from the manga root by passing manga_json_file.parent as output_json_base
                res = process_input_path(
                    p,
                    manga_json_file.parent,
                    github_subfolder="",
                    upload_to_github=False,
                    upload_images=False,
                    save_json=False,
                )
                if isinstance(res, dict) and res.get("success"):
                    # merge chapters from this item's generated json into the main manga_json_data
                    chapters = res.get("manga_json_data", {}).get("chapters", {})
                    if chapters:
                        manga_json_data.setdefault("chapters", {}).update(chapters)

            # When regenerating manga.json, re-attach proxy URLs from the on-disk upload record
            # so that previously uploaded chapters get their ImgChest links restored into the JSON.
            try:
                record_src = manga_json_file.parent
                existing_record = load_upload_record_local(record_src) if record_src.exists() else {}
                if existing_record:
                    chapters = manga_json_data.setdefault("chapters", {})
                    for rec_key, rec in existing_record.items():
                        try:
                            post_id = rec.get("post_id") or (rec.get("album_url", "").split("/")[-1] if rec.get("album_url") else "")
                            proxy = f"/proxy/api/imgchest/chapter/{post_id}" if post_id else ""
                            # Prefer to derive chapter key from the record key using parse_folder_name
                            parsed = parse_folder_name(rec_key)
                            candidate_ch_key = parsed.chapter if parsed and parsed.chapter else None
                            matched_key = None

                            # 1) Direct chapter number match (most reliable)
                            if candidate_ch_key and candidate_ch_key in chapters:
                                matched_key = candidate_ch_key

                            # 2) Name-based normalized match (sanitized exact or substring)
                            if not matched_key:
                                try:
                                    rec_san = sanitize_filename(rec_key).lower()
                                except Exception:
                                    rec_san = rec_key.lower()
                                for ck in list(chapters.keys()):
                                    try:
                                        ck_san = sanitize_filename(str(ck)).lower()
                                    except Exception:
                                        ck_san = str(ck).lower()
                                    if ck_san == rec_san or rec_san in ck_san or ck_san in rec_san:
                                        matched_key = ck
                                        break

                            # 3) Volume-based match: if the record contains an explicit volume, try to match chapters with same volume
                            if not matched_key:
                                rec_vol = rec.get("volume") or ""
                                if rec_vol:
                                    for ck, ch in chapters.items():
                                        try:
                                            ch_vol = ch.get("volume") or ""
                                            if ch_vol:
                                                try:
                                                    if str(int(str(ch_vol))) == str(int(str(rec_vol))):
                                                        matched_key = ck
                                                        break
                                                except Exception:
                                                    if str(ch_vol).strip() == str(rec_vol).strip():
                                                        matched_key = ck
                                                        break
                                        except Exception:
                                            continue

                            if matched_key:
                                groups_str = manga_info.get("groups", "") or ""
                                groups_list = [g.strip() for g in groups_str.split(",") if g.strip()] or ["UnknownGroup"]
                                ch_entry = chapters.setdefault(matched_key, {})
                                ch_entry.setdefault("groups", {})
                                for grp in groups_list:
                                    ch_entry["groups"][grp] = proxy if proxy else {}
                                # Restore title/last_updated if present in the record
                                if not ch_entry.get("title"):
                                    ch_entry["title"] = parsed.title if parsed and parsed.title else f"Ch.{matched_key if matched_key else '1'}"
                                ts = rec.get("timestamp")
                                if ts:
                                    try:
                                        dt = time.strptime(ts, "%Y-%m-%d %H:%M:%S")
                                        ch_entry["last_updated"] = str(int(time.mktime(dt)))
                                    except Exception:
                                        ch_entry["last_updated"] = ch_entry.get("last_updated", str(int(time.time())))
                        except Exception:
                            # Ignore malformed record entries but continue processing others
                            continue
            except Exception:
                # Best-effort; don't fail regeneration if record loading/merging breaks
                pass
            # Persist combined manga.json; when regenerating we should apply the root info groups to all chapters.
            populate_chapter_groups_from_root(
                manga_json_data,
                refreshed_info if 'refreshed_info' in locals() else manga_info,
                overwrite=True,
            )
            # Ensure no root-level "groups" key remains; groups should live per-chapter only.
            manga_json_data.pop("groups", None)
            # Restore album URLs from the upload record so regeneration re-attaches previously uploaded proxies.
            try:
                restore_album_urls_from_upload_record(manga_json_data, manga_json_file.parent, refreshed_info if 'refreshed_info' in locals() else manga_info)
            except Exception:
                pass
            normalize_chapters(manga_json_data)
            save_manga_json(manga_json_file, manga_json_data)
            console.line()
            console.print("[green]Regeneration complete. Exiting.[/green]")
            console.line()
            sys.exit(0)
        elif choice_input == "6":
            console.print("[yellow]Processing canceled by user.[/yellow]")
            console.line()
            return
        else:
            console.print(
                "[red]Invalid choice. Please enter a number between 1 and 6.[/red]"
            )

    # If we get here and it's not GitHub-only, process selected items
    newly_processed_count = 0
    skipped_count = 0

    if not is_github_only_choice:
        if not folders_to_process:
            console.line()
            console.print(
                "[yellow]No items were identified for processing based on your selection.[/yellow]"
            )
            populate_chapter_groups_from_root(manga_json_data, refreshed_info if 'refreshed_info' in locals() else manga_info)
            # Ensure no root-level "groups" key remains; groups should live per-chapter only.
            manga_json_data.pop("groups", None)
            normalize_chapters(manga_json_data)
            save_manga_json(manga_json_file, manga_json_data)
            console.line()
            return

        console.line()
        console.print(
            f"[bold underline]Will process the following items:[/bold underline]"
        )
        for p in folders_to_process:
            console.print(f"  - {p.name if isinstance(p, Path) else str(p)}")
        console.line()
        if (
            console.input(
                "[bold yellow]Proceed with processing these items? (y/N):[/bold yellow] "
            )
            .strip()
            .lower()
            != "y"
        ):
            console.print("[yellow]Processing canceled by user.[/yellow]")
            populate_chapter_groups_from_root(manga_json_data, refreshed_info if 'refreshed_info' in locals() else manga_info)
            normalize_chapters(manga_json_data)
            save_manga_json(manga_json_file, manga_json_data)
            console.line()
            return

        # Decide whether to upload images via ImgChest
        imgchest_api_key = load_api_key_local() if "load_api_key" in globals() else None
        upload_images = False
        if imgchest_api_key:
            upload_images = (
                console.input(
                    "[bold cyan]Upload images to ImgChest for selected items? (y/N):[/bold cyan] "
                )
                .strip()
                .lower()
                == "y"
            )
        else:
            if (
                console.input(
                    "[yellow]ImgChest API key not configured. Continue without image upload? (y/N): [/yellow]"
                )
                .strip()
                .lower()
                == "y"
            ):
                upload_images = False
            else:
                console.print("[yellow]Proceeding without image uploads.[/yellow]")
                upload_images = False

        for p in folders_to_process:
            res = process_input_path(
                p,
                manga_json_file.parent,
                github_subfolder="",
                upload_to_github=False,
                upload_images=upload_images,
                imgchest_api_key=imgchest_api_key,
                save_json=False,
            )
            if isinstance(res, dict) and res.get("success"):
                # merge returned chapters into the main manga_json_data
                child = res.get("manga_json_data", {})
                chapters = child.get("chapters", {})
                if chapters:
                    manga_json_data.setdefault("chapters", {}).update(chapters)
                # Merge metadata fields if main is missing
                for meta_key in [
                    "title",
                    "description",
                    "artist",
                    "author",
                    "cover",
                ]:
                    # Preserve chapter-level 'groups' only inside chapters; avoid copying root 'groups' from child JSONs.
                    if child.get(meta_key) and not manga_json_data.get(meta_key):
                        val = child.get(meta_key)
                        if meta_key == "description" and isinstance(val, str):
                            try:
                                val = val.replace("\\n", "\n")
                            except Exception:
                                pass
                        manga_json_data[meta_key] = val
                newly_processed_count += 1
            else:
                console.print(f"[red]Failed processing: {p}[/red]")
            # After each, save upload record (best-effort)
            try:
                dest = manga_json_file.parent
                existing = load_upload_record_local(dest) if dest.exists() else {}
                # Merge any entries we have into the existing record to avoid accidental overwrite.
                if uploaded_record:
                    existing.update(uploaded_record)
                save_upload_record(dest, existing)
            except Exception:
                pass

    populate_chapter_groups_from_root(manga_json_data, manga_info)
    populate_chapter_groups_from_root(manga_json_data, refreshed_info if 'refreshed_info' in locals() else manga_info)
    # Ensure no root-level "groups" key remains; groups should live per-chapter only.
    manga_json_data.pop("groups", None)
    normalize_chapters(manga_json_data)
    save_manga_json(manga_json_file, manga_json_data)
    console.line()
    console.print(f"Manga JSON reference: [cyan]{manga_json_file}[/cyan]")
    console.line()

    # GitHub upload step
    proceed_with_github = False
    if is_github_only_choice:
        console.print(
            f"[info]GitHub-only mode selected. Will attempt to upload '{manga_json_file.name}'.[/info]"
        )
        proceed_with_github = True
    else:
        if (
            console.input(
                f"[bold cyan]Upload/Update manga JSON '[white]{manga_json_file.name}[/white]' on GitHub? (y/N):[/bold cyan] "
            )
            .strip()
            .lower()
            == "y"
        ):
            proceed_with_github = True

    if proceed_with_github:
        console.line()
        gh_config = load_github_config()
        if not gh_config:
            console.print(
                f"[red]GitHub configuration ({'github.txt'}) is missing or invalid. Cannot upload to GitHub.[/red]"
            )
            console.line()
            sys.exit(1)

        uploader = GitHubJSONUploader(
            token=gh_config["token"],
            owner=gh_config["owner"],
            repo=gh_config["repo"],
            console_instance=console,
        )

        repo_subfolder = console.input(
            f"[cyan]Enter target subfolder in GitHub repo for '{manga_json_file.name}' (press Enter for root):[/cyan] "
        ).strip()
        repo_path_parts = [
            p.strip("/") for p in [repo_subfolder, manga_json_file.name] if p.strip("/")
        ]
        repo_file_path_str = "/".join(repo_path_parts).replace("\\", "/")
        commit_message = f"Update: {manga_title} ({manga_json_file.name})"

        console.print("[bold underline]Uploading to GitHub...[/bold underline]")
        res = uploader.upload_file(
            str(manga_json_file), repo_file_path_str, commit_message=commit_message
        )
        if res.get("success"):
            cubari_item_for_log = {
                "title": manga_title,
                "folder_path": str(manga_json_file.parent.resolve()),
                "file": manga_json_file.name,
                "repo_path": repo_file_path_str,
                "raw_url": res.get("raw_url"),
                "cubari_url": res.get("cubari_url"),
                "action": res.get("action"),
                "last_modified": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            if "previous_last_modified" in res:
                cubari_item_for_log["previous_last_modified_in_log"] = res[
                    "previous_last_modified"
                ]
            try:
                save_cubari_urls([cubari_item_for_log], mode="append")
            except Exception:
                pass
        else:
            console.print(
                f"[bold red]GitHub upload failed: {res.get('error')}[/bold red]"
            )
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
