"""附件私有访问归属校验自动化测试（SQLite 内存库，不触碰 MySQL）。

运行方式：
    $env:DATABASE_URL="sqlite:///:memory:"; python manage.py test apps.attachments

覆盖（T4-04 安全缺口修复：PERSON/TEAM 归属校验器注册 + can_access 收紧）：
- 资源所有者（账号绑定本人档案）可访问本人 PERSON 附件；
- 无关账号不能跨人员访问私有附件；
- 无关球队账号不能跨球队访问私有附件（一期球队数据范围仅 ADMIN）；
- PROVINCE_REVIEWER/MATCH_RECORDER 特权角色不能仅凭角色跨资源访问；
- ADMIN 全局访问符合 A′ 一期决策；
- 未注册校验器的 ownerType 与归属未确定（空 ownerId）默认拒绝；
- 归属记录不存在（人员/球队不存在或已软删）默认拒绝且无残留；
- 删除他人上传文件的归属校验不可被绕过；上传者本人删除不受影响。

所有用例均走真实 HTTP 调用链（上传/元数据/受控下载/删除），
不直接调用 can_access 等辅助函数。
"""

from __future__ import annotations

import json

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from apps.accounts.models import Account
from apps.attachments.models import FileRecord
from apps.common.authentication import issue_access_token, make_password
from apps.common.ids import new_account_id
from apps.persons.models import Person
from apps.teams.models import Team

FILES = "/api/v1/files"
PDF_BYTES = b"%PDF-1.4\n"


