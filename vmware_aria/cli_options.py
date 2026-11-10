"""Shared CLI option annotations for command registration."""

from pathlib import Path
from typing import Annotated
import typer

TargetOption = Annotated[str | None, typer.Option("--target", "-t", help="Target name from config")]

ConfigOption = Annotated[Path | None, typer.Option("--config", "-c", help="Config file path")]
