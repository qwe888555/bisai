from django.apps import AppConfig


class EnrollmentsConfig(AppConfig):
    name = "apps.enrollments"
    verbose_name = "报名与审核"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        # 成员3 义务：报名表建好后必须注册 ENROLLMENT 归属校验器，
        # 否则成员2 的 /files 无法把文件挂到报名对象上（上传会 403）。
        from apps.attachments.services import register_owner_verifier

        from .scope import verify_enrollment_owner

        register_owner_verifier("ENROLLMENT", verify_enrollment_owner)
