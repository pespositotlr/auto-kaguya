import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Use Python's built-in timezone library (requires Python 3.9+)
try:
    from zoneinfo import ZoneInfo
except ImportError:
    print("[Error] Python 3.9 or higher is required for accurate timezone handling.")
    sys.exit(1)

def run_automated_upload(base_folder: str, chapter_num: str):
    kaguya_script = "kaguya.py"
    
    # 1. Base folder path
    # 2. Option 3 (Select specific folders)
    # 3. The specific chapter/folder number
    input_sequence = f"{base_folder}\n3\n{chapter_num}\n"

    print("-" * 50)
    print(f"[Wrapper] Launching {kaguya_script}...")
    print(f"[Wrapper] Target path: {base_folder}")
    print(f"[Wrapper] Selecting Option 3 for Chapter: {chapter_num}\n")
    
    try:
        subprocess.run(
            [sys.executable, kaguya_script],
            input=input_sequence,
            text=True,
            check=True
        )
    except subprocess.CalledProcessError as e:
        print("\n[Error] The script encountered an error or was aborted.")
        sys.exit(e.returncode)
    except FileNotFoundError:
        print(f"\n[Error] Could not find {kaguya_script} in the current directory.")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Automate kaguya.py uploads with optional scheduling.")
    
    # Changed to named arguments (flags) and made them required
    parser.add_argument("--base_folder", required=True, help="Path to the base manga folder (use quotes if it contains spaces)")
    parser.add_argument("--number", required=True, help="The chapter number to upload")
    
    # Optional schedule argument remains the same
    parser.add_argument("--schedule", help="Optional: Schedule time in 'YYYY-MM-DD HH:MM:SS' (24-hour clock, NY Time)", default=None)

    args = parser.parse_args()

    if args.schedule:
        ny_tz = ZoneInfo("America/New_York")
        
        try:
            target_time = datetime.strptime(args.schedule, "%Y-%m-%d %H:%M:%S")
            target_time = target_time.replace(tzinfo=ny_tz)
        except ValueError:
            print("[Error] Invalid schedule format. Please use exact format: 'YYYY-MM-DD HH:MM:SS'")
            print("Example: --schedule \"2026-05-18 00:08:00\"")
            sys.exit(1)

        now = datetime.now(ny_tz)
        sleep_seconds = (target_time - now).total_seconds()

        if sleep_seconds > 0:
            print(f"[Schedule] Target time: {target_time.strftime('%Y-%m-%d %H:%M:%S')} (America/New_York)")
            print(f"[Schedule] Sleeping for {int(sleep_seconds)} seconds...")
            time.sleep(sleep_seconds)
            print("\n[Schedule] Time reached! Executing upload...")
        else:
            print("[Schedule] Warning: The scheduled time is in the past. Running immediately...")

    # Notice we now access the arguments via their new names: args.base_folder and args.number
    run_automated_upload(args.base_folder, args.number)

if __name__ == "__main__":
    main()