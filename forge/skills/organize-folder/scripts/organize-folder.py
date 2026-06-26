#!/usr/bin/env python3

import argparse
import csv
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Shared forge embeddings client lives at forge/lib; this script is at
# forge/skills/organize-folder/scripts/organize-folder.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
import forge_embeddings


SCAN_SCHEMA_VERSION = 3
DEFAULT_CONFIDENCE_THRESHOLD = 0.75

# Bytes hashed from the head and tail of a file for the fast fingerprint. Files
# at or below twice this size are hashed in full instead.
FINGERPRINT_EDGE_BYTES = 65536

# Cap the targeted-sampling list so review stays cheap on large folders.
REVIEW_QUEUE_LIMIT = 150

# Embedding-based content similarity. Files whose extracted text is more similar
# than the near-duplicate threshold are flagged as near-duplicate candidates
# (reformatted copies, drafts, versions) that exact-hash duplicate detection
# cannot see. The looser cluster threshold groups related documents to inform
# the destination layout. Near-duplicates are advisory only; they are never
# auto-routed to _duplicates the way exact SHA-256 duplicates are.
DEFAULT_NEAR_DUPLICATE_THRESHOLD = 0.95
DEFAULT_CONTENT_CLUSTER_THRESHOLD = 0.80

# Maximum characters of extracted text embedded per file. Enough to separate
# documents by content without sending whole files over the wire.
CONTENT_SAMPLE_CHARS = 4000

# Cap the near-duplicate report so review stays bounded on large folders.
NEAR_DUPLICATE_REPORT_LIMIT = 200

MANIFEST_FIELDS = [
    "relative_source_path",
    "parent_folder",
    "filename",
    "sha256",
    "fingerprint",
    "size_bytes",
    "modified",
    "extension",
    "detected_type",
    "peek",
    "name_cluster",
    "content_cluster",
    "category",
    "confidence",
    "is_duplicate",
    "duplicate_of",
    "near_duplicate_of",
    "content_similarity",
    "proposed_destination",
    "status",
    "note",
]

# Editable statuses the user or model may set in manifest.csv before apply.
MOVE_STATUSES = {"pending", "duplicate"}
KEEP_STATUSES = {"keep"}
PLAN_STATUSES = MOVE_STATUSES | KEEP_STATUSES

DUPLICATES_FOLDER = "_duplicates"

CATEGORY_FOLDERS = {
    "images": "Images",
    "videos": "Videos",
    "audio": "Audio",
    "documents": "Documents",
    "spreadsheets": "Spreadsheets",
    "presentations": "Presentations",
    "archives": "Archives",
    "code": "Code",
    "data": "Data",
    "fonts": "Fonts",
    "applications": "Applications",
    "other": "Other",
}

EXTENSION_CATEGORY = {}


def _register(category, extensions):
    for extension in extensions:
        EXTENSION_CATEGORY[extension] = category


