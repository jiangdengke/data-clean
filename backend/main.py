"""Production application entry point."""

from .app import create_app
from .config import AppSettings

app = create_app(AppSettings.from_environment())
