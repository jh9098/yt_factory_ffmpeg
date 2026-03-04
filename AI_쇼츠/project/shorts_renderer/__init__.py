"""shorts_renderer package."""


def main() -> None:
    from .cli import main as cli_main

    cli_main()


__all__ = ["main"]