_register("images", [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic", ".heif", ".svg", ".ico", ".raw", ".cr2", ".nef", ".arw", ".dng"])
_register("videos", [".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".wmv", ".flv", ".mpg", ".mpeg", ".3gp"])
_register("audio", [".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".oga", ".aiff", ".aif", ".wma", ".opus"])
_register("documents", [".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".markdown", ".tex", ".pages", ".epub", ".mobi", ".azw3"])
_register("spreadsheets", [".csv", ".tsv", ".xls", ".xlsx", ".xlsm", ".ods", ".numbers"])
_register("presentations", [".ppt", ".pptx", ".odp", ".key"])
_register("archives", [".zip", ".tar", ".gz", ".tgz", ".rar", ".7z", ".bz2", ".xz", ".zst"])
_register("code", [".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".cc", ".cpp", ".h", ".hpp", ".go", ".rs", ".rb", ".php", ".sh", ".bash", ".zsh", ".html", ".htm", ".css", ".scss", ".sql", ".swift", ".kt", ".lua", ".pl", ".r"])
_register("data", [".json", ".yaml", ".yml", ".xml", ".toml", ".ini", ".cfg", ".plist", ".parquet", ".ndjson", ".jsonl"])
_register("fonts", [".ttf", ".otf", ".woff", ".woff2", ".eot"])
_register("applications", [".dmg", ".pkg", ".exe", ".msi", ".deb", ".rpm", ".appimage"])

# Categories whose default destinations are grouped by year so dated media does
# not pile into one flat folder.
MEDIA_CATEGORIES = {"images", "videos", "audio"}

# Extensions whose content can be safely peeked as UTF-8 text.
TEXT_PEEK_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".tex", ".log", ".csv", ".tsv", ".json", ".jsonl",
    ".ndjson", ".yaml", ".yml", ".xml", ".toml", ".ini", ".cfg", ".plist",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".cc", ".cpp", ".h",
    ".hpp", ".go", ".rs", ".rb", ".php", ".sh", ".bash", ".zsh", ".html", ".htm",
    ".css", ".scss", ".sql", ".swift", ".kt", ".lua", ".pl", ".r",
}

# Filename stems that carry no meaning on their own and should be flagged for
# review rather than trusted for clustering.
GENERIC_STEMS = {
    "untitled", "document", "new", "copy", "image", "photo", "file", "download",
    "img", "picture", "final", "draft", "temp", "tmp", "output",
}

# Leading tokens that mark bulk camera or device exports.
CAMERA_PREFIXES = ("img", "dsc", "dscn", "pxl", "vid", "mov", "gopr", "dcim", "p10", "p_")

# Directories that must never be traversed or moved. Moving their contents
# breaks repositories, dependency trees, virtual environments, and macOS bundles.
PROTECTED_DIR_NAMES = {
    ".git",
    ".svn",
    ".hg",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "site-packages",
    ".tox",
    ".gradle",
    ".idea",
    ".vscode",
    "DerivedData",
}

PROTECTED_DIR_SUFFIXES = (
    ".app",
    ".bundle",
    ".framework",
    ".xcodeproj",
    ".xcworkspace",
    ".photoslibrary",
    ".musiclibrary",
    ".tvlibrary",
    ".lproj",
    ".plugin",
)

# Markers whose presence in a directory indicates a project or repository root
# whose layout would break if files were moved.
PROJECT_MARKERS = (
    ".git",
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "Gemfile",
    "pom.xml",
    "build.gradle",
    "CMakeLists.txt",
    "pyvenv.cfg",
)

# Absolute system trees that must never be organized.
SYSTEM_TREES = (
    "/System",
    "/Library",
    "/usr",
    "/bin",
    "/sbin",
    "/etc",
    "/opt",
    "/Applications",
    "/cores",
    "/dev",
    "/proc",
    "/boot",
    "/Windows",
    "/Program Files",
    "/Program Files (x86)",
)


def fail(message, exit_code=1):
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(exit_code)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(path, size):
    """Cheap content fingerprint: size plus a hash of the head and tail blocks.
    Small files are hashed whole. Two files with identical content always share
    a size, so this is enough to detect change without reading large files end
    to end."""
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    with path.open("rb") as handle:
        if size <= 2 * FINGERPRINT_EDGE_BYTES:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        else:
            digest.update(handle.read(FINGERPRINT_EDGE_BYTES))
            handle.seek(-FINGERPRINT_EDGE_BYTES, os.SEEK_END)
            digest.update(handle.read(FINGERPRINT_EDGE_BYTES))
    return "fp:" + digest.hexdigest()


def integrity_changed(path, sha256_value, fingerprint_value):
    """Return True if the file no longer matches the strongest hash recorded for
    it at scan time. Full sha256 is preferred; otherwise the fingerprint is used."""
    if sha256_value:
        return sha256(path) != sha256_value
    size = path.stat().st_size
    return fingerprint(path, size) != fingerprint_value


def image_dimensions(path):
    """Best-effort width/height from common image headers, standard library
    only. Returns None when the format is unsupported or the header is short."""
    with path.open("rb") as handle:
        header = handle.read(26)
    if len(header) < 24:
        return None
    png_signature = bytes([137, 80, 78, 71, 13, 10, 26, 10])
    if header[:8] == png_signature:
        return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")
    if header[:6] in (b"GIF87a", b"GIF89a"):
        return int.from_bytes(header[6:8], "little"), int.from_bytes(header[8:10], "little")
    if header[:2] == b"BM":
        return int.from_bytes(header[18:22], "little"), int.from_bytes(header[22:26], "little")
    if header[:2] == bytes([255, 216]):
        return jpeg_dimensions(path)
    return None


def jpeg_dimensions(path):
    """Scan JPEG segment markers for the start-of-frame that carries the size."""
    with path.open("rb") as handle:
        handle.read(2)
        while True:
            byte = handle.read(1)
            if not byte:
                return None
            if byte != bytes([255]):
                continue
            marker = handle.read(1)
            while marker == bytes([255]):
                marker = handle.read(1)
            if not marker:
                return None
            code = marker[0]
            if 0xC0 <= code <= 0xCF and code not in (0xC4, 0xC8, 0xCC):
                handle.read(3)
                height = int.from_bytes(handle.read(2), "big")
                width = int.from_bytes(handle.read(2), "big")
                return width, height
            length = int.from_bytes(handle.read(2), "big")
            if length < 2:
                return None
            handle.seek(length - 2, os.SEEK_CUR)


def text_peek(path, max_chars=160, max_lines=4):
    lines = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for _ in range(max_lines):
            line = handle.readline()
            if not line:
                break
            stripped = line.strip()
            if stripped:
                lines.append(stripped)
    collapsed = " ".join(" ".join(lines).split())
    return collapsed[:max_chars]


def archive_peek(path, extension):
    if extension == ".zip" and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
        sample = ", ".join(names[:5])
        return f"{len(names)} entries: {sample}" if names else "empty archive"
    if tarfile.is_tarfile(path):
        with tarfile.open(path) as archive:
            names = archive.getnames()
        sample = ", ".join(names[:5])
        return f"{len(names)} entries: {sample}" if names else "empty archive"
    return ""


def make_peek(path, category, extension):
    """Light, std-lib content signal. Always best-effort: any failure yields an
    empty peek rather than aborting the scan."""
    try:
        if extension in TEXT_PEEK_EXTENSIONS:
            return text_peek(path)
        if category == "archives":
            return archive_peek(path, extension)
        if category == "images":
            dims = image_dimensions(path)
            return f"{dims[0]}x{dims[1]}" if dims else ""
    except (OSError, ValueError, zipfile.BadZipFile, tarfile.TarError):
        return ""
    return ""


def name_cluster(filename):
    """Group similar filenames under a short label so the model can reason about
    and route them in bulk."""
    stem = Path(filename).stem.lower()
    if not stem:
        return "other"
    if "screenshot" in stem or "screen shot" in stem or "screen_shot" in stem:
        return "screenshots"
    if "scan" in stem:
        return "scans"
    if stem.startswith(CAMERA_PREFIXES) and any(char.isdigit() for char in stem):
        return "camera"
    base = re.sub(r"[\s_\-]*(\(?\d+\)?|v\d+|copy|final|draft|rev\d*)$", "", stem).strip(" _-")
    token = re.match(r"[a-z]+", base)
    if token:
        word = token.group(0)
        if word in GENERIC_STEMS or len(word) < 2:
            return "generic"
        return word
    return "generic"


def text_content_sample(path, max_chars=CONTENT_SAMPLE_CHARS):
    """Read up to max_chars of a text file for embedding. Best-effort: any read
    failure yields an empty sample rather than aborting the scan."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(max_chars)
    except OSError:
        return ""
    return " ".join(text.split())


def pdf_content_sample(path, max_chars=CONTENT_SAMPLE_CHARS):
    """Best-effort PDF text via pdftotext when it is installed. Returns an empty
    sample when the tool is missing or extraction fails, so PDFs simply get no
    content vector rather than failing the scan."""
    if shutil.which("pdftotext") is None:
        return ""
    try:
        completed = subprocess.run(
            ["pdftotext", "-q", "-l", "5", str(path), "-"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return " ".join(completed.stdout.split())[:max_chars]


def content_sample(path, extension):
    if extension in TEXT_PEEK_EXTENSIONS:
        return text_content_sample(path)
    if extension == ".pdf":
        return pdf_content_sample(path)
    return ""


def compute_content_similarity(records, target, args):
    """Embed text-bearing files and annotate records with content clusters and
    near-duplicate candidates. Mutates records in place and returns
    (info, near_pairs). Always degrades cleanly: when embeddings are disabled or
    the endpoint is unreachable, records keep their empty similarity fields and
    info records why.

    Exact SHA-256 duplicates are excluded from this pass: they are already
    handled, and near-duplicate detection is about finding files that are similar
    but not byte-identical."""
    url = forge_embeddings.endpoint_url(getattr(args, "embeddings_url", None))
    info = {
        "enabled": False,
        "reason": None,
        "url": url,
        "model": forge_embeddings.model_name(),
        "nearDuplicateThreshold": args.near_duplicate_threshold,
        "clusterThreshold": args.cluster_threshold,
        "embeddedCount": 0,
        "nearDuplicateCount": 0,
        "contentClusterCount": 0,
    }
    if args.no_embeddings:
        info["reason"] = "disabled with --no-embeddings"
        return info, []

    pool = [record for record in records if record.get("is_duplicate") != "true"]
    samples = []
    sampled = []
    for record in pool:
        sample = content_sample(target / record["relative_source_path"], record["extension"])
        if sample:
            samples.append(sample)
            sampled.append(record)
    if not sampled:
        info["reason"] = "no text-bearing files to embed"
        return info, []

    result = forge_embeddings.embed_texts(samples, url=url)
    if not result["ok"]:
        info["reason"] = f"embeddings endpoint unavailable: {result['reason']}"
        return info, []

    info["enabled"] = True
    info["model"] = result["model"]
    info["embeddedCount"] = len(sampled)
    vectors = [forge_embeddings.normalize(vector) for vector in result["vectors"]]

    clusters = forge_embeddings.cluster_components(vectors, args.cluster_threshold)
    cluster_index = 0
    for component in sorted(clusters, key=lambda part: min(part)):
        if len(component) < 2:
            continue
        cluster_index += 1
        label = f"c{cluster_index}"
        for position in component:
            sampled[position]["content_cluster"] = label
    info["contentClusterCount"] = cluster_index

    near_components = forge_embeddings.cluster_components(vectors, args.near_duplicate_threshold)
    near_pairs = []
    near_duplicate_count = 0
    for component in sorted(near_components, key=lambda part: min(part)):
        if len(component) < 2:
            continue
        primary_position = min(component, key=lambda position: sampled[position]["relative_source_path"].lower())
        primary = sampled[primary_position]
        for position in component:
            if position == primary_position:
                continue
            record = sampled[position]
            similarity = forge_embeddings.cosine(vectors[position], vectors[primary_position])
            record["near_duplicate_of"] = primary["relative_source_path"]
            record["content_similarity"] = f"{similarity:.3f}"
            existing_note = record.get("note") or ""
            note = "near-duplicate of " + primary["relative_source_path"] + "; review before moving"
            record["note"] = f"{existing_note}; {note}" if existing_note else note
            near_pairs.append((record["relative_source_path"], primary["relative_source_path"], similarity))
            near_duplicate_count += 1
    near_pairs.sort(key=lambda item: item[2], reverse=True)
    info["nearDuplicateCount"] = near_duplicate_count
    return info, near_pairs


def require_new_directory(raw_path):
    path = Path(raw_path).expanduser().resolve()
    if path.exists():
        fail(f"output already exists: {path}")
    path.mkdir(parents=True)
    return path


def require_run_directory(raw_path):
    path = Path(raw_path).expanduser().resolve()
    if not path.is_dir():
        fail(f"run directory does not exist: {path}")
    if not (path / "scan.json").is_file():
        fail(f"scan.json is missing: {path}")
    return path


def is_project_root(path):
    for marker in PROJECT_MARKERS:
        if (path / marker).exists():
            return marker
    for child in path.iterdir() if path.is_dir() else []:
        if child.name.endswith(".xcodeproj"):
            return child.name
    return None


def require_target(raw_path):
    path = Path(raw_path).expanduser().resolve()
    if not path.exists():
        fail(f"target does not exist: {path}")
    if not path.is_dir():
        fail(f"target is not a directory: {path}")
    if path == Path(path.anchor):
        fail(f"refusing to organize a filesystem root: {path}")
    if path == Path.home():
        fail(f"refusing to organize the home directory: {path}")
    for tree in SYSTEM_TREES:
        tree_path = Path(tree)
        if path == tree_path or tree_path in path.parents:
            fail(f"refusing to organize a system path: {path} (under {tree})")
    marker = is_project_root(path)
    if marker is not None:
        fail(
            f"refusing to organize a project or repository root: {path} "
            f"(found '{marker}'). Moving files here would break the project. "
            "Choose a specific content subfolder instead."
        )
    return path


def protected_dir_reason(path):
    name = path.name
    if name.startswith("."):
        return "hidden directory"
    if name in PROTECTED_DIR_NAMES:
        return f"protected directory name '{name}'"
    for suffix in PROTECTED_DIR_SUFFIXES:
        if name.endswith(suffix):
            return f"bundle or package directory ('{suffix}')"
    marker = is_project_root(path)
    if marker is not None:
        return f"nested project root (found '{marker}')"
    return None


def classify(path):
    extension = path.suffix.lower()
    if extension in EXTENSION_CATEGORY:
        return EXTENSION_CATEGORY[extension], 0.95
    mime, _ = mimetypes.guess_type(path.name)
    if mime:
        top = mime.split("/", 1)[0]
        if top == "image":
            return "images", 0.6
        if top == "video":
            return "videos", 0.6
        if top == "audio":
            return "audio", 0.6
        if top == "text":
            return "documents", 0.6
        if mime == "application/pdf":
            return "documents", 0.6
        return "other", 0.5
    return "other", 0.3


def detected_type(path):
    mime, _ = mimetypes.guess_type(path.name)
    return mime or "unknown"


def normalize_destination(target, raw_destination):
    """Resolve a manifest destination relative to the target and confirm it
    stays inside the target without escaping via absolute or parent paths."""
    candidate = Path(raw_destination)
    if candidate.is_absolute():
        return None, "destination must be relative to the target folder"
    resolved = (target / candidate).resolve()
    if resolved != target and target not in resolved.parents:
        return None, "destination escapes the target folder"
    if resolved == target:
        return None, "destination must include a file name"
    return resolved, None


def destination_in_protected(target, resolved):
    relative = resolved.relative_to(target)
    for part in relative.parts[:-1]:
        if part in PROTECTED_DIR_NAMES or part.startswith("."):
            return f"destination passes through protected directory '{part}'"
        for suffix in PROTECTED_DIR_SUFFIXES:
            if part.endswith(suffix):
                return f"destination passes through bundle directory '{part}'"
    return None


def unique_destination(folder, filename, used):
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    candidate = f"{folder}/{filename}"
    counter = 2
    while candidate.lower() in used:
        candidate = f"{folder}/{stem} ({counter}){suffix}"
        counter += 1
    used.add(candidate.lower())
    return candidate


def default_destination(record, used):
    """Pre-fill a sensible destination so the model adjusts by cluster rather
    than typing every path. Media is grouped by year; screenshots and scans get
    their own subfolders; everything else lands under its category folder."""
    folder = CATEGORY_FOLDERS[record["category"]]
    cluster = record["name_cluster"]
    if cluster == "screenshots":
        folder = f"{folder}/Screenshots"
    elif cluster == "scans":
        folder = f"{folder}/Scans"
    elif record["category"] in MEDIA_CATEGORIES:
        year = record["modified"][:4]
        if year.isdigit():
            folder = f"{folder}/{year}"
    return unique_destination(folder, record["filename"], used)


def needs_review(record, threshold):
    reasons = []
    if record["confidence"] < threshold:
        reasons.append("low confidence")
    if record["detected_type"] == "unknown":
        reasons.append("unknown type")
    if record["name_cluster"] == "generic":
        reasons.append("generic name")
    if record["category"] == "other":
        reasons.append("uncategorized")
    return reasons


def command_doctor(args):
    embeddings = forge_embeddings.embeddings_doctor()
    info = {
        "pythonVersion": sys.version.split()[0],
        "platform": sys.platform,
        "dependencies": "Python standard library only.",
        "defaultConfidenceThreshold": DEFAULT_CONFIDENCE_THRESHOLD,
        "categories": sorted(set(CATEGORY_FOLDERS) - {"other"}) + ["other"],
        "embeddings": embeddings,
    }
    if args.json:
        print(json.dumps(info, indent=2))
        return
    print("organize-folder doctor")
    print(f"- Python: {info['pythonVersion']} ({info['platform']})")
    print(f"- Dependencies: {info['dependencies']}")
    print(f"- Default confidence threshold: {DEFAULT_CONFIDENCE_THRESHOLD}")
    print("- Scan writes manifest.csv, profile.md, profile.json, review_queue.md, skipped.md, and near_duplicates.md.")
    print("- Fingerprints every file and fully hashes only same-size duplicate candidates (use --full-hash to hash all).")
    reach = "reachable" if embeddings["reachable"] else "unreachable"
    print(f"- Embeddings ({embeddings['url']}): {reach} - {embeddings['detail']}.")
    print("  Used for content clusters and near-duplicate candidates; the scan degrades cleanly when unreachable.")
    print("- Refuses filesystem roots, the home directory, system trees, and project roots.")
    print("- Skips hidden paths, symlinks, repositories, dependency trees, and bundles.")


def command_scan(args):
    target = require_target(args.target)
    run_directory = require_new_directory(args.output)
    threshold = args.confidence_threshold
    if not 0.0 <= threshold <= 1.0:
        fail("--confidence-threshold must be between 0 and 1")
    if not -1.0 <= args.near_duplicate_threshold <= 1.0:
        fail("--near-duplicate-threshold must be between -1 and 1")
    if not -1.0 <= args.cluster_threshold <= 1.0:
        fail("--cluster-threshold must be between -1 and 1")

    records = []
    skipped = []

    for root, directories, files in os.walk(target, topdown=True, followlinks=False):
        root_path = Path(root)
        kept_directories = []
        for name in sorted(directories):
            directory = root_path / name
            if directory.is_symlink():
                skipped.append({"path": str(directory.relative_to(target)), "reason": "symlinked directory"})
                continue
            if directory.resolve() == run_directory:
                continue
            reason = protected_dir_reason(directory)
            if reason is not None:
                skipped.append({"path": str(directory.relative_to(target)), "reason": reason})
                continue
            kept_directories.append(name)
        directories[:] = kept_directories

        for name in sorted(files):
            file_path = root_path / name
            relative = str(file_path.relative_to(target))
            if file_path.is_symlink():
                skipped.append({"path": relative, "reason": "symlinked file"})
                continue
            if name.startswith("."):
                skipped.append({"path": relative, "reason": "hidden file"})
                continue
            try:
                stat = file_path.stat()
            except OSError as error:
                skipped.append({"path": relative, "reason": f"unreadable file: {error}"})
                continue
            category, confidence = classify(file_path)
            parent = str(file_path.parent.relative_to(target))
            records.append({
                "relative_source_path": relative,
                "parent_folder": "" if parent == "." else parent,
                "filename": name,
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                "extension": file_path.suffix.lower(),
                "detected_type": detected_type(file_path),
                "category": category,
                "confidence": round(confidence, 2),
                "name_cluster": name_cluster(name),
            })

    records.sort(key=lambda item: item["relative_source_path"].lower())

    # Fingerprint every file cheaply; reserve full sha256 for files whose size
    # collides with another (the only exact-duplicate candidates) unless the
    # user asked for full hashing of everything.
    size_counts = Counter(record["size_bytes"] for record in records)
    hashed = []
    for record in records:
        file_path = target / record["relative_source_path"]
        try:
            record["fingerprint"] = fingerprint(file_path, record["size_bytes"])
            if args.full_hash or size_counts[record["size_bytes"]] > 1:
                record["sha256"] = sha256(file_path)
            else:
                record["sha256"] = ""
        except OSError as error:
            skipped.append({"path": record["relative_source_path"], "reason": f"unreadable file: {error}"})
            continue
        record["peek"] = make_peek(file_path, record["category"], record["extension"])
        hashed.append(record)
    records = hashed

    by_hash = defaultdict(list)
    for record in records:
        if record["sha256"]:
            by_hash[record["sha256"]].append(record)

    used_destinations = set()
    for record in records:
        digest = record["sha256"]
        group = by_hash[digest] if digest else [record]
        if len(group) > 1:
            primary = min(group, key=lambda item: item["relative_source_path"].lower())
            is_duplicate = record is not primary
        else:
            primary = record
            is_duplicate = False
        if is_duplicate:
            record["is_duplicate"] = "true"
            record["duplicate_of"] = primary["relative_source_path"]
            record["status"] = "duplicate"
            record["proposed_destination"] = f"{DUPLICATES_FOLDER}/{record['relative_source_path']}"
            record["note"] = ""
        else:
            record["is_duplicate"] = "false"
            record["duplicate_of"] = ""
            record["status"] = "pending"
            record["proposed_destination"] = default_destination(record, used_destinations)
            record["note"] = "" if record["confidence"] >= threshold else "low confidence: review file content before moving"

    embeddings_info, near_pairs = compute_content_similarity(records, target, args)

    write_manifest(run_directory / "manifest.csv", records)
    write_near_duplicates_report(run_directory / "near_duplicates.md", target, near_pairs, embeddings_info)

    review = []
    for record in records:
        if record["is_duplicate"] == "true":
            continue
        reasons = needs_review(record, threshold)
        if reasons:
            review.append((record, reasons))

    scan = {
        "schemaVersion": SCAN_SCHEMA_VERSION,
        "target": str(target),
        "scannedAt": utc_now(),
        "confidenceThreshold": threshold,
        "fullHash": bool(args.full_hash),
        "fileCount": len(records),
        "duplicateCount": sum(1 for record in records if record["is_duplicate"] == "true"),
        "nearDuplicateCount": embeddings_info["nearDuplicateCount"],
        "lowConfidenceCount": sum(1 for record in records if record["confidence"] < threshold),
        "reviewCount": len(review),
        "skippedCount": len(skipped),
        "duplicatesFolder": DUPLICATES_FOLDER,
        "embeddings": embeddings_info,
        "files": {
            record["relative_source_path"]: {
                "sha256": record["sha256"],
                "fingerprint": record["fingerprint"],
                "size_bytes": record["size_bytes"],
                "is_duplicate": record["is_duplicate"] == "true",
                "duplicate_of": record["duplicate_of"],
            }
            for record in records
        },
    }
    (run_directory / "scan.json").write_text(json.dumps(scan, indent=2), encoding="utf-8")
    write_skipped_report(run_directory / "skipped.md", target, skipped)
    write_review_queue(run_directory / "review_queue.md", target, review)
    profile = build_profile(target, records)
    (run_directory / "profile.json").write_text(json.dumps(profile, indent=2), encoding="utf-8")
    (run_directory / "profile.md").write_text(profile_markdown(profile), encoding="utf-8")

    print(
        json.dumps(
            {
                "runDirectory": str(run_directory),
                "manifest": str(run_directory / "manifest.csv"),
                "profile": str(run_directory / "profile.md"),
                "reviewQueue": str(run_directory / "review_queue.md"),
                "nearDuplicates": str(run_directory / "near_duplicates.md"),
                "fileCount": scan["fileCount"],
                "duplicateCount": scan["duplicateCount"],
                "nearDuplicateCount": scan["nearDuplicateCount"],
                "lowConfidenceCount": scan["lowConfidenceCount"],
                "reviewCount": scan["reviewCount"],
                "skippedCount": scan["skippedCount"],
                "embeddings": {"enabled": embeddings_info["enabled"], "reason": embeddings_info["reason"]},
            },
            indent=2,
        )
    )


def write_manifest(path, records):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        for record in records:
            writer.writerow({field: record.get(field, "") for field in MANIFEST_FIELDS})


def write_skipped_report(path, target, skipped):
    lines = [
        "# Skipped and Protected Paths",
        "",
        f"Target: `{target}`",
        "",
        "These paths were left untouched during the scan and will never be moved. "
        "Hidden paths, symlinks, repositories, dependency trees, virtual environments, "
        "and application bundles are protected so reorganizing cannot break how they function.",
        "",
    ]
    if not skipped:
        lines.append("None.")
    else:
        lines.extend(["| Path | Reason |", "|---|---|"])
        for item in skipped:
            path_cell = item["path"].replace("|", "\\|")
            reason_cell = item["reason"].replace("|", "\\|")
            lines.append(f"| `{path_cell}` | {reason_cell} |")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def human_size(num_bytes):
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def write_review_queue(path, target, review):
    lines = [
        "# Review Queue",
        "",
        f"Target: `{target}`",
        "",
        "These files could not be categorized confidently from metadata alone. "
        "Open them to confirm what they are before finalizing their category and "
        "destination in `manifest.csv`. Files not listed here were classified with "
        "high confidence and usually need no per-file inspection.",
        "",
    ]
    if not review:
        lines.append("None. Every file was classified with high confidence.")
        lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")
        return
    if len(review) > REVIEW_QUEUE_LIMIT:
        lines.append(
            f"Showing the first {REVIEW_QUEUE_LIMIT} of {len(review)} files needing review."
        )
        lines.append("")
        review = review[:REVIEW_QUEUE_LIMIT]
    lines.extend(["| Path | Size | Category | Why | Peek |", "|---|---|---|---|---|"])
    for record, reasons in review:
        path_cell = record["relative_source_path"].replace("|", "\\|")
        peek_cell = (record.get("peek") or "").replace("|", "\\|")
        lines.append(
            f"| `{path_cell}` | {human_size(record['size_bytes'])} | "
            f"{record['category']} | {', '.join(reasons)} | {peek_cell} |"
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_near_duplicates_report(path, target, near_pairs, embeddings_info):
    lines = [
        "# Near-Duplicate Candidates",
        "",
        f"Target: `{target}`",
        "",
        "These files are highly similar in content but are not byte-identical, so "
        "exact-hash duplicate detection does not catch them: reformatted exports, "
        "drafts, versions, or lightly edited copies. They are advisory only and are "
        "never routed to `_duplicates/` automatically. Review each pair and decide "
        "whether to keep, relocate, or set one to `duplicate` in `manifest.csv`.",
        "",
    ]
    if not embeddings_info["enabled"]:
        lines.append(f"Content similarity was not computed: {embeddings_info['reason']}.")
        lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")
        return
    lines.append(
        f"Embedded {embeddings_info['embeddedCount']} text-bearing files with "
        f"`{embeddings_info['model']}` at a near-duplicate threshold of "
        f"{embeddings_info['nearDuplicateThreshold']}."
    )
    lines.append("")
    if not near_pairs:
        lines.append("No near-duplicate candidates found.")
        lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")
        return
    if len(near_pairs) > NEAR_DUPLICATE_REPORT_LIMIT:
        lines.append(
            f"Showing the {NEAR_DUPLICATE_REPORT_LIMIT} most similar of "
            f"{len(near_pairs)} candidate pairs."
        )
        lines.append("")
        near_pairs = near_pairs[:NEAR_DUPLICATE_REPORT_LIMIT]
    lines.extend(["| File | Near-duplicate of | Similarity |", "|---|---|---:|"])
    for source, primary, similarity in near_pairs:
        source_cell = source.replace("|", "\\|")
        primary_cell = primary.replace("|", "\\|")
        lines.append(f"| `{source_cell}` | `{primary_cell}` | {similarity:.3f} |")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def build_profile(target, rows):
    """Summarize the scanned folder into a compact, deterministic profile the
    model reads instead of crawling every file. Accepts records from `scan` or
    rows loaded from `manifest.csv`."""
    folders = Counter()
    folder_bytes = Counter()
    extensions = Counter()
    extension_bytes = Counter()
    categories = Counter()
    category_bytes = Counter()
    clusters = Counter()
    cluster_samples = defaultdict(list)
    content_clusters = Counter()
    content_cluster_samples = defaultdict(list)
    years = Counter()
    peeks = []
    largest = []
    total_bytes = 0
    duplicate_files = 0
    duplicate_bytes = 0
    near_duplicate_files = 0

    for row in rows:
        size = int(row.get("size_bytes") or 0)
        total_bytes += size
        parent = row.get("parent_folder") or "(root)"
        folders[parent] += 1
        folder_bytes[parent] += size
        extension = row.get("extension") or "(none)"
        extensions[extension] += 1
        extension_bytes[extension] += size
        category = row.get("category") or "other"
        categories[category] += 1
        category_bytes[category] += size
        cluster = row.get("name_cluster") or "other"
        clusters[cluster] += 1
        if len(cluster_samples[cluster]) < 3:
            cluster_samples[cluster].append(row.get("filename") or row.get("relative_source_path"))
        content_cluster = (row.get("content_cluster") or "").strip()
        if content_cluster:
            content_clusters[content_cluster] += 1
            if len(content_cluster_samples[content_cluster]) < 4:
                content_cluster_samples[content_cluster].append(
                    row.get("relative_source_path") or row.get("filename")
                )
        if (row.get("near_duplicate_of") or "").strip():
            near_duplicate_files += 1
        modified = row.get("modified") or ""
        year = modified[:4] if modified[:4].isdigit() else "unknown"
        years[year] += 1
        if str(row.get("is_duplicate")).lower() == "true":
            duplicate_files += 1
            duplicate_bytes += size
        peek = (row.get("peek") or "").strip()
        if peek and len(peeks) < 12:
            peeks.append({"path": row.get("relative_source_path"), "peek": peek})
        largest.append((size, row.get("relative_source_path")))

    largest.sort(reverse=True)
    return {
        "target": str(target),
        "generatedAt": utc_now(),
        "fileCount": len(rows),
        "totalBytes": total_bytes,
        "folders": [{"path": p, "count": c, "bytes": folder_bytes[p]} for p, c in folders.most_common()],
        "extensions": [{"extension": e, "count": c, "bytes": extension_bytes[e]} for e, c in extensions.most_common()],
        "categories": [{"category": c, "count": n, "bytes": category_bytes[c]} for c, n in categories.most_common()],
        "nameClusters": [{"cluster": cl, "count": n, "samples": cluster_samples[cl]} for cl, n in clusters.most_common()],
        "contentClusters": [
            {"cluster": cl, "count": n, "samples": content_cluster_samples[cl]}
            for cl, n in content_clusters.most_common()
        ],
        "dateClusters": [{"year": y, "count": n} for y, n in sorted(years.items())],
        "duplicates": {"files": duplicate_files, "bytes": duplicate_bytes},
        "nearDuplicates": {"files": near_duplicate_files},
        "largestFiles": [{"path": p, "bytes": b} for b, p in largest[:10]],
        "samplePeeks": peeks,
    }


def profile_markdown(profile):
    lines = [
        "# Folder Profile",
        "",
        f"Target: `{profile['target']}`",
        f"Generated: {profile['generatedAt']}",
        "",
        f"- Files: {profile['fileCount']}",
        f"- Total size: {human_size(profile['totalBytes'])}",
        f"- Duplicate files: {profile['duplicates']['files']} ({human_size(profile['duplicates']['bytes'])})",
        f"- Near-duplicate candidates: {profile.get('nearDuplicates', {}).get('files', 0)} (see `near_duplicates.md`)",
        "",
        "Use this profile to understand what the folder holds and design a layout "
        "that fits it. Adjust destinations in `manifest.csv` by cluster rather than "
        "row by row, and open the files in `review_queue.md` before trusting their "
        "category.",
        "",
        "## Folders",
        "",
        "| Folder | Files | Size |",
        "|---|---:|---:|",
    ]
    for entry in profile["folders"][:25]:
        label = entry["path"].replace("|", "\\|")
        lines.append(f"| `{label}` | {entry['count']} | {human_size(entry['bytes'])} |")
    lines.extend(["", "## Categories", "", "| Category | Files | Size |", "|---|---:|---:|"])
    for entry in profile["categories"]:
        lines.append(f"| {entry['category']} | {entry['count']} | {human_size(entry['bytes'])} |")
    lines.extend(["", "## Extensions", "", "| Extension | Files | Size |", "|---|---:|---:|"])
    for entry in profile["extensions"][:25]:
        lines.append(f"| {entry['extension']} | {entry['count']} | {human_size(entry['bytes'])} |")
    lines.extend(["", "## Name clusters", "", "| Cluster | Files | Examples |", "|---|---:|---|"])
    for entry in profile["nameClusters"][:25]:
        samples = ", ".join((s or "") for s in entry["samples"]).replace("|", "\\|")
        lines.append(f"| {entry['cluster']} | {entry['count']} | {samples} |")
    content_clusters = profile.get("contentClusters") or []
    if content_clusters:
        lines.extend(
            [
                "",
                "## Content clusters",
                "",
                "Groups of files with similar content (from embeddings), independent of "
                "filename. Use them to place related documents together.",
                "",
                "| Cluster | Files | Examples |",
                "|---|---:|---|",
            ]
        )
        for entry in content_clusters[:25]:
            samples = ", ".join((s or "") for s in entry["samples"]).replace("|", "\\|")
            lines.append(f"| {entry['cluster']} | {entry['count']} | {samples} |")
    lines.extend(["", "## Years (modified)", "", "| Year | Files |", "|---|---:|"])
    for entry in profile["dateClusters"]:
        lines.append(f"| {entry['year']} | {entry['count']} |")
    lines.extend(["", "## Largest files", "", "| Path | Size |", "|---|---:|"])
    for entry in profile["largestFiles"]:
        label = entry["path"].replace("|", "\\|")
        lines.append(f"| `{label}` | {human_size(entry['bytes'])} |")
    if profile["samplePeeks"]:
        lines.extend(["", "## Sample content peeks", ""])
        for entry in profile["samplePeeks"]:
            label = entry["path"].replace("`", "'")
            lines.append(f"- `{label}`: {entry['peek']}")
    lines.append("")
    return "\n".join(lines)


def load_scan(run_directory):
    return json.loads((run_directory / "scan.json").read_text(encoding="utf-8"))


def load_manifest(run_directory):
    manifest_path = run_directory / "manifest.csv"
    if not manifest_path.is_file():
        fail(f"manifest.csv is missing: {manifest_path}")
    with manifest_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != MANIFEST_FIELDS:
            fail("manifest.csv header was changed; restore the original column order")
        return list(reader)


def validate_manifest(scan, rows):
    """Return (movable, errors, warnings). movable is a list of
    (row, resolved_destination) for rows that will move."""
    target = Path(scan["target"])
    files = scan["files"]
    errors = []
    warnings = []
    movable = []
    seen_paths = set()
    used_destinations = {}

    for index, row in enumerate(rows, start=2):
        relative = row.get("relative_source_path", "")
        if not relative:
            errors.append(f"row {index}: empty relative_source_path")
            continue
        if relative in seen_paths:
            errors.append(f"row {index}: duplicate manifest entry for '{relative}'")
            continue
        seen_paths.add(relative)
        if relative not in files:
            errors.append(f"row {index}: '{relative}' was not part of the scan")
            continue
        if row.get("sha256") != files[relative]["sha256"]:
            errors.append(f"row {index}: sha256 for '{relative}' was edited; restore the scanned value")
            continue
        if row.get("fingerprint") != files[relative].get("fingerprint", ""):
            errors.append(f"row {index}: fingerprint for '{relative}' was edited; restore the scanned value")
            continue
        status = (row.get("status") or "").strip()
        if status not in PLAN_STATUSES:
            errors.append(f"row {index}: invalid status '{status}' (use {', '.join(sorted(PLAN_STATUSES))})")
            continue
        if status in KEEP_STATUSES:
            continue
        destination = (row.get("proposed_destination") or "").strip()
        if not destination:
            errors.append(f"row {index}: '{relative}' is '{status}' but has no proposed_destination")
            continue
        resolved, problem = normalize_destination(target, destination)
        if problem is not None:
            errors.append(f"row {index}: {problem} ('{destination}')")
            continue
        protected = destination_in_protected(target, resolved)
        if protected is not None:
            errors.append(f"row {index}: {protected}")
            continue
        key = str(resolved).lower() if sys.platform == "darwin" else str(resolved)
        if key in used_destinations:
            errors.append(f"row {index}: destination '{destination}' collides with '{used_destinations[key]}'")
            continue
        used_destinations[key] = relative
        movable.append((row, resolved))

    move_targets = {str(resolved) for _, resolved in movable}
    for row, resolved in movable:
        source = target / row["relative_source_path"]
        if resolved.exists() and str(resolved) not in move_targets:
            warnings.append(f"destination already exists and is not itself being moved: '{row['proposed_destination']}'")
        if resolved == source:
            warnings.append(f"'{row['relative_source_path']}' already at its destination; it will be left in place")
    return movable, errors, warnings


def command_plan(args):
    run_directory = require_run_directory(args.run_directory)
    scan = load_scan(run_directory)
    rows = load_manifest(run_directory)
    movable, errors, warnings = validate_manifest(scan, rows)

    categories = Counter()
    duplicates = 0
    keep = 0
    for row in rows:
        status = (row.get("status") or "").strip()
        if status == "duplicate":
            duplicates += 1
        elif status in KEEP_STATUSES:
            keep += 1
        categories[row.get("category") or "other"] += 1

    report = build_plan_report(scan, rows, movable, errors, warnings, categories, duplicates, keep)
    (run_directory / "plan_report.md").write_text(report, encoding="utf-8")

    result = {
        "valid": not errors,
        "filesToMove": len(movable),
        "duplicates": duplicates,
        "keep": keep,
        "errors": errors,
        "warnings": warnings,
        "report": str(run_directory / "plan_report.md"),
    }
    print(json.dumps(result, indent=2))
    if errors:
        raise SystemExit(1)


def build_plan_report(scan, rows, movable, errors, warnings, categories, duplicates, keep):
    lines = [
        "# Organization Plan",
        "",
        f"Target: `{scan['target']}`",
        f"Generated: {utc_now()}",
        "",
        "## Summary",
        "",
        f"- Files in manifest: {len(rows)}",
        f"- Files to move: {len(movable)}",
        f"- Duplicates routed to `{scan.get('duplicatesFolder', DUPLICATES_FOLDER)}/`: {duplicates}",
        f"- Files kept in place: {keep}",
        f"- Protected or skipped at scan time: {scan.get('skippedCount', 0)} (see `skipped.md`)",
        "",
        "## Files by category",
        "",
        "| Category | Count |",
        "|---|---:|",
    ]
    for category, count in sorted(categories.items()):
        lines.append(f"| {category} | {count} |")
    lines.extend(["", "## Validation", ""])
    if errors:
        lines.append("Errors must be resolved in `manifest.csv` before applying:")
        lines.append("")
        for error in errors:
            lines.append(f"- {error}")
    else:
        lines.append("No errors. The manifest is ready to apply.")
    if warnings:
        lines.extend(["", "Warnings:", ""])
        for warning in warnings:
            lines.append(f"- {warning}")
    lines.extend(["", "## Planned moves", ""])
    if movable:
        lines.extend(["| From | To |", "|---|---|"])
        for row, _ in movable:
            source = row["relative_source_path"].replace("|", "\\|")
            destination = row["proposed_destination"].replace("|", "\\|")
            lines.append(f"| `{source}` | `{destination}` |")
    else:
        lines.append("No moves planned.")
    lines.append("")
    return "\n".join(lines)


def load_move_log(run_directory):
    log_path = run_directory / "move_log.jsonl"
    entries = []
    if log_path.is_file():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def command_apply(args):
    run_directory = require_run_directory(args.run_directory)
    scan = load_scan(run_directory)
    rows = load_manifest(run_directory)
    movable, errors, warnings = validate_manifest(scan, rows)
    if errors:
        for error in errors:
            print(f"Error: {error}", file=sys.stderr)
        fail("manifest has validation errors; run 'plan' and fix them before applying")

    target = Path(scan["target"])
    log_path = run_directory / "move_log.jsonl"
    already_moved = {entry["source"] for entry in load_move_log(run_directory)}

    moved = 0
    failed = 0
    skipped = 0
    final_status = {}
    final_path = {}

    with log_path.open("a", encoding="utf-8") as log_handle:
        for row, resolved in movable:
            relative = row["relative_source_path"]
            source = target / relative
            if str(source) in already_moved:
                final_status[relative] = "moved"
                final_path[relative] = str(resolved.relative_to(target))
                skipped += 1
                continue
            if not source.exists() and resolved.exists():
                final_status[relative] = "moved"
                final_path[relative] = str(resolved.relative_to(target))
                skipped += 1
                continue
            if not source.is_file():
                final_status[relative] = "failed"
                final_path[relative] = relative
                row["note"] = "source file missing at apply time"
                failed += 1
                continue
            if integrity_changed(source, row["sha256"], row["fingerprint"]):
                final_status[relative] = "failed"
                final_path[relative] = relative
                row["note"] = "source changed since scan; not moved"
                failed += 1
                continue
            if resolved.exists():
                final_status[relative] = "failed"
                final_path[relative] = relative
                row["note"] = "destination already exists; not moved"
                failed += 1
                continue
            resolved.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(resolved))
            log_handle.write(
                json.dumps(
                    {
                        "source": str(source),
                        "destination": str(resolved),
                        "sha256": row["sha256"],
                        "movedAt": utc_now(),
                    }
                )
                + "\n"
            )
            log_handle.flush()
            final_status[relative] = "moved"
            final_path[relative] = str(resolved.relative_to(target))
            moved += 1

    write_final_manifest(run_directory / "final_manifest.csv", rows, final_status, final_path)

    print(
        json.dumps(
            {
                "moved": moved,
                "alreadyMoved": skipped,
                "failed": failed,
                "kept": sum(1 for row in rows if (row.get("status") or "").strip() in KEEP_STATUSES),
                "moveLog": str(log_path),
                "finalManifest": str(run_directory / "final_manifest.csv"),
            },
            indent=2,
        )
    )
    if failed:
        raise SystemExit(1)


def write_final_manifest(path, rows, final_status, final_path):
    fields = MANIFEST_FIELDS + ["final_status", "final_path"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            relative = row.get("relative_source_path", "")
            output = {field: row.get(field, "") for field in MANIFEST_FIELDS}
            status = (row.get("status") or "").strip()
            output["final_status"] = final_status.get(relative, "kept" if status in KEEP_STATUSES else "")
            output["final_path"] = final_path.get(relative, relative)
            writer.writerow(output)


def command_undo(args):
    run_directory = require_run_directory(args.run_directory)
    entries = load_move_log(run_directory)
    if not entries:
        print(json.dumps({"reversed": 0, "skipped": 0, "failed": 0, "note": "no moves to undo"}, indent=2))
        return

    reversed_count = 0
    skipped = 0
    failed = 0
    undo_log = run_directory / "undo_log.jsonl"
    with undo_log.open("a", encoding="utf-8") as handle:
        for entry in reversed(entries):
            source = Path(entry["source"])
            destination = Path(entry["destination"])
            if source.exists() and not destination.exists():
                skipped += 1
                continue
            if not destination.exists():
                failed += 1
                handle.write(json.dumps({"destination": str(destination), "result": "missing"}) + "\n")
                continue
            if source.exists():
                failed += 1
                handle.write(json.dumps({"source": str(source), "result": "source path occupied"}) + "\n")
                continue
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(destination), str(source))
            handle.write(json.dumps({"source": str(source), "destination": str(destination), "result": "reversed", "undoneAt": utc_now()}) + "\n")
            reversed_count += 1

    print(json.dumps({"reversed": reversed_count, "alreadyInPlace": skipped, "failed": failed, "undoLog": str(undo_log)}, indent=2))
    if failed:
        raise SystemExit(1)


def command_profile(args):
    run_directory = require_run_directory(args.run_directory)
    scan = load_scan(run_directory)
    rows = load_manifest(run_directory)
    profile = build_profile(Path(scan["target"]), rows)
    (run_directory / "profile.json").write_text(json.dumps(profile, indent=2), encoding="utf-8")
    (run_directory / "profile.md").write_text(profile_markdown(profile), encoding="utf-8")
    print(
        json.dumps(
            {
                "profile": str(run_directory / "profile.md"),
                "fileCount": profile["fileCount"],
                "totalBytes": profile["totalBytes"],
            },
            indent=2,
        )
    )


def parser():
    root = argparse.ArgumentParser(description="Non-destructively organize a folder through a reviewable manifest.")
    subparsers = root.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Report capabilities and safeguards.")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(handler=command_doctor)

    scan = subparsers.add_parser("scan", help="Scan a folder into a reviewable manifest and profile.")
    scan.add_argument("target")
    scan.add_argument("--output", required=True)
    scan.add_argument("--confidence-threshold", type=float, default=DEFAULT_CONFIDENCE_THRESHOLD)
    scan.add_argument(
        "--full-hash",
        action="store_true",
        help="Compute a full SHA-256 for every file instead of only same-size duplicate candidates.",
    )
    scan.add_argument(
        "--no-embeddings",
        action="store_true",
        help="Skip content embeddings (no content clusters or near-duplicate candidates).",
    )
    scan.add_argument(
        "--embeddings-url",
        help="Override the embeddings endpoint (default FORGE_EMBEDDINGS_URL or http://llms:8005/v1/embeddings).",
    )
    scan.add_argument(
        "--near-duplicate-threshold",
        type=float,
        default=DEFAULT_NEAR_DUPLICATE_THRESHOLD,
        help="Cosine similarity at or above which two files are flagged as near-duplicate candidates.",
    )
    scan.add_argument(
        "--cluster-threshold",
        type=float,
        default=DEFAULT_CONTENT_CLUSTER_THRESHOLD,
        help="Cosine similarity at or above which files are grouped into a content cluster.",
    )
    scan.set_defaults(handler=command_scan)

    profile = subparsers.add_parser("profile", help="Regenerate profile.md and profile.json from an existing run.")
    profile.add_argument("run_directory")
    profile.set_defaults(handler=command_profile)

    plan = subparsers.add_parser("plan", help="Validate the edited manifest and write a plan report.")
    plan.add_argument("run_directory")
    plan.set_defaults(handler=command_plan)

    apply_command = subparsers.add_parser("apply", help="Move files according to the validated manifest.")
    apply_command.add_argument("run_directory")
    apply_command.set_defaults(handler=command_apply)

    undo = subparsers.add_parser("undo", help="Reverse moves recorded in move_log.jsonl.")
    undo.add_argument("run_directory")
    undo.set_defaults(handler=command_undo)
    return root


def main():
    args = parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
