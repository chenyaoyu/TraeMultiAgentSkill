from setuptools import find_namespace_packages, setup


setup(
    name="cli-anything-trae",
    version="0.2.0",
    description="CLI-Anything harness for Trae",
    python_requires=">=3.9",
    packages=find_namespace_packages(include=["cli_anything.*"]),
    include_package_data=True,
    package_data={
        "cli_anything.trae.utils": [
            "*.js",
            "bridge_extension/*",
        ]
    },
    install_requires=["click>=8,<9"],
    entry_points={
        "console_scripts": [
            "cli-anything-trae=cli_anything.trae.trae_cli:main",
            "traecli=cli_anything.trae.trae_cli:main",
        ]
    },
)
