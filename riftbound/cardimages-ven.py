import json
import re
import sys
from collections import defaultdict
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

try:
    import pillow_avif
    from PIL import Image
except ImportError:
    print(
        "Missing image-conversion dependencies. Install them with: "
        "python -m pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(1)

API_URL = "https://api.riftcodex.com/cards"
SET_CODE = "VEN"
PAGE_SIZE = 100
IMAGE_DIR = Path(__file__).parent.parent / "riftbound-images"


def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-")


def image_filename(card: dict, collector_suffix: str | None = None) -> str:
    name = card.get("name")
    metadata = card.get("metadata")
    card_set = card.get("set")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("A Vendetta card is missing its name.")
    if not isinstance(metadata, dict) or not isinstance(card_set, dict):
        raise ValueError(f"Card '{name}' is missing set or variant metadata.")
    set_id = card_set.get("set_id")
    if not isinstance(set_id, str) or set_id.upper() != SET_CODE:
        raise ValueError(f"Card '{name}' does not belong to the Vendetta set.")

    alt_art = metadata.get("alternate_art", False)
    overnumbered = metadata.get("overnumbered", False)
    if not isinstance(alt_art, bool) or not isinstance(overnumbered, bool):
        raise ValueError(f"Card '{name}' has invalid alternate-art metadata.")

    name = re.sub(r"\s*\(Alternate Art\)$", "", name, flags=re.IGNORECASE)
    riftbound_id = card.get("riftbound_id", "")
    if isinstance(riftbound_id, str) and re.match(r"^ven-\d+a(?:-|$)", riftbound_id, re.IGNORECASE):
        alt_art = True

    filename = f"{slugify(name)}-{SET_CODE.lower()}"
    if collector_suffix:
        filename += f"-{slugify(collector_suffix)}"
    if alt_art:
        filename += "-a"
    if overnumbered:
        filename += "-o"
    return f"{filename}.avif"


def image_url_for(card: dict) -> str:
    name = card.get("name", "Unknown card")
    media = card.get("media")
    image_url = media.get("image_url") if isinstance(media, dict) else None
    if not isinstance(image_url, str):
        raise ValueError(f"Card '{name}' is missing its image URL.")
    parsed_url = urlparse(image_url)
    if parsed_url.scheme != "https" or parsed_url.hostname != "cmsassets.rgpub.io":
        raise ValueError(f"Card '{name}' has an unsupported image URL.")
    return image_url


def prepare_image_downloads(cards: list[dict]) -> list[tuple[dict, str, str]]:
    grouped_cards = defaultdict(dict)
    for card in cards:
        base_filename = image_filename(card)
        image_url = image_url_for(card)
        grouped_cards[base_filename].setdefault(image_url, card)

    downloads = []
    for base_filename, images in grouped_cards.items():
        unique_images = sorted(
            images.items(),
            key=lambda item: (
                item[1].get("collector_number", float("inf")),
                item[1].get("riftbound_id", ""),
            ),
        )
        for index, (image_url, card) in enumerate(unique_images):
            if index == 0:
                filename = base_filename
            else:
                riftbound_id = card.get("riftbound_id", "")
                parts = riftbound_id.split("-") if isinstance(riftbound_id, str) else []
                suffix = parts[1] if len(parts) > 1 else str(card.get("collector_number", ""))
                filename = image_filename(card, suffix)
            downloads.append((card, filename, image_url))

    filenames = [filename for _, filename, _ in downloads]
    if len(set(filenames)) != len(filenames):
        raise ValueError("Vendetta cards still have conflicting image filenames after variant suffixes.")
    return downloads


def api_get_page(page: int) -> dict:
    query = urlencode({
        "set_id": SET_CODE.lower(),
        "size": PAGE_SIZE,
        "page": page,
        "sort": "collector_number",
        "dir": 1,
    })
    request = Request(API_URL + "?" + query, headers={"User-Agent": "RiftboundCardImages/1.0"})
    with urlopen(request, timeout=60) as response:
        if response.headers.get_content_type() != "application/json":
            raise ValueError(f"Riftcodex returned {response.headers.get_content_type()} instead of JSON.")
        return json.load(response)


def fetch_ven_cards() -> list[dict]:
    cards = []
    expected_total = None
    expected_pages = None
    seen_ids = set()

    page = 1
    while expected_pages is None or page <= expected_pages:
        result = api_get_page(page)
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise ValueError(f"Unexpected API response on page {page}.")

        total = result.get("total")
        pages = result.get("pages")
        if not isinstance(total, int) or not isinstance(pages, int) or pages < 1:
            raise ValueError(f"Missing or invalid pagination details on page {page}.")
        if expected_total is None:
            expected_total = total
            expected_pages = pages
        elif total != expected_total or pages != expected_pages:
            raise ValueError("The set changed while pages were being fetched; run the downloader again.")

        items = result["items"]
        if not items:
            raise ValueError(f"The API returned an empty page before all cards were fetched (page {page}).")
        for card in items:
            if not isinstance(card, dict):
                raise ValueError(f"Unexpected card data on page {page}.")
            card_id = card.get("id")
            if not isinstance(card_id, str) or not card_id:
                raise ValueError(f"A card on page {page} is missing its API ID.")
            if card_id in seen_ids:
                raise ValueError(f"The API returned duplicate card ID {card_id}.")
            seen_ids.add(card_id)
            cards.append(card)
        page += 1

    if len(cards) != expected_total:
        raise ValueError(f"Expected {expected_total} cards, but fetched {len(cards)}.")
    return cards


def download_as_avif(image_url: str, target: Path) -> None:
    parsed_url = urlparse(image_url)
    if parsed_url.scheme != "https" or parsed_url.hostname != "cmsassets.rgpub.io":
        raise ValueError(f"Unsupported image URL: {image_url}")

    request = Request(image_url, headers={"User-Agent": "RiftboundCardImages/1.0"})
    with urlopen(request, timeout=60) as response:
        if not response.headers.get_content_type().startswith("image/"):
            raise ValueError(f"Image URL returned {response.headers.get_content_type()} instead of an image.")
        image_bytes = response.read()

    with Image.open(BytesIO(image_bytes)) as source_image:
        image = source_image.convert("RGBA" if "A" in source_image.getbands() else "RGB")
        temporary_path = target.with_name(f".{target.stem}.tmp.avif")
        try:
            image.save(temporary_path, format="AVIF", quality=90)
            temporary_path.replace(target)
        finally:
            temporary_path.unlink(missing_ok=True)


def main() -> int:
    try:
        cards = fetch_ven_cards()
        downloads = prepare_image_downloads(cards)
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    except HTTPError as error:
        if error.code == 403:
            print(
                "Vendetta image download failed: Riftcodex denied the request (HTTP 403). "
                "The API may require browser verification; this script does not bypass that protection.",
                file=sys.stderr,
            )
        else:
            print(f"Vendetta image download failed: {error}", file=sys.stderr)
        return 1
    except (URLError, TimeoutError, json.JSONDecodeError, ValueError, OSError) as error:
        print(f"Vendetta image download failed: {error}", file=sys.stderr)
        return 1

    downloaded = 0
    skipped = 0
    failed = []
    for card, filename, image_url in downloads:
        target = IMAGE_DIR / filename
        if target.exists():
            skipped += 1
            continue

        try:
            download_as_avif(image_url, target)
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as error:
            failed.append(f"{card.get('name', 'Unknown card')}: {error}")
            continue
        downloaded += 1
        print(f"Downloaded {filename}")

    print(f"Vendetta image files: {downloaded} downloaded, {skipped} already existed, {len(failed)} failed.")
    if failed:
        for error in failed:
            print(f"  {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
