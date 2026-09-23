"""Documentation that gives operational instructions is checked, not trusted.

Each assertion is one inconsistency a review found: a README naming make
targets that do not exist, commands pushing straight to the protected main,
release commits that leave out part of the release record, and unsigned curl
against the IAM-authenticated Function URL.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import readme_block  # noqa: E402

README = (ROOT / "README.md").read_text()
RUNBOOK = (ROOT / "docs" / "runbook.md").read_text()
RELEASE_FILES = (
    "models/champion.json",
    "models/champion_meta.json",
    "models/champion_fixture.csv",
    "docs/promotions.md",
)


def _code(text: str) -> str:
    return "\n".join(re.findall(r"```[a-z]*\n(.*?)```", text, re.S))


def _make_targets() -> set[str]:
    mk = (ROOT / "Makefile").read_text()
    return set(re.findall(r"^([a-z][a-z-]*):", mk, re.M))


def test_every_make_target_the_docs_name_exists() -> None:
    targets = _make_targets()
    for doc, text in (("README.md", README), ("docs/runbook.md", RUNBOOK)):
        for line in _code(text).splitlines():
            for m in re.finditer(r"\bmake ((?:[a-z][a-z-]*\s*)+)", line.split("#")[0]):
                for name in m.group(1).split():
                    assert name in targets, f"{doc}: `make {name}` does not exist"


def test_fresh_machine_block_is_extractable_and_needs_no_aws() -> None:
    block = readme_block.block("fresh-machine", README)
    assert block.startswith("git clone https://github.com/")
    assert "make reproduce SOURCE=public" in block
    assert "dvc pull" not in block and "aws " not in block


def test_no_documented_command_pushes_to_main() -> None:
    for doc in (ROOT / "docs").glob("*.md"):
        for line in _code(doc.read_text()).splitlines():
            for push in re.findall(r"git push[^&|;#]*", line):
                # only the push command itself, not what follows `&&`
                assert "-u origin" in push and not re.search(r"\bmain\b", push), (
                    f"{doc.name}: {push.strip()}"
                )


def test_release_and_rollback_commit_the_whole_release_record() -> None:
    for heading in ("## Release a champion", "## Rollback (model"):
        section = RUNBOOK.split(heading, 1)[1].split("\n## ", 1)[0]
        (add,) = [ln for ln in _code(section).splitlines() if ln.startswith("git add")]
        for f in RELEASE_FILES:
            assert f in add, f"{heading}: `git add` misses {f}"


def test_no_unsigned_curl_against_the_function_url() -> None:
    for doc in [ROOT / "README.md", *(ROOT / "docs").glob("*.md")]:
        for line in doc.read_text().splitlines():
            # a curl *command* aimed at the URL variable, not prose about curl
            if re.search(r"\bcurl\s+(?:-\S+\s+)*[\"']?\$\{?(FUNCTION_)?URL", line):
                raise AssertionError(f"{doc.name}: unsigned curl: {line.strip()}")


def test_readme_status_says_live() -> None:
    assert "Not yet live" not in README and "**Live on AWS**" in README
