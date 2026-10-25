"""ensure_business_org 类型冲突与事务回滚用例（自 origin/main 6f4ca99 移植）。

原用例：main 的 backend/tests/test_smoke.py::ApiSmokeTests
（提交 6f4ca99"补 ensure_business_org 类型冲突与事务回滚用例，40条冒烟全绿"）。
本分支（team-archive）无 tests 包，故以自足夹具等价移植：不依赖原文件的
login/create_org API 夹具，改用 ORM 直建组织树；断言与原用例一致：
1. 同 code 已被其他业务类型占用 → 409 STATE_CONFLICT；
2. 组织创建失败时，同事务内的业务记录必须一起回滚（无孤立写入）。

运行：$env:DATABASE_URL="sqlite:///:memory:"; python manage.py test tests.test_business_org
"""

from __future__ import annotations

from django.db import transaction
from django.test import TestCase

from apps.accounts.models import Account, Organization
from apps.accounts.services import ensure_business_org
from apps.common.errors import ApiError
from apps.common.ids import new_account_id, new_organization_id


class EnsureBusinessOrgTests(TestCase):
    """两棵最小组织树：省根 + 市子节点（父类型矩阵校验所需）。"""

    @classmethod
    def setUpTestData(cls):
        cls.org_prov = Organization.objects.create(
            id=new_organization_id(), name="移植省", org_type="PROVINCE", code="BIZ_PROV_ROOT"
        )
        cls.org_city = Organization.objects.create(
            id=new_organization_id(),
            name="移植市",
            org_type="CITY",
            code="BIZ_CITY_CHILD",
            parent=cls.org_prov,
        )

    def test_ensure_business_org_type_conflict_and_rollback(self):
        # 同事务先建 UNIVERSITY 节点占用编码
        with transaction.atomic():
            ensure_business_org("UNIVERSITY", "BIZ_CONFLICT_CODE", "冲突高校", self.org_prov.pk)

        # 同编码改挂 SCHOOL（父为 CITY，父类型合法）→ 命中类型冲突 409
        with self.assertRaises(ApiError) as caught:
            with transaction.atomic():
                ensure_business_org("SCHOOL", "BIZ_CONFLICT_CODE", "冲突学校", self.org_city.pk)
        self.assertEqual(caught.exception.code, "STATE_CONFLICT")

        # 组织创建失败（TEAM 挂省根：父类型不合法）时，同事务内的业务记录必须一起回滚
        username = "rollback_case"
        try:
            with transaction.atomic():
                Account.objects.create(
                    id=new_account_id(),
                    username=username,
                    display_name="回滚用例",
                    roles=["COACH"],
                    password="x",
                )
                ensure_business_org("TEAM", "BIZ_ROLLBACK_TEAM", "回滚球队", self.org_prov.pk)
        except ApiError:
            pass
        self.assertFalse(Account.all_objects.filter(username=username).exists())
        self.assertFalse(Organization.all_objects.filter(code="BIZ_ROLLBACK_TEAM").exists())
