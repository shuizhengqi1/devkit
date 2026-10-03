"""embedded.py —— EMBEDDED_TEMPLATES 统一访问入口（设计 §4.4.1 / §4.5.2 C3 双轨一致性）。

- bundle 形态：scripts/build_devkit_flow_bundle.py 仍把 spec/template/ 注入到产物末尾的
  ``EMBEDDED_TEMPLATES`` 全局（本文件**不得**定义该名字，bundler 校验拒绝）。
- 源码树形态（``PYTHONPATH=src python -m devkit_flow`` / 测试进程）：没有注入全局，
  ``embedded_templates()`` 按 ``TEMPLATE_REGISTRY`` 从 spec/template/ 磁盘加载并缓存。
- 消费方一律 ``from devkit_flow.embedded import embedded_templates`` 后在函数体内调用，
  不再裸引用 ``EMBEDDED_TEMPLATES``；两轨同一函数，行为一致。
- ``TEMPLATE_REGISTRY`` 是 (key, filename, mode) 唯一事实源；bundler 的 RAW/ZLIB 清单由此派生。
"""

from __future__ import annotations

from pathlib import Path

# (EMBEDDED_TEMPLATES key, spec/template/ 下文件名, 注入模式 raw|zlib)。
# 新增模板 = 加模板文件 + 这里加一行；bundler / 守卫测试均从这里读取。
TEMPLATE_REGISTRY: tuple[tuple[str, str, str], ...] = (
    ("STATUS-TEMPLATE", "STATUS-TEMPLATE.md", "raw"),
    ("FEATURE-TEMPLATE", "FEATURE-TEMPLATE.md", "raw"),
    ("DECISIONS-TEMPLATE", "DECISIONS-TEMPLATE.md", "raw"),
    ("dashboard-template", "dashboard-template.html", "raw"),
    ("workflow-visualization-template", "workflow-visualization-template.html", "zlib"),
)

_EMBEDDED_DISK_CACHE: dict[str, str] | None = None


def source_template_dir() -> Path:
    """源码树模板目录：<repo>/spec/template（本文件位于 <repo>/src/devkit_flow/）。"""
    return Path(__file__).resolve().parents[2] / "spec" / "template"


def _load_disk_templates() -> dict[str, str]:
    global _EMBEDDED_DISK_CACHE
    if _EMBEDDED_DISK_CACHE is None:
        base = source_template_dir()
        loaded: dict[str, str] = {}
        for key, filename, _mode in TEMPLATE_REGISTRY:
            path = base / filename
            if not path.is_file():
                raise FileNotFoundError(f"embedded template source missing: {path}")
            loaded[key] = path.read_text(encoding="utf-8")
        _EMBEDDED_DISK_CACHE = loaded
    return _EMBEDDED_DISK_CACHE


def embedded_templates() -> dict[str, str]:
    """统一访问入口：bundle 内返回注入全局，源码树按注册表从磁盘加载（同一函数、两轨一致）。"""
    injected = globals().get("EMBEDDED_TEMPLATES")
    if isinstance(injected, dict):
        return injected
    return _load_disk_templates()


def embedded_template_names() -> list[str]:
    return sorted(embedded_templates())
