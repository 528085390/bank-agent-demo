"""受控任务临时目录。

本模块是附件处理器**唯一允许写文件的地方**。所有落盘路径都由这里生成，
用户输入（文件名、压缩包成员名、密码）不参与路径构造。
"""

from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

_ILLEGAL = re.compile(r"[^A-Za-z0-9_-]")

#: 沙箱根目录的默认位置：仓库根的 `/.tmp/`（已在 .gitignore 中）。
DEFAULT_SANDBOX_ROOT = Path(__file__).resolve().parents[3] / ".tmp" / "billing"


def sanitize_task_id(task_id: str) -> str:
    """只保留 ``[A-Za-z0-9_-]``，杜绝 ``../`` 与盘符进入路径。"""

    cleaned = _ILLEGAL.sub("", task_id)
    if not cleaned:
        raise ValueError("task_id 去掉非法字符后为空，无法作为目录名")
    return cleaned


def allocate_dir(parent: Path, label: str) -> Path:
    """在受控父目录下新建一个唯一子目录。

    名字由固定前缀加随机串拼成，**不含任何用户输入**：同一次调用里多个压缩包
    （例如一个 ZIP 里装着两个 ZIP）各自解到互不覆盖的目录，靠的是随机串而不是
    成员名。
    """

    target = parent / f"{sanitize_task_id(label)}-{uuid.uuid4().hex[:8]}"
    target.mkdir(parents=True, exist_ok=False)
    return target


class TaskSandbox:
    """一个任务一个临时目录，支持 ``with`` 语句。"""

    def __init__(self, root: Path, task_id: str) -> None:
        self._root = Path(root)
        self._task_id = sanitize_task_id(task_id)
        self._path = self._root / self._task_id
        self._path.mkdir(parents=True, exist_ok=True)

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def path(self) -> Path:
        """本任务的目录，已确保存在。"""

        return self._path

    def resolve(self, name: str) -> Path:
        """把沙箱内的相对名解析为绝对路径；越界立即抛错。

        用 ``Path.resolve()`` 之后再比对前缀（``is_relative_to``），
        不是对原始字符串做 ``startswith``：符号链接与前缀相同的兄弟目录都能挡住。
        """

        if not name or name.strip() in {".", ".."}:
            raise ValueError(f"非法的沙箱内路径：{name!r}")
        candidate = (self._path / name).resolve()
        if not candidate.is_relative_to(self._path.resolve()):
            raise ValueError(f"路径越出沙箱：{name!r}")
        return candidate

    def new_scratch(self) -> Path:
        """新建一次处理专属的子目录，避免多次调用互相覆盖。"""

        return allocate_dir(self._path, "scratch")

    def cleanup(self) -> None:
        """删除整个任务目录；幂等，可重复调用。"""

        shutil.rmtree(self._path, ignore_errors=True)

    def __enter__(self) -> TaskSandbox:
        return self

    def __exit__(self, *_exc_info) -> None:
        self.cleanup()
