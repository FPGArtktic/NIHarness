# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The wheel, which is the only way into the air-gapped host.

Nothing else in the suite would have caught what this does. Tests run against
the source tree, so a packaging mistake is invisible to them - and one went
unnoticed: a `force-include` of the prompts directory added the same files a
second time at the same path, hatchling refused the build, and the wheel could
not be produced at all. With no wheel there is no wheelhouse, with no
wheelhouse no sandbox image, and with no image the whole isolation story is
unreachable. A failure that large should cost five seconds of test time.
"""

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_LAYERS = 9
"""The prompt layers under `src/niharness/prompts`, excluding `__init__.py`."""


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the wheel once for this module.

    Through the `uv` binary rather than `python -m uv`, which is not importable
    from the project venv. The quick gate runs under `uv run`, so uv is
    present wherever these tests are - a skip here would mean the check was
    absent exactly when it mattered, which is how the regression it catches
    went unnoticed in the first place.
    """
    binary = shutil.which("uv")
    if binary is None:  # pragma: no cover - uv runs the gate
        pytest.skip("uv is not on PATH")
    out = tmp_path_factory.mktemp("wheel")
    done = subprocess.run(
        [binary, "build", "--wheel", "--out-dir", str(out)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    built = sorted(out.glob("*.whl"))
    assert done.returncode == 0, done.stderr
    assert built, done.stderr
    return built[0]


def test_the_wheel_builds_at_all(wheel: Path) -> None:
    """The regression: it did not, and nothing said so."""
    assert wheel.is_file()
    assert wheel.stat().st_size > 10_000


def test_every_prompt_layer_is_in_the_wheel(wheel: Path) -> None:
    """A prompt that does not ship is a role that cannot be assembled.

    This is the guarantee the broken `force-include` was reaching for.
    `packages` already carries them; what was needed was a check, not a
    second copy.
    """
    names = zipfile.ZipFile(wheel).namelist()
    shipped = {name for name in names if "/prompts/" in name and name.endswith(".md")}
    on_disk = {
        f"niharness/prompts/{path.name}"
        for path in (ROOT / "src" / "niharness" / "prompts").glob("*.md")
    }
    assert shipped == on_disk
    assert len(shipped) == EXPECTED_LAYERS


def test_no_file_is_added_to_the_wheel_twice(wheel: Path) -> None:
    """What hatchling refused, stated as the property it was protecting."""
    names = zipfile.ZipFile(wheel).namelist()
    assert len(names) == len(set(names))


def test_the_toolserver_is_importable_from_the_wheel(wheel: Path) -> None:
    """The sandbox runs `python -m niharness.toolserver` out of this wheel."""
    names = zipfile.ZipFile(wheel).namelist()
    assert "niharness/toolserver/__main__.py" in names
    assert "niharness/toolserver/server.py" in names
