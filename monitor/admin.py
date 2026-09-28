from django.contrib import admin

from .models import CustomMetric, DashCard, RequestMetric


@admin.register(RequestMetric)
class RequestMetricAdmin(admin.ModelAdmin):
    list_display = (
        'path', 'method', 'status_code', 'duration_ms',
        'sql_count', 'sql_time_ms', 'slow_query_count', 'cpu_percent', 'created_at',
    )
    list_filter = ('method', 'status_code')
    search_fields = ('path', 'trace_id', 'view_name', 'client_ip')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)
    # spans / slow_queries 是大 JSON 文本，编辑页全量渲染会卡后台，改为只读
    readonly_fields = ('spans', 'slow_queries')


@admin.register(CustomMetric)
class CustomMetricAdmin(admin.ModelAdmin):
    list_display = ('name', 'value', 'created_at')
    search_fields = ('name',)
    date_hierarchy = 'created_at'


@admin.register(DashCard)
class DashCardAdmin(admin.ModelAdmin):
    list_display = ('title', 'metric_key', 'chart_type', 'minutes', 'span', 'order')
    list_editable = ('order',)
