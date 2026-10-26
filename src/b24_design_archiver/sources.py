"""按变更编号查找资料目录与散落的文件。"""

from __future__ import annotations

import os
from pathlib import Path


def _starts_with_id(name: str, change_id: str) -> bool:
    if not name.casefold().startswith(change_id.casefold()):
        return False
    rest = name[len(change_id):]
    return not rest or not rest[0].isdigit()


class SourceIndex:
    def __init__(self, roots: list[Path], change_ids: list[str]):
        self.roots = roots
        self.change_ids = sorted(change_ids, key=len, reverse=True)
        self.directories: dict[str, list[Path]] = {key: [] for key in change_ids}
        self.files: dict[str, list[Path]] = {key: [] for key in change_ids}
        self.missing_roots: list[str] = []
        self.ready = False

    def invalidate(self) -> None:
        self.directories = {key: [] for key in self.change_ids}
        self.files = {key: [] for key in self.change_ids}
        self.missing_roots = []
        self.ready = False

    def _key(self, name: str) -> str | None:
        for change_id in self.change_ids:
            if _starts_with_id(name, change_id):
                return change_id
        return None

    def build(self) -> None:
        if self.ready:
            return
        self.invalidate()
        for source_root in self.roots:
            if not source_root.is_dir():
                self.missing_roots.append(str(source_root))
                continue
            for root_str, dirs, files in os.walk(source_root):
                root = Path(root_str)
                for dirname in list(dirs):
                    key = self._key(dirname)
                    if key:
                        self.directories[key].append(root / dirname)
                        dirs.remove(dirname)
                for filename in files:
                    key = self._key(filename)
                    if key:
                        self.files[key].append(root / filename)
        self.ready = True

    def for_record(self, change_id: str) -> dict:
        self.build()
        dirs = sorted(self.directories.get(change_id, []))
        loose = sorted(self.files.get(change_id, []))
        pdfs = list(loose_file for loose_file in loose if loose_file.suffix.lower() == ".pdf")
        docxs = list(loose_file for loose_file in loose if loose_file.suffix.lower() == ".docx")
        for directory in dirs:
            for path in directory.rglob("*"):
                if not path.is_file():
                    continue
                if path.suffix.lower() == ".pdf":
                    pdfs.append(path)
                elif path.suffix.lower() == ".docx":
                    docxs.append(path)
        return {
            "directories": [str(path) for path in dirs],
            "pdfs": [str(path) for path in sorted(set(pdfs))],
            "docxs": [str(path) for path in sorted(set(docxs))],
            "loose_files": [str(path) for path in loose],
            "missing_roots": self.missing_roots,
        }
