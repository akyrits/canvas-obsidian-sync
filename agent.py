import sys

# Windows consoles default to a non-UTF-8 code page, which mangles the
# em-dashes/curly quotes Claude's responses commonly use. Force UTF-8 output
# regardless of platform default.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from agent.cli import main

if __name__ == "__main__":
    sys.exit(main())
