from __future__ import annotations

import errno

try:
    import fcntl  # ast.Try：bundler 不把 fcntl 提升到 bundle 头部（DR-11）
except ImportError:
    fcntl = None
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from devkit_flow.constants import (
    ANSI_ESCAPE,
    LATEST_CHECK_CACHE_RELATIVE_PATH,
    MEP_INSTALL_COMMAND,
    NPM_VIEW_TIMEOUT_SECONDS,
    PR_REVIEW_LOG_ALIGN_BYTES,
)
from devkit_flow.errors import DevkitFlowError, ExternalCommandError
from devkit_flow.version import NPM_PACKAGE_NAME, NPM_REGISTRY, VERSION


class LockBusy(Exception):
    """非阻塞加锁未取得，或阻塞等待超过 timeout。

    不是「文件系统不支持 flock」。ENOTSUP 等基础设施错误原样抛出，
    flow status 后备链据此把目录锁判为不可达。
    """


# Windows 阻塞锁只轮询 LK_NBLCK。LK_LOCK 每秒重试约 10 次后抛 OSError，与 flock 不等价（DR-44）。
_PLATFORM_LOCK_POLL_SECONDS = 0.2
# fd -> (持有线程, 重入次数, st_dev, st_ino)。
# 重入以 (线程, 文件身份) 判定。fd 号被复用时旧条目作废，必须重新加操作系统锁。
_LOCK_REFS: dict[int, tuple[int, int, int, int]] = {}
_LOCK_REFS_GUARD = threading.Lock()


def _errno_values(*names: str) -> frozenset[int]:
    values: list[int] = []
    for name in names:
        value = getattr(errno, name, None)
        if isinstance(value, int):
            values.append(value)
    return frozenset(values)


# EACCES 在 POSIX 上不一定是竞争（保留给调用方按「不可达」处理）；Windows locking 冲突常用 EACCES。
_POSIX_BUSY_ERRNOS = _errno_values("EAGAIN", "EWOULDBLOCK")
_WINDOWS_BUSY_ERRNOS = _errno_values("EAGAIN", "EWOULDBLOCK", "EACCES", "EDEADLK", "EDEADLOCK")


def _is_posix_contention(exc: BaseException) -> bool:
    if isinstance(exc, BlockingIOError):
        return True
    return isinstance(exc, OSError) and exc.errno in _POSIX_BUSY_ERRNOS


def _is_windows_contention(exc: BaseException) -> bool:
    if isinstance(exc, BlockingIOError):
        return True
    return isinstance(exc, OSError) and exc.errno in _WINDOWS_BUSY_ERRNOS


def _win_byte_lock(fd: int, mode: int) -> None:
    """对 fd 首字节加/解锁。空文件不补字节：锁定超出 EOF 的 1 字节区间。"""
    import msvcrt

    saved = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, mode, 1)
    finally:
        try:
            os.lseek(fd, saved, os.SEEK_SET)
        except OSError:
            pass


def _os_acquire(fd: int, *, blocking: bool, timeout: float | None) -> None:
    """读模块全局 ``fcntl``：bundle 扁平命名空间里，flow status 单测会换成假实现。"""
    if fcntl is not None:
        if blocking and timeout is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
            return
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError as exc:
                if not _is_posix_contention(exc):
                    raise
                if not blocking or (deadline is not None and time.monotonic() >= deadline):
                    raise LockBusy("file lock busy") from exc
                time.sleep(_PLATFORM_LOCK_POLL_SECONDS)
    import msvcrt

    deadline = time.monotonic() + max(0.0, timeout) if blocking and timeout is not None else None
    while True:
        try:
            _win_byte_lock(fd, msvcrt.LK_NBLCK)
            return
        except OSError as exc:
            if not _is_windows_contention(exc):
                raise
            if not blocking or (deadline is not None and time.monotonic() >= deadline):
                raise LockBusy("file lock busy") from exc
            time.sleep(_PLATFORM_LOCK_POLL_SECONDS)


def _os_release(fd: int) -> None:
    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        return
    import msvcrt

    try:
        _win_byte_lock(fd, msvcrt.LK_UNLCK)
    except OSError:
        pass


def _lock_file_identity(fd: int) -> tuple[int, int] | None:
    """当前 fd 的文件身份 ``(st_dev, st_ino)``。fd 已关闭或无效时返回 None。"""
    try:
        info = os.fstat(fd)
    except OSError:
        return None
    return (info.st_dev, info.st_ino)


def _holds_same_file(
    current: tuple[int, int, int, int] | None,
    me: int,
    identity: tuple[int, int] | None,
) -> bool:
    """重入以 (线程, 文件身份) 判定。身份缺失或 dev/ino 不同都不是重入。"""
    return (
        current is not None
        and identity is not None
        and current[0] == me
        and (current[2], current[3]) == identity
    )


def _acquire_platform_lock(fd: int, *, blocking: bool, timeout: float | None) -> None:
    me = threading.get_ident()
    identity = _lock_file_identity(fd)
    with _LOCK_REFS_GUARD:
        current = _LOCK_REFS.get(fd)
        if _holds_same_file(current, me, identity) and current is not None and identity is not None:
            _LOCK_REFS[fd] = (me, current[1] + 1, identity[0], identity[1])
            return
        # fstat 失败，或 fd 号已指向另一个文件：旧条目过期。丢掉后再走操作系统锁。
        if current is not None and (
            identity is None or (current[2], current[3]) != identity
        ):
            _LOCK_REFS.pop(fd, None)
    _os_acquire(fd, blocking=blocking, timeout=timeout)
    if identity is None:
        identity = _lock_file_identity(fd)
    with _LOCK_REFS_GUARD:
        current = _LOCK_REFS.get(fd)
        if _holds_same_file(current, me, identity) and current is not None and identity is not None:
            _LOCK_REFS[fd] = (me, current[1] + 1, identity[0], identity[1])
        elif identity is not None:
            _LOCK_REFS[fd] = (me, 1, identity[0], identity[1])
        else:
            _LOCK_REFS.pop(fd, None)


