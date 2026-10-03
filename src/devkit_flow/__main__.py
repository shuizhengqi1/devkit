"""源码树直跑入口：``PYTHONPATH=src python -m devkit_flow ...``（设计 §4.4.1 / R7）。

不拼入 bundle：scripts/build_devkit_flow_bundle.py 通过 KNOWN_NON_BUNDLE_MODULES 登记豁免。
"""

from devkit_flow.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
