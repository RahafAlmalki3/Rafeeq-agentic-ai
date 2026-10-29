"""Long-term memory for Rafeeq: the command set of Anthropic's `memory` tool over a capped folder (Lab 2.3).

The model sees only paths under /memories. Every path must resolve inside the memory root (no `../` escapes), and the
store is capped so memory can't grow without bound. What may be stored is enforced by `rafeeq.guard` (no personal
data), not by this class.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Literal


class Memory:
    MAX_FILE = 1_500    # characters per file
    MAX_TOTAL = 4_000   # characters across the store

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, path: str) -> Path:
        if not path or not (path == "/memories" or path.startswith("/memories/")):
            raise ValueError(f"Paths must start with /memories, got {path!r}")
        real = (self.root / path.removeprefix("/memories").lstrip("/")).resolve()
        if real != self.root and self.root not in real.parents:
            raise ValueError(f"Path escapes the memory directory: {path!r}")
        return real

    def _total(self, excluding: Path | None = None) -> int:
        return sum(len(p.read_text(encoding="utf-8")) for p in self.root.rglob("*") if p.is_file() and p != excluding)

    def view(self, path: str = "/memories") -> str:
        p = self._resolve(path)
        if p.is_dir():
            files = sorted(str(f.relative_to(self.root)).replace("\\", "/") for f in p.rglob("*") if f.is_file())
            return "\n".join(f"/memories/{f}" for f in files) or "(empty)"
        if not p.exists():
            raise ValueError(f"{path} does not exist")
        return "\n".join(f"{i:4d}\t{line}" for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1))

    def create(self, path: str, file_text: str) -> str:
        p = self._resolve(path)
        if len(file_text) > self.MAX_FILE:
            raise ValueError(f"File too long ({len(file_text)} > {self.MAX_FILE} chars). Keep only what matters.")
        if self._total(excluding=p) + len(file_text) > self.MAX_TOTAL:
            raise ValueError("Memory is full. Delete or shorten old memories first.")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(file_text, encoding="utf-8")
        return f"Created {path}"

    def str_replace(self, path: str, old_str: str, new_str: str) -> str:
        p = self._resolve(path)
        text = p.read_text(encoding="utf-8")
        n = text.count(old_str)
        if n != 1:
            raise ValueError(f"old_str must occur exactly once in {path}; found {n} times.")
        return self.create(path, text.replace(old_str, new_str)).replace("Created", "Edited")

    def insert(self, path: str, insert_line: int, insert_text: str) -> str:
        p = self._resolve(path)
        lines = p.read_text(encoding="utf-8").splitlines()
        lines.insert(insert_line, insert_text.rstrip("\n"))
        return self.create(path, "\n".join(lines)).replace("Created", "Edited")

    def delete(self, path: str) -> str:
        p = self._resolve(path)
        if p == self.root:
            raise ValueError("Refusing to delete the whole memory directory")
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        return f"Deleted {path}"

    def rename(self, old_path: str, new_path: str) -> str:
        src, dst = self._resolve(old_path), self._resolve(new_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        return f"Renamed {old_path} → {new_path}"

    def contents(self) -> str:
        """Everything stored, for tests and audits."""
        return "\n".join(p.read_text(encoding="utf-8") for p in self.root.rglob("*") if p.is_file())

    def tool(self):
        """The contract as one tool function for run_agent."""
        mem = self

        def memory(command: Literal["view", "create", "str_replace", "insert", "delete", "rename"],
                   path: str | None = None, file_text: str | None = None, old_str: str | None = None,
                   new_str: str | None = None, insert_line: int | None = None, insert_text: str | None = None,
                   old_path: str | None = None, new_path: str | None = None) -> str:
            """Your long-term memory: files under /memories that persist between conversations.

            Args:
                command: view, create, str_replace, insert, delete or rename.
                path: a path under /memories, e.g. /memories/preferences.md.
                file_text: full content for create.
                old_str: exact text to replace (str_replace).
                new_str: replacement text (str_replace).
                insert_line: line number to insert after (insert; 0 = top).
                insert_text: text to insert (insert).
                old_path: source path (rename).
                new_path: destination path (rename).
            """
            args = {k: v for k, v in dict(path=path, file_text=file_text, old_str=old_str, new_str=new_str,
                                           insert_line=insert_line, insert_text=insert_text, old_path=old_path,
                                           new_path=new_path).items() if v is not None}
            try:
                return getattr(mem, command)(**args)
            except (ValueError, OSError, TypeError) as e:
                return json.dumps({"error": str(e)})

        return memory
