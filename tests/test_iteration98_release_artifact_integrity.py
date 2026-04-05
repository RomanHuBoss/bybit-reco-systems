from __future__ import annotations

from pathlib import Path


def test_requirements_dev_is_shipped_with_quality_gate_tooling() -> None:
    """Релизный quality-gate должен быть воспроизводим из чистого checkout.

    README уже рекомендует `pip install -r requirements.txt -r requirements-dev.txt`.
    Если dev-файл отсутствует или в нём нет базовых инструментов проверки,
    поставка становится несогласованной: инструкция есть, а сам артефакт — нет.
    """
    root = Path(__file__).resolve().parent.parent
    req_dev = root / "requirements-dev.txt"

    assert req_dev.exists(), "requirements-dev.txt must be included in the repository"

    content = req_dev.read_text(encoding="utf-8")
    for package_name in ("pytest", "pytest-cov", "ruff"):
        assert package_name in content, f"{package_name} must be pinned in requirements-dev.txt"



def test_readme_release_checks_reference_existing_artifacts() -> None:
    """README не должен обещать release-artifacts, которых нет в поставке."""
    root = Path(__file__).resolve().parent.parent
    readme = (root / "README.md").read_text(encoding="utf-8")

    assert "requirements-dev.txt" in readme
    assert (root / "requirements-dev.txt").is_file()
    assert (root / "docs" / "instrukciya_operatora_bybit_recommender.docx").is_file()
    assert (root / "docs" / "instrukciya_operatora_bybit_recommender.pdf").is_file()