def _release_platform_lock(fd: int) -> None:
    """配对释放。身份对不上时当作没有条目，不减过期计数，并保守释放当前 fd。

    同一文件仍由别的线程持有时不释放，避免把那个线程的锁解掉。
    """
    me = threading.get_ident()
    identity = _lock_file_identity(fd)
    unlock = False
    with _LOCK_REFS_GUARD:
        current = _LOCK_REFS.get(fd)
        if current is None:
            unlock = True
        elif _holds_same_file(current, me, identity) and current is not None and identity is not None:
            if current[1] > 1:
                _LOCK_REFS[fd] = (me, current[1] - 1, identity[0], identity[1])
                return
            _LOCK_REFS.pop(fd, None)
            unlock = True
        elif identity is None or (current[2], current[3]) != identity:
            _LOCK_REFS.pop(fd, None)
            unlock = True
    if unlock:
        _os_release(fd)


@contextmanager
def platform_lock(fd: int, *, blocking: bool = True) -> Iterator[None]:
    """对已打开的 fd 加排他锁。退出时释放。

    POSIX：``flock(LOCK_EX)``；``blocking=False`` 时加 ``LOCK_NB``。
    Windows：只对首字节循环 ``msvcrt.locking(LK_NBLCK)``，间隔 0.2s。
    ``blocking=False`` 竞争失败抛 :class:`LockBusy`。
    重入以 (线程, 文件身份) 判定，不重复打操作系统锁。
    """
    _acquire_platform_lock(fd, blocking=blocking, timeout=None)
    try:
        yield
    finally:
        _release_platform_lock(fd)


def try_platform_lock(fd: int) -> bool:
    """非阻塞加锁。成功返回 True 且当前线程持有锁；竞争返回 False，不抛 LockBusy。

    重入以 (线程, 文件身份) 判定。成功后须在同一线程释放（:func:`_release_platform_lock`，或外层尚未持有时用
    :func:`platform_lock` 包住临界区）。不要在已持有后再进出一次 ``platform_lock``
    来释放：那只是重入计数加一减一，操作系统锁还在。
    """
    try:
        _acquire_platform_lock(fd, blocking=False, timeout=None)
    except LockBusy:
        return False
    return True


@contextmanager
def locked_path(
    path: Path, *, blocking: bool = True, timeout: float | None = None
) -> Iterator[None]:
    """打开或创建 ``path``，对 fd 加排他锁，退出时释放并关闭。

    ``blocking=True`` 且 ``timeout=None`` 与 ``flock(LOCK_EX)`` 一样一直等待。
    ``timeout`` 到期抛 :class:`LockBusy`。Windows 轮询间隔 0.2s，不用会自行放弃的阻塞 locking。
    """
    target = Path(path)
    parent = target.parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(target), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if blocking:
            _acquire_platform_lock(descriptor, blocking=True, timeout=timeout)
        else:
            _acquire_platform_lock(descriptor, blocking=False, timeout=None)
        try:
            yield
        finally:
            _release_platform_lock(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_text(path: Path, content: str) -> None:
    _atomic_replace_bytes(path, content.encode("utf-8"))


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    """跨进程排他锁。锁文件放在系统临时目录，不污染目标路径所在目录。"""
    lock_root = Path(tempfile.gettempdir()) / "devkit-flow-locks"
    lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_root.chmod(0o700)
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
    lock_path = lock_root / f"{digest}.lock"
    lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.chmod(lock_path, 0o600)
    with os.fdopen(lock_descriptor, "a+", encoding="utf-8") as lock_file:
        with platform_lock(lock_file.fileno()):
            yield


# ---------------------------------------------------------------------------
# 016 T-10（BC-05，D-40 / D-51 / D-57 / D-61 修订）：STATUS.md / task.md 读-改-写排他锁
#
# - 主协调点（D-57①）：对**被写文件所在目录本身**（STATUS.md / task.md 所在 spec 目录）只读打开目录 fd 后 flock，
#   不新建任何文件、不依赖 HOME / TMPDIR / DEVKIT_FLOW_LOCK_ROOT。同一目录下的 STATUS.md 与 task.md 共用这把锁。
# - 有效共同锁**只有目录锁**（D-61① / D-62）：能写目录里的文件不等于能只读打开目录（沙箱禁读目录、``--wx`` 权限、
#   NFS 把 flock 转成需写打开的 fcntl 锁），后备链无法证明与目录锁持有者互斥。因此：
#   * 事务命令与 ONES 远端建单认领（``required=True``）取不到目录锁即**失败关闭**（``FlowStatusLockUnavailable``），
#     不尝试后备链；
#   * 单步本地写（``required=False``）按 R-24 退到后备链：按固定顺序 主 ``~/.config/devkit/locks/flow-status`` → 私有回退
#     ``<gettempdir()>/devkit-flow-locks-<uid>``，对**所有可达**目录逐一加锁（D-51①；``DEVKIT_FLOW_LOCK_ROOT``
#     非空时只用该目录，仅测试隔离用），并在 stderr 告警一行——即使后备链全部取得也告警，因为此时无法证明与目录锁
#     持有者互斥（单步互斥在这一极端情形不保证）。
# - 竞争 → 等待（给了 ``timeout`` 时共享同一截止时间，超时抛 ``FlowStatusLockTimeout``，绝不降级为无锁写）；
#   open/flock 本身失败（非竞争）才视为不受支持/不可达。
# - 错误与告警文本不含本机绝对路径（N3-3）：只写文件名、锁目录标签与 errno 名。
# - 可重入按「线程 + 协调键」区分：目录锁模式的键是解析后的目录（同线程 STATUS.md → task.md 嵌套直接放行，
#   否则同进程另开目录 fd 会自锁死）；后备模式的键是解析后的文件路径（与 D-51 行为一致）。同进程其他线程另开
#   fd 再 flock，与跨进程一样被串行化。外层持有的不是目录锁、内层却要求 ``required=True``，同样失败关闭。
# ---------------------------------------------------------------------------

FLOW_STATUS_LOCK_DIRNAME = "flow-status"
FLOW_STATUS_LOCK_ROOT_ENV = "DEVKIT_FLOW_LOCK_ROOT"
_FLOW_STATUS_LOCK_MODE_DIR = "dir"
_FLOW_STATUS_LOCK_MODE_FALLBACK = "fallback"  # 单步写退到后备链（取得几把都不是有效共同锁，D-61①）
_FLOW_STATUS_LOCK_VALID_MODES = (_FLOW_STATUS_LOCK_MODE_DIR,)
# (线程, 协调键) → [深度, 模式]
_FLOW_STATUS_LOCKS_HELD: dict[tuple[int, str], list[Any]] = {}
_FLOW_STATUS_LOCKS_GUARD = threading.Lock()
_FLOW_STATUS_LOCK_POLL_SECONDS = 0.05


class FlowStatusLockTimeout(DevkitFlowError):
    """在给定时限内未取得 STATUS 写锁（他人持锁）。"""


class FlowStatusLockUnavailable(DevkitFlowError):
    """无有效共同锁（spec 目录 fd 锁不可用）；仅 ``required=True`` 的调用方会收到。"""


def _lock_failure_reason(error: BaseException) -> str:
    """锁基础设施失败的简短原因（N3-3：不含任何路径）——errno 名 + strerror，否则异常类型名。"""
    if isinstance(error, OSError):
        name = errno.errorcode.get(error.errno or 0, "") if error.errno else ""
        text = error.strerror or ""
        return " ".join(part for part in (name, text) if part) or type(error).__name__
    return type(error).__name__


def _flow_status_lock_root_label(position: int) -> str:
    """后备锁目录的无路径标签（N3-3）。"""
    if os.environ.get(FLOW_STATUS_LOCK_ROOT_ENV, "").strip():
        return FLOW_STATUS_LOCK_ROOT_ENV
    return "主后备锁目录(~/.config/devkit/locks)" if position == 0 else "回退锁目录(系统临时目录)"


def flow_status_lock_roots() -> list[Path]:
    """后备锁目录（固定顺序）：TMPDIR 无关的主目录 → 用户私有回退目录；``DEVKIT_FLOW_LOCK_ROOT`` 覆盖时只用它。"""
    override = os.environ.get(FLOW_STATUS_LOCK_ROOT_ENV, "").strip()
    if override:
        return [Path(override)]
    roots: list[Path] = []
    try:
        roots.append(Path.home() / ".config" / "devkit" / "locks" / FLOW_STATUS_LOCK_DIRNAME)
    except Exception:
        pass
    try:
        uid = os.getuid() if hasattr(os, "getuid") else "user"
        roots.append(Path(tempfile.gettempdir()) / f"devkit-flow-locks-{uid}")
    except Exception:
        pass
    return roots


def _flow_status_lock_key(path: Path) -> str:
    try:
        return str(path.resolve())
    except Exception:
        return str(path.absolute())


def _flow_status_directory_key(path: Path) -> str:
    return _flow_status_lock_key(path.parent)


def _open_directory_lock(directory: Path) -> int:
    """只读打开目录 fd（不新建任何文件）；失败抛异常（调用方视为目录锁不受支持）。"""
    return os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))


