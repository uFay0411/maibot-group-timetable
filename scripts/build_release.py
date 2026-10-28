"""从明确的文件白名单构建 MaiBot WebUI 可安装 ZIP，不打包本地配置或数据。"""

from argparse import ArgumentParser
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import hashlib
import json

FILES = (
    ".gitignore", "__init__.py", "_manifest.json", "calendar_engine.py", "files.py", "imports.py",
    "plugin.py", "query.py", "semantic.py", "matching.py", "management.py", "group_access.py", "group_current.py", "group_scope.py", "presentation.py",
    "wakeup.py", "_wakeup_protocol.py", "store.py", "batch.py", "recognition.py", "group_upcoming.py",
    "pyproject.toml", "requirements.txt", "README.md", "CHANGELOG.md",
    "LICENSE", "config.default.toml", "examples/demo.ics", "examples/timetable-preview.png", "examples/group-current-preview.png",
    "examples/group-upcoming-preview.png",
    "assets/NotoSansCJKsc-Regular.otf", "licenses/NotoSansCJK-OFL.txt", "tests/conftest.py", "tests/test_timetable.py",
    "tests/test_reply_latency.py", "tests/test_query.py", "tests/test_semantic.py", "tests/test_matching.py", "tests/test_imports.py",
    "tests/test_official_group.py", "tests/test_management.py", "tests/test_natural_management.py", "tests/test_wakeup.py", "tests/test_group_images.py", "tests/test_group_current.py",
    "tests/test_batch_input.py", "tests/test_group_upcoming.py",
    "licenses/WakeUpDecoder-APACHE-2.0.txt", "THIRD_PARTY_NOTICES.md", "scripts/build_release.py",
)


def build(output_dir: Path) -> Path:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "_manifest.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    if any(not part.isdigit() for part in version.split(".")) or len(version.split(".")) != 3:
        raise ValueError("manifest 版本号必须为三段数字")
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"group_timetable-{version}.zip"
    # 先确认白名单完整，避免缺文件时留下被误认为成功的安装包。
    for name in FILES:
        if not (root / name).is_file():
            raise FileNotFoundError(name)
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name in FILES:
            archive.write(root / name, "group_timetable/" + name)
        archive.writestr("group_timetable/config.toml", (root / "config.default.toml").read_bytes())
    with ZipFile(output) as archive:
        if archive.testzip() is not None or len(archive.namelist()) != len(FILES) + 1:
            raise RuntimeError("安装包完整性校验失败")
        if archive.read("group_timetable/config.toml") != (root / "config.default.toml").read_bytes():
            raise RuntimeError("安装包默认配置校验失败")
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".zip.sha256").write_text(f"{checksum}  {output.name}\n", encoding="utf-8")
    return output


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parents[1] / "dist")
    args = parser.parse_args()
    print(build(args.output_dir.resolve()))
