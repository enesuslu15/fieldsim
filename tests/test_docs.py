"""Every complete device file shown in the README and the docs must be valid."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from fieldsim.config import parse_config

ROOT = Path(__file__).parent.parent
PAGES = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
BLOCK = re.compile(r"```yaml[^\n]*\n(.*?)```", re.S)


def complete_examples() -> list[tuple[str, str]]:
    found = []
    for page in PAGES:
        for index, block in enumerate(BLOCK.findall(page.read_text(encoding="utf-8"))):
            if block.lstrip().startswith(("version:", "devices:")):
                found.append((f"{page.relative_to(ROOT)}#{index}", block))
    return found


EXAMPLES = complete_examples()


def test_examples_were_found():
    assert len(EXAMPLES) >= 3


@pytest.mark.parametrize(("where", "block"), EXAMPLES, ids=[w for w, _ in EXAMPLES])
def test_documented_device_file_is_valid(where, block):
    parse_config(yaml.safe_load(block), source=where)
