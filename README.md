# Swing-Trading-System
This repo will be used as fund manager to maximize profit and minimize loss.

## Development

Run the health-checked service locally:

```bash
python -m app
curl http://localhost:8080/health
```

Run its tests:

```bash
python -m unittest discover -s tests -v
```

## Container

```bash
docker build -t swing-trading-system .
docker run --rm -p 8080:8080 swing-trading-system
```

The GitHub Actions workflow calls the reusable Python CI/CD workflows from
[`debarpan-bose-chowdhury/CI-CD`](https://github.com/debarpan-bose-chowdhury/CI-CD).
It runs CI for pull requests and builds, deploys, and scans the container after
pushes to `main`. Configure the `staging` and `production` GitHub environments
before enabling deployments.
