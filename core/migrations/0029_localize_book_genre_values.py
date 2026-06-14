from django.db import migrations, models


GENRE_MAP_EN_TO_RU = {
    'Sci-Fi': 'Научная фантастика',
    'Fantasy': 'Фэнтези',
    'History': 'История',
    'Mystery': 'Детектив',
    'Drama': 'Драма',
    'Novel': 'Роман',
    'Fiction': 'Художественная литература',
    'Romance': 'Романтика',
    'Thriller': 'Триллер',
    'Biography': 'Биография',
    'Poetry': 'Поэзия',
    'Non-Fiction': 'Нон-фикшн',
}


def forwards_map_genres(apps, schema_editor):
    Book = apps.get_model('core', 'Book')
    for old_value, new_value in GENRE_MAP_EN_TO_RU.items():
        Book.objects.filter(genre=old_value).update(genre=new_value)


def backwards_map_genres(apps, schema_editor):
    Book = apps.get_model('core', 'Book')
    for old_value, new_value in GENRE_MAP_EN_TO_RU.items():
        Book.objects.filter(genre=new_value).update(genre=old_value)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0028_alter_book_genre_choices'),
    ]

    operations = [
        migrations.AlterField(
            model_name='book',
            name='genre',
            field=models.CharField(
                choices=[
                    ('Научная фантастика', 'Научная фантастика'),
                    ('Фэнтези', 'Фэнтези'),
                    ('История', 'История'),
                    ('Детектив', 'Детектив'),
                    ('Драма', 'Драма'),
                    ('Роман', 'Роман'),
                    ('Художественная литература', 'Художественная литература'),
                    ('Романтика', 'Романтика'),
                    ('Триллер', 'Триллер'),
                    ('Биография', 'Биография'),
                    ('Поэзия', 'Поэзия'),
                    ('Нон-фикшн', 'Нон-фикшн'),
                ],
                max_length=50,
            ),
        ),
        migrations.RunPython(forwards_map_genres, backwards_map_genres),
    ]
