"""批量导入、文档生成、Excel 导出与统计口径。"""

from __future__ import annotations

import io
import json
import zipfile

from django.core.files.uploadedfile import SimpleUploadedFile
from openpyxl import Workbook, load_workbook
from PIL import Image
from pypdf import PdfReader

from apps.enrollments import imports
from apps.enrollments.clients import person_client
from apps.enrollments.tests.factories import (
    BASE,
    add_player,
    approve_flow,
    complete_draft,
    create_draft,
    get_detail,
)

WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
CHECK_CODES = ("1", "0", "X", "9", "8", "7", "6", "5", "4", "3", "2")


def make_id(prefix17: str) -> str:
    total = sum(int(prefix17[index]) * WEIGHTS[index] for index in range(17))
    return prefix17 + CHECK_CODES[total % 11]


def roster_workbook(rows) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(list(imports.TEMPLATE_HEADERS))
    for row in rows:
        sheet.append(list(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def post_xlsx(client, url: str, headers: dict, data: bytes, mode: str):
    return client.post(
        url,
        data={
            "mode": mode,
            "file": SimpleUploadedFile(
                "players.xlsx",
                data,
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        },
        **headers,
    )


def test_roster_template_headers(client, auth):
    headers = auth("university")
    response = client.get(f"{BASE}/imports/templates/roster.xlsx", **headers)
    assert response.status_code == 200
    workbook = load_workbook(io.BytesIO(response.content))
    header = tuple(cell.value for cell in workbook.active[1])
    assert header == imports.TEMPLATE_HEADERS


def test_import_players_validate_then_apply(client, auth):
    headers = auth("university")
    enrollment_id = create_draft(client, headers)["id"]
    valid_id = make_id("11010520100101001")
    rows = [
        ("张三", valid_id, "SCH001", "ST001", "男", "2010-05-01", 9, "GK"),
        ("李四", "123456789012345678", "SCH001", "", "男", "2010-05-01", 10, "DF"),
    ]

    version = get_detail(client, headers, enrollment_id)["version"]
    validate = post_xlsx(
        client,
        f"{BASE}/enrollments/{enrollment_id}/imports/players?version={version}",
        headers,
        roster_workbook(rows),
        "VALIDATE_ONLY",
    )
    assert validate.status_code == 200, validate.json()
    report = validate.json()["data"]
    assert report["total"] == 2
    assert report["successCount"] == 1
    assert report["failureCount"] == 1
    assert report["errors"][0]["code"] == "INVALID_ID_NUMBER"
    assert get_detail(client, headers, enrollment_id)["players"] == []

    errors_csv = client.get(
        f"{BASE}/imports/{report['batchId']}/errors.csv", **headers
    )
    assert errors_csv.status_code == 200
    text = errors_csv.content.decode("utf-8-sig")
    assert "INVALID_ID_NUMBER" in text
    assert "123456789012345678" not in text

    version = get_detail(client, headers, enrollment_id)["version"]
    apply = post_xlsx(
        client,
        f"{BASE}/enrollments/{enrollment_id}/imports/players?version={version}",
        headers,
        roster_workbook([rows[0]]),
        "APPLY_VALID_ROWS",
    )
    assert apply.status_code == 200, apply.json()
    assert apply.json()["data"]["successCount"] == 1
    detail = get_detail(client, headers, enrollment_id)
    assert len(detail["players"]) == 1
    assert detail["players"][0]["athleteName"] == "张三"
    assert detail["version"] == version + 1


def test_photo_zip_matching_and_unmatched(client, auth):
    headers = auth("university")
    enrollment_id = create_draft(client, headers)["id"]
    matched_id = make_id("11010520100101003")
    athlete_id = person_client.resolve_person("", matched_id)["id"]
    add_player(client, headers, enrollment_id, athlete_id, jersey=7, position="GK")

    image_buffer = io.BytesIO()
    Image.new("RGB", (16, 16), (200, 10, 10)).save(image_buffer, "JPEG")
    image_bytes = image_buffer.getvalue()

    def zip_with(name: str) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(name, image_bytes)
        return buffer.getvalue()

    version = get_detail(client, headers, enrollment_id)["version"]
    matched = client.post(
        f"{BASE}/enrollments/{enrollment_id}/imports/photos?version={version}",
        data={"file": SimpleUploadedFile("photos.zip", zip_with(f"{matched_id}.jpg"))},
        **headers,
    )
    assert matched.status_code == 200, matched.json()
    report = matched.json()["data"]
    assert report["mode"] == "PHOTO_IMPORT"
    assert report["successCount"] == 1
    detail = get_detail(client, headers, enrollment_id)
    assert detail["players"][0]["photoFileId"]

    unmatched_id = make_id("11010520100101004")
    version = detail["version"]
    unmatched = client.post(
        f"{BASE}/enrollments/{enrollment_id}/imports/photos?version={version}",
        data={"file": SimpleUploadedFile("photos2.zip", zip_with(f"{unmatched_id}.jpg"))},
        **headers,
    )
    assert unmatched.status_code == 200, unmatched.json()
    report = unmatched.json()["data"]
    assert report["failureCount"] == 1
    assert report["errors"][0]["code"] == "PHOTO_UNMATCHED"
    assert matched_id not in str(report["errors"])


def test_documents_bundle_and_excel_are_masked(client, auth):
    school_headers = auth("university")
    city_headers = auth("city")
    province_headers = auth("province")
    enrollment_id = complete_draft(client, school_headers)
    approved = approve_flow(
        client, school_headers, city_headers, province_headers, enrollment_id, "doc"
    )
    assert approved["status"] == "APPROVED"

    pdf = client.get(
        f"{BASE}/enrollments/{enrollment_id}/documents/registration.pdf",
        **school_headers,
    )
    assert pdf.status_code == 200
    assert pdf.content[:4] == b"%PDF"
    assert len(PdfReader(io.BytesIO(pdf.content)).pages) == 1

    docx = client.get(
        f"{BASE}/enrollments/{enrollment_id}/documents/players.docx", **school_headers
    )
    assert docx.status_code == 200
    assert docx.content[:2] == b"PK"

    export_body = {"filters": {"eventId": "event_01"}, "scope": "FILTERED"}
    bundle = client.post(
        f"{BASE}/enrollment-exports/documents.zip",
        data=json.dumps(export_body),
        content_type="application/json",
        **province_headers,
    )
    assert bundle.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(bundle.content)).namelist()
    assert len(names) == 1
    assert names[0].endswith("_报名表.pdf")

    excel = client.post(
        f"{BASE}/enrollment-exports/registrations.xlsx",
        data=json.dumps(export_body),
        content_type="application/json",
        **province_headers,
    )
    assert excel.status_code == 200
    workbook = load_workbook(io.BytesIO(excel.content))
    values = [
        str(cell.value)
        for sheet in workbook.worksheets
        for row in sheet.iter_rows()
        for cell in row
        if cell.value is not None
    ]
    joined = " ".join(values)
    assert "13800000001" not in joined
    assert "138****0001" in joined


def test_statistics_merges_org_team_id_and_team_pk(client, auth, db):
    """球队组织节点 ID 与 Team.pk 指向同一支队时必须合并，且按球队归属地归市州。

    历史数据里学校报送存 Team.pk、球队报送存 team 组织 ID；两条记录的
    city_org_id 还可能写错。统计应以球队主档的市州为准，并把同一支队
    （同组别）只算一次。
    """
    from apps.accounts.models import Organization
    from apps.enrollments.models import STATUS_APPROVED, Enrollment
    from apps.persons.models import Region, School
    from apps.teams.models import Team

    province_headers = auth("province")
    assert Organization.objects.filter(pk="org_team_01", code="TEAM001").exists()

    region = Region.objects.create(name="示例市州", code="CITY01", level=2)
    school = School.objects.create(
        name="示例第一中学", code="SCH001", school_type="SENIOR", region=region
    )
    # 故意让组织 code（TEAM001）与球队编队号不一致，覆盖演示库的真实情况：
    # 只能靠同校球队名把 org_team_01 认成这支队。
    team = Team.objects.create(
        team_no="T202600000017",
        name="示例第一中学男子足球队",
        category_code="U15",
        region=region,
        school=school,
    )

    def add_enrollment(pk: str, team_id: str, group_id: str, submission_no: int):
        Enrollment.objects.create(
            id=pk,
            event_id="event_01",
            group_id=group_id,
            team_id=team_id,
            subject_type="SCHOOL_TEAM",
            status=STATUS_APPROVED,
            submission_no=submission_no,
            contact_name="张老师",
            # 故意写成另一个市州，验证以球队主档归属地为准
            city_org_id="org_city_02",
        )

    # 学校端（Team.pk）与球队端（组织节点）各报一个组别，
    # 同组别再各报一次，验证合并去重。
    add_enrollment("enr_pk_u15", str(team.pk), "group_u15", 2)
    add_enrollment("enr_org_u15", "org_team_01", "group_u15", 1)
    add_enrollment("enr_org_u12", "org_team_01", "group_u12", 1)

    response = client.get(
        f"{BASE}/enrollment-statistics?eventId=event_01&dimension=CITY",
        **province_headers,
    )
    assert response.status_code == 200, response.json()
    rows = response.json()["data"]["items"]
    assert len(rows) == 1
    assert rows[0]["key"] == "org_city_01"
    assert rows[0]["label"] == "示例市州教育局"
    assert rows[0]["teamCount"] == 1
    assert rows[0]["enrollmentCount"] == 2
    assert rows[0]["approvedCount"] == 2

    # 写入侧：组织节点 ID 建单也要落成 Team.pk
    school_headers = auth("university")
    created = create_draft(client, school_headers, team_id="org_team_01", group_id="group_u13")
    assert Enrollment.objects.get(pk=created["id"]).team_id == str(team.pk)


def test_statistics_excludes_draft_and_dedups(client, auth):
    school_headers = auth("university")
    other_headers = auth("university2")
    city_headers = auth("city")
    province_headers = auth("province")

    approved_id = complete_draft(client, school_headers)
    approve_flow(
        client, school_headers, city_headers, province_headers, approved_id, "stat"
    )
    # 另一队的草稿不应计入
    complete_draft(
        client, other_headers, team_id="org_team_02", players=2, materials=False
    )

    response = client.get(
        f"{BASE}/enrollment-statistics?eventId=event_01&dimension=CITY",
        **province_headers,
    )
    assert response.status_code == 200
    rows = response.json()["data"]["items"]
    assert len(rows) == 1
    assert rows[0]["key"] == "org_city_01"
    assert rows[0]["teamCount"] == 1
    assert rows[0]["athleteCount"] == 5
    assert rows[0]["approvedCount"] == 1

    by_group = client.get(
        f"{BASE}/enrollment-statistics?eventId=event_01&dimension=GROUP",
        **province_headers,
    )
    assert by_group.status_code == 200
    assert by_group.json()["data"]["items"][0]["teamCount"] == 1
