"""Ticker Meta Data management service."""

import csv
import io
import json
import logging
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = Path(__file__).parent / "data"
DEFAULT_CAP_LIMITS = {
    "LargeCap": 100,
    "MidCap": 100,
    "SmallCap": 100,
}

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}


def default_http_fetch(url: str, headers: dict[str, str] | None = None) -> bytes:
    """Default HTTP fetcher using urllib."""
    req = urllib.request.Request(url, headers=headers or HTTP_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.read()


def parse_date(date_str: str) -> datetime | None:
    """Parse various date string formats into datetime object."""
    date_str = date_str.strip()
    if not date_str:
        return None

    formats = [
        "%d-%b-%Y",
        "%d-%B-%Y",
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None


class TickerMetadataFetcher:
    """Fetches ticker listing info and market cap data from NSE sources."""

    def __init__(
        self,
        data_dir: Path | str = DEFAULT_DATA_DIR,
        http_fetcher: Callable[[str, dict[str, str] | None], bytes] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.http_fetcher: Callable[[str, dict[str, str] | None], bytes] = (
            http_fetcher or default_http_fetch
        )

    def fetch_equity_list(
        self, url: str = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
    ) -> dict[str, dict[str, str]]:
        """Fetch ticker name and listing/inception date from NSE equity list CSV."""
        raw_bytes = self.http_fetcher(url, HTTP_HEADERS)
        text = raw_bytes.decode("utf-8", errors="replace")

        reader = csv.DictReader(io.StringIO(text))
        result: dict[str, dict[str, str]] = {}

        for row in reader:
            cleaned_row = {k.strip(): v.strip() for k, v in row.items() if k and v}
            symbol = cleaned_row.get("SYMBOL") or cleaned_row.get("Symbol")
            if not symbol:
                continue

            name = (
                cleaned_row.get("NAME OF COMPANY")
                or cleaned_row.get("Name of Company")
                or cleaned_row.get("NAME")
                or ""
            )
            listing_date = (
                cleaned_row.get("DATE OF LISTING")
                or cleaned_row.get("Date of Listing")
                or cleaned_row.get("LISTING DATE")
                or ""
            )

            result[symbol] = {
                "name": name,
                "inception_date": listing_date,
            }

        return result

    def fetch_market_cap_data(
        self, api_url: str = "https://www.nseindia.com/api/daily-reports?key=CM"
    ) -> dict[str, float]:
        """Fetch market cap data from NSE daily report Bhavcopy zip file."""
        raw_json = self.http_fetcher(api_url, HTTP_HEADERS)
        reports = json.loads(raw_json.decode("utf-8", errors="replace"))

        report_items: list[Any] = []
        if isinstance(reports, list):
            report_items.extend(reports)  # type: ignore[arg-type]
        elif isinstance(reports, dict):
            for val in reports.values():  # type: ignore[union-attr]
                if isinstance(val, list):
                    report_items.extend(val)  # type: ignore[arg-type]

        target_report: dict[str, Any] | None = None
        for item in report_items:
            if isinstance(item, dict):
                disp = item.get("displayName")  # type: ignore[union-attr]
                key = item.get("fileKey")  # type: ignore[union-attr]
                display_name = str(disp) if isinstance(disp, (str, int, float)) else ""
                file_key = str(key) if isinstance(key, (str, int, float)) else ""
                if "bhavcopy (pr)(zip)" in display_name.lower() or file_key == "CM-BHAVCOPY-PR-ZIP":
                    target_report = item  # type: ignore[assignment]
                    break

        if not target_report:
            raise ValueError("Bhavcopy (PR)(zip) report not found in daily reports JSON.")

        fp = target_report.get("filePath")
        fn = target_report.get("fileActlName")
        file_path = fp.rstrip("/") if isinstance(fp, str) else ""
        file_name = fn if isinstance(fn, str) else ""
        if not file_path or not file_name:
            raise ValueError("Invalid filePath or fileActlName in report metadata.")

        zip_url = f"{file_path}/{file_name}"
        zip_bytes = self.http_fetcher(zip_url, HTTP_HEADERS)

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            mcap_filename = None
            for name in zf.namelist():
                if Path(name).name.lower().startswith("mcap"):
                    mcap_filename = name
                    break

            if not mcap_filename:
                raise ValueError("No mcap file found in Bhavcopy PR zip archive.")

            csv_content = zf.read(mcap_filename).decode("utf-8", errors="replace")

        reader = csv.DictReader(io.StringIO(csv_content))
        market_caps: dict[str, float] = {}

        for row in reader:
            cleaned = {k.strip().lower(): v.strip() for k, v in row.items() if k and v}
            symbol = None
            for key in ("symbol", "ticker", "sec_symbol"):
                if key in cleaned:
                    symbol = cleaned[key]
                    break

            if not symbol:
                continue

            mcap_val = None
            for k_item, v_item in cleaned.items():
                if "market cap" in k_item or "mcap" in k_item or "market_cap" in k_item or "full_mcap" in k_item:
                    try:
                        mcap_val = float(v_item.replace(",", ""))
                        break
                    except ValueError:
                        continue

            if mcap_val is not None:
                market_caps[symbol] = mcap_val

        return market_caps

    def fetch_and_save(self, date_str: str | None = None) -> Path:
        """Fetch metadata, combine equity info & market cap, and save to YYYY-MM-DD.csv."""
        if not date_str:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        equity_list = self.fetch_equity_list()
        market_caps = self.fetch_market_cap_data()

        self.data_dir.mkdir(parents=True, exist_ok=True)
        file_path = self.data_dir / f"{date_str}.csv"

        rows: list[dict[str, Any]] = []
        for symbol, info in equity_list.items():
            mcap = market_caps.get(symbol, 0.0)
            rows.append({
                "Ticker": symbol,
                "Name": info["name"],
                "MarketCap": mcap,
                "InceptionDate": info["inception_date"],
            })

        rows.sort(key=lambda x: float(x.get("MarketCap", 0.0)), reverse=True)

        with open(file_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f, fieldnames=["Ticker", "Name", "MarketCap", "InceptionDate"]
            )
            writer.writeheader()
            writer.writerows(rows)

        return file_path


class TickerMetadataFilter:
    """Filters metadata by inception date and categorizes tickers into cap tiers."""

    def __init__(
        self,
        data_dir: Path | str = DEFAULT_DATA_DIR,
        cap_limits: dict[str, int] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.cap_limits = cap_limits or DEFAULT_CAP_LIMITS

    def filter_by_inception(
        self, rows: list[dict[str, Any]], ref_date_str: str, min_days: int = 365
    ) -> list[dict[str, Any]]:
        """Exclude rows where inception date is less than min_days old relative to ref_date."""
        ref_dt = parse_date(ref_date_str) or datetime.strptime(ref_date_str, "%Y-%m-%d")
        valid_rows: list[dict[str, Any]] = []

        for row in rows:
            inc_str = str(row.get("InceptionDate", ""))
            inc_dt = parse_date(inc_str)
            if inc_dt is None:
                continue

            days_old = (ref_dt - inc_dt).days
            if days_old >= min_days:
                valid_rows.append(row)

        return valid_rows

    def categorize_and_save(
        self, input_file: Path, date_str: str | None = None
    ) -> list[Path]:
        """Read combined CSV, filter, sort, slice by cap tiers, and save per cap tier file."""
        if not date_str:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        rows: list[dict[str, Any]] = []
        with open(input_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                try:
                    mcap = float(r.get("MarketCap", 0.0))
                except ValueError:
                    mcap = 0.0
                rows.append({
                    "Ticker": r.get("Ticker", ""),
                    "Name": r.get("Name", ""),
                    "MarketCap": mcap,
                    "InceptionDate": r.get("InceptionDate", ""),
                })

        filtered_rows = self.filter_by_inception(rows, date_str, min_days=365)
        filtered_rows.sort(key=lambda x: x["MarketCap"], reverse=True)

        created_files: list[Path] = []
        self.data_dir.mkdir(parents=True, exist_ok=True)

        idx = 0
        for cap_name, limit in self.cap_limits.items():
            cap_rows = filtered_rows[idx : idx + limit]
            idx += limit

            cap_file = self.data_dir / f"{cap_name}_{date_str}.csv"
            with open(cap_file, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(
                    f, fieldnames=["Ticker", "Name", "MarketCap", "InceptionDate"]
                )
                writer.writeheader()
                writer.writerows(cap_rows)

            created_files.append(cap_file)

        return created_files


class TickerMetadataCleaner:
    """Deletes older metadata files for each cap tier, keeping only the latest one."""

    def __init__(self, data_dir: Path | str = DEFAULT_DATA_DIR) -> None:
        self.data_dir = Path(data_dir)

    def clean_old_files(
        self, cap_prefixes: list[str] | None = None
    ) -> list[Path]:
        """Keep only the latest file for each cap tier prefix, delete older ones."""
        if not cap_prefixes:
            cap_prefixes = list(DEFAULT_CAP_LIMITS.keys())

        if not self.data_dir.exists():
            return []

        deleted_files: list[Path] = []

        for cap_prefix in cap_prefixes:
            pattern = f"{cap_prefix}_*.csv"
            matching_files = list(self.data_dir.glob(pattern))

            if len(matching_files) <= 1:
                continue

            def get_sort_key(p: Path) -> str:
                prefix_idx = len(cap_prefix) + 1
                return p.stem[prefix_idx:] or str(p.stat().st_mtime)

            matching_files.sort(key=get_sort_key)

            files_to_delete = matching_files[:-1]
            for file_to_delete in files_to_delete:
                try:
                    file_to_delete.unlink()
                    deleted_files.append(file_to_delete)
                except OSError as e:
                    logger.warning("Failed to delete %s: %s", file_to_delete, e)

        return deleted_files


class TickerMetadataNotifier:
    """Checks metadata file existence and marks the system state as stable/healthy."""

    def __init__(self, data_dir: Path | str = DEFAULT_DATA_DIR) -> None:
        self.data_dir = Path(data_dir)

    def check_health(
        self, date_str: str | None = None, cap_prefixes: list[str] | None = None
    ) -> dict[str, Any]:
        """Verify existence of cap files for date_str (or latest) and return health status."""
        if not cap_prefixes:
            cap_prefixes = list(DEFAULT_CAP_LIMITS.keys())

        if not self.data_dir.exists():
            return {
                "healthy": False,
                "missing_files": [f"{cap}_*.csv" for cap in cap_prefixes],
                "existing_files": [],
            }

        existing_files: list[str] = []
        missing_files: list[str] = []

        for cap_prefix in cap_prefixes:
            if date_str:
                expected_filename = f"{cap_prefix}_{date_str}.csv"
                file_path = self.data_dir / expected_filename
                if file_path.exists() and file_path.stat().st_size > 0:
                    existing_files.append(expected_filename)
                else:
                    missing_files.append(expected_filename)
            else:
                matches = [
                    p.name
                    for p in self.data_dir.glob(f"{cap_prefix}_*.csv")
                    if p.stat().st_size > 0
                ]
                if matches:
                    existing_files.extend(matches)
                else:
                    missing_files.append(f"{cap_prefix}_*.csv")

        is_healthy = len(missing_files) == 0

        return {
            "healthy": is_healthy,
            "missing_files": missing_files,
            "existing_files": existing_files,
        }


class TickerMetadataService:
    """Orchestrates fetch, filter, clean, and health notification pipeline."""

    def __init__(
        self,
        data_dir: Path | str = DEFAULT_DATA_DIR,
        cap_limits: dict[str, int] | None = None,
        http_fetcher: Callable[[str, dict[str, str] | None], bytes] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.fetcher = TickerMetadataFetcher(data_dir=self.data_dir, http_fetcher=http_fetcher)
        self.filter = TickerMetadataFilter(data_dir=self.data_dir, cap_limits=cap_limits)
        self.cleaner = TickerMetadataCleaner(data_dir=self.data_dir)
        self.notifier = TickerMetadataNotifier(data_dir=self.data_dir)

    def run_pipeline(self, date_str: str | None = None) -> dict[str, Any]:
        """Run full metadata pipeline: fetch -> filter -> clean -> health check."""
        if not date_str:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        raw_file = self.fetcher.fetch_and_save(date_str=date_str)
        cap_files = self.filter.categorize_and_save(raw_file, date_str=date_str)
        deleted_files = self.cleaner.clean_old_files()
        health = self.notifier.check_health(date_str=date_str)

        return {
            "date": date_str,
            "raw_file": raw_file,
            "cap_files": cap_files,
            "deleted_files": deleted_files,
            "health": health,
        }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logger.info("Starting Ticker Metadata pipeline execution...")
    service = TickerMetadataService()
    result = service.run_pipeline()
    logger.info("Pipeline completed for date: %s", result["date"])
    logger.info("Raw metadata file saved to: %s", result["raw_file"])
    for cap_file in result["cap_files"]:
        logger.info("Categorized file saved to: %s", cap_file)
    if result["deleted_files"]:
        for del_file in result["deleted_files"]:
            logger.info("Cleaned old file: %s", del_file)
    logger.info("Health status: %s", result["health"])


if __name__ == "__main__":
    main()
