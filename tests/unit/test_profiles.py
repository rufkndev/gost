from pathlib import Path

import pytest
import yaml

from app.config import ROOT_DIR
from app.core.profile import ProfileError, load_profile, load_profiles

PROFILES = ROOT_DIR / "app" / "profiles"


def test_all_profiles_load_and_gost_first() -> None:
    profiles = load_profiles(PROFILES)
    assert list(profiles) == ["gost_7_32_2017", "misis"]


def test_owner_decisions_are_in_profiles() -> None:
    p = load_profiles(PROFILES)
    gost, misis = p["gost_7_32_2017"], p["misis"]
    assert gost.body.size_pt == 12
    assert gost.figures.caption.template == "Рисунок {num} — {title}"
    assert misis.title == "НИТУ МИСИС"
    assert misis.figures.caption.template == "Рисунок {num} – {title}"
    assert misis.figures.caption.line_spacing == 1.5
    assert misis.headings.structural.space_after_pt == 24
    assert misis.tables.column_numbers_row == "check"
    assert gost.toc.generate == misis.toc.generate == "auto"


def _copy_profile(tmp_path: Path, mutate: dict[str, object]) -> Path:
    data = yaml.safe_load((PROFILES / "misis" / "profile.yaml").read_text(encoding="utf-8"))
    for dotted, value in mutate.items():
        node = data
        *path, last = dotted.split(".")
        for key in path:
            node = node[key]
        if value is None:
            del node[last]
        else:
            node[last] = value
    target: Path = tmp_path / str(data["id"])
    target.mkdir()
    (target / "profile.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return target


@pytest.mark.parametrize(
    "mutate",
    [
        {"body.size_pt": -1},
        {"body.align": "middle"},
        {"body.colour": "000000"},  # опечатка в ключе
        {"headings.level2.numbering": "{n}"},
        {"figures.caption.template": "Рис. {num}"},
        {"lists.text_indent_cm": None},  # hanging без позиции текста
        {"page": None},
        {"schema_version": 2},
    ],
)
def test_invalid_profile_rejected(tmp_path: Path, mutate: dict[str, object]) -> None:
    with pytest.raises(ProfileError):
        load_profile(_copy_profile(tmp_path, mutate))


def test_id_must_match_folder(tmp_path: Path) -> None:
    folder = _copy_profile(tmp_path, {})
    renamed = folder.rename(tmp_path / "other")
    with pytest.raises(ProfileError, match="does not match"):
        load_profile(renamed)
