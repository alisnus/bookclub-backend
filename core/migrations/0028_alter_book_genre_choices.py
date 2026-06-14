from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0027_adminlog_registration'),
    ]

    operations = [
        migrations.AlterField(
            model_name='book',
            name='genre',
            field=models.CharField(
                choices=[
                    ('Sci-Fi', 'Sci-Fi'),
                    ('Fantasy', 'Fantasy'),
                    ('History', 'History'),
                    ('Mystery', 'Mystery'),
                    ('Drama', 'Drama'),
                    ('Novel', 'Novel'),
                    ('Fiction', 'Fiction'),
                    ('Romance', 'Romance'),
                    ('Thriller', 'Thriller'),
                    ('Biography', 'Biography'),
                    ('Poetry', 'Poetry'),
                    ('Non-Fiction', 'Non-Fiction'),
                ],
                max_length=50,
            ),
        ),
    ]
