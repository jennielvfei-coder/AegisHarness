"""AegisHarness — Claude Code self-evolving harness framework.

Flat-layout install: harness/*.py → top-level modules.
Packages: harness/agents/

DuoNews is an independent package (separate repo). Bridge files in harness/
that import from duonews (arxiv_fetch.py, news_*.py, etc.) are optional and
gracefully degrade when duonews is not installed.
"""

from pathlib import Path
from setuptools import setup, find_packages

HARNESS = Path("harness")

# All .py files in harness/ become top-level modules
py_modules = sorted(
    p.stem for p in HARNESS.glob("*.py")
    if p.stem != "__init__"
)

# Sub-packages: harness/agents/ (has __init__.py)
packages = find_packages(where="harness")

setup(
    name="aegis-harness",
    version="0.1.0",
    description=(
        "AegisHarness — Claude Code self-evolving harness framework. "
        "Observer, PreThink, health probes, constraint registry, "
        "and continuous tool-log monitoring."
    ),
    long_description=(Path("README.md").read_text(encoding="utf-8")
                      if Path("README.md").exists() else ""),
    long_description_content_type="text/markdown",
    author="jennielvfei-coder",
    url="https://github.com/jennielvfei-coder/AegisHarness",
    license="MIT",
    python_requires=">=3.11",
    install_requires=[
        "pyyaml>=6.0",
        "numpy>=1.24",
        "pocketflow>=0.0.3",
    ],
    extras_require={
        "dev": ["pytest>=7.0"],
    },
    py_modules=py_modules,
    package_dir={"": "harness"},
    packages=packages,
    package_data={
        "": ["harness_config.yaml"],
    },
    entry_points={
        "console_scripts": [
            "harness=harness_daemon:main",
        ],
    },
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
)
