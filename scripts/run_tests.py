"""Run the relocated test scripts; generate only synthetic smoke-test media."""
from pathlib import Path
import argparse
import json
import os
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("tests", nargs="*", help="Specific test filenames; default: all tests")
parser.add_argument("--ffmpeg", help="FFmpeg executable for synthetic media fixtures")
parser.add_argument("--ffprobe", help="ffprobe executable for the v2 media tests (default: FFPROBE_BINARY, PATH, or next to FFmpeg)")
args = parser.parse_args()
available = {p.name: p for p in (root / "tests").glob("*.py")}
names = args.tests or sorted(available)
unknown = set(names) - set(available)
if unknown:
    parser.error("Unknown tests: " + ", ".join(sorted(unknown)))
if "segmentation_cache_test.py" in names and "smoke_test.py" not in names:
    names.insert(0, "smoke_test.py")
if "smoke_test.py" in names:
    names.remove("smoke_test.py")
    names.insert(0, "smoke_test.py")
environment = os.environ.copy()
environment["PYTHONPATH"] = os.pathsep.join([str(root / "app/tools"), str(root / "tests"), environment.get("PYTHONPATH", "")])
environment.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
ffmpeg = args.ffmpeg or environment.get("FFMPEG_BINARY") or shutil.which("ffmpeg")
if not ffmpeg:
    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        pass
if ffmpeg:
    environment["FFMPEG_BINARY"] = ffmpeg
ffprobe = args.ffprobe or environment.get("FFPROBE_BINARY") or shutil.which("ffprobe")
if not ffprobe and ffmpeg:
    sibling = Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    ffprobe = str(sibling) if sibling.is_file() else None
if ffprobe:
    environment["FFPROBE_BINARY"] = ffprobe
else:
    # the v2 media tests need a real ffprobe; without one they are reported as skipped inside their logs, not as passed
    print("NOTE no ffprobe found: upgrade_media_test.py / upgrade_stages_test.py will skip their real-media cases", flush=True)
(root / "test-results").mkdir(exist_ok=True)
results = []
for name in names:
    result = subprocess.run([sys.executable, "-B", "-X", "utf8", str(available[name])],
                            cwd=root, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            encoding="utf-8", errors="replace")
    (root / "test-results" / (name + ".log")).write_text(result.stdout, encoding="utf-8")
    results.append({"test": name, "exit_code": result.returncode})
    print(("PASS" if result.returncode == 0 else "FAIL") + " " + name, flush=True)
    if result.returncode:
        print(result.stdout, flush=True)
(root / "test-results/summary.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
raise SystemExit(1 if any(row["exit_code"] for row in results) else 0)