def _open_flow_status_lock_file(root: Path, digest: str) -> int:
    """打开某一后备锁目录下的锁文件；失败抛异常（调用方视为该目录不可达）。"""
    lock_path = root / f"{digest}.lock"
    try:
        created = False
        if not root.is_dir():
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            created = True
        if created:
            root.chmod(0o700)
        return os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except Exception:
        if lock_path.is_file():
            # 目录不可写（如宿主沙箱只读放行 HOME）但锁文件已存在：只读打开即可 flock。
            return os.open(lock_path, os.O_RDONLY)
        raise


def _close_quietly(descriptor: int, *, unlock: bool) -> None:
    if unlock:
        try:
            _release_platform_lock(descriptor)
        except Exception:
            pass
    try:
        os.close(descriptor)
    except Exception:
        pass


def _flock_with_deadline(descriptor: int, path: Path, timeout: float | None, deadline: float | None) -> None:
    """竞争 → 等待/超时（FlowStatusLockTimeout）；加锁本身失败按原异常抛出（调用方视为不受支持/不可达）。"""
    if deadline is None:
        _acquire_platform_lock(descriptor, blocking=True, timeout=None)
        return
    while True:
        try:
            _acquire_platform_lock(descriptor, blocking=False, timeout=None)
            return
        except LockBusy:
            if time.monotonic() >= deadline:
                raise FlowStatusLockTimeout(
                    f"{timeout:g}s 内未取得 {path.name} 写锁（他人正在写入），请稍后重试。"
                ) from None
            time.sleep(_FLOW_STATUS_LOCK_POLL_SECONDS)


def _acquire_directory_lock(path: Path, deadline: float | None, timeout: float | None) -> tuple[int | None, str]:
    """D-57①：对 path 所在目录的目录 fd 加锁。返回 (fd, '') 或 (None, 不受支持原因，无路径)；竞争超时抛 FlowStatusLockTimeout。"""
    try:
        descriptor = _open_directory_lock(path.parent)
    except Exception as error:
        return None, f"只读打开目录失败：{_lock_failure_reason(error)}"
    try:
        _flock_with_deadline(descriptor, path, timeout, deadline)
    except FlowStatusLockTimeout:
        _close_quietly(descriptor, unlock=False)
        raise
    except Exception as error:  # flock 不受支持等（非竞争）
        _close_quietly(descriptor, unlock=False)
        return None, f"目录 flock 失败：{_lock_failure_reason(error)}"
    return descriptor, ""


def _acquire_flow_status_locks(
    path: Path, key: str, timeout: float | None, deadline: float | None = None
) -> tuple[list[int], list[str]]:
    """后备链（仅单步本地写，D-61①）：先按固定顺序打开全部锁目录的锁文件，再逐一 flock；返回 (已持有的 fd,
    不可达摘要)。不可达目录跳过继续（D-51①）；超时时释放已持有的锁再抛 FlowStatusLockTimeout。"""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    if deadline is None and timeout is not None:
        deadline = time.monotonic() + max(0.0, timeout)
    opened: list[tuple[int, int]] = []  # (链中位置, fd)
    held: list[int] = []
    failures: list[str] = []
    for position, root in enumerate(flow_status_lock_roots()):
        try:
            opened.append((position, _open_flow_status_lock_file(root, digest)))
        except Exception as error:  # 基础设施失败：该目录不可达，继续回退链（D-51①）
            failures.append(f"{_flow_status_lock_root_label(position)}: {_lock_failure_reason(error)}")
    for index, (position, descriptor) in enumerate(opened):
        try:
            _flock_with_deadline(descriptor, path, timeout, deadline)
        except FlowStatusLockTimeout:
            for _position, remaining in opened[index:]:
                _close_quietly(remaining, unlock=False)
            for previous in reversed(held):
                _close_quietly(previous, unlock=True)
            raise
        except Exception as error:  # flock 不受支持等：视同该目录不可达
            _close_quietly(descriptor, unlock=False)
            failures.append(f"{_flow_status_lock_root_label(position)}: {_lock_failure_reason(error)}")
            continue
        held.append(descriptor)
    return held, failures


