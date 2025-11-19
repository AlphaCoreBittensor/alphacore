"""
Setup configuration for the AlphaCore subnet project.

The package metadata intentionally mirrors the repository naming so that local
installs and downstream tooling recognise the AlphaCore namespace.
"""

from __future__ import annotations

from pathlib import Path
from setuptools import find_packages, setup


def read_requirements(file_path: Path) -> list[str]:
    return [line.strip() for line in file_path.read_text().splitlines() if line.strip()]


ROOT = Path(__file__).parent.resolve()
PACKAGE_NAME = "alphacore"
VERSION_FILE = ROOT / PACKAGE_NAME / "__init__.py"
README = ROOT / "README.md"
REQUIREMENTS = ROOT / "requirements.txt"


def extract_version(init_file: Path) -> str:
    for line in init_file.read_text().splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip("'\"")
    raise RuntimeError("Unable to determine package version.")


setup(
    name="alphacore-subnet",
    version=extract_version(VERSION_FILE),
    description="AlphaCore - Autonomous DevOps Subnet for Bittensor",
    long_description=README.read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    url="https://github.com/atlas-collective/alphacore-subnet",
    author="Alpha Core",
    author_email="",
    license="MIT",
    packages=find_packages(),
    include_package_data=True,
    install_requires=read_requirements(REQUIREMENTS),
    python_requires=">=3.11",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3 :: Only",
        "Programming Language :: Python :: 3.11",
        "Topic :: Scientific/Engineering",
        "Topic :: Software Development :: Build Tools",
    ],
)
