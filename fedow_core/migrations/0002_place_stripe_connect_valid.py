# Generated by Django 4.2 on 2023-08-16 13:41

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('fedow_core', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='place',
            name='stripe_connect_valid',
            field=models.BooleanField(default=False),
        ),
    ]