def _warn_flow_status_lock(message: str) -> None:
    try:
        print(f"devkit-flow: 警告：{message}", file=sys.stderr)
    except Exception:
        pass


def _flow_status_held_mode(thread_id: int, dir_key: str, file_key: str) -> tuple[tuple[int, str], str] | None:
    with _FLOW_STATUS_LOCKS_GUARD:
        for held_key in ((thread_id, dir_key), (thread_id, file_key)):
            entry = _FLOW_STATUS_LOCKS_HELD.get(held_key)
            if entry is not None:
                return held_key, entry[1]
    return None


def _flow_status_lock_unavailable(path: Path, reason: str) -> FlowStatusLockUnavailable:
    return FlowStatusLockUnavailable(
        f"{path.name} 所在 spec 目录无法加目录锁（{reason}）；批量/建单操作只认目录锁作为共同协调点"
        "（后备锁无法证明互斥），已拒绝且未写入。"
    )


@contextmanager
def flow_status_write_lock(
    path: Path, *, timeout: float | None = None, required: bool = False
) -> Iterator[None]:
    """单步、批量与 ONES 回写共用的 STATUS.md / task.md 读-改-写排他锁（见上方模块注释）。"""
    thread_id = threading.get_ident()
    dir_key = "dir:" + _flow_status_directory_key(path)
    file_key = "file:" + _flow_status_lock_key(path)
    held = _flow_status_held_mode(thread_id, dir_key, file_key)
    if held is not None:
        held_key, mode = held
        if required and mode not in _FLOW_STATUS_LOCK_VALID_MODES:
            raise _flow_status_lock_unavailable(path, "外层只持有后备锁")
        with _FLOW_STATUS_LOCKS_GUARD:
            _FLOW_STATUS_LOCKS_HELD[held_key][0] += 1
        try:
            yield
        finally:
            with _FLOW_STATUS_LOCKS_GUARD:
                _FLOW_STATUS_LOCKS_HELD[held_key][0] -= 1
        return

    deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
    descriptors: list[int] = []
    try:
        dir_descriptor, dir_failure = _acquire_directory_lock(path, deadline, timeout)
    except FlowStatusLockTimeout:
        raise
    except Exception as error:  # 兜底：锁基础设施异常不越过 CLI 边界
        dir_descriptor, dir_failure = None, _lock_failure_reason(error)
    if dir_descriptor is not None:
        descriptors = [dir_descriptor]
        held_key, mode = (thread_id, dir_key), _FLOW_STATUS_LOCK_MODE_DIR
    elif required:
        # D-61①：事务/建单只认目录锁；不尝试后备链（与目录锁持有者无法证明互斥），失败关闭。
        raise _flow_status_lock_unavailable(path, dir_failure)
    else:
        try:
            descriptors, failures = _acquire_flow_status_locks(path, file_key[len("file:"):], timeout, deadline)
        except FlowStatusLockTimeout:
            raise
        except Exception as error:  # 兜底：锁基础设施异常不越过 CLI 边界
            descriptors, failures = [], [_lock_failure_reason(error)]
        if not descriptors:
            _warn_flow_status_lock(
                f"{path.name} 写锁不可用（{'; '.join([dir_failure, *failures])}），本次不加锁继续执行。"
            )
        else:
            unreachable = f"；部分不可达：{'; '.join(failures)}" if failures else ""
            _warn_flow_status_lock(
                f"{path.name} 目录锁不可用（{dir_failure}），已改用后备锁目录{unreachable}"
                "（无法证明与其他进程互斥）。"
            )
        held_key, mode = (thread_id, file_key), _FLOW_STATUS_LOCK_MODE_FALLBACK
    with _FLOW_STATUS_LOCKS_GUARD:
        _FLOW_STATUS_LOCKS_HELD[held_key] = [1, mode]
    try:
        yield
    finally:
        with _FLOW_STATUS_LOCKS_GUARD:
            _FLOW_STATUS_LOCKS_HELD.pop(held_key, None)
        for descriptor in reversed(descriptors):
            _close_quietly(descriptor, unlock=True)


