"""Concatenate a fiction's Markdown files in publication order (Python 3.11+)."""

import argparse
from datetime import date, datetime, timezone, UTC
from pathlib import Path
import re
import sys
import tomllib


def find_date(content: str) -> datetime | None:
    """Read the leading TOML date; treat dates without a timezone as UTC."""
    opening = re.match(r"\A\ufeff?\+\+\+[^\S\r\n]*\r?\n", content)
    if opening is None:
        return None
    front_matter = content[opening.end():]
    closing = re.search(r"^\+\+\+[^\S\r\n]*\r?$", front_matter, re.MULTILINE)
    if closing is None:
        raise ValueError("unterminated TOML front matter")
    metadata = tomllib.loads(front_matter[:closing.start()])
    value = metadata.get("date")
    if value is None:
        return None
    
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    elif isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, datetime.min.time())
    
    if not isinstance(value, datetime):
        raise TypeError("front matter date must be an ISO date or datetime")
    
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fiction_name", help="directory name under content/fiction")
    args = parser.parse_args()
    fictions_path = Path(__file__).resolve().parent.parent / "content" / "fiction"

    try:
        fictions = {item.name: item for item in fictions_path.iterdir() if item.is_dir()}
        if args.fiction_name not in fictions:
            print(f"Error: unknown fiction name: {args.fiction_name}")
            return

        chapters = []
        for path in fictions[args.fiction_name].glob("*.md"):
            if not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8")
                published = None if path.name == "_index.md" else find_date(content)
            except (OSError, ValueError) as error:
                print(f"{path}: {error}")
                return
            
            key = (
                path.name != "_index.md",
                published is None,
                published or datetime.max.replace(tzinfo=UTC),
                path.name,
            )
            chapters.append((key, content))
    except OSError as error:
        print(str(error))
        return

    for _, content in sorted(chapters):
        print(content)


if __name__ == "__main__":
    main()
