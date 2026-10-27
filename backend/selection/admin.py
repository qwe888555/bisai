from django.contrib import admin

from .models import (
    PublicityRecord,
    ResultBatch,
    ResultItem,
    Score,
    ScoreItem,
    ScoreTemplate,
    TemplateItem,
)


class TemplateItemInline(admin.TabularInline):
    model = TemplateItem
    extra = 0


@admin.register(ScoreTemplate)
class ScoreTemplateAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "group_type",
        "score_round",
        "status",
        "created_at",
    )
    list_filter = ("group_type", "score_round", "status")
    search_fields = ("name",)
    inlines = [TemplateItemInline]


class ScoreItemInline(admin.TabularInline):
    model = ScoreItem
    extra = 0
    readonly_fields = (
        "dimension_code",
        "dimension_name",
        "weight",
        "max_score",
        "score",
        "sort_no",
    )
    can_delete = False


@admin.register(Score)
class ScoreAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "candidate_name",
        "team_name",
        "group_type",
        "score_round",
        "total_score",
        "version",
        "updated_at",
    )
    list_filter = ("group_type", "score_round", "event_id")
    search_fields = ("candidate_name", "team_name", "candidate_id")
    inlines = [ScoreItemInline]


class ResultItemInline(admin.TabularInline):
    model = ResultItem
    extra = 0
    readonly_fields = (
        "rank_no",
        "candidate_id",
        "candidate_name",
        "team_name",
        "player_position",
        "initial_score",
        "final_score",
        "composite_score",
        "detail_json",
    )
    can_delete = False


@admin.register(ResultBatch)
class ResultBatchAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "event_id",
        "group_type",
        "result_round",
        "version_no",
        "quota",
        "active",
        "stale",
        "generated_at",
    )
    list_filter = ("group_type", "result_round", "active", "stale")
    inlines = [ResultItemInline]


@admin.register(PublicityRecord)
class PublicityRecordAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "publicity_type",
        "subject_name",
        "team_name",
        "group_type",
        "city",
        "status",
        "published_at",
    )
    list_filter = ("publicity_type", "status", "group_type", "city", "event_id")
    search_fields = ("subject_name", "team_name", "school_name", "source_id")
    readonly_fields = (
        "source_id",
        "subject_name",
        "team_name",
        "school_name",
        "id_card_masked",
        "profile_json",
        "published_at",
        "created_at",
        "updated_at",
    )


admin.site.register(TemplateItem)
admin.site.register(ScoreItem)
admin.site.register(ResultItem)
