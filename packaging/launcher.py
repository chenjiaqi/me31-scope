"""打包用入口脚本。

PyInstaller 会把入口脚本当作顶层脚本执行，此时 ``src/me31_scope/__main__.py``
里的 ``from . import main`` 会因为「不在包内」而抛 ImportError。
这里改用绝对导入，配合 spec 里的 ``pathex=["src"]`` 就能正常找到包。

开发时不需要运行本文件，直接 ``uv run me31-scope`` 即可。
"""

from __future__ import annotations

from me31_scope import main

if __name__ == "__main__":
    main()