def emit(data: dict[str, Any], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    if "message" in data:
        print(data["message"])
        return
    print(json.dumps(data, ensure_ascii=False, indent=2))


def attach_dashboard_warning(
    data: dict[str, Any],
    refresh: Callable[[Path, dict[str, Any]], str | None] | None,
    workspace: Path,
    feature: dict[str, Any],
) -> None:
    """状态写入命令尾部的 dashboard 自动重渲染统一挂钩（设计 §4.3.3；D-13 cli.py 组装注入）。

    refresh 为 None（未注入）时 no-op；回调契约 fail-safe（返回 warning 或 None），
    此处仍兜底吞掉异常——任何情况下重渲染都不得阻断状态写入命令本身。
    有 warning 时挂到 data["dashboard"]["warning"]（接口文档 §4.8，仅失败时出现）。
    """
    if refresh is None:
        return
    try:
        warning = refresh(workspace, feature)
    except Exception as error:  # 兜底防线：回调失守也不阻断状态写入（D-5）
        warning = f"dashboard 自动重渲染失败（状态写入已完成，不受影响）: {compact_error(error)}"
    if warning:
        data["dashboard"] = {"warning": warning}


def run_external(
    command: list[str],
    *,
    check: bool = True,
    foreground: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an external command.

    ``timeout`` is optional and omitted from subprocess.run when None, so
    existing callers stay unchanged. Timed-out processes degrade as failure:
    ``check=True`` raises ExternalCommandError; ``check=False`` returns a
    non-zero CompletedProcess (identity-chain callers then fall through).
    """
    run_kwargs: dict[str, Any] = {"text": True}
    if timeout is not None:
        run_kwargs["timeout"] = timeout
    try:
        if foreground:
            result = subprocess.run(
                command, stdout=sys.stderr, stderr=sys.stderr, **run_kwargs
            )
        else:
            result = subprocess.run(command, capture_output=True, **run_kwargs)
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout if isinstance(error.stdout, str) else ""
        stderr = error.stderr if isinstance(error.stderr, str) else ""
        if isinstance(error.stdout, (bytes, bytearray)):
            stdout = error.stdout.decode("utf-8", errors="replace")
        if isinstance(error.stderr, (bytes, bytearray)):
            stderr = error.stderr.decode("utf-8", errors="replace")
        result = subprocess.CompletedProcess(
            list(command),
            124,
            stdout or "",
            stderr or f"timed out after {timeout}s",
        )
        if check:
            raise ExternalCommandError(command, result) from error
        return result
    if check and result.returncode != 0:
        raise ExternalCommandError(command, result)
    return result


def mep_logged_in(*, timeout: float | None = None) -> tuple[bool, str]:
    result = run_external(["external-cli", "ones", "sso", "status"], check=False, timeout=timeout)
    output = ANSI_ESCAPE.sub("", f"{result.stdout}\n{result.stderr}").strip()
    logged_in = (
        result.returncode == 0
        and "已登录" in output
        and "未登录" not in output
        and "失效" not in output
        and "过期" not in output
    )
    return logged_in, output


def mep_current_user(*, timeout: float | None = None) -> str:
    """Return the authenticated ONES MIS without reading or persisting credentials."""
    result = run_external(["external-cli", "ones", "config"], timeout=timeout)
    output = ANSI_ESCAPE.sub("", f"{result.stdout}\n{result.stderr}")
    for pattern in (
        r"MIS\s*(?:账号|account)\s*[：:]\s*([^\s]+)",
        r"(?:当前用户|current\s+user)\s*[：:]\s*([^\s]+)",
    ):
        match = re.search(pattern, output, flags=re.IGNORECASE)
        if match and match.group(1).strip():
            return match.group(1).strip()
    raise DevkitFlowError(
        "Unable to determine the current ONES login MIS from `external-cli ones config`; "
        "provide --requirement-assignee explicitly."
    )


def ensure_mep_and_login() -> dict[str, Any]:
    installed = False
    if shutil.which("external-cli") is None:
        if shutil.which("npm") is None:
            raise DevkitFlowError("ONES is enabled but npm is not installed; cannot install .")
        run_external(MEP_INSTALL_COMMAND)
        installed = True
        if shutil.which("external-cli") is None:
            raise DevkitFlowError("Installed  but `external-cli` is still not available on PATH.")

    logged_in, _ = mep_logged_in()
    login_performed = False
    if not logged_in:
        run_external(["external-cli", "ones", "sso", "login"], foreground=True)
        login_performed = True
        logged_in, status_output = mep_logged_in()
        if not logged_in:
            raise DevkitFlowError(f"external-cli ones sso login did not establish a valid session: {status_output}")

    return {
        "mep_installed": True,
        "installed_now": installed,
        "logged_in": True,
        "login_performed": login_performed,
    }


def parse_json_output(output: str) -> Any:
    cleaned = ANSI_ESCAPE.sub("", output).strip()
    if not cleaned:
        return {}
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    starts = [index for index in (cleaned.find("{"), cleaned.find("[")) if index >= 0]
    if starts:
        start = min(starts)
        for closing in ("}", "]"):
            end = cleaned.rfind(closing)
            if end > start:
                try:
                    return json.loads(cleaned[start : end + 1])
                except json.JSONDecodeError:
                    continue
    raise DevkitFlowError("external-cli returned non-JSON output where JSON was required.")


def run_mep_json(arguments: list[str]) -> Any:
    result = run_external(["external-cli", "ones", *arguments])
    return parse_json_output(result.stdout)


def extract_numeric_id(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("issueId", "workItemId", "id"):
            value = data.get(key)
            if isinstance(value, (str, int)) and str(value).isdigit():
                return str(value)
            if isinstance(value, dict):
                nested = value.get("value", value.get("id"))
                if isinstance(nested, (str, int)) and str(nested).isdigit():
                    return str(nested)
        for key in ("data", "result", "issue", "workItem"):
            if key in data:
                found = extract_numeric_id(data[key])
                if found:
                    return found
        for value in data.values():
            found = extract_numeric_id(value)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = extract_numeric_id(item)
            if found:
                return found
    return ""


def find_objects_with_type(data: Any, target_type: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(data, dict):
        if data.get("type") == target_type:
            found.append(data)
        for value in data.values():
            found.extend(find_objects_with_type(value, target_type))
    elif isinstance(data, list):
        for item in data:
            found.extend(find_objects_with_type(item, target_type))
    return found


def compact_error(error: Exception) -> str:
    text = ANSI_ESCAPE.sub("", str(error)).replace("\n", " ").strip()
    return text[:160] or error.__class__.__name__


def mep_bootstrapped(*, timeout: float | None = None) -> bool:
    """external-cli 已安装且已登录（只读检查，不触发安装/登录）。"""
    if shutil.which("external-cli") is None:
        return False
    logged_in, _output = mep_logged_in(timeout=timeout)
    return logged_in


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_replace_bytes(path, content)


def _read_umask() -> int:
    """读取当前 umask。os.umask 是进程级全局状态：读必须先 set 再立刻 restore，非线程安全。"""
    mask = os.umask(0)
    os.umask(mask)
    return mask


def _atomic_write_mode(target: Path) -> int:
    try:
        return stat.S_IMODE(os.stat(target).st_mode)
    except FileNotFoundError:
        return 0o666 & ~_read_umask()


def _atomic_replace_bytes(path: Path, content: bytes) -> None:
    file_descriptor, temp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(file_descriptor, "wb") as temp_file:
            temp_file.write(content)
        os.chmod(temp_path, _atomic_write_mode(path))
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def ones_workitem_url(space_id: str, kind: str, workitem_id: str) -> str:
    """ONES 工作项详情页地址；kind 为 requirement 或 task。"""
    return f"/ones/product/{space_id}/workItem/{kind}/detail/{workitem_id}"


# --- 工作区初始化 / 自更新 / 体检（setup / self-update / doctor） ---------------------
def current_cli_path() -> Path:
    """当前正在运行的 CLI 文件（脚本直跑与测试加载模块两种场景都取 __file__）。"""
    return Path(__file__).resolve()


def runtime_support_candidates(cli_path: Path | None = None) -> list[Path]:
    """支撑树候选（按序）：发行包 runtime-support/、<cli_dir>/../scripts、源码态 <repo>/scripts。"""
    cli_dir = (cli_path if cli_path is not None else current_cli_path()).parent
    candidates = [cli_dir / "runtime-support", cli_dir.parent / "scripts"]
    if cli_dir.name == "devkit_flow" and cli_dir.parent.name == "src":
        candidates.append(cli_dir.parent.parent / "scripts")
    return candidates


def runtime_support_root(marker: str, cli_path: Path | None = None) -> Path | None:
    """支撑树定位的唯一实现：取第一个含完整 marker 包的候选并加入 sys.path；找不到返回 None。

    完整 = marker 是目录且其 __init__.py 是文件（与发行包 verify_lifecycle_support 同口径）；
    残缺候选（如只剩 __pycache__ 的目录）跳过、继续找下一候选。
    cli_path 缺省取 current_cli_path()；调用方可传入自身视角的 CLI 路径（保持既有可替换点）。
    """
    for candidate in runtime_support_candidates(cli_path):
        package = candidate / marker
        if package.is_dir() and (package / "__init__.py").is_file():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            return candidate
    return None


def load_support_file_readonly(support: Path, relpath: str, name: str) -> Any:
    """按文件路径只读加载单个支撑模块：不执行所在包的 __init__.py、不写 bytecode，加载后恢复原值。

    sys.modules[name] 已是同一文件（按 realpath 比较）→ 直接复用；已被其他文件（或无 __file__）的模块占用 → ImportError，
    不覆盖（name 是调用方的缓存键，静默改名会掩盖冲突）。执行失败时 sys.modules 还原为进入前状态。
    """
    path = Path(support) / relpath
    loaded = sys.modules.get(name)
    if loaded is not None:
        loaded_file = getattr(loaded, "__file__", None)
        if loaded_file is not None and os.path.realpath(loaded_file) == os.path.realpath(path):
            return loaded
        raise ImportError(f"module name {name!r} is already bound to {getattr(loaded, '__file__', None)!r}, "
                          f"refusing to replace it with {path}")
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load support file: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)  # 进入时 name 必未绑定（冲突已在上面拒绝），弹出即还原原状
            raise
        return module
    finally:
        sys.dont_write_bytecode = previous


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def npm_view_latest_version(timeout: int = NPM_VIEW_TIMEOUT_SECONDS) -> str | None:
    """npm 通道远端版本探测：npm view <pkg> version --registry=<registry>，stdout 即版本号。

    npm 不在 PATH、命令失败/超时、输出不是 x.y.z 时一律返回 None（调用方按失败静默处理）。
    """
    npm = shutil.which("npm")
    if not npm:
        return None
    try:
        result = subprocess.run(
            [npm, "view", NPM_PACKAGE_NAME, "version", f"--registry={NPM_REGISTRY}"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    latest = (result.stdout or "").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", latest):
        return None
    return latest


def version_tuple(text: str) -> tuple[int, ...]:
    """x.y.z → (x, y, z)，用于简单的版本号数字元组比较。"""
    return tuple(int(piece) for piece in text.strip().split("."))


def latest_check_cache_path() -> Path:
    return Path.home() / LATEST_CHECK_CACHE_RELATIVE_PATH


def read_latest_check_cache(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("checkedAt"), str):
        return None
    return data


def write_latest_check_cache(path: Path, latest: str | None) -> None:
    payload = {"checkedAt": datetime.now(timezone.utc).isoformat(), "latest": latest}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 缓存写不进去不影响主命令


def maybe_check_latest_update(ttl_hours: float = 4) -> dict[str, Any] | None:
    """检查是否有新版本 devkit-flow（npm 通道）；任何失败都静默返回 None，绝不打断主命令。

    - 远端版本探测走 npm view；npm 不在 PATH 或查询失败时返回 None。
    - 结果缓存 ~/.config/devkit/latest-check.json：距上次检查 < ttl_hours 直接读缓存，不访问网络；
      失败同样写入失败时间戳（latest 为 null），避免每次命令都重复打网络。
    """
    cache_path = latest_check_cache_path()
    now = datetime.now(timezone.utc)
    cached = read_latest_check_cache(cache_path)
    if cached is not None:
        try:
            checked_at = datetime.fromisoformat(cached["checkedAt"])
        except ValueError:
            checked_at = None
        if checked_at is not None:
            if checked_at.tzinfo is None:
                checked_at = checked_at.replace(tzinfo=timezone.utc)
            if now - checked_at < timedelta(hours=ttl_hours):
                latest = cached.get("latest")
                if not isinstance(latest, str) or not re.fullmatch(r"\d+\.\d+\.\d+", latest):
                    return None  # 缓存的是失败时间戳：TTL 内直接放弃，不再联网
                return {
                    "current": VERSION,
                    "latest": latest,
                    "update_available": version_tuple(latest) > version_tuple(VERSION),
                }
    latest = npm_view_latest_version()
    if latest is None:  # npm 不可用/查询失败：静默并记录失败时间戳
        write_latest_check_cache(cache_path, None)
        return None
    write_latest_check_cache(cache_path, latest)
    return {
        "current": VERSION,
        "latest": latest,
        "update_available": version_tuple(latest) > version_tuple(VERSION),
    }


def emit_update_notice(update: dict[str, Any] | None, output_format: str) -> None:
    """text 输出末尾追加一行升级提示；仅在有新版本且非 JSON 输出时打印。"""
    if output_format == "json" or not update or not update.get("update_available"):
        return
    print(
        f"发现新版本 devkit-flow {update['latest']}（当前 {update['current']}），"
        "可执行 devkit-flow self-update 升级。"
    )


def process_alive(pid: object) -> bool | None:
    """进程是否仍在。True = 存在或无权限，False = 已证明不存在，None = 无法判定。

    POSIX 用 ``os.kill(pid, 0)``。Windows 用 ``OpenProcess`` + ``GetExitCodeProcess``，
    且绝不调用 ``os.kill``（``0`` 在 nt 上是 ``CTRL_C_EVENT``）。导入本模块不会探测进程。
    """
    if type(pid) is not int or pid <= 0:
        return None
    if os.name == "nt":
        return _process_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _process_alive_windows(pid: int) -> bool | None:
    """仅 nt 调用。``use_last_error=True``，句柄 restype 为 ``HANDLE``，避免 64 位截断。"""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            code = ctypes.get_last_error()
            if code == 87:
                return False
            if code == 5:
                return True
            return None
        try:
            status = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(status)):
                return None
            return status.value == 259
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def truncate_keep_tail(
    path: Path | str,
    max_bytes: int,
    keep_bytes: int,
    marker: str,
) -> dict[str, int] | None:
    """原地保留日志尾部。不改名、不换 inode。未超阈值返回 None。

    ``marker`` 里的 ``{dropped}`` 会换成丢弃字节数。失败抛 ``OSError``，由调用方记 PRV-025。
    """
    target = Path(path)
    try:
        size = target.stat().st_size
    except OSError:
        raise
    if not target.is_file() or size <= max_bytes:
        return None
    if type(max_bytes) is not int or type(keep_bytes) is not int or keep_bytes < 0:
        return None
    with exclusive_file_lock(target):
        size = target.stat().st_size
        if size <= max_bytes:
            return None
        keep = min(keep_bytes, size)
        start = size - keep
        with open(target, "r+b") as handle:
            window = min(PR_REVIEW_LOG_ALIGN_BYTES, start)
            if window:
                handle.seek(start - window)
                prefix = handle.read(window)
                newline = prefix.rfind(b"\n")
                if newline >= 0:
                    start = start - window + newline + 1
            handle.seek(start)
            tail = handle.read()
            dropped = start
            text = marker.replace("{dropped}", str(dropped)) if "{dropped}" in marker else marker
            if not text.endswith("\n"):
                text += "\n"
            body = text.encode("utf-8") + tail
            handle.seek(0)
            handle.write(body)
            handle.truncate(len(body))
            handle.flush()
            os.fsync(handle.fileno())
        return {"sizeBefore": size, "sizeAfter": len(body), "kept": len(tail), "dropped": dropped}


# Win32 进程与派生标志。数值写死，避免在 POSIX 上读取 subprocess 没有的属性。
_WIN_DETACHED_PROCESS = 0x00000008
_WIN_CREATE_NEW_PROCESS_GROUP = 0x00000200
_WIN_CREATE_NO_WINDOW = 0x08000000
_WIN_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_INVALID_WIN_HANDLES = frozenset({0, -1, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF})
# FILETIME 从 1601-01-01 UTC 起算，与 Unix epoch 相差的秒数。
_FILETIME_UNIX_DELTA = 11644473600
_PS_PARENT_TIMEOUT_SEC = 2
_PS_LSTART_FORMAT = "%a %b %d %H:%M:%S %Y"


def _invalid_win_handle(handle: object) -> bool:
    if not handle:
        return True
    try:
        value = int(handle)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return True
    return value in _INVALID_WIN_HANDLES


def _parse_ps_ppid_comm(stdout: str) -> tuple[int, str] | None:
    line = ""
    for raw in stdout.splitlines():
        stripped = raw.strip()
        if stripped:
            line = stripped
            break
    if not line:
        return None
    parts = line.split(None, 1)
    if len(parts) != 2:
        return None
    try:
        ppid = int(parts[0])
    except ValueError:
        return None
    name = parts[1].strip()
    if not name:
        return None
    return ppid, name


def _parse_ps_lstart(stdout: str) -> str | None:
    """``ps -o lstart=`` 的本地墙钟，收成不带偏移的 ISO-8601。

    字符串只由 ps 输出决定，同一 pid 两次读取可比，用来识别 pid 复用。
    """
    cleaned = " ".join(stdout.split())
    if not cleaned:
        return None
    try:
        parsed = datetime.strptime(cleaned, _PS_LSTART_FORMAT)
    except ValueError:
        return None
    return parsed.strftime("%Y-%m-%dT%H:%M:%S")


def _ps_ppid_comm(pid: int) -> tuple[int, str] | None:
    try:
        result = run_external(
            ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
            check=False,
            timeout=_PS_PARENT_TIMEOUT_SEC,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return _parse_ps_ppid_comm(result.stdout or "")


def _walk_parent_rows(
    rows: dict[int, tuple[int, str]],
    pid: int,
    depth: int,
) -> list[tuple[int, int, str]]:
    chain: list[tuple[int, int, str]] = []
    current = pid
    seen: set[int] = set()
    for _ in range(depth):
        if current in seen or current not in rows:
            break
        seen.add(current)
        ppid, name = rows[current]
        chain.append((current, ppid, name))
        if ppid <= 0 or ppid == current:
            break
        current = ppid
    return chain


def _parent_pid_chain_posix(pid: int, depth: int) -> list[tuple[int, int, str]]:
    chain: list[tuple[int, int, str]] = []
    current = pid
    seen: set[int] = set()
    for _ in range(depth):
        if current in seen or current <= 0:
            break
        seen.add(current)
        parsed = _ps_ppid_comm(current)
        if parsed is None:
            break
        ppid, name = parsed
        chain.append((current, ppid, name))
        if ppid <= 0 or ppid == current:
            break
        current = ppid
    return chain


def _parent_pid_chain_nt(pid: int, depth: int) -> list[tuple[int, int, str]]:
    """一次进程快照后在内存里向上走。失败时返回已经能解析出的前缀。"""
    rows: dict[int, tuple[int, str]] = {}
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return []
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if _invalid_win_handle(snapshot):
            return []
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                text = str(entry.szExeFile).split("\x00", 1)[0].strip()
                rows[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), text)
                ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
    except Exception:
        return _walk_parent_rows(rows, pid, depth)
    return _walk_parent_rows(rows, pid, depth)


def parent_pid_chain(pid: object, depth: object) -> list[tuple[int, int, str]]:
    """从 ``pid`` 向上的进程链，最多 ``depth`` 项，每项 ``(pid, ppid, name)``。

    POSIX 用 ``ps -o ppid=,comm= -p``（``run_external``，超时 2 秒）。
    nt 用 ``CreateToolhelp32Snapshot`` + ``Process32FirstW`` / ``Process32NextW``。
    中途失败返回已经取得的前缀，不抛出。非法 pid 或 depth 返回空列表。
    """
    if type(pid) is not int or pid <= 0:
        return []
    if type(depth) is not int or depth <= 0:
        return []
    if os.name == "nt":
        return _parent_pid_chain_nt(pid, depth)
    return _parent_pid_chain_posix(pid, depth)


def _filetime_to_iso(high: int, low: int) -> str | None:
    ticks = (int(high) << 32) | (int(low) & 0xFFFFFFFF)
    if ticks <= 0:
        return None
    unix = (ticks // 10_000_000) - _FILETIME_UNIX_DELTA
    if unix < 0:
        return None
    moment = datetime.fromtimestamp(unix, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _process_start_time_kernel(pid: int) -> str | None:
    """macOS ``proc_pidinfo(PROC_PIDTBSDINFO)`` 的 ``pbi_start_tvsec``，本地墙钟、秒精度。

    布局与 xnu ``proc_bsdinfo`` 一致（到 ``pbi_start_tvusec`` 为止 136 字节）。取不到返回 None，
    调用方再退回 ``ps``。不发起子进程。
    """
    if sys.platform != "darwin":
        return None
    try:
        import ctypes

        class _ProcBsdInfo(ctypes.Structure):
            _fields_ = [
                ("pbi_flags", ctypes.c_uint32),
                ("pbi_status", ctypes.c_uint32),
                ("pbi_xstatus", ctypes.c_uint32),
                ("pbi_pid", ctypes.c_uint32),
                ("pbi_ppid", ctypes.c_uint32),
                ("pbi_uid", ctypes.c_uint32),
                ("pbi_gid", ctypes.c_uint32),
                ("pbi_ruid", ctypes.c_uint32),
                ("pbi_rgid", ctypes.c_uint32),
                ("pbi_svuid", ctypes.c_uint32),
                ("pbi_svgid", ctypes.c_uint32),
                ("rfu_1", ctypes.c_uint32),
                ("pbi_comm", ctypes.c_char * 16),
                ("pbi_name", ctypes.c_char * 32),
                ("pbi_nfiles", ctypes.c_uint32),
                ("pbi_pgid", ctypes.c_uint32),
                ("pbi_pjobc", ctypes.c_uint32),
                ("e_tdev", ctypes.c_uint32),
                ("e_tpgid", ctypes.c_uint32),
                ("pbi_nice", ctypes.c_int32),
                ("pbi_start_tvsec", ctypes.c_uint64),
                ("pbi_start_tvusec", ctypes.c_uint64),
            ]

        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        libc.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        libc.proc_pidinfo.restype = ctypes.c_int
        info = _ProcBsdInfo()
        written = libc.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if written < ctypes.sizeof(info):
            return None
        tvsec = int(info.pbi_start_tvsec)
        if tvsec <= 0:
            return None
        return datetime.fromtimestamp(tvsec).strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        return None


def _process_start_time_posix(pid: int) -> str | None:
    kernel = _process_start_time_kernel(pid)
    if kernel is not None:
        return kernel
    try:
        result = run_external(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            check=False,
            timeout=_PS_PARENT_TIMEOUT_SEC,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return _parse_ps_lstart(result.stdout or "")


def _process_start_time_nt(pid: int) -> str | None:
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class FILETIME(ctypes.Structure):
            _fields_ = [
                ("dwLowDateTime", wintypes.DWORD),
                ("dwHighDateTime", wintypes.DWORD),
            ]

        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
        )
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if _invalid_win_handle(handle):
            return None
        try:
            created = FILETIME()
            exited = FILETIME()
            kernel_time = FILETIME()
            user_time = FILETIME()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                return None
            return _filetime_to_iso(int(created.dwHighDateTime), int(created.dwLowDateTime))
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def process_start_time(pid: object) -> str | None:
    """进程启动时间。取不到返回 None，不抛出。

    POSIX：先读内核启动时间（macOS ``proc_pidinfo``），失败再 ``ps -o lstart=``，都收成
    ISO-8601 本地墙钟。nt：``GetProcessTimes`` 收成 UTC ``Z``。
    """
    if type(pid) is not int or pid <= 0:
        return None
    if os.name == "nt":
        return _process_start_time_nt(pid)
    return _process_start_time_posix(pid)


def _spawn_command(argv: object) -> list[str] | None:
    if isinstance(argv, (str, bytes)) or not isinstance(argv, (list, tuple)):
        return None
    if not argv:
        return None
    command: list[str] = []
    for part in argv:
        if part is None:
            return None
        text = str(part)
        if not text:
            return None
        command.append(text)
    return command


def _spawn_detached_windows(command: list[str], log_file: object) -> None:
    """nt：脱离作业、新进程组、无窗口。breakaway 被拒时去掉该标志再试一次。"""
    # 按位或放在函数内：模块级 ``Name | Name`` 会被 3.9 守卫当成类型联合。
    flags = (
        _WIN_DETACHED_PROCESS
        | _WIN_CREATE_NEW_PROCESS_GROUP
        | _WIN_CREATE_NO_WINDOW
        | _WIN_CREATE_BREAKAWAY_FROM_JOB
    )
    shared = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_file,
        "stderr": log_file,
        "close_fds": True,
    }
    try:
        subprocess.Popen(command, creationflags=flags, **shared)
    except OSError:
        subprocess.Popen(command, creationflags=flags & ~_WIN_CREATE_BREAKAWAY_FROM_JOB, **shared)


def spawn_detached(argv: object, log_path: object) -> bool:
    """派生独立进程，stdout / stderr 追加写入日志。成功 True，失败 False，不抛出。

    调用方用返回值把派生失败记入 ``errors[]``。日志父目录不存在视为失败。
    """
    command = _spawn_command(argv)
    if command is None:
        return False
    try:
        log_file = open(os.fspath(log_path), "ab")  # type: ignore[arg-type]
    except (OSError, TypeError, ValueError):
        return False
    try:
        if os.name != "nt":
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
                close_fds=True,
            )
            return True
        _spawn_detached_windows(command, log_file)
        return True
    except Exception:
        return False
    finally:
        log_file.close()


def devkit_flow_argv(*args: object) -> list[str]:
    """重新拉起本 CLI 的 argv。三态 argv0 的唯一实现，``self_command`` 只做包装：

    1. argv0 是已存在的 ``devkit-flow`` / ``cli.py`` / ``__main__.py`` → ``[python, 绝对路径, *args]``
    2. 其余（测试进程、找不到入口文件）→ ``python -c`` 导入 ``devkit_flow.cli``
    """
    arguments = [str(item) for item in args]
    argv0 = str(sys.argv[0] or "")
    candidate = Path(argv0) if argv0 else None
    if (
        candidate is not None
        and candidate.is_file()
        and (candidate.name == "devkit-flow" or candidate.name in {"cli.py", "__main__.py"})
    ):
        return [sys.executable, str(candidate.resolve()), *arguments]
    literal = json.dumps(arguments)
    return [
        sys.executable,
        "-c",
        "import sys; from devkit_flow.cli import main; "
        f"sys.argv = ['devkit-flow', *{literal}]; "
        "raise SystemExit(main())",
    ]
