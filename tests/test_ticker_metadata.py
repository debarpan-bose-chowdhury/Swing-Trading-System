import csv
import io
import json
import sys
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ticker_metadata import (
    HTTP_HEADERS,
    TickerMetadataCleaner,
    TickerMetadataFetcher,
    TickerMetadataFilter,
    TickerMetadataNotifier,
    TickerMetadataService,
    default_http_fetch,
    parse_date,
)


class TestTickerMetadataHelpers(unittest.TestCase):
    @patch("urllib.request.urlopen")
    @patch("urllib.request.Request")
    def test_default_http_fetch(self, mock_request: MagicMock, mock_urlopen: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.read.return_value = b"test data"
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        res = default_http_fetch("https://example.com")

        self.assertEqual(res, b"test data")
        mock_request.assert_called_once_with("https://example.com", headers=HTTP_HEADERS)
        mock_urlopen.assert_called_once()

    def test_parse_date_various_formats(self) -> None:
        self.assertIsNone(parse_date(""))
        self.assertIsNone(parse_date("   "))
        self.assertEqual(parse_date("12-SEP-2022"), datetime(2022, 9, 12))
        self.assertEqual(parse_date("12-September-2022"), datetime(2022, 9, 12))
        self.assertEqual(parse_date("2022-09-12"), datetime(2022, 9, 12))
        self.assertEqual(parse_date("12/09/2022"), datetime(2022, 9, 12))
        self.assertEqual(parse_date("12-09-2022"), datetime(2022, 9, 12))
        self.assertIsNone(parse_date("invalid-date"))


class TestTickerMetadataFetcher(unittest.TestCase):
    def test_fetch_equity_list_success(self) -> None:
        csv_data = (
            "SYMBOL,NAME OF COMPANY,DATE OF LISTING\n"
            "TICKER1,Company One,01-JAN-2020\n"
            "TICKER2,Company Two,15-JUN-2021\n"
            ",Empty Symbol,10-10-2020\n"
        ).encode("utf-8")

        mock_fetch = MagicMock(return_value=csv_data)
        fetcher = TickerMetadataFetcher(http_fetcher=mock_fetch)

        result = fetcher.fetch_equity_list()

        self.assertEqual(
            result,
            {
                "TICKER1": {"name": "Company One", "inception_date": "01-JAN-2020"},
                "TICKER2": {"name": "Company Two", "inception_date": "15-JUN-2021"},
            },
        )

    def test_fetch_equity_list_alternate_columns(self) -> None:
        csv_data = (
            "Symbol,Name of Company,Date of Listing\n"
            "TICKER1,Company One,01-JAN-2020\n"
        ).encode("utf-8")

        mock_fetch = MagicMock(return_value=csv_data)
        fetcher = TickerMetadataFetcher(http_fetcher=mock_fetch)

        result = fetcher.fetch_equity_list()
        self.assertEqual(result["TICKER1"]["name"], "Company One")

    def test_fetch_market_cap_data_success(self) -> None:
        reports_json = json.dumps([
            {
                "displayName": "Other Report",
                "fileKey": "OTHER",
            },
            {
                "displayName": "Bhavcopy (PR)(zip)",
                "fileKey": "CM-BHAVCOPY-PR-ZIP",
                "filePath": "https://nsearchives.nseindia.com/archives/equities/bhavcopy/pr",
                "fileActlName": "PR070926.zip",
            }
        ]).encode("utf-8")

        mcap_csv = (
            "SYMBOL,MARKET_CAP\n"
            "TICKER1,\"1,000,000.50\"\n"
            "TICKER2,500000.00\n"
            "TICKER3,INVALID_NUM\n"
            ",100.00\n"
        )

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w") as zf:
            zf.writestr("mcap070926.csv", mcap_csv)
        zip_bytes = zip_buf.getvalue()

        def mock_fetch(url: str, headers: dict[str, str] | None = None) -> bytes:
            if "daily-reports" in url:
                return reports_json
            return zip_bytes

        fetcher = TickerMetadataFetcher(http_fetcher=mock_fetch)
        result = fetcher.fetch_market_cap_data()

        self.assertEqual(result, {"TICKER1": 1000000.50, "TICKER2": 500000.00})

    def test_fetch_market_cap_data_errors(self) -> None:
        # 1. Bhavcopy PR report not in JSON list
        mock_fetch1 = MagicMock(return_value=json.dumps([{"displayName": "Other"}]).encode("utf-8"))
        fetcher1 = TickerMetadataFetcher(http_fetcher=mock_fetch1)
        with self.assertRaisesRegex(ValueError, "Bhavcopy \\(PR\\)\\(zip\\) report not found"):
            fetcher1.fetch_market_cap_data()

        # 1b. JSON is non-list object
        mock_fetch1b = MagicMock(return_value=json.dumps({"error": "not a list"}).encode("utf-8"))
        fetcher1b = TickerMetadataFetcher(http_fetcher=mock_fetch1b)
        with self.assertRaisesRegex(ValueError, "Bhavcopy \\(PR\\)\\(zip\\) report not found"):
            fetcher1b.fetch_market_cap_data()

        # 2. Invalid filePath or fileActlName
        reports_json_invalid = json.dumps([{"displayName": "Bhavcopy (PR)(zip)", "filePath": ""}]).encode("utf-8")
        mock_fetch2 = MagicMock(return_value=reports_json_invalid)
        fetcher2 = TickerMetadataFetcher(http_fetcher=mock_fetch2)
        with self.assertRaisesRegex(ValueError, "Invalid filePath or fileActlName"):
            fetcher2.fetch_market_cap_data()

        # 3. Zip archive lacks mcap file
        reports_json = json.dumps([{
            "displayName": "Bhavcopy (PR)(zip)",
            "filePath": "https://example.com",
            "fileActlName": "PR.zip"
        }]).encode("utf-8")

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w") as zf:
            zf.writestr("other.csv", "a,b\n1,2\n")
        zip_bytes = zip_buf.getvalue()

        def mock_fetch3(url: str, headers: dict[str, str] | None = None) -> bytes:
            if "daily-reports" in url:
                return reports_json
            return zip_bytes

        fetcher3 = TickerMetadataFetcher(http_fetcher=mock_fetch3)
        with self.assertRaisesRegex(ValueError, "No mcap file found in Bhavcopy PR zip archive."):
            fetcher3.fetch_market_cap_data()

    def test_fetch_market_cap_data_dict_format(self) -> None:
        reports_dict_json = json.dumps({
            "PreviousDay": [
                {
                    "displayName": "Bhavcopy (PR)(zip)",
                    "fileKey": "CM-BHAVCOPY-PR-ZIP",
                    "filePath": "https://nsearchives.nseindia.com/archives/equities/bhavcopy/pr",
                    "fileActlName": "PR070926.zip",
                }
            ]
        }).encode("utf-8")

        mcap_csv = "SYMBOL,MARKET_CAP\nTICKER1,100000.00\n"
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w") as zf:
            zf.writestr("mcap070926.csv", mcap_csv)
        zip_bytes = zip_buf.getvalue()

        def mock_fetch(url: str, headers: dict[str, str] | None = None) -> bytes:
            if "daily-reports" in url:
                return reports_dict_json
            return zip_bytes

        fetcher = TickerMetadataFetcher(http_fetcher=mock_fetch)
        result = fetcher.fetch_market_cap_data()

        self.assertEqual(result, {"TICKER1": 100000.00})

    def test_main_execution(self) -> None:
        with patch.object(TickerMetadataService, "run_pipeline") as mock_pipeline:
            mock_pipeline.return_value = {
                "date": "2026-09-12",
                "raw_file": Path("app/data/2026-09-12.csv"),
                "cap_files": [Path("app/data/LargeCap_2026-09-12.csv")],
                "deleted_files": [Path("app/data/LargeCap_2026-01-01.csv")],
                "health": {"healthy": True, "missing_files": [], "existing_files": ["LargeCap_2026-09-12.csv"]},
            }
            from app.ticker_metadata import main
            main()
            mock_pipeline.assert_called_once()
        import tempfile
        tmp_dir = Path(tempfile.mkdtemp())

        fetcher = TickerMetadataFetcher(data_dir=tmp_dir)
        fetcher.fetch_equity_list = MagicMock(return_value={
            "T1": {"name": "Comp 1", "inception_date": "01-JAN-2020"},
            "T2": {"name": "Comp 2", "inception_date": "01-JAN-2021"},
        })
        fetcher.fetch_market_cap_data = MagicMock(return_value={"T1": 1000.0, "T2": 2000.0})

        file_path = fetcher.fetch_and_save("2026-09-12")

        self.assertTrue(file_path.exists())
        self.assertEqual(file_path.name, "2026-09-12.csv")

        with open(file_path, "r", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
            self.assertEqual(len(reader), 2)
            self.assertEqual(reader[0]["Ticker"], "T2")
            self.assertEqual(reader[1]["Ticker"], "T1")

        # Test default date_str=None
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        default_file = fetcher.fetch_and_save()
        self.assertEqual(default_file.name, f"{today_str}.csv")


class TestTickerMetadataFilter(unittest.TestCase):
    def test_filter_by_inception(self) -> None:
        filter_obj = TickerMetadataFilter()
        rows = [
            {"Ticker": "T1", "InceptionDate": "2020-01-01"},
            {"Ticker": "T2", "InceptionDate": "2026-05-01"},
            {"Ticker": "T3", "InceptionDate": "invalid"},
        ]

        valid = filter_obj.filter_by_inception(rows, "2026-09-12", min_days=365)
        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0]["Ticker"], "T1")

    def test_categorize_and_save(self) -> None:
        import tempfile
        tmp_dir = Path(tempfile.mkdtemp())

        input_csv = tmp_dir / "2026-09-12.csv"
        rows = [
            {"Ticker": f"T{i}", "Name": f"Comp {i}", "MarketCap": str((1000 - i) * 10), "InceptionDate": "01-JAN-2020"}
            for i in range(10)
        ]
        rows.append({"Ticker": "T_BAD", "Name": "Bad", "MarketCap": "NOT_A_NUM", "InceptionDate": "01-JAN-2020"})

        with open(input_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["Ticker", "Name", "MarketCap", "InceptionDate"])
            writer.writeheader()
            writer.writerows(rows)

        filter_obj = TickerMetadataFilter(
            data_dir=tmp_dir,
            cap_limits={"LargeCap": 2, "MidCap": 2, "SmallCap": 2},
        )

        created = filter_obj.categorize_and_save(input_csv, "2026-09-12")

        self.assertEqual(len(created), 3)
        large_cap_file = tmp_dir / "LargeCap_2026-09-12.csv"
        self.assertTrue(large_cap_file.exists())

        with open(large_cap_file, "r", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
            self.assertEqual(len(reader), 2)
            self.assertEqual(reader[0]["Ticker"], "T0")

        # Test default date_str=None
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        default_created = filter_obj.categorize_and_save(input_csv)
        self.assertEqual(default_created[0].name, f"LargeCap_{today_str}.csv")


class TestTickerMetadataCleaner(unittest.TestCase):
    def test_clean_old_files(self) -> None:
        import tempfile
        tmp_dir = Path(tempfile.mkdtemp())

        f1 = tmp_dir / "LargeCap_2026-01-01.csv"
        f2 = tmp_dir / "LargeCap_2026-09-12.csv"
        f3 = tmp_dir / "LargeCap_.csv"
        f4 = tmp_dir / "MidCap_2026-09-12.csv"

        f1.write_text("a,b\n1,2\n")
        f2.write_text("a,b\n1,2\n")
        f3.write_text("a,b\n1,2\n")
        f4.write_text("a,b\n1,2\n")

        cleaner = TickerMetadataCleaner(data_dir=tmp_dir)
        deleted = cleaner.clean_old_files()

        self.assertIn(f1, deleted)
        self.assertIn(f3, deleted)
        self.assertFalse(f1.exists())
        self.assertFalse(f3.exists())
        self.assertTrue(f2.exists())
        self.assertTrue(f4.exists())

    def test_clean_old_files_nonexistent_dir(self) -> None:
        cleaner = TickerMetadataCleaner(data_dir=Path("/nonexistent_path_12345"))
        deleted = cleaner.clean_old_files()
        self.assertEqual(deleted, [])

    @patch("app.ticker_metadata.logger")
    @patch.object(Path, "unlink", side_effect=OSError("Permission denied"))
    def test_clean_old_files_os_error(self, mock_unlink: MagicMock, mock_logger: MagicMock) -> None:
        import tempfile
        tmp_dir = Path(tempfile.mkdtemp())

        f1 = tmp_dir / "LargeCap_2026-01-01.csv"
        f2 = tmp_dir / "LargeCap_2026-09-12.csv"
        f1.write_text("data")
        f2.write_text("data")

        cleaner = TickerMetadataCleaner(data_dir=tmp_dir)
        deleted = cleaner.clean_old_files()

        self.assertEqual(deleted, [])
        mock_logger.warning.assert_called_once()


class TestTickerMetadataNotifier(unittest.TestCase):
    def test_check_health_nonexistent_dir(self) -> None:
        notifier = TickerMetadataNotifier(data_dir=Path("/nonexistent_path_12345"))
        res = notifier.check_health()
        self.assertFalse(res["healthy"])
        self.assertEqual(len(res["missing_files"]), 3)

    def test_check_health_success_and_failure(self) -> None:
        import tempfile
        tmp_dir = Path(tempfile.mkdtemp())

        notifier = TickerMetadataNotifier(data_dir=tmp_dir)

        # Before files are created
        res = notifier.check_health("2026-09-12")
        self.assertFalse(res["healthy"])

        # 0-byte file test (exists=True, st_size>0 is False)
        empty_file = tmp_dir / "LargeCap_2026-09-12.csv"
        empty_file.write_text("")
        res_empty_file = notifier.check_health("2026-09-12")
        self.assertFalse(res_empty_file["healthy"])

        # Create valid required files
        (tmp_dir / "LargeCap_2026-09-12.csv").write_text("Ticker,Name\nT1,C1\n")
        (tmp_dir / "MidCap_2026-09-12.csv").write_text("Ticker,Name\nT2,C2\n")
        (tmp_dir / "SmallCap_2026-09-12.csv").write_text("Ticker,Name\nT3,C3\n")

        res_healthy = notifier.check_health("2026-09-12")
        self.assertTrue(res_healthy["healthy"])
        self.assertEqual(len(res_healthy["existing_files"]), 3)

        # Test without date_str when files exist
        res_no_date = notifier.check_health()
        self.assertTrue(res_no_date["healthy"])

        # Test without date_str when files missing for some caps
        tmp_dir_partial = Path(tempfile.mkdtemp())
        (tmp_dir_partial / "LargeCap_2026-09-12.csv").write_text("Ticker,Name\nT1,C1\n")
        notifier_partial = TickerMetadataNotifier(data_dir=tmp_dir_partial)
        res_partial = notifier_partial.check_health()
        self.assertFalse(res_partial["healthy"])


class TestTickerMetadataService(unittest.TestCase):
    def test_run_pipeline(self) -> None:
        import tempfile
        tmp_dir = Path(tempfile.mkdtemp())

        mock_equity = {
            "T1": {"name": "Comp 1", "inception_date": "01-JAN-2020"},
            "T2": {"name": "Comp 2", "inception_date": "01-JAN-2020"},
        }
        mock_mcaps = {"T1": 1000.0, "T2": 2000.0}

        def mock_fetcher(url: str, headers: dict[str, str] | None = None) -> bytes:
            return b""

        service = TickerMetadataService(data_dir=tmp_dir, http_fetcher=mock_fetcher)
        service.fetcher.fetch_equity_list = MagicMock(return_value=mock_equity)
        service.fetcher.fetch_market_cap_data = MagicMock(return_value=mock_mcaps)

        res = service.run_pipeline("2026-09-12")

        self.assertEqual(res["date"], "2026-09-12")
        self.assertTrue(res["health"]["healthy"])
        self.assertTrue(res["raw_file"].exists())
        self.assertEqual(len(res["cap_files"]), 3)

        # Test pipeline with default date_str=None
        res_default = service.run_pipeline()
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.assertEqual(res_default["date"], today_str)


if __name__ == "__main__":
    unittest.main()
