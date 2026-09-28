from django.contrib import admin

from .models import Reply, Topic


@admin.register(Topic)
class TopicAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'category', 'author', 'views', 'created_at')
    list_filter = ('category',)
    search_fields = ('title',)


@admin.register(Reply)
class ReplyAdmin(admin.ModelAdmin):
    list_display = ('id', 'topic', 'author', 'created_at')
