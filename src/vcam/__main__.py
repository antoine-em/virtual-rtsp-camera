"""Allow ``python -m vcam`` as an entry point (used by service units on some installs)."""

from vcam.cli import main

if __name__ == "__main__":
    main()
