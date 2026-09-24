#!/usr/bin/env python3
"""Transcribe an image or document with Sarvam Document AI.

Usage: python3 tools/sarvam_ocr.py path/to/image.png
       python3 tools/sarvam_ocr.py samples/editor/sample2.png --output ocr_outputs/sarvam/raw/sample2
"""
import argparse
import json
import mimetypes
import os
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath

API = "https://api.sarvam.ai/doc-ai/v1/job"
ROOT = Path(__file__).resolve().parents[1]
SUPPORTED = {".pdf", ".png", ".jpg", ".jpeg", ".zip"}
MAX_BYTES = 200 * 1024 * 1024


def api_key():
    if os.environ.get("SARVAM_API_KEY"):
        return os.environ["SARVAM_API_KEY"]
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("SARVAM_API_KEY="):
                return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("Set SARVAM_API_KEY or add it to the ignored repo-root .env file.")


def json_request(url, key, data=None, boundary=None, timeout=120):
    headers = {"api-subscription-key": key}
    if data is not None:
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Sarvam API request failed: HTTP {exc.code} (response withheld)") from None
    except urllib.error.URLError:
        raise SystemExit("Sarvam API request failed: network error") from None


def save_archive(data, output, stem, output_format):
    text_dir, json_dir, raw_dir = (output / name for name in ("text", "json", "raw"))
    text_dir.mkdir(parents=True, exist_ok=True)
    json_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "result.zip").write_bytes(data)
    saved = []
    with zipfile.ZipFile(output / "raw" / "result.zip") as archive:
        primary_index = 0
        for item in archive.infolist():
            if item.is_dir():
                continue
            member = PurePosixPath(item.filename)
            name = member.name
            if name == "manifest.json":
                target = raw_dir / name
            elif "metadata" in member.parts and name.startswith("page_") and name.endswith(".json"):
                target = json_dir / name
            elif member.suffix.lower() == f".{output_format}":
                primary_index += 1
                folder = json_dir if output_format == "json" else text_dir
                target = folder / f"{stem}{'_' + str(primary_index) if primary_index > 1 else ''}.{output_format}"
            else:
                continue
            target.write_bytes(archive.read(item))
            saved.append(target)
    if not saved:
        raise SystemExit("Sarvam returned an archive without a recognized output file.")
    return saved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, help="image/document path to transcribe")
    parser.add_argument("--output", type=Path, help="output folder (default: ocr_outputs/sarvam/<input-name>)")
    parser.add_argument("--language", default="ta-IN", help="document language (default: ta-IN)")
    parser.add_argument("--format", choices=("md", "html", "json"), default="md", dest="output_format")
    parser.add_argument("--timeout", type=int, default=600, help="job timeout in seconds (default: 600)")
    args = parser.parse_args()

    image = args.image.expanduser().resolve()
    if not image.is_file():
        parser.error(f"input file does not exist: {image}")
    if image.suffix.lower() not in SUPPORTED:
        parser.error(f"unsupported input type: {image.suffix}; use PDF, PNG, JPG, or ZIP")
    if image.stat().st_size > MAX_BYTES:
        parser.error("input exceeds Sarvam's 200 MB file limit")

    output = args.output or ROOT / "ocr_outputs" / "sarvam" / image.stem
    output = output.expanduser().resolve()
    key = api_key()
    boundary = "----pi-" + uuid.uuid4().hex
    mime = mimetypes.guess_type(image.name)[0] or "application/octet-stream"
    body = bytearray()
    for name, value in (("language", args.language), ("output_format", args.output_format)):
        body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{image.name}\"\r\nContent-Type: {mime}\r\n\r\n".encode())
    body.extend(image.read_bytes())
    body.extend(f"\r\n--{boundary}--\r\n".encode())

    start = time.monotonic()
    job = json_request(f"{API}/digitise", key, bytes(body), boundary, timeout=120)
    job_id = job.get("job_id")
    if not job_id:
        raise SystemExit("Sarvam response did not include a job ID.")
    print(f"Job submitted; status={job.get('status', 'unknown')}")

    deadline = start + args.timeout
    while time.monotonic() < deadline:
        status = json_request(f"{API}/{job_id}/status", key)
        state = status.get("status", "unknown")
        print(f"Job status: {state}")
        if state in ("completed", "partially_completed"):
            break
        if state in ("failed", "cancelled", "canceled"):
            raise SystemExit("Sarvam OCR job failed (response withheld).")
        time.sleep(5)
    else:
        raise SystemExit(f"Sarvam OCR job timed out after {args.timeout} seconds.")

    download = json_request(f"{API}/{job_id}/download-url", key)
    try:
        with urllib.request.urlopen(download["url"], timeout=120) as response:
            saved = save_archive(response.read(), output, image.stem, args.output_format)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Sarvam result download failed: HTTP {exc.code}") from None
    print(f"Saved under {output} ({time.monotonic() - start:.2f}s end-to-end):")
    for path in saved:
        print(f"  {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")


if __name__ == "__main__":
    main()
