import argparse
import json
import re
import sys
from collections import defaultdict
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import requests

try:
    import pillow_avif
    from PIL import Image
except ImportError:
    print(
        "Missing image-conversion dependencies. Install them with: "
        "py -m pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(1)

GALLERY_URL = "https://playriftbound.com/en-us/card-gallery/"
IMAGE_DIR = Path(__file__).parent.parent / "riftbound-images"
IMAGE_HOST = "cmsassets.rgpub.io"


class NextDataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.reading_next_data = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and dict(attrs).get("id") == "__NEXT_DATA__":
            self.reading_next_data = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.reading_next_data:
            self.reading_next_data = False

    def handle_data(self, data: str) -> None:
        if self.reading_next_data:
            self.parts.append(data)


def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-")


def fetch_gallery() -> tuple[list[dict], list[dict]]:
    response = requests.get(
        GALLERY_URL,
        headers={"User-Agent": "RiftboundCardImages/1.0"},
        timeout=60,
    )
    response.raise_for_status()
    parser = NextDataParser()
    parser.feed(response.text)
    if not parser.parts:
        raise ValueError("The official gallery page did not contain its card data.")

    data = json.loads("".join(parser.parts))
    blades = data.get("props", {}).get("pageProps", {}).get("page", {}).get("blades")
    if not isinstance(blades, list):
        raise ValueError("The official gallery returned an unexpected page structure.")

    gallery = next(
        (blade for blade in blades if isinstance(blade, dict) and blade.get("type") == "riftboundCardGallery"),
        None,
    )
    if gallery is None:
        gallery = next(
            (blade for blade in blades if isinstance(blade, dict) and "cards" in blade and "sets" in blade),
            None,
        )
    if gallery is None:
        raise ValueError("The official gallery page did not contain a card gallery.")

    cards = gallery.get("cards", {}).get("items")
    sets = gallery.get("sets", {}).get("items")
    if not isinstance(cards, list) or not isinstance(sets, list):
        raise ValueError("The official gallery returned an invalid card or set list.")
    if not cards or not sets:
        raise ValueError("The official gallery returned no cards or sets.")
    return cards, sets


def parse_public_code(card: dict) -> str:
    public_code = card.get("publicCode")
    if not isinstance(public_code, str) or not public_code.strip():
        return ""
    match = re.match(r"^[A-Z0-9]+-([^/]+)", public_code.strip(), flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def variant_markers(card: dict) -> tuple[bool, bool]:
    token = parse_public_code(card).lower()
    name = card.get("name", "")
    alt_art = bool(re.search(r"\ba$", token)) or bool(
        isinstance(name, str) and re.search(r"\s*\(alternate art\)$", name, flags=re.IGNORECASE)
    )
    overnumbered = "*" in token or "star" in token.split("-")
    return alt_art, overnumbered


def card_image_url(card: dict) -> str:
    card_name = card.get("name", "Unknown card")
    card_image = card.get("cardImage")
    image_url = card_image.get("url") if isinstance(card_image, dict) else None
    if not isinstance(image_url, str):
        raise ValueError(f"Card '{card_name}' is missing its image URL.")
    parsed_url = urlparse(image_url)
    if parsed_url.scheme != "https" or parsed_url.hostname != IMAGE_HOST:
        raise ValueError(f"Card '{card_name}' has an unsupported image URL.")
    return image_url


def official_card_name(card: dict) -> str:
    card_image = card.get("cardImage")
    accessibility_text = (
        card_image.get("accessibilityText")
        if isinstance(card_image, dict)
        else None
    )
    if not isinstance(accessibility_text, str):
        raise ValueError(f"Card '{card.get('id', 'unknown')}' is missing its official card name.")

    first_line = accessibility_text.splitlines()[0].strip()
    match = re.match(r"^Riftbound[^:]*:\s*(.+)$", first_line)
    if not match:
        raise ValueError(f"Could not read the official card name for '{card.get('id', 'unknown')}'.")
    name = re.split(r"\.\s", match.group(1), maxsplit=1)[0].strip().rstrip(".")
    if not name:
        raise ValueError(f"Could not read the official card name for '{card.get('id', 'unknown')}'.")
    return name


def card_base_name(card: dict, set_code: str) -> str:
    name = re.sub(
        r"\s*\(Alternate Art\)$",
        "",
        official_card_name(card),
        flags=re.IGNORECASE,
    )
    return f"{slugify(name)}-{set_code.lower()}"


def variant_sort_key(card: dict) -> tuple:
    alt_art, overnumbered = variant_markers(card)
    token = parse_public_code(card)
    number_match = re.search(r"\d+", token)
    number = int(number_match.group()) if number_match else float("inf")
    return (
        alt_art or overnumbered,
        not bool(re.match(r"^(?:\d|r\d)", token, flags=re.IGNORECASE)),
        number,
        token.lower(),
        str(card.get("id", "")),
    )


def prepare_downloads(cards: list[dict], set_code: str) -> list[tuple[dict, str, str]]:
    grouped_cards: dict[str, dict[str, dict]] = defaultdict(dict)
    for card in cards:
        if not isinstance(card, dict):
            raise ValueError("The official gallery returned invalid card data.")
        card_set = card.get("set")
        set_value = card_set.get("value") if isinstance(card_set, dict) else None
        card_set_id = set_value.get("id") if isinstance(set_value, dict) else None
        if not isinstance(card_set_id, str) or card_set_id.upper() != set_code:
            continue

        base_name = card_base_name(card, set_code)
        image_url = card_image_url(card)
        image_key = urlparse(image_url)._replace(query="", fragment="").geturl()
        current = grouped_cards[base_name].get(image_key)
        if current is None or variant_sort_key(card) < variant_sort_key(current):
            grouped_cards[base_name][image_key] = card

    if not grouped_cards:
        raise ValueError(f"The official gallery contains no cards for set {set_code}.")

    downloads = []
    for base_name, images in grouped_cards.items():
        unique_cards = sorted(images.items(), key=lambda item: variant_sort_key(item[1]))
        for index, (image_key, card) in enumerate(unique_cards):
            suffix = ""
            if len(unique_cards) > 1 or variant_markers(card) != (False, False):
                alt_art, overnumbered = variant_markers(card)
                if alt_art:
                    suffix += "-a"
                if overnumbered:
                    suffix += "-o"

                if not suffix and index > 0:
                    collector_code = parse_public_code(card)
                    suffix = f"-{slugify(collector_code)}" if collector_code else f"-{index + 1}"

            downloads.append((card, f"{base_name}{suffix}.avif", card_image_url(card)))

    filenames = [filename for _, filename, _ in downloads]
    if len(set(filenames)) != len(filenames):
        raise ValueError("Some gallery cards resolve to the same image filename.")
    return downloads


def download_as_avif(image_url: str, target: Path, session: requests.Session) -> None:
    response = session.get(image_url, timeout=60)
    response.raise_for_status()
    if not response.headers.get("Content-Type", "").lower().startswith("image/"):
        raise ValueError("The image URL did not return an image.")

    with Image.open(BytesIO(response.content)) as source_image:
        image = source_image.convert("RGBA" if "A" in source_image.getbands() else "RGB")
        temporary_path = target.with_name(f".{target.stem}.tmp.avif")
        try:
            image.save(temporary_path, format="AVIF", quality=90)
            temporary_path.replace(target)
        finally:
            temporary_path.unlink(missing_ok=True)


def is_valid_avif(path: Path) -> bool:
    try:
        with Image.open(path) as image:
            if image.format != "AVIF":
                return False
            image.verify()
        return True
    except (OSError, ValueError):
        return False


def choose_set(sets: list[dict], requested_set: str | None) -> tuple[str, str]:
    available_sets = {
        str(item["id"]).upper(): str(item["name"])
        for item in sets
        if isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("name"), str)
    }
    if not available_sets:
        raise ValueError("The official gallery did not provide any valid set names.")

    if requested_set:
        requested = requested_set.strip()
        code = next(
            (
                set_code for set_code, name in available_sets.items()
                if set_code.casefold() == requested.casefold() or name.casefold() == requested.casefold()
            ),
            None,
        )
        if code is None:
            choices = ", ".join(f"{code} ({name})" for code, name in available_sets.items())
            raise ValueError(f"Unknown set '{requested_set}'. Available sets: {choices}")
        return code, available_sets[code]

    choices = list(available_sets.items())
    print("Available sets:")
    for index, (code, name) in enumerate(choices, start=1):
        print(f"  {index}. {name} ({code})")
    while True:
        selection = input("Choose a set by number, name, or code: ").strip()
        if selection.isdigit() and 1 <= int(selection) <= len(choices):
            return choices[int(selection) - 1]
        for code, name in choices:
            if selection.casefold() in {code.casefold(), name.casefold()}:
                return code, name
        print("Please enter a listed number, set name, or set code.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download card images from the official Riftbound gallery as AVIF files."
    )
    parser.add_argument(
        "set",
        nargs="?",
        help="Set name or code (for example: Radiance or RAD). If omitted, choose from the gallery list.",
    )
    parser.add_argument(
        "--list-sets",
        action="store_true",
        help="List the sets currently available in the official gallery and exit.",
    )
    args = parser.parse_args()

    try:
        cards, sets = fetch_gallery()
        if args.list_sets:
            for item in sets:
                if isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("name"), str):
                    print(f"{item['id']}: {item['name']}")
            return 0

        set_code, set_name = choose_set(sets, args.set)
        downloads = prepare_downloads(cards, set_code)
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    except requests.RequestException as error:
        print(f"Could not load the official Riftbound gallery: {error}", file=sys.stderr)
        return 1
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        print(f"Could not prepare gallery images: {error}", file=sys.stderr)
        return 1
    except EOFError:
        print("No set was selected.", file=sys.stderr)
        return 1

    downloaded = 0
    repaired = 0
    skipped = 0
    failed = []
    with requests.Session() as session:
        session.headers.update({"User-Agent": "RiftboundCardImages/1.0"})
        for card, filename, image_url in downloads:
            target = IMAGE_DIR / filename
            existed = target.exists()
            if existed and is_valid_avif(target):
                skipped += 1
                continue
            try:
                download_as_avif(image_url, target, session)
            except (requests.RequestException, OSError, ValueError) as error:
                failed.append(f"{card.get('name', 'Unknown card')}: {error}")
                continue
            if existed:
                repaired += 1
                print(f"Replaced invalid image with AVIF: {filename}")
            else:
                downloaded += 1
                print(f"Downloaded {filename}")

    print(
        f"{set_name} images: {downloaded} downloaded, {repaired} repaired, "
        f"{skipped} already existed, {len(failed)} failed."
    )
    if failed:
        for error in failed:
            print(f"  {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
