from django.contrib import admin

from .models import Instance


@admin.register(Instance)
class InstanceAdmin(admin.ModelAdmin):
    list_display = ["name", "user", "image", "created_at"]
    list_filter = ["user"]
    search_fields = ["name"]
