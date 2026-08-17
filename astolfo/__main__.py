from .app import run
from .logging_setup import configure


def main() -> None:
    configure()
    run()


if __name__ == "__main__":
    main()
