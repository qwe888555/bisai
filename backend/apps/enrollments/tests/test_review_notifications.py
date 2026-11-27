"""审核结果站内信：写 notices.Message，学校端 /me/messages 可见。"""

from __future__ import annotations

from apps.enrollments.models import Enrollment
from apps.notices.models import Message

from .factories import BASE, complete_draft, review, submit


def _messages(enrollment_id: str):
    return Message.objects.filter(
        recipient_account_id="acc_university",
        business_type="REVIEW",
        business_id=enrollment_id,
    )


def test_city_reject_creates_review_message(client, auth):
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    submitted = submit(client, school, enrollment_id, "notify-submit")
    assert submitted.status_code == 200, submitted.json()

    city = auth("city")
    rejected = review(
        client,
        city,
        enrollment_id,
        "REJECT",
        "notify-reject",
        reason="材料不完整，请补正",
    )
    assert rejected.status_code == 200, rejected.json()

    assert _messages(enrollment_id).count() == 1
    message = _messages(enrollment_id).get()
    assert "驳回" in message.title
    assert "材料不完整" in message.body
    assert "13800000001" not in message.body

    listing = client.get(f"{BASE}/me/messages?page=1&pageSize=10", **school)
    assert listing.status_code == 200, listing.json()
    titles = [item["title"] for item in listing.json()["data"]["items"]]
    assert any("驳回" in title for title in titles)


def test_city_approve_creates_review_message(client, auth):
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    assert submit(client, school, enrollment_id, "notify-approve-submit").status_code == 200

    city = auth("city")
    approved = review(client, city, enrollment_id, "APPROVE", "notify-approve-city")
    assert approved.status_code == 200, approved.json()

    assert _messages(enrollment_id).count() == 1
    assert "通过" in _messages(enrollment_id).get().title


def test_return_for_correction_creates_message(client, auth):
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    assert submit(client, school, enrollment_id, "notify-return-submit").status_code == 200

    city = auth("city")
    returned = review(
        client,
        city,
        enrollment_id,
        "RETURN_FOR_CORRECTION",
        "notify-return",
        reason="请补充盖章材料",
    )
    assert returned.status_code == 200, returned.json()
    assert _messages(enrollment_id).count() == 1
    assert "补正" in _messages(enrollment_id).get().title


def test_created_by_empty_falls_back_to_school_account(client, auth):
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    assert submit(client, school, enrollment_id, "notify-fallback-submit").status_code == 200
    Enrollment.objects.filter(pk=enrollment_id).update(created_by="")

    city = auth("city")
    rejected = review(
        client,
        city,
        enrollment_id,
        "REJECT",
        "notify-fallback-reject",
        reason="请补正",
    )
    assert rejected.status_code == 200, rejected.json()
    assert _messages(enrollment_id).count() == 1
