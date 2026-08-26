"""Count the project's real, verifiable metrics.

The spec is emphatic that metrics must never be invented, so every number in
the README comes from this script rather than from memory. Anything that
cannot be measured without a running stack -- latency, throughput, image sizes
-- is deliberately absent; those come from the load test and from Docker.

    python scripts/collect_metrics.py
    python scripts/collect_metrics.py --json
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SERVICES_DIR = REPO / "services"

# FastAPI route decorators, e.g. @router.get("/products")
ROUTE_DECORATOR = re.compile(
    r'@\w+\.(get|post|put|patch|delete|api_route)\s*\(\s*["\']([^"\']*)["\']'
)


def services() -> list[str]:
    return sorted(p.name for p in SERVICES_DIR.iterdir() if (p / "app").is_dir())


def count_endpoints() -> tuple[int, dict[str, int]]:
    """Count declared HTTP routes by scanning the route modules.

    Parsing the source rather than importing the apps keeps this runnable
    without a database, and counts what is actually written down.
    """
    per_service: dict[str, int] = {}
    for service in services():
        total = 0
        for path in (SERVICES_DIR / service / "app").rglob("*.py"):
            total += len(ROUTE_DECORATOR.findall(path.read_text(encoding="utf-8")))
        per_service[service] = total
    return sum(per_service.values()), per_service


def count_tables() -> tuple[int, dict[str, list[str]]]:
    """Find every SQLAlchemy __tablename__ across the services."""
    per_service: dict[str, list[str]] = {}
    for service in services():
        tables: set[str] = set()
        for path in (SERVICES_DIR / service / "app").rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            for match in re.finditer(r'__tablename__\s*=\s*["\'](\w+)["\']', source):
                tables.add(match.group(1))
        if tables:
            per_service[service] = sorted(tables)

    # The shared library contributes processed_events and outbox_events to
    # every consuming service; count the distinct table names once.
    shared = REPO / "libs" / "retailpulse_common" / "retailpulse_common" / "events"
    shared_tables: set[str] = set()
    for path in shared.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r'__tablename__\s*=\s*["\'](\w+)["\']', source):
            shared_tables.add(match.group(1))
    if shared_tables:
        per_service["retailpulse_common (shared)"] = sorted(shared_tables)

    distinct = {t for tables in per_service.values() for t in tables}
    return len(distinct), per_service


def count_topics() -> tuple[int, int]:
    """Read the topic registry rather than counting strings."""
    sys.path.insert(0, str(REPO / "libs" / "retailpulse_common"))
    from retailpulse_common.events.topics import ALL_DLQ_TOPICS, ALL_TOPICS

    return len(ALL_TOPICS), len(ALL_DLQ_TOPICS)


def count_tests() -> tuple[int, dict[str, int]]:
    """Collect (but do not run) every test, per suite."""
    suites = [
        ("common", REPO / "libs" / "retailpulse_common"),
        ("ml", REPO / "ml"),
        *[(s, SERVICES_DIR / s) for s in services() if (SERVICES_DIR / s / "tests").is_dir()],
    ]

    per_suite: dict[str, int] = {}
    python = REPO / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = Path(sys.executable)

    for name, path in suites:
        if not (path / "tests").is_dir():
            continue
        try:
            result = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [str(python), "-m", "pytest", "--collect-only", "-q"],
                cwd=path,
                capture_output=True,
                text=True,
                timeout=300,
            )
            # pytest 8+ with `-q --collect-only` prints one line per file --
            # "tests/test_x.py: 17" -- and no "N tests collected" summary, so
            # the per-file counts are summed rather than scraped from a total.
            per_suite[name] = sum(
                int(m.group(1))
                for m in re.finditer(r"^\S+\.py:\s*(\d+)\s*$", result.stdout, re.MULTILINE)
            )
        except (subprocess.TimeoutExpired, OSError):
            per_suite[name] = 0

    return sum(per_suite.values()), per_suite


def count_lines() -> dict[str, int]:
    """Lines of Python, JS/JSX and YAML, excluding generated and vendored code."""
    counts = {"python": 0, "javascript": 0, "yaml": 0}
    skip = {".venv", "node_modules", "dist", "__pycache__", ".git", "versions"}

    for path in REPO.rglob("*"):
        if not path.is_file() or any(part in skip for part in path.parts):
            continue
        suffix = path.suffix.lower()
        key = (
            "python" if suffix == ".py"
            else "javascript" if suffix in (".js", ".jsx")
            else "yaml" if suffix in (".yml", ".yaml")
            else None
        )
        if key:
            try:
                counts[key] += len(path.read_text(encoding="utf-8").splitlines())
            except (UnicodeDecodeError, OSError):
                pass
    return counts


def model_metrics() -> dict | None:
    """The model's measured accuracy, read from the training report."""
    path = REPO / "ml" / "artifacts" / "metrics_v1.json"
    if not path.exists():
        return None
    report = json.loads(path.read_text(encoding="utf-8"))
    return {
        "model": report["model_name"],
        "version": report["model_version"],
        "mae": round(report["metrics"]["mae"], 3),
        "rmse": round(report["metrics"]["rmse"], 3),
        "r2": round(report["metrics"]["r2"], 4),
        "baseline_mae": round(report["baseline_naive_last_7_days"]["mae"], 3),
        "baseline_rmse": round(report["baseline_naive_last_7_days"]["rmse"], 3),
        "improvement_over_baseline_pct": report["mae_improvement_over_naive_pct"],
        "rows_train": report["rows_train"],
        "rows_test": report["rows_test"],
        "train_period": report["train_period"],
        "test_period": report["test_period"],
    }


