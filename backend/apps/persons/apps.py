from django.apps import AppConfig


class PersonsConfig(AppConfig):
    name = "apps.persons"
    label = "persons"
    verbose_name = "人员与基础数据"

    def ready(self) -> None:
        """注册 PERSON 附件归属校验器（T4-04 安全缺口修复）。

        ready() 阶段所有 app 已加载，attachments 不反向依赖本域，无循环
        导入；注册为幂等字典赋值，不做任何数据库查询，对 migrate/check
        等管理命令无副作用。
        """
        from apps.attachments.services import register_owner_verifier

        from . import services

        register_owner_verifier("PERSON", services.verify_person_owner)