class FileOwnershipApiTests(TestCase):
    """PERSON/TEAM 私有附件归属校验（真实调用链）。"""

    @classmethod
    def setUpTestData(cls):
        # 人员/球队先建（账号绑定需要主键）
        cls.person_a = Person.objects.create(
            person_no="P202600000001",
            full_name="甲员",
            id_card_cipher="CIPHER-A",
            id_card_hash="HASH-A",
            id_card_masked="110***0001",
            person_category="ATHLETE",
        )
        cls.person_b = Person.objects.create(
            person_no="P202600000002",
            full_name="乙员",
            id_card_cipher="CIPHER-B",
            id_card_hash="HASH-B",
            id_card_masked="110***0002",
            person_category="ATHLETE",
        )
        cls.team_a = Team.objects.create(
            team_no="T202600000001", name="甲队", category_code="U12"
        )
        cls.team_b = Team.objects.create(
            team_no="T202600000002", name="乙队", category_code="U12"
        )

        def _account(username, roles, **extra):
            return Account.objects.create(
                id=new_account_id(),
                username=username,
                roles=roles,
                password=make_password("pass-123456"),
                enabled=True,
                **extra,
            )

        cls.admin = _account("files-admin", ["ADMIN"])
        cls.province = _account("files-province", ["PROVINCE_REVIEWER"])
        cls.recorder = _account("files-recorder", ["MATCH_RECORDER"])
        cls.city_user = _account("files-city", ["CITY"])
        # 本人绑定档案：与 /me/profile 同源（account.person_id 服务端签发）
        cls.athlete_a = _account(
            "files-athlete-a", ["ATHLETE"], person_id=str(cls.person_a.pk)
        )
        cls.athlete_b = _account(
            "files-athlete-b", ["ATHLETE"], person_id=str(cls.person_b.pk)
        )
        # TEAM 角色账号：team_ids 指向他队（乙队），访问甲队文件必须被拒
        cls.team_user = _account(
            "files-team", ["TEAM"], team_ids=[cls.team_b.pk]
        )

        cls.admin_token = issue_access_token(cls.admin)[0]
        cls.province_token = issue_access_token(cls.province)[0]
        cls.recorder_token = issue_access_token(cls.recorder)[0]
        cls.city_token = issue_access_token(cls.city_user)[0]
        cls.athlete_a_token = issue_access_token(cls.athlete_a)[0]
        cls.athlete_b_token = issue_access_token(cls.athlete_b)[0]
        cls.team_token = issue_access_token(cls.team_user)[0]

    # ------------------------------------------------------------ 请求工具

    @staticmethod
    def _auth(token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}

    def _upload(self, token, owner_type, owner_id, category="MATERIAL"):
        return self.client.post(
            FILES,
            data={
                "ownerType": owner_type,
                "ownerId": owner_id,
                "category": category,
                "file": SimpleUploadedFile("f.pdf", PDF_BYTES),
            },
            **self._auth(token),
        )

    def _upload_ok(self, token, owner_type, owner_id, category="MATERIAL") -> dict:
        resp = self._upload(token, owner_type, owner_id, category)
        self.assertEqual(resp.status_code, 200, )
        return resp.json()["data"]

    def _meta(self, token, file_id):
        return self.client.get(f"{FILES}/{file_id}", **self._auth(token))

    def _download(self, token, file_id):
        return self.client.get(f"{FILES}/{file_id}/content", **self._auth(token))

    def _delete(self, token, file_id, version):
        return self.client.delete(
            f"{FILES}/{file_id}?version={version}", **self._auth(token)
        )

    def _person_file(self, owner: Person, uploader_token=None) -> dict:
        """ADMIN 上传一个 PERSON 归属文件（默认场景），返回元数据。"""
        return self._upload_ok(uploader_token or self.admin_token, "PERSON", str(owner.pk))

    def _team_file(self, team: Team) -> dict:
        return self._upload_ok(self.admin_token, "TEAM", str(team.pk))

    # ------------------------------------------------------------ 资源所有者

    def test_owner_binding_and_admin_can_access(self):
        """资源所有者（本人绑定档案）可访问；ADMIN 全局访问符合 A′。"""
        meta = self._person_file(self.person_a)
        file_id = meta["id"]

        # 本人绑定档案账号：元数据 + 受控下载均放行，内容字节一致
        resp = self._meta(self.athlete_a_token, file_id)
        self.assertEqual(resp.status_code, 200, )
        self.assertEqual(resp.json()["data"]["id"], file_id)
        resp = self._download(self.athlete_a_token, file_id)
        self.assertEqual(resp.status_code, 200, )
        # FileResponse 为流式响应，无 content 属性；须拼接 streaming_content 比对
        self.assertEqual(b"".join(resp.streaming_content), PDF_BYTES)

        # ADMIN 全局通道（A′ 一期数据范围仅 ADMIN 全量）
        resp = self._download(self.admin_token, file_id)
        self.assertEqual(resp.status_code, 200, )

    def test_unrelated_accounts_cannot_cross_person(self):
        """无关账号不能跨人员访问私有附件（元数据与下载同口径）。"""
        file_id = self._person_file(self.person_a)["id"]
        for token in (self.athlete_b_token, self.city_token):
            resp = self._download(token, file_id)
            self.assertEqual(resp.status_code, 403, )
            self.assertEqual(resp.json()["code"], "FORBIDDEN")
            resp = self._meta(token, file_id)
            self.assertEqual(resp.status_code, 403, )
            self.assertEqual(resp.json()["code"], "FORBIDDEN")

    def test_privileged_roles_cannot_bypass_ownership(self):
        """PROVINCE_REVIEWER/MATCH_RECORDER 不能仅凭角色跨资源访问。"""
        file_id = self._person_file(self.person_a)["id"]
        for token in (self.province_token, self.recorder_token):
            resp = self._download(token, file_id)
            self.assertEqual(resp.status_code, 403, )
            self.assertEqual(resp.json()["code"], "FORBIDDEN")

    # ------------------------------------------------------------ 球队归属

    def test_team_files_scope_admin_only_and_cross_team_denied(self):
        """TEAM 附件一期数据范围仅 ADMIN；无关球队账号访问/上传均被拒。"""
        meta = self._team_file(self.team_a)
        file_id = meta["id"]

        resp = self._download(self.team_token, file_id)
        self.assertEqual(resp.status_code, 403, )
        self.assertEqual(resp.json()["code"], "FORBIDDEN")

        # 上传链同样受归属校验器裁决（team_ids 指向他队不得给甲队传文件）
        resp = self._upload(self.team_token, "TEAM", str(self.team_a.pk))
        self.assertEqual(resp.status_code, 403, )
        self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")

        resp = self._download(self.admin_token, file_id)
        self.assertEqual(resp.status_code, 200, )

    # ------------------------------------------------------------ 默认拒绝

    def test_unregistered_owner_type_deny_by_default(self):
        """未注册校验器的 ownerType：非上传者非 ADMIN 一律拒绝，不默认放行。

        上传链迁移期兜底保留（IDENTITY_CHECK 核验照片仍可由特权角色上传），
        访问链不再给特权角色放行：仅上传者本人与 ADMIN 可读。
        """
        meta = self._upload_ok(
            self.recorder_token, "IDENTITY_CHECK", "IC-0001"
        )
        file_id = meta["id"]

        resp = self._download(self.recorder_token, file_id)
        self.assertEqual(resp.status_code, 200, )
        resp = self._download(self.province_token, file_id)
        self.assertEqual(resp.status_code, 403, )
        resp = self._download(self.admin_token, file_id)
        self.assertEqual(resp.status_code, 200, )

    def test_missing_or_unconfirmed_owner_denied(self):
        """归属未确定（空 ownerId）与归属记录不存在均默认拒绝。"""
        before = FileRecord.objects.count()

        # 先传后绑：空归属可上传（角色兜底），但归属未确定前他人不可读
        meta = self._upload_ok(self.admin_token, "PERSON", "")
        resp = self._download(self.athlete_a_token, meta["id"])
        self.assertEqual(resp.status_code, 403, )

        # 归属记录不存在：上传即 404，且不落库无残留
        resp = self._upload(self.admin_token, "PERSON", "99999999")
        self.assertEqual(resp.status_code, 404, )
        self.assertEqual(resp.json()["code"], "NOT_FOUND")
        self.assertEqual(FileRecord.objects.count(), before + 1)  # 仅空归属 1 个

    def test_upload_ownership_enforced_for_person(self):
        """上传链归属校验：本人可传，他人归属不可传。"""
        resp = self._upload(
            self.athlete_a_token, "PERSON", str(self.person_a.pk)
        )
        self.assertEqual(resp.status_code, 200, )

        resp = self._upload(
            self.athlete_a_token, "PERSON", str(self.person_b.pk)
        )
        self.assertEqual(resp.status_code, 403, )
        self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")

        resp = self._upload(
            self.athlete_b_token, "PERSON", str(self.person_a.pk)
        )
        self.assertEqual(resp.status_code, 403, )

    # ------------------------------------------------------------ 删除

    def test_delete_ownership_not_bypassed(self):
        """删除他人上传文件时归属校验不可绕过；上传者本人删除不受影响。"""
        # 上传者本人删除自己的文件：放行，且软删后不可见
        meta = self._person_file(self.person_a)
        resp = self._delete(
            self.admin_token, meta["id"], meta["version"]
        )
        self.assertEqual(resp.status_code, 200, )
        resp = self._meta(self.admin_token, meta["id"])
        self.assertEqual(resp.status_code, 404, )
        self.assertEqual(resp.json()["code"], "NOT_FOUND")

        # 资源所有者（本人绑定）但非上传者：删除被拦截，文件不受影响
        meta2 = self._person_file(self.person_a)
        resp = self._delete(
            self.athlete_a_token, meta2["id"], meta2["version"]
        )
        self.assertEqual(resp.status_code, 403, )
        self.assertEqual(resp.json()["code"], "FORBIDDEN")
        resp = self._download(self.athlete_a_token, meta2["id"])
        self.assertEqual(resp.status_code, 200, )

        # 无归属权限者先被访问校验拦截
        resp = self._delete(
            self.province_token, meta2["id"], meta2["version"]
        )
        self.assertEqual(resp.status_code, 403, )