def _git_executable() -> str | None:
    """Locate git.

    A freshly-installed git is not on the PATH of an already-open shell, so
    relying on the inherited environment silently reports "no commits".
    """
    import shutil

    found = shutil.which("git")
    if found:
        return found
    for candidate in (
        r"C:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files (x86)\Git\cmd\git.exe",
        "/usr/bin/git",
    ):
        if Path(candidate).exists():
            return candidate
    return None


def count_commits() -> int | None:
    git = _git_executable()
    if git is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603 - resolved path, fixed argv
            [git, "rev-list", "--count", "HEAD"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return int(result.stdout.strip()) if result.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None


def collect() -> dict:
    endpoints_total, endpoints_by_service = count_endpoints()
    tables_total, tables_by_service = count_tables()
    topics, dlq_topics = count_topics()
    tests_total, tests_by_suite = count_tests()

    return {
        "services": len(services()),
        "service_names": services(),
        "rest_endpoints": endpoints_total,
        "endpoints_by_service": endpoints_by_service,
        "database_tables": tables_total,
        "tables_by_service": tables_by_service,
        "kafka_topics": topics,
        "kafka_dlq_topics": dlq_topics,
        "tests": tests_total,
        "tests_by_suite": tests_by_suite,
        "lines_of_code": count_lines(),
        "commits": count_commits(),
        "model": model_metrics(),
        # Explicitly named so a reader knows these are absent by choice rather
        # than by oversight.
        "not_measured_here": [
            "API latency (P50/P95/P99) -- run load-tests/locustfile.py",
            "requests per second -- run load-tests/locustfile.py",
            "docker image sizes -- docker images",
            "test coverage percentage -- pytest --cov",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect verifiable project metrics.")
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    args = parser.parse_args()

    data = collect()

    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    print("=" * 58)
    print("  RetailPulse -- measured project metrics")
    print("=" * 58)
    print(f"  Microservices          {data['services']}")
    print(f"  REST endpoints         {data['rest_endpoints']}")
    print(f"  Database tables        {data['database_tables']}")
    print(f"  Kafka topics           {data['kafka_topics']} (+ {data['kafka_dlq_topics']} dead-letter)")
    print(f"  Automated tests        {data['tests']}")
    print(f"  Commits                {data['commits']}")
    loc = data["lines_of_code"]
    print(f"  Lines of Python        {loc['python']:,}")
    print(f"  Lines of JS/JSX        {loc['javascript']:,}")
    print(f"  Lines of YAML          {loc['yaml']:,}")

    if data["model"]:
        m = data["model"]
        print()
        print(f"  Model                  {m['model']} ({m['version']})")
        print(f"    MAE                  {m['mae']}  (baseline {m['baseline_mae']})")
        print(f"    RMSE                 {m['rmse']}  (baseline {m['baseline_rmse']})")
        print(f"    Improvement          {m['improvement_over_baseline_pct']}% over naive")

    print()
    print("  Tests by suite:")
    for suite, count in sorted(data["tests_by_suite"].items(), key=lambda kv: -kv[1]):
        print(f"    {suite:<24} {count}")

    print()
    print("  Not measured here (need a running stack):")
    for item in data["not_measured_here"]:
        print(f"    - {item}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
