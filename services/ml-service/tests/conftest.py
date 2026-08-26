"""ML service test fixtures.

A small model is trained once per session from generated data, rather than
loading the committed artifact. That keeps the tests self-contained -- they
pass on a fresh clone with no artifact present -- and means they exercise the
real training-to-serving path end to end.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from pathlib import Path

import pytest

os.environ["JWT_SECRET_KEY"] = "ml-service-test-secret-key-long-enough-hs256"
os.environ["ENVIRONMENT"] = "test"
os.environ["BCRYPT_ROUNDS"] = "4"

ML_ROOT = Path(__file__).resolve().parents[3] / "ml"
sys.path.insert(0, str(ML_ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from generate_dataset import generate  # noqa: E402
from train import train  # noqa: E402

from app.deps import get_forecaster  # noqa: E402
from app.forecaster import DemandForecaster  # noqa: E402
from app.main import app  # noqa: E402
from retailpulse_common.auth import Role, create_access_token  # noqa: E402

TEST_SECRET = "ml-service-test-secret-key-long-enough-hs256"

KNOWN_PRODUCT = "ELE-0001"
KNOWN_STORE = "BLR01"


@pytest.fixture(scope="session")
def sales():
    """Eighteen months for two stores.

    Long enough that the model has observed a full seasonal cycle -- see
    ml/tests/test_train.py, which measures how a model trained on less than a
    year loses to the naive baseline across an unseen festive season. Using a
    short window here would make these tests assert on a model that is not
    representative of the one that ships.
    """
    frame, _catalog = generate(date(2024, 1, 1), 550, seed=11)
    return frame[frame["store_id"].isin(["BLR01", "MAA01"])].reset_index(drop=True)


@pytest.fixture(scope="session")
def forecaster(sales) -> DemandForecaster:
    report = train(sales, model_name="gradient_boosting", test_days=45)
    pipeline = report.pop("_pipeline")
    report.pop("_test_frame")
    report.pop("_predictions")
    return DemandForecaster(pipeline, report, sales)


@pytest.fixture()
def client(forecaster) -> TestClient:
    app.dependency_overrides[get_forecaster] = lambda: forecaster
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _token(role: Role) -> str:
    return create_access_token(
        user_id=uuid.uuid4(),
        email=f"{role.value.lower()}@retailpulse.com",
        role=role,
        secret_key=TEST_SECRET,
        expires_minutes=30,
    )


@pytest.fixture()
def staff_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(Role.WAREHOUSE_OPERATOR)}"}


@pytest.fixture()
def customer_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(Role.CUSTOMER)}"}
