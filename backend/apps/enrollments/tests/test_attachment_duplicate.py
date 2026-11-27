"""报名材料同类型去重（服务层 409，不依赖数据库唯一约束）。"""

from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile

from .factories import BASE, add_player, create_draft, post_json

_file_seq = 0


def upload_file(client, headers, enrollment_id: str, category: str) -> str:
    global _file_seq
    _file_seq += 1
    response = client.post(
        f"{BASE}/files",
        data={
            "ownerType": "ENROLLMENT",
            "ownerId": enrollment_id,
            "category": category,
            "file": SimpleUploadedFile(
                f"{category}-{_file_seq}.pdf",
                b"%PDF-1.4\n% duplicate material test\n",
                content_type="application/pdf",
            ),
        },
        **headers,
    )
    assert response.status_code == 200, response.json()
    return response.json()["data"]["id"]


def attach(
    client,
    headers,
    enrollment_id: str,
    material_type: str,
    file_id: str,
    athlete_id: str = "",
):
    payload = {"materialType": material_type, "fileId": file_id}
    if athlete_id:
        payload["athleteId"] = athlete_id
    return post_json(
        client, f"{BASE}/enrollments/{enrollment_id}/attachments", headers, payload
    )


def test_same_material_type_rejected(client, auth):
    headers = auth("university")
    enrollment = create_draft(client, headers)
    first = attach(
        client,
        headers,
        enrollment["id"],
        "STAMPED_FORM",
        upload_file(client, headers, enrollment["id"], "STAMPED_FORM"),
    )
    assert first.status_code == 200, first.json()

    duplicate = attach(
        client,
        headers,
        enrollment["id"],
        "STAMPED_FORM",
        upload_file(client, headers, enrollment["id"], "STAMPED_FORM"),
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "DUPLICATE_RESOURCE"


def test_same_file_twice_still_rejected(client, auth):
    headers = auth("university")
    enrollment = create_draft(client, headers)
    file_id = upload_file(client, headers, enrollment["id"], "STAMPED_FORM")
    assert attach(client, headers, enrollment["id"], "STAMPED_FORM", file_id).status_code == 200

    again = attach(client, headers, enrollment["id"], "STAMPED_FORM", file_id)
    assert again.status_code == 409
    assert again.json()["code"] == "DUPLICATE_RESOURCE"


def test_delete_then_reupload_same_type_allowed(client, auth):
    headers = auth("university")
    enrollment = create_draft(client, headers)
    created = attach(
        client,
        headers,
        enrollment["id"],
        "STAMPED_FORM",
        upload_file(client, headers, enrollment["id"], "STAMPED_FORM"),
    )
    assert created.status_code == 200
    attachment = created.json()["data"]

    deleted = client.delete(
        f"{BASE}/enrollments/{enrollment['id']}/attachments/{attachment['id']}"
        f"?version={attachment['version']}",
        **headers,
    )
    assert deleted.status_code == 200, deleted.json()

    reuploaded = attach(
        client,
        headers,
        enrollment["id"],
        "STAMPED_FORM",
        upload_file(client, headers, enrollment["id"], "STAMPED_FORM"),
    )
    assert reuploaded.status_code == 200, reuploaded.json()


def test_different_material_types_allowed(client, auth):
    headers = auth("university")
    enrollment = create_draft(client, headers)
    for material_type in ("STAMPED_FORM", "STUDENT_STATUS"):
        response = attach(
            client,
            headers,
            enrollment["id"],
            material_type,
            upload_file(client, headers, enrollment["id"], material_type),
        )
        assert response.status_code == 200, response.json()


def test_same_type_different_athlete_allowed(client, auth):
    headers = auth("university")
    enrollment = create_draft(client, headers)
    add_player(client, headers, enrollment["id"], "per_dup_00", jersey=1, position="GK")
    add_player(client, headers, enrollment["id"], "per_dup_01", jersey=2, position="DF")

    for athlete_id in ("per_dup_00", "per_dup_01"):
        response = attach(
            client,
            headers,
            enrollment["id"],
            "STUDENT_STATUS",
            upload_file(client, headers, enrollment["id"], "STUDENT_STATUS"),
            athlete_id=athlete_id,
        )
        assert response.status_code == 200, response.json()
