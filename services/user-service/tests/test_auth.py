"""Authentication, authorization and audit tests."""

from __future__ import annotations

import uuid


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def test_register_returns_201_without_leaking_the_hash(client):
    response = client.post(
        "/auth/register",
        json={
            "email": "new@retailpulse.com",
            "password": "a-good-password",
            "full_name": "New User",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["role"] == "CUSTOMER"
    assert body["is_active"] is True
    # The response model must never expose credential material.
    assert "password" not in body
    assert "password_hash" not in body


def test_email_is_normalised_to_lowercase(client):
    response = client.post(
        "/auth/register",
        json={
            "email": "MixedCase@retailpulse.com",
            "password": "a-good-password",
            "full_name": "Mixed Case",
        },
    )
    assert response.json()["email"] == "mixedcase@retailpulse.com"


def test_duplicate_email_returns_409_regardless_of_case(client):
    payload = {
        "email": "dupe@retailpulse.com",
        "password": "a-good-password",
        "full_name": "First",
    }
    assert client.post("/auth/register", json=payload).status_code == 201

    second = client.post(
        "/auth/register", json={**payload, "email": "DUPE@retailpulse.com"}
    )

    assert second.status_code == 409


def test_short_password_is_rejected(client):
    response = client.post(
        "/auth/register",
        json={"email": "short@retailpulse.com", "password": "abc", "full_name": "Short"},
    )
    assert response.status_code == 422


def test_malformed_email_is_rejected(client):
    response = client.post(
        "/auth/register",
        json={"email": "not-an-email", "password": "a-good-password", "full_name": "Bad"},
    )
    assert response.status_code == 422


def test_registration_cannot_self_assign_admin(client):
    """A `role` field in the payload must be ignored, not honoured."""
    response = client.post(
        "/auth/register",
        json={
            "email": "sneaky@retailpulse.com",
            "password": "a-good-password",
            "full_name": "Sneaky",
            "role": "ADMIN",
        },
    )

    assert response.status_code == 201
    assert response.json()["role"] == "CUSTOMER"


def test_password_is_hashed_not_stored_plaintext(client, database):
    from sqlalchemy import select

    from app.models import User

    client.post(
        "/auth/register",
        json={
            "email": "hash@retailpulse.com",
            "password": "plaintext-should-never-appear",
            "full_name": "Hash Check",
        },
    )

    with database.session() as s:
        user = s.scalar(select(User).where(User.email == "hash@retailpulse.com"))
        assert user.password_hash != "plaintext-should-never-appear"
        assert user.password_hash.startswith("$2")
        assert "plaintext-should-never-appear" not in user.password_hash


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
def test_login_returns_a_usable_token(client, customer):
    response = client.get("/users/me", headers=customer["headers"])
    assert response.status_code == 200
    assert response.json()["email"] == "customer@retailpulse.com"


def test_login_response_shape(client):
    client.post(
        "/auth/register",
        json={
            "email": "shape@retailpulse.com",
            "password": "a-good-password",
            "full_name": "Shape",
        },
    )
    body = client.post(
        "/auth/login",
        json={"email": "shape@retailpulse.com", "password": "a-good-password"},
    ).json()

    assert body["token_type"] == "bearer"
    assert body["expires_in_seconds"] > 0
    assert body["user"]["email"] == "shape@retailpulse.com"


def test_wrong_password_returns_401(client, customer):
    response = client.post(
        "/auth/login",
        json={"email": "customer@retailpulse.com", "password": "wrong-password"},
    )
    assert response.status_code == 401


def test_unknown_email_and_wrong_password_are_indistinguishable(client, customer):
    """Same status and same message -- no account enumeration oracle."""
    unknown = client.post(
        "/auth/login",
        json={"email": "nobody@retailpulse.com", "password": "any-password"},
    )
    wrong = client.post(
        "/auth/login",
        json={"email": "customer@retailpulse.com", "password": "wrong-password"},
    )

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["error"]["message"] == wrong.json()["error"]["message"]


def test_login_case_insensitive_on_email(client, customer):
    response = client.post(
        "/auth/login",
        json={"email": "CUSTOMER@retailpulse.com", "password": "customer-pass-123"},
    )
    assert response.status_code == 200


def test_deactivated_account_cannot_log_in(client, admin, customer):
    customer_id = customer["user"]["user_id"]
    client.patch(
        f"/users/{customer_id}/status", json={"is_active": False}, headers=admin["headers"]
    )

    response = client.post(
        "/auth/login",
        json={"email": "customer@retailpulse.com", "password": "customer-pass-123"},
    )

    assert response.status_code == 403


def test_login_records_last_login_at(client, customer):
    assert client.get("/users/me", headers=customer["headers"]).json()["last_login_at"]


# ---------------------------------------------------------------------------
# Token handling
# ---------------------------------------------------------------------------
def test_me_without_a_token_returns_401(client):
    assert client.get("/users/me").status_code == 401


def test_me_with_a_garbage_token_returns_401(client):
    response = client.get("/users/me", headers={"Authorization": "Bearer garbage.token.here"})
    assert response.status_code == 401


def test_token_signed_with_the_wrong_key_is_rejected(client, customer):
    from retailpulse_common.auth import Role, create_access_token

    forged = create_access_token(
        user_id=uuid.uuid4(),
        email="attacker@evil.com",
        role=Role.ADMIN,
        secret_key="attacker-key-that-is-definitely-long-enough",
    )

    response = client.get("/users/me", headers={"Authorization": f"Bearer {forged}"})

    assert response.status_code == 401


def test_role_change_takes_effect_without_reissuing_the_token(client, admin, customer):
    """/users/me reads the database, so revocation is not delayed until expiry."""
    customer_id = customer["user"]["user_id"]

    client.patch(
        f"/users/{customer_id}/role",
        json={"role": "WAREHOUSE_OPERATOR"},
        headers=admin["headers"],
    )

    # Same (now stale) token, but the live record shows the new role.
    body = client.get("/users/me", headers=customer["headers"]).json()
    assert body["role"] == "WAREHOUSE_OPERATOR"


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------
def test_customer_cannot_list_users(client, customer):
    assert client.get("/users", headers=customer["headers"]).status_code == 403


def test_admin_can_list_users(client, admin, customer):
    response = client.get("/users", headers=admin["headers"])
    assert response.status_code == 200
    assert len(response.json()) >= 2


def test_customer_cannot_change_roles(client, customer, admin):
    response = client.patch(
        f"/users/{admin['user']['user_id']}/role",
        json={"role": "CUSTOMER"},
        headers=customer["headers"],
    )
    assert response.status_code == 403


def test_customer_can_read_own_record_but_not_others(client, customer, admin):
    own = client.get(f"/users/{customer['user']['user_id']}", headers=customer["headers"])
    other = client.get(f"/users/{admin['user']['user_id']}", headers=customer["headers"])

    assert own.status_code == 200
    assert other.status_code == 403


def test_admin_can_read_any_record(client, admin, customer):
    response = client.get(f"/users/{customer['user']['user_id']}", headers=admin["headers"])
    assert response.status_code == 200


def test_promoting_to_the_same_role_returns_409(client, admin, customer):
    customer_id = customer["user"]["user_id"]
    client.patch(
        f"/users/{customer_id}/role", json={"role": "ADMIN"}, headers=admin["headers"]
    )

    second = client.patch(
        f"/users/{customer_id}/role", json={"role": "ADMIN"}, headers=admin["headers"]
    )

    assert second.status_code == 409


def test_admin_cannot_deactivate_themselves(client, admin):
    response = client.patch(
        f"/users/{admin['user']['user_id']}/status",
        json={"is_active": False},
        headers=admin["headers"],
    )
    assert response.status_code == 409


def test_unknown_user_returns_404(client, admin):
    response = client.patch(
        f"/users/{uuid.uuid4()}/role", json={"role": "ADMIN"}, headers=admin["headers"]
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------
def test_registration_and_login_are_audited(client, admin, customer):
    actions = [a["action"] for a in client.get("/users/audit-logs", headers=admin["headers"]).json()]
    assert "USER_REGISTERED" in actions
    assert "USER_LOGIN" in actions


def test_failed_login_is_audited_with_a_reason(client, admin, customer):
    client.post(
        "/auth/login",
        json={"email": "customer@retailpulse.com", "password": "wrong-password"},
    )

    logs = client.get("/users/audit-logs", headers=admin["headers"]).json()
    failures = [a for a in logs if a["action"] == "USER_LOGIN_FAILED"]

    assert failures
    assert failures[0]["detail"]["reason"] == "bad_password"


def test_failed_login_audit_never_records_the_password(client, admin, customer):
    client.post(
        "/auth/login",
        json={"email": "customer@retailpulse.com", "password": "super-secret-attempt"},
    )

    logs = client.get("/users/audit-logs", headers=admin["headers"]).json()

    assert "super-secret-attempt" not in str(logs)


def test_role_change_is_audited_with_before_and_after(client, admin, customer):
    client.patch(
        f"/users/{customer['user']['user_id']}/role",
        json={"role": "WAREHOUSE_OPERATOR"},
        headers=admin["headers"],
    )

    logs = client.get("/users/audit-logs", headers=admin["headers"]).json()
    changes = [a for a in logs if a["action"] == "USER_ROLE_CHANGED"]

    assert changes[0]["detail"] == {"from": "CUSTOMER", "to": "WAREHOUSE_OPERATOR"}


def test_audit_logs_require_admin(client, customer):
    assert client.get("/users/audit-logs", headers=customer["headers"]).status_code == 403


# ---------------------------------------------------------------------------
# Platform
# ---------------------------------------------------------------------------
def test_health_and_openapi(client):
    assert client.get("/health").json()["service"] == "user-service"
    paths = client.get("/openapi.json").json()["paths"]
    assert "/auth/register" in paths
    assert "/auth/login" in paths
    assert "/users/me" in paths
