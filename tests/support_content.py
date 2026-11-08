"""Fixed synthetic test inputs, independent of the operator's live content.

The project content directory is editable production data, even when individual
legacy files are tracked by Git. Tests must never copy it into fixtures: private
imports and per-document always_include flags can both leak data and change a
synthetic prompt's budget. Backup/restore tests may still copy their own temporary
snapshots. Feature-specific knowledge tests should author their own documents.
"""
from pathlib import Path


PERSONA = "# 合成测试角色\n\n你是专用于离线回归的原创角色。自然回答当前问题，不虚构管理权限。\n"
BACKGROUND = """---
title: 合成测试背景
category: fiction
keywords: [合成星球]
---

合成星球是这个离线测试原创世界中的一颗虚构星球，与任何真实人物无关。
"""


def seed_content(root: Path) -> Path:
    content = Path(root) / "content"
    content.mkdir(parents=True)
    (content / "persona.md").write_text(PERSONA, encoding="utf-8")
    (content / "lore").mkdir()
    (content / "lore/synthetic.md").write_text(BACKGROUND, encoding="utf-8")
    (content / "assets").mkdir()
    return content
