from setuptools import setup

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="docassemblecli3",
    version="0.0.1",
    author="Jack Adamson",
    author_email="jackadamson@gmail.com",
    description="CLI utilities for using Docassemble requring Python 3",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/jpagh/docassemblecli3",
    project_urls={
        "Bug Tracker": "https://github.com/jpagh/docassemblecli3/issues",
    },
    classifiers=[
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.8",
    install_requires=[
        "click",
        "packaging",
        "pyyaml",
        "requests",
        "watchdog"
    ],
    py_modules=["docassemblecli3"],
    entry_points={
        'console_scripts': [
            'docassemblecli3 = docassemblecli3:cli',
            'da = docassemblecli3:cli',
        ],
    },
)
