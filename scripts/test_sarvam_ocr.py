from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

from tools.sarvam_ocr import save_archive


with TemporaryDirectory() as tmp:
    archive = BytesIO()
    with ZipFile(archive, "w", ZIP_DEFLATED) as zf:
        zf.writestr("sample.png/sample.md", "தமிழ் உரை")
        zf.writestr("metadata/page_001.json", '{"text":"தமிழ் உரை"}')
        zf.writestr("manifest.json", "{}")
    saved = save_archive(archive.getvalue(), Path(tmp), "sample", "md")
    assert (Path(tmp) / "text/sample.md").read_text() == "தமிழ் உரை"
    assert (Path(tmp) / "json/page_001.json").exists()
    assert (Path(tmp) / "raw/manifest.json").exists()
    assert len(saved) == 3
print("Sarvam archive check passed")
