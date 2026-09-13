# Swing-Trading-System

A Python 3.12+ service for the swing-trading system. The current implementation
provides an HTTPS health endpoint and an NSE ticker-metadata pipeline.

## Development

### HTTPS health service

The service listens on port `8080` by default and requires both
`TLS_CERTFILE` and `TLS_KEYFILE`. Create or provide a certificate and key, then
start the service:

```bash
set TLS_CERTFILE=certs/cert.pem
set TLS_KEYFILE=certs/key.pem
python -m app
```

On PowerShell, use `$env:TLS_CERTFILE` and `$env:TLS_KEYFILE` instead of `set`.
The port can be changed with `PORT`.

Check the health endpoint over HTTPS:

```bash
curl -k https://localhost:8080/health
```

The endpoint returns `{"status": "ok"}`. Unknown paths return `404`.

### Ticker-metadata pipeline

Run the pipeline directly with:

```bash
python -m app.ticker_metadata
```

The pipeline downloads the NSE equity list and daily market-cap Bhavcopy,
combines them, and writes the following files under `app/data/`:

- `YYYY-MM-DD.csv`: combined ticker, company name, market cap, and inception date data.
- `LargeCap_YYYY-MM-DD.csv`, `MidCap_YYYY-MM-DD.csv`, and `SmallCap_YYYY-MM-DD.csv`:
	the top 100 eligible tickers in each tier by market cap.

Tickers with an inception date less than 365 days before the pipeline date are
excluded. Older tier files are removed so only the latest file for each tier is
retained. The pipeline uses the current UTC date unless a date is supplied when
calling the Python service API directly.

Run its tests:

```bash
python -m unittest discover -s tests -v
```

## Container

The image starts the HTTPS service. Mount a directory containing `cert.pem` and
`key.pem`, and pass their paths into the container:

```bash
docker build -t swing-trading-system .
docker run --rm -p 8080:8080 \
	-e TLS_CERTFILE=/certs/cert.pem \
	-e TLS_KEYFILE=/certs/key.pem \
	-v "${PWD}/certs:/certs:ro" \
	swing-trading-system
```

For PowerShell, `${PWD}` resolves to the current directory. The repository also
includes Compose configurations that mount `./certs` and define a TLS-aware
health check:

```bash
docker compose up
```

Set `APP_PORT` to change the host port, or `IMAGE_REF` to select a different
image. The staging configuration uses the same settings with fewer health-check
retries.

## Project layout

- `app/__main__.py`: HTTPS server and `/health` endpoint.
- `app/ticker_metadata.py`: fetch, filter, categorize, clean, and health-check
	pipeline for ticker metadata.
- `app/data/`: generated and sample CSV metadata files.
- `tests/`: unit tests for the HTTP service and metadata pipeline.

The GitHub Actions workflow calls the reusable Python CI/CD workflows from
[`debarpan-bose-chowdhury/CI-CD`](https://github.com/debarpan-bose-chowdhury/CI-CD).
It runs CI for pull requests and builds, deploys, and scans the container after
pushes to `main`. Configure the `staging` and `production` GitHub environments
before enabling deployments.
