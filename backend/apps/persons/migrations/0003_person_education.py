"""人员主档增加学历列（education）——E1 冻结评审批准（组长 2026-10-27 审阅草案后批示推进）。

依据与决策记录：
- 契约清单 D2：education 无承载列，唯一持久化路径 = 解冻加列走冻结评审
  （备选 "athlete 侧或主档"，选主档：教练同样有学历属性，AthleteProfile
  承载会遗漏 COACH 类档案）；
- 形态 = 可选自由文本（业务未定字典；前端 MyProfileUpdate/PersonCreate/
  PersonUpdate 现有约束均为 max_length=100 自由文本）；
- MySQL 兼容性（**推断，未实测**）：按 MySQL 8.0 Online DDL 规则，加可空
  无默认列通常为 INSTANT/INPLACE、不锁表不回填——该锁行为**待目标库
  版本实测确认**（见 Models 落地核对报告 E 节事项 1a），本迁移不在真实
  MySQL 上执行过；SQLite 开发库直接支持（仅测试环境验证）。

回滚：RemoveField 删除列（会丢失该列数据——仅限尚未录入业务数据时执行；
已录入后回滚需先导出数据，责任在回滚执行人）。

本迁移文件随 E1 落盘，但按批示不连接真实 MySQL、不执行 migrate；
标准部署仍待冻结评审登记完成后由运维执行。
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    # 与 0002 同口径：MySQL DDL 隐式提交，不包事务
    atomic = False

    dependencies = [
        ("persons", "0002_cascade_fks"),
    ]

    operations = [
        migrations.AddField(
            model_name="person",
            name="education",
            field=models.CharField(
                blank=True,
                db_comment="学历(自由文本,可选,E1冻结评审批准加列)",
                null=True,
                max_length=100,
            ),
            preserve_default=False,
        ),
    ]
