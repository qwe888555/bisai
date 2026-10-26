"""报名域测试夹具：组织树、各角色账号与登录辅助。"""

from __future__ import annotations

import json

import pytest
from django.contrib.auth.hashers import make_password

from apps.accounts.models import Account, Organization

PASSWORD = "Foot@2026"

ORGANIZATIONS = (
    ("org_prov", None, "省教育厅", "PROVINCE", "PROV"),
    ("org_city_01", "org_prov", "示例市州教育局", "CITY", "CITY01"),
    ("org_city_02", "org_prov", "示例市州二教育局", "CITY", "CITY02"),
    ("org_school_01", "org_city_01", "示例第一中学", "SCHOOL", "SCH001"),
    ("org_school_02", "org_city_02", "示例第二中学", "SCHOOL", "SCH002"),
    ("org_univ_01", "org_prov", "示例大学", "UNIVERSITY", "UNI001"),
    ("org_team_01", "org_school_01", "示例第一中学男子队", "TEAM", "TEAM001"),
    ("org_team_02", "org_school_02", "示例第二中学女子队", "TEAM", "TEAM002"),
)

ACCOUNTS = (
    ("acc_university", "university", ["UNIVERSITY"], "org_school_01"),
    ("acc_university2", "university2", ["UNIVERSITY"], "org_school_02"),
    ("acc_team", "team", ["TEAM"], "org_team_01"),
    ("acc_team2", "team2", ["TEAM"], "org_team_02"),
    ("acc_city", "city", ["CITY"], "org_city_01"),
    ("acc_province", "province", ["PROVINCE_REVIEWER"], "org_prov"),
    ("acc_admin", "admin", ["ADMIN"], "org_prov"),
)


@pytest.fixture
def seeded(db):
    for org_id, parent_id, name, org_type, code in ORGANIZATIONS:
        Organization.objects.create(
            id=org_id,
            parent_id=parent_id,
            name=name,
            org_type=org_type,
            code=code,
            enabled=True,
        )
    for account_id, username, roles, org_id in ACCOUNTS:
        Account.objects.create(
            id=account_id,
            username=username,
            display_name=username,
            roles=roles,
            organization_id=org_id,
            password=make_password(PASSWORD),
            enabled=True,
        )
    return {"password": PASSWORD}


@pytest.fixture
def auth(client, seeded):
    def _login(username: str) -> dict[str, str]:
        response = client.post(
            "/api/v1/auth/login",
            data=json.dumps({"username": username, "password": PASSWORD}),
            content_type="application/json",
        )
        assert response.status_code == 200, response.json()
        token = response.json()["data"]["accessToken"]
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    return _login
