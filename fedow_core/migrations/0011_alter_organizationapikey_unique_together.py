# Generated by Django 4.2.8 on 2024-05-14 13:24

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('fedow_core', '0010_configuration_stripe_api_key'),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name='organizationapikey',
            unique_together=set(),
        ),
    ]
