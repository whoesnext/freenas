# -*- coding: utf-8 -*-
# Generated by Django 1.10.8 on 2018-12-09 07:30
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion
import freenasUI.freeadmin.models.fields


def rename_vmware_snapshot_alert(apps, schema_editor):
    Alert = apps.get_model('system', 'Alert')
    for alert in Alert.objects.all():
        if alert.source in ["VMWareLoginFailed", "VMWareSnapshotFailed", "VMWareSnapshotDeleteFail"]:
            alert.source = f"Legacy{alert.source}"
        alert.save()


class Migration(migrations.Migration):

    dependencies = [
        ('system', '0034_keychain_credential'),
    ]

    operations = [
        migrations.RunPython(rename_vmware_snapshot_alert),
    ]