from setuptools import find_packages, setup


setup(
    name="myralis-backend",
    version="0.0.0",
    description="Myralis Backend desktop application",
    packages=find_packages(include=["core", "core.*", "ui", "ui.*"]),
    install_requires=[
        "PySide6>=6.7,<7",
        "openai>=1.90.0",
        "python-dotenv>=1.0.1",
        "requests>=2.32.0",
        "sounddevice>=0.4.6",
        "soundfile>=0.12.1",
        "pydub>=0.25.1",
        "faster-whisper>=1.0.0",
        "websockets>=12.0,<16",
        "psycopg[binary]>=3.2,<4",
    ],
    extras_require={
        "dev": [
            "pytest>=8.0",
            "pyinstaller>=6.10",
        ]
    },
)
